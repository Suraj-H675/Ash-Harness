"""Durable session-scoped objective state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


DEFAULT_MAX_GOAL_CONTINUATIONS = 10
MAX_GOAL_CONTINUATIONS = 100
MAX_GOAL_OBJECTIVE_BYTES = 32 * 1024
MAX_GOAL_EVIDENCE_BYTES = 16 * 1024


class GoalState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETE = "complete"
    BUDGET_LIMITED = "budget_limited"
    CLEARED = "cleared"


@dataclass(frozen=True)
class GoalRecord:
    goal_id: str
    session_id: str
    objective: str
    state: GoalState
    max_continuations: int
    continuations_used: int
    last_evidence: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @property
    def can_auto_continue(self) -> bool:
        return self.state is GoalState.ACTIVE


def bounded_goal_text(value: str, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} cannot be empty")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > maximum:
        raise ValueError(f"{label} exceeds {maximum} UTF-8 bytes")
    return normalized


def validate_goal_continuation_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("goal continuation limit must be an integer")
    if not 1 <= value <= MAX_GOAL_CONTINUATIONS:
        raise ValueError(
            "goal continuation limit must be between 1 and "
            f"{MAX_GOAL_CONTINUATIONS}"
        )
    return value
