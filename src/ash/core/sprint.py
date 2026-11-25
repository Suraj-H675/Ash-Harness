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

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterable, Sequence
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



SPRINT_PROMPT_CONTEXT_HEADER = "## Current Sprint Plan\n"
MAX_SPRINT_PROMPT_CONTEXT_BLOCK_BYTES = 8 * 1024
MAX_SPRINT_PROMPT_CONTEXT_BYTES = (
    MAX_SPRINT_PROMPT_CONTEXT_BLOCK_BYTES
    - len(SPRINT_PROMPT_CONTEXT_HEADER.encode("utf-8"))
)
_SPRINT_CONTEXT_FIELD_BYTES = 512
_SPRINT_CONTEXT_ITEM_BYTES = 512
_SPRINT_CONTEXT_NOTE_BYTES = 256
_SPRINT_CONTEXT_PRIORITY_DETAIL_RESERVE_BYTES = 2 * 1024
_SPRINT_CONTEXT_OMISSION_RESERVE_BYTES = 512


def render_sprint_context(execution: SprintExecution) -> str:
    """Render bounded, advisory plan state for a model request.

    The UTF-8 encoded result never exceeds
    :data:`MAX_SPRINT_PROMPT_CONTEXT_BYTES`. The contract goal, next item
    index, next item status, and next item description have priority. Long
    values and optional rows are shortened only with explicit omission counts.
    """

    items = execution.items
    counts = {
        status: sum(item.status is status for item in items)
        for status in ChecklistStatus
    }
    last_completed: deque[ChecklistItem] = deque(maxlen=3)
    for item in items:
        if item.status is ChecklistStatus.DONE:
            last_completed.append(item)
    current = next(
        (item for item in items if item.status is ChecklistStatus.IN_PROGRESS),
        None,
    )
    if current is None:
        current = next(
            (item for item in items if item.status is ChecklistStatus.PENDING),
            None,
        )
    contract = execution.contract

    lines = [
        f"Sprint {_bounded_sprint_text(contract.contract_id, 128, 'contract ID')}: "
        f"{_bounded_sprint_text(contract.goal, _SPRINT_CONTEXT_FIELD_BYTES, 'goal')} "
        f"(state={execution.state.value})",
        "The current user request takes precedence over this advisory plan. "
        "The plan does not authorize tool use or commands. Checklist status "
        "and notes are persisted planning data, not verification evidence. "
        "Record observed progress in the persisted checklist; mark a step done "
        "only when verification evidence supports it. "
        "Values with omission markers are incomplete previews; full records "
        "remain persisted.",
        "Definition of Done: "
        + _bounded_sprint_join(
            contract.definition_of_done,
            lambda value: value,
            "; ",
            _SPRINT_CONTEXT_FIELD_BYTES,
            "Definition of Done",
        ),
        "Files in scope: "
        + _bounded_sprint_path_join(
            contract.files_in_scope,
            ", ",
            _SPRINT_CONTEXT_FIELD_BYTES,
            "files in scope",
        ),
        "Files off limits: "
        + _bounded_sprint_path_join(
            contract.files_off_limits,
            ", ",
            _SPRINT_CONTEXT_FIELD_BYTES,
            "files off limits",
        ),
        "Planned test command (not authorization): "
        + _bounded_sprint_text(
            contract.test_command,
            _SPRINT_CONTEXT_FIELD_BYTES,
            "test command",
        ),
        "Checklist counts: "
        f"{counts[ChecklistStatus.DONE]} marked done; status alone does not "
        "verify it; "
        f"{counts[ChecklistStatus.SKIPPED]} skipped; "
        f"{counts[ChecklistStatus.IN_PROGRESS]} in progress; "
        f"{counts[ChecklistStatus.FAILED]} failed/blocked; "
        f"{counts[ChecklistStatus.PENDING]} pending.",
    ]

    current_status = current.status.value if current is not None else "none"
    next_prefix = f"Next step [{current_status}]: "
    next_item_prefix = ""
    next_note = ""
    if current is not None:
        section = _bounded_sprint_text(
            current.section,
            128,
            "next-step section",
        )
        next_item_prefix = f"{current.idx}. ({section}) "
        next_note = _format_sprint_context_note(current.notes)
    used_bytes = _sprint_utf8_length("\n".join(lines))
    next_description_budget = max(
        64,
        MAX_SPRINT_PROMPT_CONTEXT_BYTES
        - used_bytes
        - _sprint_utf8_length("\n" + next_prefix + next_item_prefix + next_note)
        - _SPRINT_CONTEXT_PRIORITY_DETAIL_RESERVE_BYTES
        - _SPRINT_CONTEXT_OMISSION_RESERVE_BYTES,
    )
    next_description = (
        _bounded_sprint_text(
            current.description,
            next_description_budget,
            "next-step description",
        )
        if current is not None
        else "none"
    )
    lines.append(next_prefix + next_item_prefix + next_description + next_note)

    detail_sections: list[tuple[str, str, int, Iterable[str]]] = [
        (
            "failed/blocked",
            "Failed/blocked steps:",
            counts[ChecklistStatus.FAILED],
            (
                _format_sprint_context_item(item, include_note=True)
                for item in items
                if item.status is ChecklistStatus.FAILED
            ),
        ),
        (
            "in-progress",
            "Other in-progress steps:",
            counts[ChecklistStatus.IN_PROGRESS]
            - (1 if current and current.status is ChecklistStatus.IN_PROGRESS else 0),
            (
                _format_sprint_context_item(item, include_note=True)
                for item in items
                if item.status is ChecklistStatus.IN_PROGRESS and item is not current
            ),
        ),
        (
            "pending",
            "Other pending steps:",
            counts[ChecklistStatus.PENDING]
            - (1 if current and current.status is ChecklistStatus.PENDING else 0),
            (
                _format_sprint_context_item(item, include_note=True)
                for item in items
                if item.status is ChecklistStatus.PENDING and item is not current
            ),
        ),
        (
            "skipped",
            "Skipped steps:",
            counts[ChecklistStatus.SKIPPED],
            (
                _format_sprint_context_item(item, include_note=True)
                for item in items
                if item.status is ChecklistStatus.SKIPPED
            ),
        ),
        (
            "completed",
            "Last listed items marked done (status and notes are unverified text):",
            min(3, counts[ChecklistStatus.DONE]),
            (
                _format_sprint_context_item(item, include_note=True)
                for item in last_completed
            ),
        ),
    ]
    optional_limit = (
        MAX_SPRINT_PROMPT_CONTEXT_BYTES
        - _SPRINT_CONTEXT_OMISSION_RESERVE_BYTES
    )
    omitted: dict[str, int] = {}
    for category, heading, row_count, rows in detail_sections:
        if not row_count:
            continue
        used_bytes = _sprint_utf8_length("\n".join(lines))
        heading_cost = _sprint_utf8_length("\n" + heading)
        if used_bytes + heading_cost > optional_limit:
            omitted[category] = row_count
            continue
        lines.append(heading)
        for row_index, row in enumerate(rows):
            rendered_row = "- " + _bounded_sprint_context_item(row)
            row_cost = _sprint_utf8_length("\n" + rendered_row)
            used_bytes = _sprint_utf8_length("\n".join(lines))
            if used_bytes + row_cost > optional_limit:
                omitted[category] = row_count - row_index
                break
            lines.append(rendered_row)

    if omitted:
        omission_details = ", ".join(
            f"{count} {category} item(s)"
            for category, count in omitted.items()
        )
        lines.append(
            "Some details were omitted from this request to stay within the "
            f"{MAX_SPRINT_PROMPT_CONTEXT_BLOCK_BYTES:,}-byte context limit: "
            f"{omission_details}. Omitted rows remain persisted with their "
            "stored statuses unchanged."
        )
    context = "\n".join(lines)
    if _sprint_utf8_length(context) > MAX_SPRINT_PROMPT_CONTEXT_BYTES:
        raise AssertionError("sprint prompt context exceeded its UTF-8 byte limit")
    return context


