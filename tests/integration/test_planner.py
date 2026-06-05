"""Integration tests for the Sprint 12 planner + sprint state machine."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest

from ash.core.planner import Planner, parse_sprint_response, render_sprint_markdown
from ash.core.session import SessionStore
from ash.core.sprint import (
    ChecklistItem,
    ChecklistStatus,
    SprintContract,
    SprintExecution,
    SprintState,
    looks_like_sprint_request,
)
from ash.providers.base import StreamChunk
from ash.safety.guard import SafetyGuard
from ash.ui.terminal import TerminalUI


# ---------------------------------------------------------------------------
# Fakes (mirroring tests/integration/test_loop.py patterns)
# ---------------------------------------------------------------------------


class FakeProvider:
    def __init__(self, scripts: list[list[str]]) -> None:
        self._scripts = [list(s) for s in scripts]
        self._call_count = 0
        self.received_messages: list[list[dict]] = []

    @property
    def model_name(self) -> str:
        return "fake-planner"

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def stream_chat(self, messages, temperature: float = 0.0):
        self.received_messages.append(list(messages))
        if self._call_count >= len(self._scripts):
            yield StreamChunk(content="", is_done=True)
            return
        script = self._scripts[self._call_count]
        self._call_count += 1
        for fragment in script:
            yield StreamChunk(content=fragment)
        yield StreamChunk(content="", is_done=True)


def _silent_console():
    from rich.console import Console

    return Console(file=io.StringIO(), force_terminal=False, width=120)


def _make_ui(input_text: str = "") -> TerminalUI:
    return TerminalUI(
        safety_tier="auto_approve",
        console=_silent_console(),
        input_stream=io.StringIO(input_text),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_tmp_path() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix="ash-planner-"))


# ---------------------------------------------------------------------------
# Heuristic
# ---------------------------------------------------------------------------


def test_looks_like_sprint_request_heuristic() -> None:
    assert looks_like_sprint_request("Implement user authentication for the API")
    assert looks_like_sprint_request("Refactor the auth module to use bcrypt")
    assert not looks_like_sprint_request("hi")
    assert not looks_like_sprint_request("read x.py")
    assert not looks_like_sprint_request("")  # empty
    # Too short to count as multi-step
    assert not looks_like_sprint_request("Add login")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_sprint_response_extracts_every_section() -> None:
    raw = """## Goal
Add user authentication to the API

## Definition of Done
- All endpoints protected except /login
- JWT tokens expire after 1 hour
- All tests pass

## Files in Scope
- auth/models.py
- auth/views.py

## Files Off Limits
- config/secrets.py

## Test Command
pytest tests/auth/

## Rollback Plan
git revert HEAD and re-run the suite

## Checklist

### Research
- [ ] Read existing API structure
- [ ] Check for existing auth patterns

### Implementation
- [ ] Create User model
- [ ] Add login endpoint

### Testing
- [ ] Write tests
- [ ] Run full suite
"""
    exec = parse_sprint_response(raw, fallback_goal="ignored")
    contract = exec.contract

    assert contract.goal == "Add user authentication to the API"
    assert contract.definition_of_done == (
        "All endpoints protected except /login",
        "JWT tokens expire after 1 hour",
        "All tests pass",
    )
    assert [str(p) for p in contract.files_in_scope] == ["auth/models.py", "auth/views.py"]
    assert [str(p) for p in contract.files_off_limits] == ["config/secrets.py"]
    assert contract.test_command == "pytest tests/auth/"
    assert "git revert HEAD" in contract.rollback_plan
    assert exec.state == SprintState.PLANNING
    assert len(exec.items) == 6
    assert exec.items[0].section == "Research"
    assert exec.items[1].section == "Research"
    assert exec.items[2].section == "Implementation"
    assert exec.items[4].section == "Testing"
    assert all(item.status == ChecklistStatus.PENDING for item in exec.items)


def test_parse_sprint_response_with_missing_checklist_returns_empty_items() -> None:
    exec = parse_sprint_response("## Goal\ndo the thing\n", fallback_goal="fallback")
    assert exec.contract.goal == "do the thing"
    assert exec.items == []


def test_parse_sprint_response_uses_fallback_when_goal_missing() -> None:
    exec = parse_sprint_response("## Checklist\n### X\n- [ ] only item\n", fallback_goal="from fallback")
    assert exec.contract.goal == "from fallback"
    assert len(exec.items) == 1


def test_render_sprint_markdown_round_trip() -> None:
    exec = parse_sprint_response(
        """## Goal
