"""Cancellable multi-worker execution of durable Ash automations."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from ash.automation.models import (
    AutomationRun,
    AutomationRunLease,
    AutomationWorkerSummary,
)
from ash.automation.store import (
    AutomationError,
    AutomationRestartRequired,
    AutomationStore,
)
from ash.config import AshConfig
from ash.core.redaction import redact_text
from ash.safety.trust import is_workspace_trusted
from ash.sandbox.process_utils import (
    INHERIT_PROCESS_GROUP_ENV,
    ProcessOutputLimitExceeded,
    ProcessTreeError,
    ProcessTreePlan,
    ProcessTreeUnavailable,
    communicate_process,
    prepare_process_tree,
    settle_process_tree_after_cancellation,
    terminate_process_tree,
)
from ash.sdk import AshResult
from ash.logging import get_logger


_log = get_logger(__name__)


async def _settle_worker_task_after_cancellation(
    task: asyncio.Task[Any],
) -> tuple[Any | None, BaseException | None, bool]:
    """Settle one owned worker task while preserving its caller's cancellation."""

    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    try:
        return task.result(), None, interrupted
    except BaseException as exc:
        return None, exc, interrupted


class AutomationClient(Protocol):
    async def prompt(
        self,
        text: str,
        *,
        user_metadata: dict[str, Any] | None = None,
    ) -> AshResult: ...

    async def close(self) -> None: ...


ClientFactory = Callable[[AshConfig, Path], Awaitable[AutomationClient]]
ConfigLoader = Callable[[], AshConfig]
RunObserver = Callable[[AutomationRun], None]


async def _default_client_factory(
    config: AshConfig, workspace: Path
) -> AutomationClient:
    return _SubprocessAutomationClient(config, workspace)


class _SubprocessAutomationClient:
    """Run one unattended turn in a killable process group."""

    _RESULT_PREFIX = "ASH_AUTOMATION_RESULT="
    _MAX_OUTPUT_BYTES = 2 * 1024 * 1024

    def __init__(self, config: AshConfig, workspace: Path) -> None:
        self._config = config
        self._workspace = workspace
        self._process: asyncio.subprocess.Process | None = None
        self._process_tree_plan: ProcessTreePlan | None = None

    async def prompt(
        self,
        text: str,
        *,
        user_metadata: dict[str, Any] | None = None,
    ) -> AshResult:
        if self._process is not None:
            raise RuntimeError("automation subprocess client only supports one prompt")
        request = {
            "config": self._config.model_dump(mode="json"),
            "workspace": str(self._workspace),
            "prompt": text,
            "user_metadata": user_metadata,
        }
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        environment[INHERIT_PROCESS_GROUP_ENV] = "1"
        try:
            process_tree_plan = prepare_process_tree(workspace_root=self._workspace)
        except ProcessTreeUnavailable as exc:
            raise AutomationError(
                f"automation subprocess was not started: {exc}"
            ) from exc
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "ash.automation.runner",
            cwd=self._workspace,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_tree_plan.spawn_options,
        )
        self._process = process
        self._process_tree_plan = process_tree_plan
        cleanup_error: ProcessTreeError | None = None
        try:
            stdout, stderr = await communicate_process(
                process,
                input_data=json.dumps(request, allow_nan=False).encode("utf-8"),
                max_output_bytes=self._MAX_OUTPUT_BYTES,
                process_tree_plan=process_tree_plan,
            )
        except ProcessOutputLimitExceeded as primary:
            cleanup_error = primary.cleanup_error
            raise
        except asyncio.CancelledError as cancellation:
            cleanup_error, cleanup_cancelled = (
                await settle_process_tree_after_cancellation(
                    process, plan=process_tree_plan
                )
            )
            if cleanup_error is not None:
                cancellation.add_note(f"Process-tree cleanup failed: {cleanup_error}")
            if cleanup_cancelled:
                cancellation.add_note("Process-tree cleanup was cancelled")
            raise
        except BaseException as primary:
            cleanup_error, cleanup_cancelled = (
                await settle_process_tree_after_cancellation(
                    process, plan=process_tree_plan
                )
            )
            if cleanup_error is not None:
                primary.add_note(f"Process-tree cleanup failed: {cleanup_error}")
            if cleanup_cancelled:
                primary.add_note("Process-tree cleanup was cancelled")
            raise
        finally:
            if cleanup_error is None:
                self._process = None
                self._process_tree_plan = None

        payload = self._parse_payload(stdout)
        if process.returncode != 0 or not payload.get("ok"):
            error = payload.get("error")
            if not isinstance(error, str) or not error.strip():
                error = stderr.decode("utf-8", errors="replace")[-4000:].strip()
            raise RuntimeError(redact_text(error or "automation subprocess failed"))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("automation subprocess returned an invalid result")
        return AshResult(**result)

    async def close(self) -> None:
        process = self._process
        if process is not None:
            plan = self._process_tree_plan
            try:
                await terminate_process_tree(process, plan=plan)
            except ProcessTreeError as exc:
                raise AutomationError(
                    f"automation process cleanup failed: {exc}"
                ) from exc
            else:
                self._process = None
                self._process_tree_plan = None

    def _parse_payload(self, stdout: bytes) -> dict[str, Any]:
        for line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
            if not line.startswith(self._RESULT_PREFIX):
                continue
            try:
                payload = json.loads(line.removeprefix(self._RESULT_PREFIX))
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "automation subprocess returned malformed JSON"
                ) from exc
            if isinstance(payload, dict):
                return payload
        raise RuntimeError("automation subprocess returned no result")


