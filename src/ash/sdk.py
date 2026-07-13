"""Stable asynchronous library API for embedding Ash without a terminal UI."""

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, cast

from ash.agents.shared_state import SharedState
from ash.agents.tasks import AgentArtifact, AgentTask, AgentTaskEvent, TaskState
from ash.automation.models import (
    AutomationJob,
    AutomationRun,
    AutomationRunLease,
    UsageSource,
)
from ash.automation.schedules import build_schedule
from ash.automation.store import AutomationError, AutomationStore
from ash.config import AshConfig
from ash.core.events import EVENT_SCHEMA_VERSION, envelope_event, event_data
from ash.core.loop import AshLoop
from ash.core.redaction import redact_text
from ash.core.session import SessionLineage, SessionSummary
from ash.mcp.server import MCPServerConfig
from ash.providers.base import ProviderABC
from ash.runtime import build_runtime
from ash.safety.trust import is_workspace_trusted
from ash.ui.headless import HeadlessUI


ApprovalCallback = Callable[[str, dict], Awaitable[bool]]


@dataclass(frozen=True)
class AshResult:
    response: str
    session_id: str
    model: str
    context_tokens: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    usage_source: UsageSource = "unavailable"
    estimated_prompt_tokens: int = 0
    estimated_completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    budget_exhausted: bool = False

    @property
    def usage(self) -> dict[str, int | float | str | bool]:
        has_estimates = self.usage_source in {"estimated", "mixed"}
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "usage_source": self.usage_source,
            "estimated_prompt_tokens": self.estimated_prompt_tokens,
            "estimated_completion_tokens": self.estimated_completion_tokens,
            "has_estimates": has_estimates,
            "cache_hit_rate": (
                self.cache_read_tokens / self.prompt_tokens
                if self.prompt_tokens
                else 0.0
            ),
            "cost_usd": self.cost_usd,
            "estimated_cost_usd": self.estimated_cost_usd,
            "cost_is_estimated": self.estimated_cost_usd > 0,
        }


@dataclass(frozen=True)
class AshDelegationResult:
    graph_id: str
    tasks: tuple[dict[str, Any], ...]
    success: bool
    error: str | None = None


@dataclass(frozen=True)
class AshEvent:
    type: str
    data: dict[str, Any]
    schema_version: int = EVENT_SCHEMA_VERSION
    event_id: str = ""
    timestamp: str = ""
    source: dict[str, str] = field(default_factory=dict)
    session_id: str | None = None
    turn_id: str | None = None
    operation_id: str | None = None
    parent_event_id: str | None = None

    def __post_init__(self) -> None:
        metadata = {
            "schema_version": self.schema_version,
            **({"event_id": self.event_id} if self.event_id else {}),
            **({"timestamp": self.timestamp} if self.timestamp else {}),
            **({"source": self.source} if self.source else {}),
            **({"session_id": self.session_id} if self.session_id else {}),
            **({"turn_id": self.turn_id} if self.turn_id else {}),
            **({"operation_id": self.operation_id} if self.operation_id else {}),
            **(
                {"parent_event_id": self.parent_event_id}
                if self.parent_event_id
                else {}
            ),
        }
        payload = envelope_event({"type": self.type, **self.data, **metadata})
        for name in (
            "schema_version",
            "event_id",
            "timestamp",
            "source",
            "session_id",
            "turn_id",
            "operation_id",
            "parent_event_id",
        ):
            object.__setattr__(self, name, payload[name])

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> "AshEvent":
        event = envelope_event(payload)
        data = event_data(event)
        if event["type"] == "turn.completed" and event["session_id"]:
            # Retain the pre-v1 SDK location while exposing typed metadata.
            data.setdefault("session_id", event["session_id"])
        return cls(
            type=event["type"],
            data=data,
            schema_version=event["schema_version"],
            event_id=event["event_id"],
            timestamp=event["timestamp"],
            source=event["source"],
            session_id=event["session_id"],
            turn_id=event["turn_id"],
            operation_id=event["operation_id"],
            parent_event_id=event["parent_event_id"],
        )

    def to_wire(self, *, include_type: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "source": dict(self.source),
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "operation_id": self.operation_id,
            "parent_event_id": self.parent_event_id,
            **self.data,
        }
        if include_type:
            payload["type"] = self.type
        return payload


@dataclass(frozen=True)
class AshEventRecord:
    sequence: int
    event: AshEvent