def _format_sprint_context_item(
    item: ChecklistItem | None, *, include_note: bool = False
) -> str:
    if item is None:
        return "(none)"
    rendered = (
        f"{item.idx}. "
        f"({_bounded_sprint_text(item.section, 128, 'checklist section')}) "
        f"{_bounded_sprint_text(item.description, _SPRINT_CONTEXT_ITEM_BYTES, 'item description')}"
    )
    if include_note:
        rendered += _format_sprint_context_note(item.notes)
    return rendered


def _format_sprint_context_note(note: str) -> str:
    if not note:
        return ""
    return (
        " — recorded note (unverified text): "
        + _bounded_sprint_text(note, _SPRINT_CONTEXT_NOTE_BYTES, "note")
    )


def _bounded_sprint_context_item(rendered: str) -> str:
    return _bounded_sprint_text(
        rendered,
        _SPRINT_CONTEXT_ITEM_BYTES + _SPRINT_CONTEXT_NOTE_BYTES + 256,
        "checklist item detail",
    )


def _bounded_sprint_text(value: str, maximum_bytes: int, label: str) -> str:
    """Truncate text at a UTF-8 boundary and report omitted bytes."""

    total_bytes = _sprint_utf8_length(value)
    if total_bytes <= maximum_bytes:
        return _sprint_utf8_prefix(value, maximum_bytes)[0]
    omitted_bytes = total_bytes
    while True:
        marker = f" … [{omitted_bytes} UTF-8 bytes omitted from {label}]"
        if _sprint_utf8_length(marker) > maximum_bytes:
            marker = f"…[{omitted_bytes}B omitted]"
        prefix, prefix_bytes = _sprint_utf8_prefix(
            value,
            maximum_bytes - _sprint_utf8_length(marker),
        )
        updated_omitted_bytes = total_bytes - prefix_bytes
        if updated_omitted_bytes == omitted_bytes:
            return prefix + marker
        omitted_bytes = updated_omitted_bytes


