"""Subagent subprocess worker (V6).

A :class:`SubprocessAgent` runs a single specialized task in a worker
process and reports back to the lead orchestrator via the shared
SQLite state. The class supports two execution modes:

* ``"in_process"`` — the agent runs on the asyncio loop of the
  current process. Used in tests and on hosts where spawning extra
  Python processes is undesirable.
* ``"subprocess"`` — a real ``subprocess.Popen`` runs a small Python
  driver that re-imports the agent entry point. Used in production
  for true process isolation.

The agent publishes status updates (``idle`` → ``working`` →
``completed`` / ``failed``) to :class:`ash.agents.shared_state.SharedState`
so the orchestrator can poll without blocking. It also pushes a final
``AgentReport`` to the IPC channel for the lead to consume.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from ash.agents.shared_state import SharedState
from ash.safety.environment import build_scrubbed_environment


# --- public enums and dataclasses -----------------------------------------


AGENT_ROLES: tuple[str, ...] = (
    "researcher",
    "coder",
    "tester",
    "reviewer",
    "general",
)
_CUSTOM_ROLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


@dataclass(frozen=True)
class AgentReport:
    """The structured return value of a subagent task."""

    agent_id: str
    role: str
    task: str
    success: bool
    summary: str
    artifacts: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# A task is just an async callable that takes a context dict and
# returns either a string (summary) or a full AgentReport.
TaskFn = Callable[[dict[str, Any]], Awaitable[AgentReport | str]]


# --- the agent class ------------------------------------------------------


class SubprocessAgent:
    """
    Single-purpose agent that runs a task and reports back.

    Parameters
    ----------
    agent_id
        Stable id used for the agent_status row and IPC addressing.
    role
        One of ``AGENT_ROLES``. Drives default tool allowlists and
        status heuristics in the orchestrator.
    task
        Human-readable task description; persisted to the status row.
    shared_state
        The :class:`SharedState` to publish status and IPC to.
    tool_allowlist
        Names of tools the agent is allowed to invoke. Anything else
        is rejected by the orchestrator's guard. ``None`` means
        unrestricted.
    token_budget, return_budget
        Reserved for V7. Persisted to metadata for the orchestrator's
        dashboard.
    runner
        Async callable that performs the work. Receives a context
        dict (``{"agent_id", "role", "task", "shared_state"}``) and
        must return an :class:`AgentReport` or a ``str`` summary.
    """

    def __init__(
        self,
        agent_id: str,
        role: str,
        task: str,
        shared_state: SharedState,
        *,
        runner: TaskFn,
        tool_allowlist: Sequence[str] | None = None,
        token_budget: int = 4000,
        return_budget: int = 2000,
        metadata: dict[str, Any] | None = None,
        enforcement_guard: Callable[[str], bool] | None = None,
        sandbox_tier: int = 1,
        workspace_root: Path | None = None,
        allow_custom_role: bool = False,
    ) -> None:
        if role not in AGENT_ROLES and (
            not allow_custom_role or not _CUSTOM_ROLE.fullmatch(role)
        ):
            raise ValueError(f"Unknown role {role!r}; expected one of {AGENT_ROLES}")
        self.agent_id = agent_id
        self.role = role
        self.task = task
        self.shared_state = shared_state
        self.runner = runner
        self.tool_allowlist: tuple[str, ...] = tuple(tool_allowlist or ())
        self.token_budget = token_budget
        self.return_budget = return_budget
        self._metadata: dict[str, Any] = dict(metadata or {})
        self._enforcement_guard = enforcement_guard
        self.sandbox_tier = sandbox_tier
        self.workspace_root = workspace_root

    # --- metadata -------------------------------------------------------

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self._metadata)

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if a tool is in this agent's allowlist. Returns True if no allowlist set."""
        if not self.tool_allowlist:
            return True
        return tool_name in self.tool_allowlist

    # --- registration --------------------------------------------------

    def register(self) -> None:
        """Insert this agent into the shared ``agent_status`` table."""

        self.shared_state.register_agent(
            self.agent_id,
            role=self.role,
            metadata={
                "task": self.task,
                "tool_allowlist": list(self.tool_allowlist),
                "token_budget": self.token_budget,
                "return_budget": self.return_budget,
                **self._metadata,
            },
        )
        self.shared_state.update_status(self.agent_id, "idle", current_task=self.task)

    # --- in-process execution ------------------------------------------

    async def run_in_process(self) -> AgentReport:
        """
        Execute the agent in the current process and publish a report.

        The agent's status is updated through ``idle`` → ``working``
        → ``completed``/``failed`` so the orchestrator can poll. The
        final report is also pushed to the IPC channel addressed to
        the lead agent (``"lead"`` by default).
        """

        self.register()
        self.shared_state.update_status(
            self.agent_id, "working", current_task=self.task
        )
        try:
            result = await self.runner(
                {
                    "agent_id": self.agent_id,
                    "role": self.role,
                    "task": self.task,
                    "shared_state": self.shared_state,
                }
            )
        except Exception as exc:  # noqa: BLE001
            self.shared_state.update_status(
                self.agent_id, "failed", current_task=str(exc)
            )
            report = AgentReport(
                agent_id=self.agent_id,
                role=self.role,
                task=self.task,
                success=False,
                summary=f"agent raised: {exc}",
                artifacts={"exception": str(exc)},
            )
        else:
            report = _coerce_report(result, self)
            status = "completed" if report.success else "failed"
            self.shared_state.update_status(
                self.agent_id, status, current_task=report.summary[:200]
            )

        # Push the report to the IPC channel.
        self.shared_state.send_message(
            sender_id=self.agent_id,
            recipient_id="lead",
            message_type="agent_report",
            content=_report_to_payload(report),
        )
        return report

    # --- subprocess execution -----------------------------------------

    def spawn_subprocess(
        self,
        *,
        python_executable: str | None = None,
        extra_args: Sequence[str] = (),
    ) -> "subprocess.Popen[str]":
        """
        Launch this agent as a real subprocess.

        The driver re-imports the agent entry point and runs it
        synchronously. Stdout is captured so the orchestrator can log
        progress; the actual report is published through shared state
        rather than stdout.
        """

        cmd: list[str] = [
            python_executable or sys.executable,
            "-m",
            "ash.agents._agent_driver",
            "--agent-id",
            self.agent_id,
            "--db-path",
            self.shared_state.db_path,
            "--role",
            self.role,
            "--task",
            self.task,
        ]
        cmd.extend(extra_args)
        env = build_scrubbed_environment(
            overrides=(
                {"ASH_WORKSPACE_ROOT": str(self.workspace_root)}
                if self.workspace_root is not None
                else None
            )
        )
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

    # --- reporting helpers ---------------------------------------------

    def report_to_payload(self, report: AgentReport) -> dict[str, Any]:
        return _report_to_payload(report)