Refactor auth

## Definition of Done
- Tests pass

## Test Command
pytest

## Rollback Plan
revert

## Checklist

### Research
- [ ] Read the docs

### Implementation
- [ ] Replace SHA256 with bcrypt
""",
        fallback_goal="ignored",
    )
    md = render_sprint_markdown(exec)
    assert "Refactor auth" in md
    assert "Read the docs" in md
    assert "Replace SHA256 with bcrypt" in md


# ---------------------------------------------------------------------------
# Planner end-to-end with a fake provider
# ---------------------------------------------------------------------------


def test_planner_decompose_calls_provider_and_parses(tmp_path: Path) -> None:
    provider = FakeProvider(
        scripts=[
            [
                "## Goal\nImplement login\n\n## Definition of Done\n- works\n\n"
                "## Test Command\npytest\n\n## Rollback Plan\nrevert\n\n## Checklist\n\n"
                "### Implementation\n- [ ] Add model\n- [ ] Add view\n"
            ]
        ]
    )
    planner = Planner(provider)
    execution = asyncio.run(planner.decompose("Implement login", project_root=tmp_path))
    assert execution.contract.goal == "Implement login"
    assert len(execution.items) == 2
    assert execution.state == SprintState.PLANNING
    # Provider received exactly one user-role message (the architect prompt).
    assert len(provider.received_messages) == 1
    assert provider.received_messages[0][-1]["role"] == "user"


def test_planner_decompose_rejects_empty_request(tmp_path: Path) -> None:
    provider = FakeProvider(scripts=[])
    planner = Planner(provider)
    with pytest.raises(Exception):
        asyncio.run(planner.decompose("   ", project_root=tmp_path))


# ---------------------------------------------------------------------------
# SessionStore persistence
# ---------------------------------------------------------------------------


def test_sprint_save_and_load_round_trip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s.db")
    session = store.create_session(str(tmp_path))

    items = [
        ChecklistItem(idx=1, section="Impl", description="step a", status=ChecklistStatus.DONE, notes="ok"),
        ChecklistItem(idx=2, section="Impl", description="step b", status=ChecklistStatus.SKIPPED, notes=""),
    ]
    contract = SprintContract(
        goal="add foo",
        definition_of_done=("tests pass",),
        files_in_scope=(),
        files_off_limits=(),
    )
    exec = SprintExecution(contract=contract)
    exec.set_items(items)
    exec.start()
    exec.mark_item_done(1, "ok")
    exec.mark_item_skipped(2, "out of scope")

    store.save_sprint(session.session_id, exec)

    loaded = store.load_sprint(contract.contract_id)
    assert loaded.contract.goal == "add foo"
    assert loaded.contract.definition_of_done == ("tests pass",)
    assert loaded.state == SprintState.ACTIVE
    assert loaded.started_at is not None
    assert loaded.completed_at is None
    assert len(loaded.items) == 2
    statuses = {i.idx: i.status for i in loaded.items}
    assert statuses[1] == ChecklistStatus.DONE
    assert statuses[2] == ChecklistStatus.SKIPPED
    assert loaded.items[0].notes == "ok"

    # list_session_sprints returns the new id.
    assert contract.contract_id in store.list_session_sprints(session.session_id)


def test_sprint_save_then_re_save_updates_state(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s.db")
    session = store.create_session(str(tmp_path))

    contract = SprintContract(goal="x")
    exec = SprintExecution(contract=contract)
    store.save_sprint(session.session_id, exec)
    exec.start()
    store.save_sprint(session.session_id, exec)
    exec.complete()
    store.save_sprint(session.session_id, exec)

    loaded = store.load_sprint(contract.contract_id)
    assert loaded.state == SprintState.COMPLETE
    assert loaded.completed_at is not None


def test_load_unknown_sprint_raises_keyerror(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s.db")
    with pytest.raises(KeyError):
        store.load_sprint("does-not-exist")


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def test_state_machine_valid_transitions() -> None:
    exec = SprintExecution(contract=SprintContract(goal="x"))
    assert exec.state == SprintState.PLANNING
    exec.start()
    assert exec.state == SprintState.ACTIVE
    exec.complete()
    assert exec.state == SprintState.COMPLETE
    assert exec.is_terminal


def test_state_machine_rejects_invalid_transitions() -> None:
    from ash.core.sprint import SprintTransitionError

    exec = SprintExecution(contract=SprintContract(goal="x"))
    with pytest.raises(SprintTransitionError):
        exec.complete()  # PLANNING -> COMPLETE not allowed
    exec.start()
    with pytest.raises(SprintTransitionError):
        exec.start()  # ACTIVE -> ACTIVE not allowed
    with pytest.raises(SprintTransitionError):
        exec.transition(SprintState.PLANNING)


def test_state_machine_abort_reachable_from_any_non_terminal_state() -> None:
    from ash.core.sprint import SprintTransitionError

    # From PLANNING
    e1 = SprintExecution(contract=SprintContract(goal="x"))
    e1.abort("user said no")
    assert e1.state == SprintState.ABORTED
    assert e1.abort_reason == "user said no"

    # From ACTIVE
    e2 = SprintExecution(contract=SprintContract(goal="x"))
    e2.start()
    e2.abort("tool failed")
    assert e2.state == SprintState.ABORTED

    # From terminal: forbidden
    e3 = SprintExecution(contract=SprintContract(goal="x"))
    e3.start()
    e3.complete()
    with pytest.raises(SprintTransitionError):
        e3.abort()


def test_progress_counts_done_and_skipped() -> None:
    exec = SprintExecution(contract=SprintContract(goal="x"))
    exec.set_items(
        [
            ChecklistItem(idx=1, section="A", description="a", status=ChecklistStatus.DONE),
            ChecklistItem(idx=2, section="A", description="b", status=ChecklistStatus.SKIPPED),
            ChecklistItem(idx=3, section="A", description="c", status=ChecklistStatus.PENDING),
        ]
    )
    done, total = exec.progress
    assert (done, total) == (2, 3)


# ---------------------------------------------------------------------------
# TerminalUI show_plan
# ---------------------------------------------------------------------------


def test_show_plan_returns_true_when_user_types_y() -> None:
    exec = parse_sprint_response(
        """## Goal
Refactor auth

## Test Command
pytest

## Rollback Plan
revert

## Checklist

### Implementation
- [ ] Replace SHA256 with bcrypt
""",
        fallback_goal="ignored",
    )
    ui = _make_ui(input_text="y\n")
    assert ui.show_plan(exec) is True


def test_show_plan_returns_false_when_user_types_n() -> None:
    exec = parse_sprint_response(
        """## Goal
Refactor auth

## Test Command
pytest

## Rollback Plan
revert

## Checklist

### Implementation
- [ ] Replace SHA256
""",
        fallback_goal="ignored",
    )
    ui = _make_ui(input_text="n\n")
    assert ui.show_plan(exec) is False


def test_show_plan_rejects_on_empty_input() -> None:
    exec = parse_sprint_response("## Goal\nx\n", fallback_goal="ignored")
    ui = _make_ui(input_text="\n")
    assert ui.show_plan(exec) is False
