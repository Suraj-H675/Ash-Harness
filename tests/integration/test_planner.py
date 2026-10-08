"""Integration tests for the Sprint 12 planner + sprint state machine."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest

from ash.config import AshConfig
from ash.core.planner import (
    Planner,
    PlannerError,
    apply_sprint_markdown_edit,
    parse_sprint_response,
    render_sprint_markdown,
)
from ash.core.loop import AshLoop
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

    async def stream_chat(self, messages, temperature: float = 0.0, tools=None):
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
    assert contract.files_in_scope == (
        Path("auth/models.py"),
        Path("auth/views.py"),
    )
    assert contract.files_off_limits == (Path("config/secrets.py"),)
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
    exec = parse_sprint_response(
        "## Checklist\n### X\n- [ ] only item\n", fallback_goal="from fallback"
    )
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


def test_apply_sprint_markdown_edit_preserves_identity_and_validates_paths() -> None:
    execution = parse_sprint_response(
        "## Goal\nOld\n\n## Checklist\n### Work\n- [ ] Old step\n",
        fallback_goal="old",
    )
    contract_id = execution.contract.contract_id
    apply_sprint_markdown_edit(
        execution,
        "## Goal\nNew\n\n## Files in Scope\n- src/new.py\n\n"
        "## Checklist\n### Work\n- [ ] New step\n",
    )
    assert execution.contract.contract_id == contract_id
    assert execution.contract.goal == "New"
    assert execution.items[0].description == "New step"

    with pytest.raises(PlannerError, match="unsafe path"):
        apply_sprint_markdown_edit(
            execution,
            "## Goal\nBad\n\n## Files in Scope\n- ../outside\n\n"
            "## Checklist\n### Work\n- [ ] Bad step\n",
        )


def test_planner_error_accepts_messages() -> None:
    error = PlannerError("bad plan")
    assert str(error) == "bad plan"


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


@pytest.mark.asyncio
async def test_planner_bounds_streamed_output_and_closes_stream(tmp_path: Path) -> None:
    from ash.core.planner import MAX_PLANNER_RESPONSE_BYTES

    closed = False

    class OversizedProvider(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            nonlocal closed
            try:
                yield StreamChunk(content="x" * MAX_PLANNER_RESPONSE_BYTES)
                yield StreamChunk(content="unexpected overflow")
            finally:
                closed = True

    with pytest.raises(PlannerError, match="exceeds 256 KiB"):
        await Planner(OversizedProvider([])).decompose(
            "Build a new feature", project_root=tmp_path
        )
    assert closed is True


def test_planner_decompose_rejects_empty_request(tmp_path: Path) -> None:
    provider = FakeProvider(scripts=[])
    planner = Planner(provider)
    with pytest.raises(Exception):
        asyncio.run(planner.decompose("   ", project_root=tmp_path))


@pytest.mark.parametrize("approved", [False, True])
def test_planner_usage_is_included_in_approved_and_rejected_turns(
    tmp_path: Path, approved: bool
) -> None:
    class UsageProvider(FakeProvider):
        def configure_max_tokens(self, max_tokens: int) -> None:
            self.max_tokens = max_tokens

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            async for chunk in super().stream_chat(messages, temperature, tools):
                if chunk.is_done:
                    counts = (11, 7) if self._call_count == 1 else (5, 3)
                    yield chunk.model_copy(
                        update={
                            "prompt_tokens": counts[0],
                            "completion_tokens": counts[1],
                            "usage_source": "provider",
                        }
                    )
                else:
                    yield chunk

    provider = UsageProvider(
        scripts=[
            [
                "## Goal\nImplement login\n\n## Test Command\npytest\n\n"
                "## Checklist\n### Work\n- [ ] Add login endpoint\n"
            ],
            ["<response>implemented</response>"],
        ]
    )
    store = SessionStore(tmp_path / "sessions.db")
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        model="openai/fake-planner",
        model_pricing_usd_per_million={
            "fake-planner": {"input": 1_000_000.0, "output": 2_000_000.0}
        },
    )
    loop = AshLoop(
        store,
        provider,
        SafetyGuard(tmp_path),
        _make_ui(input_text="y\n" if approved else "n\n"),
        tmp_path,
        planner=Planner(provider),
        enable_sprint_planning=True,
        config=config,
    )
    session = asyncio.run(loop.start_session())
    if not approved:
        loop._last_turn_budget_exhausted = True
    response = asyncio.run(loop.run_turn("Implement user authentication for the API"))
    if approved:
        assert response == "implemented"
    else:
        assert "Plan rejected" in response
    assert provider._call_count == (2 if approved else 1)
    assert store.started_turns(session.session_id) == []
    usage = store.get_session_usage(session.session_id)
    assert usage.prompt_tokens == (16 if approved else 11)
    assert usage.completion_tokens == (10 if approved else 7)
    assert usage.cost_usd == pytest.approx(36 if approved else 25)
    assert loop.last_turn_usage["usage_source"] == "provider"
    assert loop.last_turn_usage["cost_known"] is True
    assert loop._last_turn_budget_exhausted is False


def test_sprint_planning_respects_turn_token_budget_before_provider_call(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(scripts=[["## Goal\nShould never be generated\n"]])
    store = SessionStore(tmp_path / "sessions.db")
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        model="openai/fake-planner",
        max_turn_total_tokens=1,
    )
    loop = AshLoop(
        store, provider, SafetyGuard(tmp_path), _make_ui(input_text="y\n"),
        tmp_path, planner=Planner(provider), enable_sprint_planning=True, config=config,
    )
    asyncio.run(loop.start_session())
    with pytest.raises(PlannerError, match="exceeds turn token budget"):
        asyncio.run(loop.run_turn("Implement user authentication for the API"))
    assert provider._call_count == 0


@pytest.mark.asyncio
async def test_planner_provider_timeout_closes_stream(tmp_path: Path) -> None:
    closed = False

    class HangingProvider(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            nonlocal closed
            try:
                yield StreamChunk(content="## Goal\nPartial\n")
                await asyncio.sleep(60)
            finally:
                closed = True

    with pytest.raises(PlannerError, match="timed out"):
        await Planner(HangingProvider([])).decompose_with_usage(
            "Build a feature", project_root=tmp_path, timeout_seconds=0.01
        )
    assert closed is True


@pytest.mark.parametrize("stop_reason", ["length", "content_filter", "error"])
def test_planner_rejects_noncomplete_terminal_outcome(
    tmp_path: Path, stop_reason: str
) -> None:
    class TerminatedProvider(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            yield StreamChunk(
                content="## Goal\nComplete\n", is_done=True, stop_reason=stop_reason
            )

    with pytest.raises(PlannerError, match="incomplete or unsuccessful"):
        asyncio.run(
            Planner(TerminatedProvider([])).decompose(
                "Build a feature", project_root=tmp_path
            )
        )


def test_planner_rejects_stream_without_terminal_chunk(tmp_path: Path) -> None:
    class IncompleteProvider(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            yield StreamChunk(content="## Goal\nPartial\n")

    with pytest.raises(PlannerError, match="without completion"):
        asyncio.run(
            Planner(IncompleteProvider([])).decompose(
                "Build a feature", project_root=tmp_path
            )
        )


def test_loop_runs_editable_planning_phase_before_execution(tmp_path: Path) -> None:
    provider = FakeProvider(
        scripts=[
            [
                "## Goal\nImplement login\n\n## Test Command\npytest\n\n"
                "## Rollback Plan\nrevert\n\n## Checklist\n\n"
                "### Implementation\n- [ ] Add login endpoint\n"
            ],
            ["<response>executed plan</response>"],
        ]
    )
    store = SessionStore(tmp_path / "sessions.db")
    guard = SafetyGuard(tmp_path)
    loop = AshLoop(
        store,
        provider,
        guard,
        _make_ui(input_text="y\n"),
        tmp_path,
        planner=Planner(provider),
        enable_sprint_planning=True,
    )

    asyncio.run(loop.start_session())
    response = asyncio.run(loop.run_turn("Implement user authentication for the API"))

    assert response == "executed plan"
    assert loop.current_session is not None
    sprint_ids = store.list_session_sprints(loop.current_session.session_id)
    assert len(sprint_ids) == 1
    sprint = store.load_sprint(sprint_ids[0])
    assert sprint.state == SprintState.ACTIVE
    assert sprint.items[0].description == "Add login endpoint"


def test_loop_injects_live_persisted_plan_state_into_provider(tmp_path: Path) -> None:
    provider = FakeProvider(
        scripts=[
            [
                "## Goal\nImplement login\n\n## Test Command\npytest\n\n"
                "## Rollback Plan\nrevert\n\n## Checklist\n\n"
                "### Implementation\n- [ ] Add login endpoint\n"
            ],
            ["<response>continued plan</response>"],
            ["<response>live plan state</response>"],
        ]
    )
    store = SessionStore(tmp_path / "sessions.db")
    loop = AshLoop(
        store,
        provider,
        SafetyGuard(tmp_path),
        _make_ui(input_text="y\n"),
        tmp_path,
        planner=Planner(provider),
        enable_sprint_planning=True,
    )

    session = asyncio.run(loop.start_session())
    response = asyncio.run(loop.run_turn("Implement user authentication for the API"))
    assert response == "continued plan"

    sprint_ids = store.list_session_sprints(session.session_id)
    execution = store.load_sprint(sprint_ids[0])
    execution.mark_item_in_progress(1)
    store.save_sprint(session.session_id, execution)

    second_response = asyncio.run(loop.run_turn("Continue implementing login"))
    assert second_response == "live plan state"

    execution_messages = [
        messages
        for messages in provider.received_messages[1:]
        if "Current Sprint Plan" in messages[0]["content"]
    ]
    assert len(execution_messages) == 2
    assert "progress=0/1" in execution_messages[-1][0]["content"]
    assert (
        "[>] 1. (Implementation) Add login endpoint"
        in (execution_messages[-1][0]["content"])
    )


def test_loop_does_not_substitute_input_without_new_plan(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    provider = FakeProvider(scripts=[["<response>normal turn</response>"]])
    loop = AshLoop(
        store,
        provider,
        SafetyGuard(tmp_path),
        _make_ui(),
        tmp_path,
        enable_sprint_planning=True,
    )

    asyncio.run(loop.start_session())
    response = asyncio.run(loop.run_turn("Implement user authentication for the API"))

    assert response == "normal turn"
    assert provider.received_messages[0][-1]["content"] == (
        "Implement user authentication for the API"
    )


# ---------------------------------------------------------------------------
# SessionStore persistence
# ---------------------------------------------------------------------------


def test_sprint_save_and_load_round_trip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s.db")
    session = store.create_session(str(tmp_path))

    items = [
        ChecklistItem(
            idx=1,
            section="Impl",
            description="step a",
            status=ChecklistStatus.DONE,
            notes="ok",
        ),
        ChecklistItem(
            idx=2,
            section="Impl",
            description="step b",
            status=ChecklistStatus.SKIPPED,
            notes="",
        ),
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
            ChecklistItem(
                idx=1, section="A", description="a", status=ChecklistStatus.DONE
            ),
            ChecklistItem(
                idx=2, section="A", description="b", status=ChecklistStatus.SKIPPED
            ),
            ChecklistItem(
                idx=3, section="A", description="c", status=ChecklistStatus.PENDING
            ),
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


def test_show_plan_can_edit_then_approve(monkeypatch) -> None:
    execution = parse_sprint_response(
        "## Goal\nOld\n\n## Checklist\n### Work\n- [ ] Old step\n",
        fallback_goal="old",
    )
    ui = _make_ui(input_text="e\ny\n")

    def edit(plan) -> None:
        apply_sprint_markdown_edit(
            plan,
            "## Goal\nEdited\n\n## Checklist\n### Work\n- [ ] Edited step\n",
        )

    monkeypatch.setattr(ui, "_edit_plan", edit)
    assert ui.show_plan(execution) is True
    assert execution.contract.goal == "Edited"
