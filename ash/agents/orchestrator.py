"""Subagent orchestrator (V6).

The :class:`SubagentOrchestrator` is the lead agent that decomposes a
high-level task into specialized subagent runs, spawns them in
parallel (in-process by default; real ``subprocess.Popen`` is wired
through :meth:`SubprocessAgent.spawn_subprocess`), and aggregates the
reports.

IPC is plain JSON-RPC-shaped messages routed through the
:class:`~ash.agents.shared_state.SharedState` SQLite database. Agents
publish status updates (``idle`` / ``working`` / ``completed`` /
``failed``) and a terminal :class:`AgentReport` to the
``"lead"`` recipient; the orchestrator polls and collects them.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence, cast

from ash.agents.shared_state import AgentStatus, IPCMessage, SharedState
from ash.agents.subprocess_agent import (
    AGENT_ROLES,
    AgentReport,
    SubprocessAgent,
    TaskFn,
    make_simple_text_task,
    payload_to_report,
)


LEAD_AGENT_ID = "lead"


@dataclass
class SubagentSpec:
    """A single subagent to spawn as part of a batch."""

    role: str
    task: str
    runner: TaskFn | None = None
    agent_id: str = ""
    tool_allowlist: tuple[str, ...] = ()
    token_budget: int = 4000
    return_budget: int = 2000
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.role not in AGENT_ROLES:
            raise ValueError(
                f"Unknown role {self.role!r}; expected one of {AGENT_ROLES}"
            )
        if not self.agent_id:
            self.agent_id = f"{self.role}-{uuid.uuid4().hex[:8]}"


@dataclass
class OrchestratorResult:
    """Aggregate result returned by :meth:`SubagentOrchestrator.run_batch`."""

    goal: str
    sprint_id: str
    reports: list[AgentReport] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def all_succeeded(self) -> bool:
        return bool(self.reports) and all(r.success for r in self.reports)


class SubagentOrchestrator:
    """
    Lead agent that fans out work to a batch of :class:`SubprocessAgent` workers.

    Parameters
    ----------
    shared_state
        Pre-initialised :class:`SharedState` used for status + IPC.
    lead_agent_id
        Identifier the lead uses when publishing its own status.
        Defaults to ``"lead"``.
    max_concurrency
        Cap on simultaneously-running subagents. The default of 4
        matches the V6 spec diagram.
    poll_interval_seconds
        Sleep between status polls while waiting for agents to
        finish.
    """

    def __init__(
        self,
        shared_state: SharedState,
        *,
        lead_agent_id: str = LEAD_AGENT_ID,
        max_concurrency: int = 4,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.shared_state = shared_state
        self.lead_agent_id = lead_agent_id
        self.max_concurrency = max_concurrency
        self.poll_interval_seconds = poll_interval_seconds
        self.shared_state.register_agent(lead_agent_id, role="lead")

    # --- public API -----------------------------------------------------

    async def run_batch(
        self,
        goal: str,
        specs: Sequence[SubagentSpec],
    ) -> OrchestratorResult:
        """Spawn ``specs`` and return an aggregated :class:`OrchestratorResult`."""

        if not specs:
            raise ValueError("specs must be a non-empty sequence")

        sprint_id = self.shared_state.create_sprint(self.lead_agent_id, goal)
        self.shared_state.update_sprint_state(sprint_id, "active")
        self.shared_state.update_status(
            self.lead_agent_id, "working", current_task=goal
        )

        start = time.monotonic()
        reports: list[AgentReport] = []
        try:
            reports = await self._run_agents(specs)
        finally:
            elapsed = time.monotonic() - start
            if reports:
                state = "complete" if all(r.success for r in reports) else "aborted"
            else:
                state = "aborted"
            self.shared_state.update_sprint_state(sprint_id, state)
            self.shared_state.update_status(
                self.lead_agent_id,
                "completed",
                current_task=f"batch done ({len(reports)} reports)",
            )

        return OrchestratorResult(
            goal=goal,
            sprint_id=sprint_id,
            reports=reports,
            elapsed_seconds=elapsed,
        )

    # --- internals ------------------------------------------------------

    async def _run_agents(self, specs: Sequence[SubagentSpec]) -> list[AgentReport]:
        semaphore = asyncio.Semaphore(self.max_concurrency)
        reports: list[AgentReport] = []

        async def _run_one(spec: SubagentSpec) -> AgentReport:
            agent = SubprocessAgent(
                agent_id=spec.agent_id,
                role=spec.role,
                task=spec.task,
                shared_state=self.shared_state,
                runner=spec.runner or make_simple_text_task(f"completed: {spec.task}"),
                tool_allowlist=spec.tool_allowlist,
                token_budget=spec.token_budget,
                return_budget=spec.return_budget,
                metadata=spec.metadata,
                enforcement_guard=lambda tool_name: (
                    tool_name in (spec.tool_allowlist or set())
                ),
            )
            async with semaphore:
                return await agent.run_in_process()

        # Launch ALL tasks immediately — each one acquires the semaphore
        # internally via `async with semaphore`. No serialized acquire in the loop.
        tasks = [asyncio.create_task(_run_one(spec)) for spec in specs]

        # Collect results as they complete (not in submission order).
        for finished in asyncio.as_completed(tasks):
            try:
                report = await finished
            except Exception as exc:  # noqa: BLE001
                report = AgentReport(
                    agent_id="<unknown>",
                    role="general",
                    task="<unknown>",
                    success=False,
                    summary=f"orchestrator caught: {exc}",
                )
            reports.append(report)
            self._drain_lead_inbox()

        return reports

    def collect_reports(self) -> list[AgentReport]:
        """Synchronous helper: drain every undelivered message in the lead inbox.

        Useful in tests; production code normally uses
        :meth:`run_batch` which already drains after each agent.
        """

        reports: list[AgentReport] = []
        for message in self._drain_lead_inbox():
            if message.message_type != "agent_report":
                continue
            reports.append(payload_to_report(message.content))
        return reports

    def status(self) -> list[AgentStatus]:
        """Return the current ``agent_status`` snapshot."""

        return self.shared_state.list_agents()

    async def await_completion(
        self,
        agent_ids: Iterable[str],
        *,
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float | None = None,
    ) -> dict[str, AgentStatus]:
        """Block until every listed agent reaches ``completed`` or ``failed``.

        Returns a ``{agent_id: AgentStatus}`` map of the final states.
        Raises :class:`TimeoutError` if any agent is still ``working``
        or ``idle`` when the deadline elapses.
        """

        interval = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else self.poll_interval_seconds
        )
        deadline = time.monotonic() + timeout_seconds
        agent_ids = list(agent_ids)
        while True:
            statuses = {st.agent_id: st for st in self.shared_state.list_agents()}
            final = {aid: statuses.get(aid) for aid in agent_ids}
            all_done = all(
                st is not None and st.status in {"completed", "failed"}
                for st in (final[aid] for aid in agent_ids)
            )
            if all_done:
                return {aid: cast(AgentStatus, final[aid]) for aid in agent_ids}
            if time.monotonic() >= deadline:
                timed_out = [
                    aid
                    for aid, st in final.items()
                    if st is None or st.status not in {"completed", "failed"}
                ]
                raise TimeoutError(
                    f"Agents did not finish within {timeout_seconds}s: {timed_out}"
                )
            await asyncio.sleep(interval)

    # --- IPC helpers ----------------------------------------------------

    def _drain_lead_inbox(self) -> list[IPCMessage]:
        msgs = self.shared_state.fetch_messages(
            self.lead_agent_id, undelivered_only=True
        )
        if msgs:
            self.shared_state.mark_delivered(m.message_id for m in msgs)
        return msgs

    # --- convenience constructors -------------------------------------

    @staticmethod
    def default_role_allowlist(role: str) -> tuple[str, ...]:
        """Return the canonical tool allowlist for a given role."""

        return {
            "researcher": ("read_file", "search_code"),
            "coder": ("read_file", "write_file", "replace_file_content"),
            "tester": ("read_file", "run_command", "search_code"),
            "reviewer": ("read_file", "search_code"),
            "general": (),
        }.get(role, ())


# --- a tiny helper to build a fan-out from a structured goal --------------


def fanout_for_goal(
    goal: str,
    *,
    phases: Sequence[tuple[str, str, str | None]] | None = None,
) -> list[SubagentSpec]:
    """
    Build a default 4-agent fan-out: Researcher → Coder → Tester → Reviewer.

    ``phases`` is an optional override: a sequence of
    ``(role, task, runner_reply)``. When ``runner_reply`` is None the
    default text runner is used. Passing your own phase list lets
    callers customise the worker pipeline without subclassing.
    """

    if phases is None:
        phases = (
            (
                "researcher",
                f"Research the codebase to support: {goal}",
                f"researched: {goal}",
            ),
            ("coder", f"Implement: {goal}", f"implemented: {goal}"),
            ("tester", f"Test: {goal}", f"tested: {goal}"),
            ("reviewer", f"Review: {goal}", f"reviewed: {goal}"),
        )
    specs: list[SubagentSpec] = []
    for role, task, reply in phases:
        runner = make_simple_text_task(reply) if reply is not None else None
        specs.append(
            SubagentSpec(
                role=role,
                task=task,
                runner=runner,
                tool_allowlist=SubagentOrchestrator.default_role_allowlist(role),
            )
        )
    return specs
