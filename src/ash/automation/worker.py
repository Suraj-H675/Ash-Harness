"""Cancellable multi-worker execution of durable Ash automations."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
import sys
import time
import threading
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from ash.automation.models import (
    AutomationDeliveryLease,
    AutomationRun,
    AutomationRunLease,
    AutomationWorkerSummary,
)
from ash.automation.delivery import (
    post_webhook,
    prepare_webhook,
)
from ash.automation.store import (
    AutomationError,
    AutomationRestartRequired,
    AutomationStore,
)
from ash.config import AshConfig
from ash.core.redaction import redact_text
from ash.providers.readiness import provider_runtime_environment
from ash.safety.environment import SAFE_ENV_KEYS, SAFE_ENV_PREFIXES
from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.safety.scoped_io import ScopedIOError
from ash.safety.trust import is_workspace_trusted
from ash.sandbox.process_utils import (
    INHERIT_PROCESS_GROUP_ENV,
    ProcessOutputLimitExceeded,
    ProcessTreeError,
    ProcessTreePlan,
    ProcessTreeUnavailable,
    communicate_process,
    prepare_process_tree,
    prepare_scoped_process_launch,
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


def _directory_identity(path: Path) -> tuple[int, int] | None:
    try:
        metadata = os.stat(path)
    except OSError:
        return None
    return (metadata.st_dev, metadata.st_ino)


def _regular_file_identity(path: Path) -> tuple[int, int] | None:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_ino == 0:
        return None
    return (metadata.st_dev, metadata.st_ino)


_PROCESS_DUMPABLE_LOCK = threading.Lock()
_PROCESS_DUMPABLE_USERS = 0
_PROCESS_DUMPABLE_ORIGINAL: int | None = None


def _linux_process_dumpable(*, value: int | None = None) -> int:
    """Read or set Linux process dumpability without a hard ctypes dependency elsewhere."""

    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    operation = 3 if value is None else 4  # PR_GET_DUMPABLE / PR_SET_DUMPABLE
    argument = 0 if value is None else value
    result = int(libc.prctl(operation, argument, 0, 0, 0))
    if result == -1:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return result


@contextlib.contextmanager
def _protect_worker_parent_environment(
    enabled: bool,
    *,
    platform_name: str | None = None,
):
    """Hide a webhook-bearing worker's environment from Linux child ``/proc`` reads.

    Automation preserves its historical runtime environment for compatibility, while
    the webhook signing variable itself is removed from the model child. On Linux the
    child could otherwise still read the worker parent's initial environment through
    ``/proc/<pid>/environ``. Process dumpability is global, so concurrent webhook jobs
    share a ref-counted guard and the original state is restored only for the last one.
    """

    if not enabled:
        yield
        return
    platform = sys.platform if platform_name is None else platform_name
    if not platform.startswith("linux"):
        raise AutomationError(
            "signed automation webhooks require parent-process environment "
            "isolation that is currently supported only on Linux"
        )

    global _PROCESS_DUMPABLE_USERS, _PROCESS_DUMPABLE_ORIGINAL
    with _PROCESS_DUMPABLE_LOCK:
        if _PROCESS_DUMPABLE_USERS == 0:
            try:
                original = _linux_process_dumpable()
                if original != 0:
                    _linux_process_dumpable(value=0)
            except OSError as exc:
                raise AutomationError(
                    "automation subprocess was not started: could not protect the "
                    "worker environment from child process inspection"
                ) from exc
            _PROCESS_DUMPABLE_ORIGINAL = original
        _PROCESS_DUMPABLE_USERS += 1
    try:
        yield
    finally:
        with _PROCESS_DUMPABLE_LOCK:
            _PROCESS_DUMPABLE_USERS -= 1
            if _PROCESS_DUMPABLE_USERS == 0:
                restore = _PROCESS_DUMPABLE_ORIGINAL
                _PROCESS_DUMPABLE_ORIGINAL = None
                if restore is not None and restore != 0:
                    try:
                        _linux_process_dumpable(value=restore)
                    except OSError as exc:
                        _log.warning(
                            "automation worker could not restore Linux process "
                            "dumpability: {}",
                            redact_text(str(exc)),
                        )


_AUTOMATION_CHILD_CAPABILITY_ENV_NAMES = frozenset(
    {
        "ASH_A2A_TOKEN",
        "ASH_ENABLE_TIKTOKEN_DOWNLOAD",
        "BRAVE_SEARCH_API_KEY",
        "TAVILY_API_KEY",
    }
)
_AUTOMATION_PARENT_LIFELINE_FD_ENV = "ASH_INTERNAL_AUTOMATION_PARENT_LIFELINE_FD"


def _move_lifeline_fd_above_stdio(descriptor: int) -> int:
    """Keep internal lifeline descriptors away from subprocess stdio remapping."""

    if descriptor >= 3:
        return descriptor
    try:
        import fcntl

        duplicate_command = getattr(fcntl, "F_DUPFD_CLOEXEC", None)
        if duplicate_command is None:
            raise AutomationError(
                "automation parent lifeline requires close-on-exec descriptor duplication"
            )
        duplicate = int(fcntl.fcntl(descriptor, duplicate_command, 3))
    except (ImportError, OSError) as exc:
        raise AutomationError(
            "automation parent lifeline descriptor could not be isolated from stdio"
        ) from exc
    try:
        os.close(descriptor)
    except OSError as exc:
        try:
            os.close(duplicate)
        except OSError:
            pass
        raise AutomationError(
            "automation parent lifeline descriptor could not be isolated from stdio"
        ) from exc
    return duplicate


def _create_parent_lifeline_pipe() -> tuple[int, int]:
    """Create a POSIX worker→runner lifeline whose descriptors are private."""

    read_fd, write_fd = os.pipe()
    try:
        read_fd = _move_lifeline_fd_above_stdio(read_fd)
        write_fd = _move_lifeline_fd_above_stdio(write_fd)
    except BaseException:
        for descriptor in (read_fd, write_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    return read_fd, write_fd


def _environment_name_key(name: str, *, platform_name: str | None = None) -> str:
    platform = os.name if platform_name is None else platform_name
    return name.casefold() if platform == "nt" else name


def _environment_names_overlap(
    first: set[str] | frozenset[str],
    second: set[str] | frozenset[str],
    *,
    platform_name: str | None = None,
) -> bool:
    left = {
        _environment_name_key(name, platform_name=platform_name) for name in first
    }
    right = {
        _environment_name_key(name, platform_name=platform_name) for name in second
    }
    return not left.isdisjoint(right)


def _webhook_secret_conflicts_with_child_runtime(
    config: AshConfig, secret_name: str
) -> bool:
    required_names = set(SAFE_ENV_KEYS)
    required_names.update(_AUTOMATION_CHILD_CAPABILITY_ENV_NAMES)
    required_names.update(config.command_env_allowlist)
    required_names.update(provider_runtime_environment(config))
    required_names.update({"PYTHONUNBUFFERED", INHERIT_PROCESS_GROUP_ENV})
    secret_key = _environment_name_key(secret_name)
    if any(
        secret_key.startswith(_environment_name_key(prefix))
        for prefix in SAFE_ENV_PREFIXES
    ):
        return True
    return _environment_names_overlap({secret_name}, required_names)


class _SubprocessAutomationClient:
    """Run one unattended turn in a killable process group."""

    _RESULT_PREFIX = "ASH_AUTOMATION_RESULT="
    _MAX_OUTPUT_BYTES = 2 * 1024 * 1024

    def __init__(
        self,
        config: AshConfig,
        workspace: Path,
        *,
        expected_workspace_identity: tuple[int, int] | None = None,
        blocked_environment_names: frozenset[str] = frozenset(),
    ) -> None:
        self._config = config
        self._guard = SafetyGuard(workspace)
        self._workspace = self._guard.project_root
        self._workspace_identity = (
            expected_workspace_identity
            if expected_workspace_identity is not None
            else _directory_identity(self._workspace)
        )
        self._blocked_environment_names = blocked_environment_names
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
        child_config = self._config.model_dump(mode="json")
        child_config["workspace_root"] = "."
        request = {
            "config": child_config,
            "workspace": ".",
            "prompt": text,
            "user_metadata": user_metadata,
        }
        for blocked_name in self._blocked_environment_names:
            if _webhook_secret_conflicts_with_child_runtime(
                self._config, blocked_name
            ):
                raise AutomationError(
                    "webhook signing environment variable is required by the automation "
                    "subprocess; use a dedicated webhook secret variable"
                )
        blocked_keys = {
            _environment_name_key(name) for name in self._blocked_environment_names
        }
        environment = {
            key: value
            for key, value in os.environ.items()
            if _environment_name_key(key) not in blocked_keys
        }
        environment["PYTHONUNBUFFERED"] = "1"
        environment[INHERIT_PROCESS_GROUP_ENV] = "1"
        if any(_environment_name_key(key) in blocked_keys for key in environment):
            raise AutomationError(
                "webhook signing environment variable could not be isolated from the "
                "automation subprocess"
            )
        with _protect_worker_parent_environment(bool(self._blocked_environment_names)):
            return await self._prompt_subprocess(request, environment)

    async def _prompt_subprocess(
        self,
        request: dict[str, Any],
        environment: dict[str, str],
    ) -> AshResult:
        command = [sys.executable, "-I", "-m", "ash.automation.runner"]
        lifeline_read_fd = -1
        lifeline_write_fd = -1
        environment.pop(_AUTOMATION_PARENT_LIFELINE_FD_ENV, None)
        if os.name == "posix":
            lifeline_read_fd, lifeline_write_fd = _create_parent_lifeline_pipe()
            environment[_AUTOMATION_PARENT_LIFELINE_FD_ENV] = str(lifeline_read_fd)
        try:
            try:
                with prepare_scoped_process_launch(
                    command,
                    cwd=self._workspace,
                    guard=self._guard,
                    search_path=environment.get("PATH"),
                    expected_cwd_identity=self._workspace_identity,
                ) as launch:
                    try:
                        process_tree_plan = prepare_process_tree(
                            workspace_root=self._guard.project_root
                        )
                    except ProcessTreeUnavailable as exc:
                        raise AutomationError(
                            f"automation subprocess was not started: {exc}"
                        ) from exc
                    if os.name == "posix" and not bool(
                        process_tree_plan.spawn_options.get("start_new_session")
                    ):
                        raise AutomationError(
                            "automation subprocess requires an isolated POSIX process group; "
                            "restart the worker outside an inherited Ash process group"
                        )
                    spawn_options = dict(process_tree_plan.spawn_options)
                    inherited_fds = tuple(
                        dict.fromkeys(
                            (
                                *launch.pass_fds,
                                *((lifeline_read_fd,) if lifeline_read_fd >= 0 else ()),
                            )
                        )
                    )
                    if inherited_fds:
                        spawn_options["pass_fds"] = inherited_fds
                    process = await asyncio.create_subprocess_exec(
                        *launch.argv,
                        cwd=launch.cwd,
                        env=environment,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        **spawn_options,
                    )
            finally:
                if lifeline_read_fd >= 0:
                    os.close(lifeline_read_fd)
                    lifeline_read_fd = -1
        except (ProcessTreeUnavailable, SafetyViolation, ScopedIOError) as exc:
            environment.pop(_AUTOMATION_PARENT_LIFELINE_FD_ENV, None)
            if lifeline_write_fd >= 0:
                os.close(lifeline_write_fd)
                lifeline_write_fd = -1
            raise AutomationError(
                f"automation subprocess was not started: {exc}"
            ) from exc
        except BaseException:
            environment.pop(_AUTOMATION_PARENT_LIFELINE_FD_ENV, None)
            if lifeline_write_fd >= 0:
                os.close(lifeline_write_fd)
                lifeline_write_fd = -1
            raise
        try:
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
        finally:
            environment.pop(_AUTOMATION_PARENT_LIFELINE_FD_ENV, None)
            if lifeline_read_fd >= 0:
                os.close(lifeline_read_fd)
            if lifeline_write_fd >= 0:
                os.close(lifeline_write_fd)

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
        self._workspace_identity = _directory_identity(self.workspace)
        self._workspace_key = str(self.workspace)
        self._database_path = Path(self.store.db_path)
        self._database_identity = _regular_file_identity(self._database_path)
        if self._database_identity is None:
            raise AutomationRestartRequired(
                "automation database identity is unavailable; restart the worker"
            )
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
        self._session_retention_warning_emitted = False
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
                await self._deliver_pending_batch()
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
                    await self._deliver_run_webhooks(
                        [
                            *(run.run_id for run in skipped),
                            *(claim.run.run_id for claim in claims),
                        ]
                    )
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
            run = await self.execute(claim)
            await self._deliver_run_webhooks([run.run_id])
            return run
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
            candidate: AutomationClient
            if self._client_factory is _default_client_factory:
                candidate = _SubprocessAutomationClient(
                    config,
                    self.workspace,
                    expected_workspace_identity=self._workspace_identity,
                    blocked_environment_names=(
                        frozenset({claim.job.webhook_secret_env})
                        if claim.job.webhook_secret_env
                        else frozenset()
                    ),
                )
            else:
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
        if self._workspace_identity is not None:
            current_identity = _directory_identity(self.workspace)
            if current_identity != self._workspace_identity:
                raise AutomationRestartRequired(
                    "automation workspace identity changed; restart the worker"
                )
        if _regular_file_identity(self._database_path) != self._database_identity:
            raise AutomationRestartRequired(
                "automation database identity changed; restart the worker"
            )
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

    async def _deliver_pending_batch(self) -> None:
        claims = self.store.claim_pending_deliveries(
            workspace=self.workspace,
            worker_id=self.worker_id,
            lease_seconds=max(self.lease_seconds, 30.0),
            limit=min(self.max_concurrent_runs, 4),
        )
        if not claims:
            return
        await self._deliver_claims(claims)

    async def _deliver_run_webhooks(self, run_ids: list[str]) -> None:
        claims: list[AutomationDeliveryLease] = []
        for run_id in dict.fromkeys(run_ids):
            claim = self.store.claim_delivery_for_run(
                run_id,
                workspace=self.workspace,
                worker_id=self.worker_id,
                lease_seconds=max(self.lease_seconds, 30.0),
            )
            if claim is not None:
                claims.append(claim)
        if claims:
            await self._deliver_claims(claims)

    async def _deliver_claims(
        self, claims: list[AutomationDeliveryLease]
    ) -> None:
        results = await asyncio.gather(
            *(self._deliver_webhook(claim) for claim in claims),
            return_exceptions=True,
        )
        for claim, result in zip(claims, results, strict=True):
            if isinstance(result, BaseException):
                _log.warning(
                    "automation webhook delivery {} did not settle: {}",
                    claim.delivery.delivery_id,
                    redact_text(str(result)),
                )

    async def _deliver_webhook(self, claim: AutomationDeliveryLease) -> None:
        delivery_id = claim.delivery.delivery_id
        try:
            prepared = await prepare_webhook(claim)
        except Exception as exc:  # noqa: BLE001 - pre-dispatch is safe to fail
            try:
                self.store.finish_delivery(
                    delivery_id,
                    claim.token,
                    status="failed",
                    error=redact_text(str(exc)),
                )
            except AutomationError:
                pass
            return
        try:
            self.store.mark_delivery_dispatch_started(delivery_id, claim.token)
        except AutomationError:
            return
        try:
            status_code = await post_webhook(prepared)
        except Exception as exc:  # noqa: BLE001 - every post-fence error is ambiguous
            try:
                self.store.finish_delivery(
                    delivery_id,
                    claim.token,
                    status="ambiguous",
                    error=redact_text(str(exc)),
                )
            except AutomationError:
                pass
            return
        try:
            if 200 <= status_code <= 299:
                self.store.finish_delivery(
                    delivery_id,
                    claim.token,
                    status="delivered",
                    response_status=status_code,
                )
            else:
                self.store.finish_delivery(
                    delivery_id,
                    claim.token,
                    status="failed",
                    response_status=status_code,
                    error=f"automation webhook returned HTTP {status_code}",
                )
        except AutomationError:
            # A response arrived after our durable lease expired. Recovery owns
            # the now-unsafe delivery and will classify it as ambiguous.
            return

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
        prune_task = asyncio.create_task(
            asyncio.to_thread(
                self.store._prune_runs_for_workspace_key,
                workspace_key=self._workspace_key,
                older_than_days=self._run_retention_days,
            ),
            name="ash-retention-prune",
        )
        try:
            await asyncio.shield(prune_task)
        except asyncio.CancelledError as cancellation:
            _, prune_error, cleanup_interrupted = (
                await _settle_worker_task_after_cancellation(prune_task)
            )
            if prune_error is not None:
                cancellation.add_note(
                    f"automation retention prune failed during cancellation: {prune_error}"
                )
            if cleanup_interrupted:
                cancellation.add_note(
                    "automation retention prune settlement was itself interrupted"
                )
            raise
        if (
            self._session_retention_days > 0
            and self._session_store_path is not None
            and not self._session_retention_warning_emitted
        ):
            _log.warning(
                "automation worker skipped session retention because safe destructive "
                "maintenance requires a stable session database owner; normal Ash "
                "runtime startup still applies session_retention_days"
            )
            self._session_retention_warning_emitted = True
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