# --- helpers --------------------------------------------------------------


def _coerce_report(result: AgentReport | str, agent: SubprocessAgent) -> AgentReport:
    if isinstance(result, AgentReport):
        return result
    if isinstance(result, str):
        return AgentReport(
            agent_id=agent.agent_id,
            role=agent.role,
            task=agent.task,
            success=True,
            summary=result,
        )
    raise TypeError(
        f"Task runner returned {type(result).__name__}; expected AgentReport or str"
    )


def _report_to_payload(report: AgentReport) -> dict[str, Any]:
    return {
        "agent_id": report.agent_id,
        "role": report.role,
        "task": report.task,
        "success": report.success,
        "summary": report.summary,
        "artifacts": dict(report.artifacts),
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
    }


def payload_to_report(payload: dict[str, Any]) -> AgentReport:
    return AgentReport(
        agent_id=payload["agent_id"],
        role=payload.get("role", "general"),
        task=payload.get("task", ""),
        success=bool(payload.get("success", False)),
        summary=str(payload.get("summary", "")),
        artifacts=dict(payload.get("artifacts", {})),
        started_at=_parse_dt(payload.get("started_at")),
        finished_at=_parse_dt(payload.get("finished_at")),
    )


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


# --- default task factories ----------------------------------------------


def make_simple_text_task(reply: str) -> TaskFn:
    """
    Build a runner that returns a canned string reply.

    Useful in tests and as a placeholder for real LLM-driven runners.
    """

    async def runner(_ctx: dict[str, Any]) -> str:
        await asyncio.sleep(0)
        return reply

    return runner