class AutomationWorkerService:
    """Poll, claim, and execute scheduled prompts through the normal Ash runtime."""

    def __init__(
        self,
        store: AutomationStore,
        workspace: Path | str,
        *,
        worker_id: str | None = None,
        max_concurrent_runs: int = 2,
        poll_seconds: float = 1.0,
        lease_seconds: float = 60.0,
        config: AshConfig | None = None,
        config_loader: ConfigLoader | None = None,
        client_factory: ClientFactory = _default_client_factory,
        on_run_finished: RunObserver | None = None,
        run_retention_days: int = 30,
        session_retention_days: int = 0,
        session_store_path: Path | None = None,
        maintenance_interval_seconds: float = 3600.0,
    ) -> None:
        self.store = store
        self.workspace = Path(workspace).expanduser().resolve()
        self.worker_id = worker_id or f"worker-{uuid.uuid4()}"
        if not 1 <= max_concurrent_runs <= 32:
            raise ValueError("max_concurrent_runs must be between 1 and 32")
        if not 0.1 <= poll_seconds <= 60:
            raise ValueError("poll_seconds must be between 0.1 and 60")
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 5 and 3600")
        self.max_concurrent_runs = max_concurrent_runs
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        if config is not None and config.workspace_root.resolve() != self.workspace:
            raise ValueError("automation worker config belongs to another workspace")
        if config is not None and config_loader is not None:
            raise ValueError("provide either config or config_loader, not both")
        self._config = config
        self._config_loader = config_loader
        self._client_factory = client_factory
        if run_retention_days < 1:
            raise ValueError("run_retention_days must be positive")
        if session_retention_days < 0:
            raise ValueError("session_retention_days cannot be negative")
        if maintenance_interval_seconds < 1:
            raise ValueError("maintenance_interval_seconds must be at least 1")
        self._on_run_finished = on_run_finished
        self._run_retention_days = run_retention_days
        self._session_retention_days = session_retention_days
        self._session_store_path = session_store_path
        self._maintenance_interval_seconds = maintenance_interval_seconds
        self._last_maintenance_at = float("-inf")
        self._stop = asyncio.Event()
        self._tasks: dict[str, asyncio.Task[AutomationRun]] = {}
        self._claims: dict[str, AutomationRunLease] = {}
        self._detached_tasks: set[asyncio.Task[Any]] = set()
        self._deferred_cleanup_tasks: set[asyncio.Task[None]] = set()

    @property
    def active_run_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._tasks))

    def request_stop(self) -> None:
        self._stop.set()

    async def run_forever(self, *, once: bool = False) -> AutomationWorkerSummary:
        """Run until stopped, or drain one due batch when ``once`` is true."""

        self._validate_workspace()
        self._heartbeat()
        summary = AutomationWorkerSummary()
        paused_reason: str | None = None
        try:
            while not self._stop.is_set():
                self._collect_finished(summary)
                try:
                    self._validate_workspace()
                except AutomationRestartRequired:
                    raise
                except (AutomationError, OSError, ValueError) as exc:
                    if once:
                        raise
                    reason = redact_text(str(exc))
                    if self._tasks:
                        await self._stop_active_runs(summary)
                    self.store.remove_worker(self.worker_id)
                    if reason != paused_reason:
                        _log.warning("automation worker paused: {}", reason)
                        paused_reason = reason
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(), timeout=self.poll_seconds
                        )
                    except TimeoutError:
                        pass
                    continue
                if paused_reason is not None:
                    _log.info("automation worker resumed after: {}", paused_reason)
                    paused_reason = None
                self._heartbeat()
                if not self._tasks:
                    await self._run_maintenance()
                    self._heartbeat()
                capacity = self.max_concurrent_runs - len(self._tasks)
                claims, skipped = (
                    self.store.claim_due_batch(
                        workspace=self.workspace,
                        worker_id=self.worker_id,
                        lease_seconds=self.lease_seconds,
                        limit=capacity,
                    )
                    if capacity > 0
                    else ([], [])
                )
                for skipped_run in skipped:
                    self._record_terminal(summary, skipped_run)
                for claim in claims:
                    task = asyncio.create_task(
                        self.execute(claim),
                        name=f"ash-automation-{claim.run.run_id}",
                    )
                    self._tasks[claim.run.run_id] = task
                    self._claims[claim.run.run_id] = claim
                if once:
                    await self._wait_for_once_batch(summary)
                    return summary
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass
        finally:
            if self._tasks:
                await self._stop_active_runs(summary)
            self._collect_finished(summary)
            self.store.remove_worker(self.worker_id)
            summary.stopped = self._stop.is_set()
        return summary

    async def run_manual(self, reference: str) -> AutomationRun:
        self._validate_workspace()
        self._heartbeat()
        try:
            claim = self.store.claim_manual(
                reference,
                workspace=self.workspace,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
            return await self.execute(claim)
        finally:
            self.store.remove_worker(self.worker_id)

    async def execute(self, claim: AutomationRunLease) -> AutomationRun:
        """Execute one owned lease and always finalize its durable outcome."""

        client: AutomationClient | None = None
        client_holder: list[AutomationClient] = []
        client_close_started = False
        operation_task: asyncio.Task[AshResult] | None = None
        monitor_task: asyncio.Task[None] | None = None
        operation_settled = True
        deferred_cleanup_task: asyncio.Task[None] | None = None
        cancellation_requested = False

        async def run_operation() -> AshResult:
            nonlocal client
            self._validate_job_runtime(claim)
            config = self._load_runtime_config()
            config = _apply_token_budget(config, claim.job.token_budget)
            candidate = await self._client_factory(config, self.workspace)
            client = candidate
            client_holder.append(candidate)
            if cancellation_requested:
                raise asyncio.CancelledError
            return await candidate.prompt(
                claim.job.prompt,
                user_metadata={
                    "source": "automation",
                    "automation_job_id": claim.job.job_id,
                    "automation_run_id": claim.run.run_id,
                    "scheduled_for": claim.run.scheduled_for.isoformat(),
                    "trigger": claim.run.trigger,
                },
            )

        async def close_client_after_operation() -> None:
            nonlocal client_close_started
            if client_close_started or not client_holder:
                return
            client_close_started = True
            try:
                await client_holder[0].close()
            except BaseException as cleanup_error:
                _log.warning(
                    "automation client cleanup failed for {}: {}",
                    claim.run.run_id,
                    redact_text(str(cleanup_error))[:500],
                )

        def retain_deferred_cleanup(task: asyncio.Task[None]) -> None:
            """Keep cleanup owned until an unbounded client operation settles."""

            self._deferred_cleanup_tasks.add(task)

            def finish_deferred_cleanup(done: asyncio.Task[None]) -> None:
                self._deferred_cleanup_tasks.discard(done)
                try:
                    done.result()
                except BaseException as cleanup_error:
                    _log.warning(
                        "automation deferred cleanup failed for {}: {}",
                        claim.run.run_id,
                        redact_text(str(cleanup_error))[:500],
                    )

            task.add_done_callback(finish_deferred_cleanup)

        async def cancel_operation() -> tuple[bool, bool]:
            if operation_task is None:
                return True, False
            cancel_task = asyncio.create_task(
                self._cancel_task(operation_task, detach=False),
                name=f"ash-automation-cancel-{claim.run.run_id}",
            )
            result, error, interrupted = (
                await _settle_worker_task_after_cancellation(cancel_task)
            )
            if error is not None:
                return False, interrupted
            return bool(result), interrupted

        def schedule_deferred_cleanup() -> None:
            nonlocal deferred_cleanup_task
            if deferred_cleanup_task is not None or operation_task is None:
                return
            owned_operation = operation_task

            async def defer_cleanup() -> None:
                try:
                    await owned_operation
                except BaseException:
                    pass
                await close_client_after_operation()

            deferred_cleanup_task = asyncio.create_task(
                defer_cleanup(),
                name=f"ash-automation-deferred-cleanup-{claim.run.run_id}",
            )
            retain_deferred_cleanup(deferred_cleanup_task)

        try:
            operation_task = asyncio.create_task(
                run_operation(), name=f"ash-automation-turn-{claim.run.run_id}"
            )
            monitor_task = asyncio.create_task(
                self._monitor_lease(claim, operation_task),
                name=f"ash-automation-lease-{claim.run.run_id}",
            )
            try:
                async with asyncio.timeout(claim.job.timeout_seconds):
                    done, _ = await asyncio.wait(
                        (operation_task, monitor_task),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if monitor_task in done:
                        monitor_error = monitor_task.exception()
                        if monitor_error is not None:
                            raise monitor_error
                    result = await operation_task
                    operation_settled = True
            except TimeoutError:
                cancellation_requested = True
                operation_settled, cleanup_interrupted = await cancel_operation()
                if not operation_settled:
                    schedule_deferred_cleanup()
                if cleanup_interrupted:
                    raise asyncio.CancelledError
                timeout_error = (
                    f"automation exceeded its {claim.job.timeout_seconds:g}s "
                    "wall-clock timeout"
                )
                if not operation_settled:
                    timeout_error += (
                        "; operation cancellation remains unsettled and client "
                        "cleanup is deferred"
                    )
                return self.store.finish_run(
                    claim.run.run_id,
                    claim.token,
                    status="failed",
                    error=timeout_error,
                )
            used_tokens = result.prompt_tokens + result.completion_tokens
            if result.budget_exhausted or used_tokens > claim.job.token_budget:
                return self.store.finish_run(
                    claim.run.run_id,
                    claim.token,
                    status="failed",
                    session_id=result.session_id,
                    response=result.response,
                    error=(
                        f"automation token budget exhausted: {used_tokens} / "
                        f"{claim.job.token_budget}"
                    ),
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    cache_read_tokens=result.cache_read_tokens,
                    cache_write_tokens=result.cache_write_tokens,
                    cost_usd=result.cost_usd,
                    usage_source=result.usage_source,
                    estimated_prompt_tokens=result.estimated_prompt_tokens,
                    estimated_completion_tokens=result.estimated_completion_tokens,
                    estimated_cost_usd=result.estimated_cost_usd,
                )
            return self.store.finish_run(
                claim.run.run_id,
                claim.token,
                status="succeeded",
                session_id=result.session_id,
                response=result.response,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cache_read_tokens=result.cache_read_tokens,
                cache_write_tokens=result.cache_write_tokens,
                cost_usd=result.cost_usd,
                usage_source=result.usage_source,
                estimated_prompt_tokens=result.estimated_prompt_tokens,
                estimated_completion_tokens=result.estimated_completion_tokens,
                estimated_cost_usd=result.estimated_cost_usd,
            )
        except asyncio.CancelledError as cancellation:
            cancellation_requested = True
            if operation_task is not None and not operation_task.done():
                operation_settled, cleanup_interrupted = await cancel_operation()
                if cleanup_interrupted:
                    cancellation.add_note("automation operation cleanup was interrupted")
            if not operation_settled:
                schedule_deferred_cleanup()
            try:
                if operation_settled:
                    return self.store.finish_run(
                        claim.run.run_id,
                        claim.token,
                        status="cancelled",
                        error="automation worker stopped or cancellation was requested",
                    )
                return self.store.interrupt_run(
                    claim.run.run_id,
                    claim.token,
                    error=(
                        "automation cancellation could not establish a terminal "
                        "operation outcome; client cleanup is deferred"
                    ),
                )
            except AutomationError:
                raise
        except Exception as exc:  # noqa: BLE001 - persist one bounded failure
            cancellation_requested = True
            if operation_task is not None and not operation_task.done():
                operation_settled, cleanup_interrupted = await cancel_operation()
                if cleanup_interrupted:
                    raise asyncio.CancelledError from exc
            if not operation_settled:
                schedule_deferred_cleanup()
            try:
                if operation_settled:
                    return self.store.finish_run(
                        claim.run.run_id,
                        claim.token,
                        status="failed",
                        error=redact_text(str(exc)),
                    )
                return self.store.interrupt_run(
                    claim.run.run_id,
                    claim.token,
                    error=(
                        "automation execution failed with an unsettled operation; "
                        "client cleanup is deferred: " + redact_text(str(exc))
                    ),
                )
            except AutomationError as finish_error:
                raise finish_error from exc
        finally:
            if monitor_task is not None:
                await self._cancel_task(monitor_task)
            if (
                client is not None
                and operation_task is not None
                and operation_task.done()
                and deferred_cleanup_task is None
            ):
                close_task = asyncio.create_task(
                    close_client_after_operation(),
                    name=f"ash-automation-close-{claim.run.run_id}",
                )
                try:
                    done, _ = await asyncio.wait({close_task}, timeout=2.0)
                except asyncio.CancelledError:
                    retain_deferred_cleanup(close_task)
                    _log.warning(
                        "automation client cleanup deferred for {}",
                        claim.run.run_id,
                    )
                    raise
                if not done:
                    close_task.cancel()
                    retain_deferred_cleanup(close_task)
                    _log.warning(
                        "automation client cleanup did not settle for {}; cleanup deferred",
                        claim.run.run_id,
                    )

    async def _monitor_lease(
        self,
        claim: AutomationRunLease,
        operation_task: asyncio.Task[AshResult],
    ) -> None:
        interval = min(max(self.lease_seconds / 3, 1.0), 20.0)
        while not operation_task.done():
            await asyncio.sleep(interval)
            if operation_task.done():
                return
            if self.store.cancel_requested(claim.run.run_id, claim.token):
                operation_task.cancel()
                return
            try:
                self.store.renew_lease(
                    claim.run.run_id,
                    claim.token,
                    lease_seconds=self.lease_seconds,
                )
                self._heartbeat()
            except Exception:
                operation_task.cancel()
                raise

    def _validate_job_runtime(self, claim: AutomationRunLease) -> None:
        if Path(claim.job.workspace).resolve() != self.workspace:
            raise AutomationError("automation belongs to another workspace")
        self._validate_workspace()

    def _validate_workspace(self) -> None:
        if not self.workspace.is_dir():
            raise AutomationError(f"automation workspace is missing: {self.workspace}")
        if not is_workspace_trusted(self.workspace):
            raise AutomationError(
                "automation workspace is not trusted; run `ash trust add` before retrying"
            )
        self._load_runtime_config()

    def _load_runtime_config(self) -> AshConfig:
        config = (
            self._config_loader()
            if self._config_loader is not None
            else self._config or AshConfig.load(workspace_root=self.workspace)
        )
        if config.workspace_root.resolve() != self.workspace:
            raise AutomationError("automation runtime config belongs to another workspace")
        if not config.automation_enabled:
            raise AutomationError("automation is disabled by user configuration")
        return config

    def _heartbeat(self) -> None:
        self.store.heartbeat_worker(
            worker_id=self.worker_id,
            workspace=self.workspace,
            pid=os.getpid(),
            max_concurrent_runs=self.max_concurrent_runs,
        )

    def _collect_finished(self, summary: AutomationWorkerSummary) -> None:
        for run_id, task in list(self._tasks.items()):
            if not task.done():
                continue
            self._tasks.pop(run_id, None)
            claim = self._claims.pop(run_id, None)
            run: AutomationRun | None = None
            if task.cancelled() and claim is not None:
                try:
                    run = self.store.finish_run(
                        run_id,
                        claim.token,
                        status="cancelled",
                        error="automation worker stopped before execution began",
                    )
                except AutomationError:
                    pass
            try:
                run = task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                # execute() has already persisted every owned failure. Lost leases are
                # recovered as interrupted by another worker.
                if claim is not None:
                    try:
                        run = self.store.interrupt_run(
                            run_id,
                            claim.token,
                            error=(
                                "automation execution ended without a provable outcome: "
                                + redact_text(str(exc))
                            ),
                        )
                    except AutomationError:
                        pass
            if run is None:
                run = self.store.get_run(run_id)
            if run is not None:
                self._record_terminal(summary, run)

    async def _wait_for_once_batch(self, summary: AutomationWorkerSummary) -> None:
        if not self._tasks:
            return
        stop_task = asyncio.create_task(
            self._stop.wait(), name=f"ash-automation-stop-{self.worker_id}"
        )
        try:
            while self._tasks and not self._stop.is_set():
                await asyncio.wait(
                    [stop_task, *self._tasks.values()],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                self._collect_finished(summary)
            if self._stop.is_set() and self._tasks:
                await self._stop_active_runs(summary)
        finally:
            await self._cancel_task(stop_task)

    async def _stop_active_runs(self, summary: AutomationWorkerSummary) -> None:
        active = list(self._tasks.values())
        for task in active:
            task.cancel()
        if active:
            await asyncio.wait(active, timeout=4.0)
        self._collect_finished(summary)
        for run_id, task in list(self._tasks.items()):
            claim = self._claims.pop(run_id, None)
            self._tasks.pop(run_id, None)
            if claim is not None:
                run: AutomationRun | None
                try:
                    run = self.store.finish_run(
                        run_id,
                        claim.token,
                        status="cancelled",
                        error="automation worker stopped before cancellation settled",
                    )
                except AutomationError:
                    run = self.store.get_run(run_id)
                if run is not None:
                    self._record_terminal(summary, run)
            self._detach_task(task)

    def _record_terminal(
        self, summary: AutomationWorkerSummary, run: AutomationRun
    ) -> None:
        if run.status not in {
            "succeeded",
            "failed",
            "cancelled",
            "interrupted",
            "skipped",
        }:
            return
        summary.record(run)
        if self._on_run_finished is not None:
            try:
                self._on_run_finished(run)
            except Exception:
                pass

    async def _cancel_task(
        self,
        task: asyncio.Task[Any],
        *,
        cancel_first: bool = True,
        timeout: float = 0.5,
        detach: bool = True,
    ) -> bool:
        if cancel_first and not task.done():
            task.cancel()
        if not task.done():
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if not done:
                if not cancel_first:
                    task.cancel()
                if detach:
                    self._detach_task(task)
                return False
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass
        return True

    def _detach_task(self, task: asyncio.Task[Any]) -> None:
        self._detached_tasks.add(task)

        def consume(done: asyncio.Task[Any]) -> None:
            self._detached_tasks.discard(done)
            try:
                done.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(consume)

    async def _run_maintenance(self) -> None:
        now = time.monotonic()
        if now - self._last_maintenance_at < self._maintenance_interval_seconds:
            return
        request = {
            "automation_db_path": self.store.db_path,
            "workspace": str(self.workspace),
            "run_retention_days": self._run_retention_days,
            "session_retention_days": self._session_retention_days,
            "session_store_path": (
                str(self._session_store_path)
                if self._session_store_path is not None
                else None
            ),
            "now": self.store._clock(),
        }
        try:
            process_tree_plan = prepare_process_tree(workspace_root=self.workspace)
        except ProcessTreeUnavailable as exc:
            raise AutomationError(
                f"automation maintenance was not started: {exc}"
            ) from exc
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "ash.automation.maintenance",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_tree_plan.spawn_options,
        )
        try:
            async with asyncio.timeout(600):
                stdout, stderr = await communicate_process(
                    process,
                    input_data=json.dumps(request, allow_nan=False).encode("utf-8"),
                    max_output_bytes=64 * 1024,
                    process_tree_plan=process_tree_plan,
                )
        except asyncio.CancelledError as cancellation:
            cleanup_error, cleanup_cancelled = (
                await settle_process_tree_after_cancellation(
                    process, plan=process_tree_plan
                )
            )
            if cleanup_error is not None:
                cancellation.add_note(f"Process-tree cleanup failed: {cleanup_error}")
            if cleanup_cancelled:
                cancellation.add_note("Process-tree cleanup was cancelled")
            raise
        except ProcessOutputLimitExceeded:
            # communicate_process already attempted managed-tree cleanup when
            # it detected the output limit; do not target the root a second time.
            raise
        except BaseException as primary:
            cleanup_error, cleanup_cancelled = (
                await settle_process_tree_after_cancellation(
                    process, plan=process_tree_plan
                )
            )
            if cleanup_error is not None:
                primary.add_note(f"Process-tree cleanup failed: {cleanup_error}")
            if cleanup_cancelled:
                primary.add_note("Process-tree cleanup was cancelled")
            raise
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[-4000:].strip()
            raise AutomationError(
                "automation maintenance failed: "
                + redact_text(detail or f"exit status {process.returncode}")
            )
        if b"ASH_AUTOMATION_MAINTENANCE_OK\n" not in stdout:
            raise AutomationError("automation maintenance returned an invalid result")
        self._last_maintenance_at = now


def _apply_token_budget(config: AshConfig, token_budget: int) -> AshConfig:
    minimum_input_tokens = len(config.context_budget_weights)
    minimum_context_limit = minimum_input_tokens + 1
    context_limit = max(
        minimum_context_limit,
        min(config.max_context_tokens, token_budget),
    )
    completion_limit = max(
        1,
        min(
            config.max_completion_tokens,
            context_limit - minimum_input_tokens,
        ),
    )
    attachment_limit = min(
        config.max_attachment_tokens, context_limit - completion_limit
    )
    return config.with_overrides(
        {
            "max_context_tokens": context_limit,
            "max_completion_tokens": completion_limit,
            "max_attachment_tokens": attachment_limit,
            "max_turn_total_tokens": token_budget,
        },
        source="automation",
        detail="automation token budget",
    )
