"""Subagent orchestration for V6 (Sprint 13)."""

from agents.orchestrator import (
    LEAD_AGENT_ID,
    OrchestratorResult,
    SubagentOrchestrator,
    SubagentSpec,
    fanout_for_goal,
)
from agents.shared_state import (
    AgentStatus,
    IPCMessage,
    SharedSprint,
    SharedState,
)
from agents.subprocess_agent import (
    AGENT_ROLES,
    AgentReport,
    SubprocessAgent,
    TaskFn,
    make_simple_text_task,
    payload_to_report,
)


__all__ = [
    "AGENT_ROLES",
    "AgentReport",
    "AgentStatus",
    "IPCMessage",
    "LEAD_AGENT_ID",
    "OrchestratorResult",
    "SharedSprint",
    "SharedState",
    "SubagentOrchestrator",
    "SubagentSpec",
    "SubprocessAgent",
    "TaskFn",
    "fanout_for_goal",
    "make_simple_text_task",
    "payload_to_report",
]
