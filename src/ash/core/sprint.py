"""Sprint state machine and contract types (Sprint 12 / V5).

The V5 spec describes "Sprint Contracts" as auditable records of a
multi-step plan: goal, definition-of-done, files in scope, test
command, rollback plan, and an execution lifecycle that walks through
``planning → active → complete`` (or ``aborted``).

This module defines the data model and the state machine. The planner
(:mod:`ash.core.planner`) produces contracts from an LLM in Architect
Mode; the loop (:class:`~ash.core.loop.AshLoop`) drives the state
transitions and persists each step to SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4


# --- state machine ---------------------------------------------------------


class SprintState(StrEnum):
    """Lifecycle states for a sprint contract."""

    PLANNING = "planning"
    ACTIVE = "active"
    COMPLETE = "complete"
    ABORTED = "aborted"


# Valid forward transitions; abort is reachable from any non-terminal state.
_SPRINT_TRANSITIONS: dict[SprintState, frozenset[SprintState]] = {
    SprintState.PLANNING: frozenset({SprintState.ACTIVE, SprintState.ABORTED}),
    SprintState.ACTIVE: frozenset({SprintState.COMPLETE, SprintState.ABORTED}),
    SprintState.COMPLETE: frozenset(),
    SprintState.ABORTED: frozenset(),
}


class SprintTransitionError(RuntimeError):
    """Raised when a state transition is not allowed."""


# --- checklist item --------------------------------------------------------


class ChecklistStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class ChecklistItem:
    """A single checkable step inside a sprint."""

    idx: int
    section: str
    description: str
    status: ChecklistStatus = ChecklistStatus.PENDING
    notes: str = ""

    def mark_done(self, notes: str = "") -> "ChecklistItem":
        return _replace(self, status=ChecklistStatus.DONE, notes=notes)

    def mark_failed(self, notes: str = "") -> "ChecklistItem":
        return _replace(self, status=ChecklistStatus.FAILED, notes=notes)

    def mark_in_progress(self) -> "ChecklistItem":
        return _replace(self, status=ChecklistStatus.IN_PROGRESS)

    def mark_skipped(self, notes: str = "") -> "ChecklistItem":
        return _replace(self, status=ChecklistStatus.SKIPPED, notes=notes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "idx": self.idx,
            "section": self.section,
            "description": self.description,
            "status": self.status,
            "notes": self.notes,
        }


def _replace(item: ChecklistItem, **changes: Any) -> ChecklistItem:
    """Functional update that preserves the frozen invariant."""

    base = item.to_dict()
    base.update(changes)
    return ChecklistItem(**base)


# --- sprint contract ------------------------------------------------------


@dataclass(frozen=True)
class SprintContract:
    """
    Auditable multi-step plan produced by the planner.

    The contract is immutable; the :class:`SprintExecution` tracker
    holds the mutable state. Callers persist the contract to SQLite
    via :class:`ash.core.session.SessionStore`.
    """

    goal: str
    definition_of_done: tuple[str, ...] = ()
    files_in_scope: tuple[Path, ...] = ()
    files_off_limits: tuple[Path, ...] = ()
    test_command: str = "pytest tests/"
    rollback_plan: str = "git revert HEAD"
    max_cost_inr: float = 0.0
    estimated_steps: int = 0
    contract_id: str = field(default_factory=lambda: str(uuid4()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "goal": self.goal,
            "definition_of_done": list(self.definition_of_done),
            "files_in_scope": [str(p) for p in self.files_in_scope],
            "files_off_limits": [str(p) for p in self.files_off_limits],
            "test_command": self.test_command,
            "rollback_plan": self.rollback_plan,
            "max_cost_inr": self.max_cost_inr,
            "estimated_steps": self.estimated_steps,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SprintContract":
        return cls(
            contract_id=data.get("contract_id", str(uuid4())),
            goal=data["goal"],
            definition_of_done=tuple(data.get("definition_of_done", [])),
            files_in_scope=tuple(Path(p) for p in data.get("files_in_scope", [])),
            files_off_limits=tuple(Path(p) for p in data.get("files_off_limits", [])),
            test_command=data.get("test_command", "pytest tests/"),
            rollback_plan=data.get("rollback_plan", "git revert HEAD"),
            max_cost_inr=float(data.get("max_cost_inr", 0.0)),
            estimated_steps=int(data.get("estimated_steps", 0)),
        )


# --- execution tracker ----------------------------------------------------


@dataclass
class SprintExecution:
    """Mutable state that lives alongside an immutable :class:`SprintContract`."""

    contract: SprintContract
    items: list[ChecklistItem] = field(default_factory=list)
    state: SprintState = SprintState.PLANNING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    abort_reason: str = ""

    # --- state transitions --------------------------------------------

    def transition(self, target: SprintState) -> None:
        """Move to ``target`` if the transition is allowed."""

        allowed = _SPRINT_TRANSITIONS[self.state]
        if target not in allowed:
            raise SprintTransitionError(
                f"Cannot transition from {self.state!r} to {target!r}; "
                f"allowed: {sorted(s.value for s in allowed)}"
            )
        self.state = target
        now = datetime.now(timezone.utc)
        if target == SprintState.ACTIVE and self.started_at is None:
            self.started_at = now
        elif target in {SprintState.COMPLETE, SprintState.ABORTED}:
            self.completed_at = now

    def start(self) -> None:
        """PLANNING → ACTIVE."""

        self.transition(SprintState.ACTIVE)

    def complete(self) -> None:
        """ACTIVE → COMPLETE."""

        self.transition(SprintState.COMPLETE)

    def abort(self, reason: str = "") -> None:
        """Move to ABORTED from any non-terminal state."""

        self.abort_reason = reason
        self.transition(SprintState.ABORTED)

    # --- checklist management -----------------------------------------

    def set_items(self, items: Iterable[ChecklistItem]) -> None:
        self.items = list(items)

    def mark_item_done(self, idx: int, notes: str = "") -> ChecklistItem:
        updated = self._find(idx).mark_done(notes)
        self._replace(updated)
        return updated

    def mark_item_failed(self, idx: int, notes: str = "") -> ChecklistItem:
        updated = self._find(idx).mark_failed(notes)
        self._replace(updated)
        return updated

    def mark_item_in_progress(self, idx: int) -> ChecklistItem:
        updated = self._find(idx).mark_in_progress()
        self._replace(updated)
        return updated

    def mark_item_skipped(self, idx: int, notes: str = "") -> ChecklistItem:
        updated = self._find(idx).mark_skipped(notes)
        self._replace(updated)
        return updated

    def set_item_status(
        self,
        idx: int,
        status: ChecklistStatus,
        notes: str = "",
    ) -> ChecklistItem:
        updated = _replace(self._find(idx), status=status, notes=notes)
        self._replace(updated)
        return updated

    def _find(self, idx: int) -> ChecklistItem:
        for item in self.items:
            if item.idx == idx:
                return item
        raise KeyError(f"No checklist item with idx={idx}")

    def _replace(self, item: ChecklistItem) -> None:
        for i, existing in enumerate(self.items):
            if existing.idx == item.idx:
                self.items[i] = item
                return
        raise KeyError(f"No checklist item with idx={item.idx}")

    # --- status reporting ---------------------------------------------

    @property
    def progress(self) -> tuple[int, int]:
        """Return ``(done_count, total_count)`` for the checklist."""

        if not self.items:
            return 0, 0
        done = sum(
            1
            for i in self.items
            if i.status in {ChecklistStatus.DONE, ChecklistStatus.SKIPPED}
        )
        return done, len(self.items)

    @property
    def is_terminal(self) -> bool:
        return self.state in {SprintState.COMPLETE, SprintState.ABORTED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract.to_dict(),
            "state": self.state,
            "items": [i.to_dict() for i in self.items],
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
            "abort_reason": self.abort_reason,
        }


# --- heuristic ------------------------------------------------------------


SPRINT_PLAN_TRIGGERS: tuple[str, ...] = (
    # Long verbs that typically signal multi-step work.
    "implement",
    "add ",
    "create ",
    "build ",
    "migrate ",
    "refactor ",
    "rewrite ",
    "design ",
    "decompose",
    "scaffold",
    "set up ",
    "integrate",
)


def looks_like_sprint_request(user_input: str, min_words: int = 6) -> bool:
    """
    Heuristic to decide whether a user request warrants a planning phase.

    A prompt counts as a sprint request if it is long enough and starts
    with a verb that suggests multi-step work. The check is deliberately
    conservative so simple reads or one-shot edits still flow through
    the normal turn loop.
    """

    if not user_input:
        return False
    lowered = user_input.strip().lower()
    if not lowered:
        return False
    word_count = len(lowered.split())
    if word_count < min_words:
        return False
    return any(lowered.startswith(verb) for verb in SPRINT_PLAN_TRIGGERS)
