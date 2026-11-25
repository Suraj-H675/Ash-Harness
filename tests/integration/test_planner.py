"""Integration tests for the Sprint 12 planner + sprint state machine."""

from __future__ import annotations

import asyncio
import io
import re
import sqlite3
from pathlib import Path

import pytest

from ash.config import AshConfig
from ash.commands.plans import list_plans
from ash.core.goals import GoalState
from ash.core.planner import (
    MAX_PLANNER_RESPONSE_BYTES,
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
    MAX_SPRINT_PROMPT_CONTEXT_BLOCK_BYTES,
    MAX_SPRINT_PROMPT_CONTEXT_BYTES,
    SprintContract,
    SprintExecution,
    SprintState,
    looks_like_sprint_request,
    render_sprint_context,
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


def test_render_sprint_context_keeps_actionable_state_without_done_item_dump() -> None:
    execution = SprintExecution(
        contract=SprintContract(
            goal="Build cursor search",
            definition_of_done=("Cursor results match indexed text",),
            files_in_scope=(Path("src/search.py"),),
            files_off_limits=(Path("src/auth.py"),),
            test_command="pytest tests/unit/test_search.py",
        ),
        state=SprintState.ACTIVE,
    )
    execution.set_items(
        [
            ChecklistItem(
                1,
                "Research",
                "Inspect the current search API",
                ChecklistStatus.DONE,
                "claimed verified by a previous run",
            ),
            ChecklistItem(
                2,
                "Testing",
                "Run the focused cursor verification",
                ChecklistStatus.PENDING,
                "No test run recorded; verification remains pending",
            ),
            ChecklistItem(
                3,
                "Implementation",
                "Resolve the null cursor regression",
                ChecklistStatus.FAILED,
                "Blocked by a stale API response observed in the fixture",
            ),
            ChecklistItem(
                4,
                "Implementation",
                "Connect cursor state to query responses",
                ChecklistStatus.IN_PROGRESS,
                "Implementation is still underway",
            ),
            ChecklistItem(
                5,
                "Research",
                "Check compatibility with old clients",
                ChecklistStatus.SKIPPED,
                "Deferred by the user",
            ),
        ]
    )

    context = render_sprint_context(execution)

    assert "Build cursor search" in context
    assert "Cursor results match indexed text" in context
    assert "src/search.py" in context
    assert "src/auth.py" in context
    assert "pytest tests/unit/test_search.py" in context
    assert "1 marked done; status alone does not verify it" in context
    assert "1 skipped; 1 in progress; 1 failed/blocked; 1 pending." in context
    assert (
        "Next step [in_progress]: 4. (Implementation) Connect cursor state "
        "to query responses"
        in context
    )
    assert "Implementation is still underway" in context
    assert (
        "Other pending steps:\n- 2. (Testing) Run the focused cursor verification"
        in context
    )
    assert "No test run recorded; verification remains pending" in context
    assert (
        "Failed/blocked steps:\n- 3. (Implementation) Resolve the null cursor regression"
        in context
    )
    assert (
        "recorded note (unverified text): Blocked by a stale API response "
        "observed in the fixture"
        in context
    )
    assert "Skipped steps:\n- 5. (Research) Check compatibility with old clients" in context
    assert "Deferred by the user" in context
    assert "Record observed progress in the persisted checklist" in context
    assert "mark a step done only when verification evidence supports it" in context
    assert (
        "Last listed items marked done (status and notes are unverified text):\n"
        "- 1. (Research) Inspect the current search API — recorded note "
        "(unverified text): claimed verified by a previous run"
        in context
    )
    assert "does not authorize tool use or commands" in context

    pending_execution = SprintExecution(
        contract=SprintContract(goal="Review bounded next-step identity"),
        state=SprintState.ACTIVE,
    )
    pending_execution.set_items(
        [
            ChecklistItem(
                7,
                "Review",
                "Check the exact pending description",
                ChecklistStatus.PENDING,
            )
        ]
    )
    pending_context = render_sprint_context(pending_execution)
    assert (
        "Next step [pending]: 7. (Review) Check the exact pending description"
        in pending_context
    )


def test_render_sprint_context_has_a_hard_utf8_ceiling_for_large_notes() -> None:
    execution = SprintExecution(
        contract=SprintContract(goal="Build bounded prompt context"),
        state=SprintState.ACTIVE,
    )
    execution.set_items(
        [
            ChecklistItem(
                idx=index,
                section="Implementation",
                description=f"Complete bounded context step {index:03d}",
                status=(
                    ChecklistStatus.PENDING
                    if index == 1
                    else ChecklistStatus.FAILED
                    if index % 7 == 0
                    else ChecklistStatus.IN_PROGRESS
                    if index == 2
                    else ChecklistStatus.DONE
                    if index <= 60
                    else ChecklistStatus.PENDING
                ),
                notes="évidence-" + ("n" * 16_384),
            )
            for index in range(1, 81)
        ]
    )

    context = render_sprint_context(execution)

    assert len(context.encode("utf-8")) <= MAX_SPRINT_PROMPT_CONTEXT_BYTES
    assert "Next step [in_progress]: 2. (Implementation) Complete bounded context step 002" in context
    assert "11 failed/blocked" in context
    assert "18 pending." in context
    assert "Some details were omitted from this request to stay within the 8,192-byte context limit" in context
    assert re.search(r"\[\d+ UTF-8 bytes omitted from note\]", context)
    assert "Omitted rows remain persisted with their stored statuses unchanged" in context
    assert "does not authorize tool use or commands" in context


def test_render_sprint_context_truncates_unbounded_plan_strings_at_utf8_boundaries() -> None:
    long_text = "界" * 20_000
    execution = SprintExecution(
        contract=SprintContract(
            goal=long_text,
            definition_of_done=(long_text,),
            files_in_scope=(Path(long_text),),
            files_off_limits=(Path(long_text),),
            test_command=long_text,
        ),
        state=SprintState.ACTIVE,
    )
    execution.set_items(
        [
            ChecklistItem(
                idx=1,
                section=long_text,
                description=long_text,
                status=ChecklistStatus.PENDING,
            ),
            ChecklistItem(
                idx=2,
                section=long_text,
                description=long_text,
                status=ChecklistStatus.FAILED,
                notes=long_text,
            ),
            *[
                ChecklistItem(
                    idx=index,
                    section=long_text,
                    description=long_text,
                    status=ChecklistStatus.PENDING,
                    notes=long_text,
                )
                for index in range(3, 100)
            ],
        ]
    )

    context = render_sprint_context(execution)

    assert len(context.encode("utf-8")) <= MAX_SPRINT_PROMPT_CONTEXT_BYTES
    assert "[pending]: 1." in context
    assert "98 pending." in context
    assert re.search(r"\[\d+ UTF-8 bytes omitted from goal\]", context)
    assert re.search(r"\[\d+ UTF-8 bytes omitted from Definition of Done\]", context)
    assert re.search(r"\[\d+ UTF-8 bytes omitted from files in scope\]", context)
    assert re.search(r"\[\d+ UTF-8 bytes omitted from files off limits\]", context)
    assert re.search(r"\[\d+ UTF-8 bytes omitted from test command\]", context)
    assert re.search(r"\[\d+ UTF-8 bytes omitted from next-step description\]", context)
    assert "\ufffd" not in context
    assert "Failed/blocked steps:" in context
    assert "Omitted rows remain persisted with their stored statuses unchanged" in context
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


def test_apply_sprint_markdown_edit_rejects_unbounded_text() -> None:
    execution = parse_sprint_response(
        "## Goal\nOriginal\n\n## Checklist\n### Work\n- [ ] Original step\n",
        fallback_goal="Original",
    )
    original_goal = execution.contract.goal
    original_items = list(execution.items)
    oversized = (
        "## Goal\n" + ("x" * (MAX_PLANNER_RESPONSE_BYTES + 1)) +
        "\n## Checklist\n### Work\n- [ ] Next step\n"
    )

    with pytest.raises(PlannerError, match="edited plan exceeds 256 KiB"):
        apply_sprint_markdown_edit(execution, oversized)

    assert execution.contract.goal == original_goal
    assert execution.items == original_items


def test_sprint_requirement_edit_preserves_only_matching_progress_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sessions.db"
    store = SessionStore(database)
    session = store.create_session(str(tmp_path))
    execution = parse_sprint_response(
        "## Goal\nBuild cursor search\n\n## Checklist\n"
        "### Research\n- [ ] Read current API\n\n"
        "### Implementation\n- [ ] Draft cursor shape\n\n"
        "### Verification\n- [ ] Run focused tests\n",
        fallback_goal="Build cursor search",
    )
    execution.mark_item_done(1, "Reviewed existing request and response handling")
    execution.set_item_status(
        2,
        ChecklistStatus.IN_PROGRESS,
        "Draft is under review",
    )
    execution.mark_item_failed(3, "Blocked until provider fixture is available")
    store.save_sprint(session.session_id, execution)

    reopened = SessionStore(database)
    persisted = reopened.load_sprint(execution.contract.contract_id)
    apply_sprint_markdown_edit(
        persisted,
        "## Goal\nSupport opaque cursors and resumable lookup\n\n"
        "## Definition of Done\n- Preserve opaque cursor values\n\n"
        "## Checklist\n"
        "### Product\n- [ ] Confirm opaque cursor semantics\n\n"
        "### Research\n- [ ] Read current API\n\n"
        "### Implementation\n- [ ] Draft cursor shape for opaque tokens\n\n"
        "### Verification\n- [ ] Run focused tests\n- [ ] Resume from persisted plan\n",
    )
    reopened.save_sprint(session.session_id, persisted)

    resumed_store = SessionStore(database)
    resumed = resumed_store.load_sprint(execution.contract.contract_id)
    by_description = {item.description: item for item in resumed.items}
    summary = list_plans(resumed_store, project_path=str(tmp_path))[0]

    assert resumed.contract.goal == "Support opaque cursors and resumable lookup"
    assert resumed.contract.contract_id == execution.contract.contract_id
    assert summary.goal == resumed.contract.goal
    assert by_description["Read current API"].status is ChecklistStatus.DONE
    assert by_description["Read current API"].notes == (
        "Reviewed existing request and response handling"
    )
    assert by_description["Run focused tests"].status is ChecklistStatus.FAILED
    assert by_description["Run focused tests"].notes == (
        "Blocked until provider fixture is available"
    )
    assert by_description["Draft cursor shape for opaque tokens"].status is (
        ChecklistStatus.PENDING
    )
    assert by_description["Draft cursor shape for opaque tokens"].notes == ""
    assert by_description["Confirm opaque cursor semantics"].status is (
        ChecklistStatus.PENDING
    )
    assert by_description["Resume from persisted plan"].status is (
        ChecklistStatus.PENDING
    )
    assert "Draft cursor shape" not in by_description

    context = render_sprint_context(resumed)
    assert "Support opaque cursors and resumable lookup" in context
    assert "Definition of Done: Preserve opaque cursor values" in context
    assert "1 marked done; status alone does not verify it" in context
    assert "Next step [pending]: 1. (Product) Confirm opaque cursor semantics" in context
    assert "3. (Implementation) Draft cursor shape for opaque tokens" in context
    assert "Blocked until provider fixture is available" in context
    assert "Run focused tests" in context
    assert "Resume from persisted plan" in context
    assert (
        "recorded note (unverified text): Reviewed existing request and "
        "response handling"
        in context
    )
    assert "current user request takes precedence" in context


def test_sprint_requirement_edit_does_not_guess_between_duplicate_steps() -> None:
    execution = parse_sprint_response(
        "## Goal\nCheck output\n\n## Checklist\n"
        "### Verification\n- [ ] Run the fixture\n- [ ] Run the fixture\n",
        fallback_goal="Check output",
    )
    execution.mark_item_done(1, "First fixture run passed")

    apply_sprint_markdown_edit(
        execution,
        "## Goal\nCheck output after new setup\n\n## Checklist\n"
        "### Setup\n- [ ] Prepare the fixture\n\n"
        "### Verification\n- [ ] Run the fixture\n- [ ] Run the fixture\n",
    )

    duplicate_steps = [
        item for item in execution.items if item.description == "Run the fixture"
    ]
    assert len(duplicate_steps) == 2
    assert all(item.status is ChecklistStatus.PENDING for item in duplicate_steps)
    assert all(item.notes == "" for item in duplicate_steps)


def test_goal_block_pause_resume_and_changed_objective_survive_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sessions.db"
    store = SessionStore(database)
    session = store.create_session(str(tmp_path))
    goal = store.create_goal(
        session.session_id,
        "Build cursor search using the original pagination contract",
        max_continuations=3,
    )
    blocked = store.record_goal_progress(
        goal.goal_id,
        "Source review is complete; implementation is blocked pending the fixture format.",
    )
    paused = store.pause_current_goal(session.session_id)
    assert paused is not None and paused.state is GoalState.PAUSED
    assert paused.last_evidence == blocked.last_evidence

    restarted = SessionStore(database)
    recovered = restarted.load_current_goal(session.session_id)
    assert recovered is not None
    assert recovered.goal_id == goal.goal_id
    assert recovered.state is GoalState.PAUSED
    assert recovered.last_evidence == blocked.last_evidence

    resumed = restarted.resume_current_goal(session.session_id)
    assert resumed.state is GoalState.ACTIVE
    assert resumed.continuations_used == 0
    assert resumed.objective == goal.objective

    cleared = restarted.clear_current_goal(session.session_id)
    assert cleared.state is GoalState.CLEARED
    assert cleared.completed_at is None
    updated = restarted.create_goal(
        session.session_id,
        "Build cursor search with opaque tokens and resume coverage",
        max_continuations=3,
    )
    assert restarted.load_current_goal(session.session_id) == updated
    completed = restarted.record_goal_progress(
        updated.goal_id,
        "Opaque-token regression and resumed-session verification both passed.",
        complete=True,
    )
    assert completed.state is GoalState.COMPLETE
    assert completed.completed_at is not None
    assert restarted.load_current_goal(session.session_id) is None


def test_removed_requirement_is_not_restored_from_persisted_plan_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sessions.db"
    store = SessionStore(database)
    session = store.create_session(str(tmp_path))
    execution = parse_sprint_response(
        "## Goal\nOld cursor behavior\n\n## Checklist\n### Work\n"
        "- [ ] Remove obsolete search path\n"
        "- [ ] Retain cursor behavior\n",
        fallback_goal="Old cursor behavior",
    )
    store.save_sprint(session.session_id, execution)

    apply_sprint_markdown_edit(
        execution,
        "## Goal\nUpdated cursor behavior\n\n## Checklist\n### Work\n"
        "- [ ] Retain cursor behavior\n",
    )
    store.save_sprint(session.session_id, execution)

    reopened = SessionStore(database).load_sprint(execution.contract.contract_id)
    assert [item.description for item in reopened.items] == [
        "Retain cursor behavior"
    ]


def test_sprint_replacement_rolls_back_if_a_checklist_insert_fails(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sessions.db"
    store = SessionStore(database)
    session = store.create_session(str(tmp_path))
    execution = parse_sprint_response(
        "## Goal\nOriginal plan\n\n## Checklist\n### Work\n"
        "- [ ] Original first item\n- [ ] Original second item\n",
        fallback_goal="Original plan",
    )
    store.save_sprint(session.session_id, execution)

    connection = store.open_connection()
    try:
        connection.execute(
            """
            CREATE TRIGGER reject_task19_second_item
            BEFORE INSERT ON checklist_items
            WHEN NEW.idx = 2
            BEGIN
                SELECT RAISE(ABORT, 'injected checklist insert failure');
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    apply_sprint_markdown_edit(
        execution,
        "## Goal\nUpdated plan\n\n## Checklist\n### Work\n"
        "- [ ] Updated first item\n- [ ] Updated second item\n",
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected checklist insert failure"):
        store.save_sprint(session.session_id, execution)

    reopened = SessionStore(database).load_sprint(execution.contract.contract_id)
    assert reopened.contract.goal == "Original plan"
    assert [item.description for item in reopened.items] == [
        "Original first item",
        "Original second item",
    ]


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
    profile_label = "SECRET_LOOKING_PLANNER_PROFILE_FOR_TEST_ONLY"

    class UsageProvider(FakeProvider):
        active_profile = profile_label
        profile_ids = (profile_label,)

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
    events = store.list_recent_runtime_events(session.session_id, limit=100)
    planner_assembly = next(
        item.event
        for item in events
        if item.event.get("type") == "context.usage"
        and item.event.get("assembly_purpose") == "request"
    )
    planner_events = [
        item.event
        for item in events
        if item.event.get("context_id") == planner_assembly["context_id"]
    ]
    assert any(event.get("type") == "model.request.started" for event in planner_events)
    assert any(
        event.get("type") in {"model.request.completed", "model.request.error"}
        for event in planner_events
    )
    assert all("credential_profile" not in event for event in planner_events)
    assert profile_label not in repr(planner_events)


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

    session = asyncio.run(loop.start_session())
    response = asyncio.run(loop.run_turn("Implement user authentication for the API"))

    assert response == "executed plan"
    assert loop.current_session is not None
    sprint_ids = store.list_session_sprints(loop.current_session.session_id)
    assert len(sprint_ids) == 1
    sprint = store.load_sprint(sprint_ids[0])
    assert sprint.state == SprintState.ACTIVE
    assert sprint.items[0].description == "Add login endpoint"
    events = store.list_recent_runtime_events(session.session_id, limit=100)
    planner_start = next(
        event.event
        for event in events
        if event.event.get("type") == "model.request.started"
        and event.event.get("model") == "custom/fake-planner"
    )
    planner_context_id = planner_start["context_id"]
    planner_assembly = next(
        event.event
        for event in events
        if event.event.get("type") == "context.usage"
        and event.event.get("context_id") == planner_context_id
    )
    planner_completion = next(
        event.event
        for event in events
        if event.event.get("type") == "model.request.completed"
        and event.event.get("context_id") == planner_context_id
    )
    assert planner_assembly["assembly_purpose"] == "request"
    assert planner_assembly["estimate_source"] == "unavailable"
    assert planner_assembly["current"] is None
    assert planner_start["operation_id"] == planner_completion["operation_id"]
    assert planner_start["requested_route"] == "custom/fake-planner"


def test_planner_and_loop_use_their_own_provider_budgets(tmp_path: Path) -> None:
    from ash.providers.capabilities import ProviderCapabilities

    class BudgetProvider(FakeProvider):
        def __init__(self, scripts: list[list[str]], capabilities: ProviderCapabilities):
            super().__init__(scripts)
            self.capabilities = capabilities
            self.completion_limits: list[int] = []

        def configure_max_tokens(self, max_tokens: int) -> None:
            self.completion_limits.append(max_tokens)

    plan_provider = BudgetProvider(
        [
            [
                "## Goal\nImplement login\n\n## Test Command\npytest\n\n"
                "## Rollback Plan\nrevert\n\n## Checklist\n\n"
                "### Implementation\n- [ ] Add login endpoint\n"
            ],
        ],
        ProviderCapabilities(
            context_window=1_000,
            max_input_tokens=500,
            max_output_tokens=80,
        ),
    )
    execution_provider = BudgetProvider(
        [["<response>executed plan</response>"]],
        ProviderCapabilities(
            context_window=2_000,
            max_input_tokens=1_200,
            max_output_tokens=40,
        ),
    )
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        max_context_tokens=5_000,
        max_completion_tokens=1_000,
        memory_backend="off",
    )
    store = SessionStore(tmp_path / "route-budget.db")
    loop = AshLoop(
        store,
        execution_provider,
        SafetyGuard(tmp_path),
        _make_ui(input_text="y\n"),
        tmp_path,
        tools={},
        config=config,
        system_prompt="test",
        planner=Planner(plan_provider),
        enable_sprint_planning=True,
    )

    asyncio.run(loop.start_session())
    response = asyncio.run(
        loop.run_turn("Implement user authentication for the API")
    )

    assert response == "executed plan"
    assert plan_provider.completion_limits == [80]
    assert execution_provider.completion_limits == [40]


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
    latest_context = execution_messages[-1][0]["content"]
    assert "Checklist counts: 0 marked done" in latest_context
    assert "Next step [in_progress]: 1. (Implementation) Add login endpoint" in latest_context
    assert "does not authorize tool use or commands" in latest_context
    context_start = latest_context.index("## Current Sprint Plan\n")
    context_end = latest_context.index("\n\n", context_start)
    assert (
        len(latest_context[context_start:context_end].encode("utf-8"))
        <= MAX_SPRINT_PROMPT_CONTEXT_BLOCK_BYTES
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
    assert len(provider.received_messages) == 1
    assert "Current Sprint Plan" not in provider.received_messages[0][0]["content"]
    assert provider.received_messages[0][-1]["content"] == (
        "Implement user authentication for the API"
    )


def test_one_shot_request_skips_configured_planner(tmp_path: Path) -> None:
    provider = FakeProvider(scripts=[["<response>One-shot coding path</response>"]])
    store = SessionStore(tmp_path / "sessions.db")
    loop = AshLoop(
        store,
        provider,
        SafetyGuard(tmp_path),
        _make_ui(),
        tmp_path,
        planner=Planner(provider),
        enable_sprint_planning=True,
    )

    asyncio.run(loop.start_session())
    response = asyncio.run(loop.run_turn("Fix typo in README."))

    assert response == "One-shot coding path"
    assert len(provider.received_messages) == 1
    assert "Current Sprint Plan" not in provider.received_messages[0][0]["content"]
    assert provider.received_messages[0][-1]["content"] == "Fix typo in README."


def test_paused_or_cleared_goal_does_not_override_changed_user_intent(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(
        scripts=[
            ["<response>Current request honored</response>"],
            ["<response>New request honored</response>"],
        ]
    )
    provider.aclose = lambda: asyncio.sleep(0)
    store = SessionStore(tmp_path / "sessions.db")
    loop = AshLoop(
        store,
        provider,
        SafetyGuard(tmp_path),
        _make_ui(),
        tmp_path,
        enable_sprint_planning=True,
    )

    async def journey() -> None:
        session = await loop.start_session()
        execution = SprintExecution(
            contract=SprintContract(goal="Continue the previous checkout rewrite")
        )
        execution.set_items(
            [
                ChecklistItem(
                    1,
                    "Implementation",
                    "Rewrite checkout pricing",
                    ChecklistStatus.PENDING,
                )
            ]
        )
        execution.start()
        store.save_sprint(session.session_id, execution)
        loop.create_goal("Finish the previous checkout rewrite")

        paused = loop.pause_goal()
        assert paused is not None and paused.state is GoalState.PAUSED
        first_request = "Summarize only the current checkout diff."
        assert await loop.run_turn(first_request) == "Current request honored"

        cleared = loop.clear_goal()
        assert cleared.state is GoalState.CLEARED
        second_request = "Report current test status only; do not continue the old task."
        assert await loop.run_turn(second_request) == "New request honored"

    try:
        asyncio.run(journey())
        assert len(provider.received_messages) == 2
        for messages, expected_request in zip(
            provider.received_messages,
            (
                "Summarize only the current checkout diff.",
                "Report current test status only; do not continue the old task.",
            ),
            strict=True,
        ):
            system_text = messages[0]["content"]
            assert "Current Sprint Plan" in system_text
            assert "The current user request takes precedence" in system_text
            assert "## Active Goal" not in system_text
            assert messages[-1]["content"] == expected_request
    finally:
        asyncio.run(loop.aclose())


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