class AshClient:
    """Own one Ash runtime and its provider, tools, sessions, and subprocesses."""

    def __init__(self, loop: AshLoop, config: AshConfig) -> None:
        self.loop = loop
        self.config = config
        self._started = False
        self._turn_lock = asyncio.Lock()

    @classmethod
    async def create(
        cls,
        *,
        config: AshConfig | None = None,
        workspace: Path | None = None,
        provider: ProviderABC | None = None,
        agent_provider_factory: Callable[[], ProviderABC] | None = None,
        approval_callback: ApprovalCallback | None = None,
        workspace_trusted: bool | None = None,
        session_id: str | None = None,
        additional_mcp_configs: dict[str, MCPServerConfig] | None = None,
        run_maintenance: bool = True,
    ) -> "AshClient":
        """Create a client, honoring persisted workspace trust unless overridden."""

        runtime_config = config or AshConfig.load()
        if workspace is not None:
            runtime_config = runtime_config.model_copy(
                update={"workspace_root": workspace.resolve()}
            )
        runtime = build_runtime(
            runtime_config,
            ui=HeadlessUI(output_format="text", stream=io.StringIO()),
            provider=provider,
            agent_provider_factory=agent_provider_factory,
            workspace_trusted=workspace_trusted,
            approval_callback=approval_callback,
            additional_mcp_configs=additional_mcp_configs,
            run_maintenance=run_maintenance,
        )
        client = cls(runtime.loop, runtime_config)
        try:
            await client.start(session_id)
        except Exception:
            await runtime.loop.aclose()
            raise
        return client

    async def start(self, session_id: str | None = None) -> str:
        async with self._turn_lock:
            return await self._start_unlocked(session_id)

    async def _start_unlocked(self, session_id: str | None = None) -> str:
        if self._started and self.loop.current_session is not None:
            return self.loop.current_session.session_id
        session = await self.loop.start_session(session_id)
        self._started = True
        return session.session_id

    async def prompt(
        self,
        text: str,
        *,
        user_metadata: dict[str, Any] | None = None,
    ) -> AshResult:
        async with self._turn_lock:
            return await self._prompt_unlocked(text, user_metadata=user_metadata)

    async def steer(self, text: str) -> int:
        """Queue guidance for the currently running turn without waiting on it."""

        if not self.loop.is_turn_running:
            raise RuntimeError("no turn is currently running")
        return self.loop.queue_steering(text)

    async def _prompt_unlocked(
        self,
        text: str,
        *,
        user_metadata: dict[str, Any] | None = None,
    ) -> AshResult:
        if not text.strip():
            raise ValueError("prompt cannot be empty")
        if not self._started:
            await self._start_unlocked()
        response = await self.loop.run_turn(text, user_metadata=user_metadata)
        assert self.loop.current_session is not None
        usage = self.loop.last_turn_usage
        return AshResult(
            response=response,
            session_id=self.loop.current_session.session_id,
            model=self.config.model,
            context_tokens=self.loop._last_context_tokens,
            prompt_tokens=int(usage["prompt_tokens"]),
            completion_tokens=int(usage["completion_tokens"]),
            cache_read_tokens=int(usage["cache_read_tokens"]),
            cache_write_tokens=int(usage["cache_write_tokens"]),
            cost_usd=float(usage["cost_usd"]),
            usage_source=cast(UsageSource, str(usage["usage_source"])),
            estimated_prompt_tokens=int(usage["estimated_prompt_tokens"]),
            estimated_completion_tokens=int(usage["estimated_completion_tokens"]),
            estimated_cost_usd=float(usage["estimated_cost_usd"]),
            budget_exhausted=self.loop._last_turn_budget_exhausted,
        )

    async def stream_prompt(
        self,
        text: str,
        *,
        user_metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[AshEvent]:
        """Yield real runtime deltas and one terminal completion/error event."""

        if not text.strip():
            raise ValueError("prompt cannot be empty")
        async with self._turn_lock:
            ui = self.loop.ui
            if not isinstance(ui, HeadlessUI):
                raise RuntimeError("stream_prompt requires Ash's headless event UI")
            queue: asyncio.Queue[AshEvent] = asyncio.Queue()
            terminal_seen = False

            def receive(payload: dict[str, Any]) -> None:
                nonlocal terminal_seen
                event = AshEvent.from_wire(payload)
                if event.type in {"turn.completed", "turn.error", "turn.cancelled"}:
                    terminal_seen = True
                queue.put_nowait(event)

            unsubscribe = ui.subscribe(receive)

            async def run() -> None:
                try:
                    result = await self._prompt_unlocked(
                        text, user_metadata=user_metadata
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if not terminal_seen:
                        queue.put_nowait(
                            AshEvent("turn.error", {"error": redact_text(str(exc))})
                        )
                    return
                if not terminal_seen:
                    queue.put_nowait(
                        AshEvent(
                            "turn.completed",
                            {
                                "response": result.response,
                                "session_id": result.session_id,
                                "model": result.model,
                                "context_tokens": result.context_tokens,
                                "usage": result.usage,
                            },
                        )
                    )

            task = asyncio.create_task(run())
            try:
                while True:
                    event = await queue.get()
                    yield event
                    if event.type in {
                        "turn.completed",
                        "turn.error",
                        "turn.cancelled",
                    }:
                        break
            finally:
                unsubscribe()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def sessions(self, *, query: str = "", limit: int = 20) -> list[SessionSummary]:
        return self.loop.session_store.list_sessions(
            project_path=str(self.loop.project_root), query=query, limit=limit
        )

    def session_tree(self, session_id: str | None = None) -> list[SessionLineage]:
        """Return the complete lineage tree containing a session."""

        active_session = self.loop.current_session
        resolved_session_id = session_id or (
            active_session.session_id if active_session is not None else None
        )
        if resolved_session_id is None:
            raise RuntimeError("no session is active; provide session_id")
        return self.loop.session_store.session_tree(resolved_session_id)

    def automations(
        self,
        *,
        include_disabled: bool = False,
        limit: int = 200,
    ) -> list[AutomationJob]:
        """Return durable automations belonging to this client's workspace."""

        with self._automation_store() as store:
            return store.list_jobs(
                self.loop.project_root,
                include_disabled=include_disabled,
                limit=limit,
            )

    def automation(self, reference: str) -> AutomationJob | None:
        """Look up a durable automation by ID or case-insensitive name."""

        with self._automation_store() as store:
            return store.get_job(reference, workspace=self.loop.project_root)

    def create_automation(
        self,
        name: str,
        prompt: str,
        *,
        at: str | None = None,
        every: str | None = None,
        cron: str | None = None,
        timezone: str = "UTC",
        enabled: bool = True,
        misfire_grace_seconds: int = 86_400,
        timeout_seconds: float = 1800.0,
        token_budget: int = 100_000,
    ) -> AutomationJob:
        """Create a validated schedule for unattended execution by an Ash worker."""

        self._require_automation_execution_allowed()
        schedule = build_schedule(
            at=at,
            every=every,
            cron=cron,
            timezone_name=timezone,
        )
        with self._automation_store() as store:
            return store.create_job(
                name=name,
                prompt=prompt,
                workspace=self.loop.project_root,
                schedule=schedule,
                enabled=enabled,
                misfire_grace_seconds=misfire_grace_seconds,
                timeout_seconds=timeout_seconds,
                token_budget=token_budget,
            )

    def pause_automation(self, reference: str) -> AutomationJob:
        """Prevent future scheduled claims without interrupting an active run."""

        with self._automation_store() as store:
            return store.set_enabled(
                reference,
                workspace=self.loop.project_root,
                enabled=False,
            )

    def resume_automation(self, reference: str) -> AutomationJob:
        """Resume future scheduled claims for an existing automation."""

        self._require_automation_execution_allowed()
        with self._automation_store() as store:
            return store.set_enabled(
                reference,
                workspace=self.loop.project_root,
                enabled=True,
            )

    def remove_automation(self, reference: str) -> AutomationJob:
        """Soft-delete an automation while retaining its durable run history."""

        with self._automation_store() as store:
            return store.remove_job(reference, workspace=self.loop.project_root)

    def automation_runs(
        self,
        automation: str | None = None,
        *,
        limit: int = 100,
    ) -> list[AutomationRun]:
        """Return newest-first run history, optionally for one automation."""

        with self._automation_store() as store:
            job_id = None
            if automation is not None:
                job = store.get_job(
                    automation,
                    workspace=self.loop.project_root,
                    include_deleted=True,
                )
                if job is None:
                    raise AutomationError(f"automation not found: {automation}")
                job_id = job.job_id
            return store.list_runs(
                workspace=self.loop.project_root,
                job_id=job_id,
                limit=limit,
            )

    def cancel_automation_run(self, run_id: str) -> AutomationRun:
        """Request cooperative cancellation of a running workspace automation."""

        with self._automation_store() as store:
            return store.request_cancel(run_id, workspace=self.loop.project_root)

    def claim_automation(
        self,
        reference: str,
        *,
        worker_id: str,
        lease_seconds: float | None = None,
    ) -> AutomationRunLease:
        """Claim a manual run for an external executor without running it here.

        The returned token is the renewable ownership capability required by
        :class:`AutomationStore` to finalize the run. Callers must either execute
        the lease promptly or let recovery mark an expired outcome interrupted.
        """

        self._require_automation_execution_allowed()
        duration = (
            self.config.automation_lease_seconds
            if lease_seconds is None
            else lease_seconds
        )
        with self._automation_store() as store:
            return store.claim_manual(
                reference,
                workspace=self.loop.project_root,
                worker_id=worker_id,
                lease_seconds=duration,
            )

    def _automation_store(self) -> AutomationStore:
        return AutomationStore(self.config.db_directory / "automation.db")

    def _require_automation_execution_allowed(self) -> None:
        if not self.config.automation_enabled:
            raise AutomationError("durable automation is disabled in Ash configuration")
        if not is_workspace_trusted(self.loop.project_root):
            raise AutomationError(
                "workspace must be trusted before unattended automation can run"
            )

    def agent_tasks(
        self,
        *,
        state: TaskState | None = None,
        owner_agent_id: str | None = None,
        graph_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentTask]:
        """Return durable subagent tasks from the shared coordination store."""

        shared = SharedState(self.config.db_directory / "agents.db")
        try:
            return shared.tasks.list_tasks(
                state=state,
                owner_agent_id=owner_agent_id,
                graph_id=graph_id,
                limit=limit,
            )
        finally:
            shared.close()

    def cancel_agent_graph(
        self,
        graph_id: str,
        *,
        reason: str = "cancelled by SDK caller",
    ) -> list[str]:
        """Cancel all nonterminal work in a durable delegated graph."""

        shared = SharedState(self.config.db_directory / "agents.db")
        try:
            return shared.tasks.cancel_graph(graph_id, reason=reason)
        finally:
            shared.close()

    async def delegate_agents(
        self,
        goal: str,
        tasks: list[dict[str, Any]],
        *,
        background: bool = False,
    ) -> AshDelegationResult:
        """Submit a durable provider-backed task DAG through the runtime tool."""

        async with self._turn_lock:
            if not self._started:
                await self._start_unlocked()
            tool = self.loop.tools.get("delegate_agents")
            if tool is None:
                raise RuntimeError("delegate_agents is unavailable in this runtime")
            result = await tool.run(goal=goal, tasks=tasks, background=background)
        if not result.output:
            raise RuntimeError(result.error or "delegated graph submission failed")
        payload = json.loads(result.output)
        return AshDelegationResult(
            graph_id=str(payload["graph_id"]),
            tasks=tuple(payload["tasks"]),
            success=result.success,
            error=result.error,
        )

    def agent_artifacts(self, task_id: str) -> list[AgentArtifact]:
        """Return durable artifacts produced for one subagent task."""

        shared = SharedState(self.config.db_directory / "agents.db")
        try:
            return shared.tasks.list_artifacts(task_id)
        finally:
            shared.close()

    def agent_task_events(
        self,
        *,
        task_id: str | None = None,
        event_type: str | None = None,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[AgentTaskEvent]:
        """Replay versioned durable subagent task events."""

        shared = SharedState(self.config.db_directory / "agents.db")
        try:
            return shared.tasks.list_events(
                task_id=task_id,
                event_type=event_type,
                after_sequence=after_sequence,
                limit=limit,
            )
        finally:
            shared.close()

    def events(
        self,
        session_id: str | None = None,
        *,
        after_sequence: int = 0,
        turn_id: str | None = None,
        limit: int = 1000,
    ) -> list[AshEventRecord]:
        """Replay persisted events from an exclusive sequence cursor."""

        active_session = self.loop.current_session
        resolved_session_id = session_id or (
            active_session.session_id if active_session is not None else None
        )
        if resolved_session_id is None:
            raise RuntimeError("no session is active; provide session_id")
        return [
            AshEventRecord(item.sequence, AshEvent.from_wire(item.event))
            for item in self.loop.session_store.list_runtime_events(
                resolved_session_id,
                after_sequence=after_sequence,
                turn_id=turn_id,
                limit=limit,
            )
        ]

    async def resume(self, session_id: str) -> str:
        async with self._turn_lock:
            session = await self.loop.start_session(session_id)
            self._started = True
            return session.session_id

    async def new_session(self) -> str:
        async with self._turn_lock:
            session = await self.loop.start_session()
            self._started = True
            return session.session_id

    async def fork(
        self,
        session_id: str | None = None,
        *,
        message_count: int | None = None,
        branch_name: str = "",
        branch_summary: str = "",
    ) -> str:
        """Fork a session at a complete turn boundary and activate the child."""

        async with self._turn_lock:
            active_session = self.loop.current_session
            resolved_session_id = session_id or (
                active_session.session_id if active_session is not None else None
            )
            if resolved_session_id is None:
                raise RuntimeError("no session is active; provide session_id")
            forked = self.loop.session_store.fork_session(
                resolved_session_id,
                message_count=message_count,
                branch_name=branch_name,
                branch_summary=branch_summary,
            )
            session = await self.loop.start_session(forked.session_id)
            self._started = True
            return session.session_id

    async def close(self) -> None:
        async with self._turn_lock:
            await self.loop.aclose()
            self._started = False

    async def __aenter__(self) -> "AshClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