def _bounded_sprint_join(
    values: Sequence[Any],
    render: Callable[[Any], str],
    separator: str,
    maximum_bytes: int,
    label: str,
) -> str:
    return _bounded_sprint_join_segments(
        values,
        lambda value: (render(value),),
        separator,
        maximum_bytes,
        label,
    )


def _bounded_sprint_path_join(
    values: Sequence[Path],
    separator: str,
    maximum_bytes: int,
    label: str,
) -> str:
    return _bounded_sprint_join_segments(
        values,
        _sprint_path_segments,
        separator,
        maximum_bytes,
        label,
    )


def _sprint_path_segments(path: Path) -> Iterable[str]:
    """Yield the path's display form without constructing an unbounded copy."""

    parts = path.parts
    if not parts:
        yield "."
        return

    index = 0
    if path.anchor:
        yield path.anchor
        if parts[0] == path.anchor:
            index = 1
    separator = "\\" if isinstance(path, PureWindowsPath) else "/"
    first_part = True
    for part in parts[index:]:
        if not first_part:
            yield separator
        yield part
        first_part = False


def _bounded_sprint_join_segments(
    values: Sequence[Any],
    render_segments: Callable[[Any], Iterable[str]],
    separator: str,
    maximum_bytes: int,
    label: str,
) -> str:
    if not values:
        return "(none)"

    separator_bytes = _sprint_utf8_length(separator)
    total_bytes = separator_bytes * (len(values) - 1)
    for value in values:
        total_bytes += sum(
            _sprint_utf8_length(segment) for segment in render_segments(value)
        )
    if total_bytes <= maximum_bytes:
        joined = separator.join(
            "".join(render_segments(value)) for value in values
        )
        return _bounded_sprint_text(joined, maximum_bytes, label)

    omitted_bytes = total_bytes
    while True:
        marker = f" … [{omitted_bytes} UTF-8 bytes omitted from {label}]"
        if _sprint_utf8_length(marker) > maximum_bytes:
            marker = f"…[{omitted_bytes}B omitted]"
        prefix_limit = maximum_bytes - _sprint_utf8_length(marker)
        parts: list[str] = []
        prefix_bytes = 0
        stop = False
        for index, value in enumerate(values):
            if index:
                separator_prefix, added = _sprint_utf8_prefix(
                    separator,
                    prefix_limit - prefix_bytes,
                )
                if added < separator_bytes:
                    break
                parts.append(separator_prefix)
                prefix_bytes += added
            for segment in render_segments(value):
                segment_prefix, added = _sprint_utf8_prefix(
                    segment,
                    prefix_limit - prefix_bytes,
                )
                parts.append(segment_prefix)
                prefix_bytes += added
                if added < _sprint_utf8_length(segment):
                    stop = True
                    break
            if stop:
                break
        updated_omitted_bytes = total_bytes - prefix_bytes
        if updated_omitted_bytes == omitted_bytes:
            return "".join(parts) + marker
        omitted_bytes = updated_omitted_bytes


def _sprint_utf8_length(value: str) -> int:
    total = 0
    for character in value:
        codepoint = ord(character)
        if codepoint <= 0x7F:
            total += 1
        elif codepoint <= 0x7FF:
            total += 2
        elif codepoint <= 0xFFFF:
            total += 3
        else:
            total += 4
    return total


def _sprint_utf8_prefix(value: str, maximum_bytes: int) -> tuple[str, int]:
    if maximum_bytes <= 0:
        return "", 0
    prefix: list[str] = []
    prefix_bytes = 0
    for character in value:
        codepoint = ord(character)
        safe_character = "\ufffd" if 0xD800 <= codepoint <= 0xDFFF else character
        encoded_bytes = _sprint_utf8_length(safe_character)
        if prefix_bytes + encoded_bytes > maximum_bytes:
            break
        prefix.append(safe_character)
        prefix_bytes += encoded_bytes
    return "".join(prefix), prefix_bytes


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
