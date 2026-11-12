"""Fresh reasoning-window continuation never replays uncertain actions."""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ash.cli_parser import configure_parser
from ash.cli import _bootstrap_and_headless
from ash.context.history import ContextBudgetExceededError
from ash.core.context_continuation import run_with_context_continuation
from ash.core.session import (
    SessionForkConflictError,
    SessionStore,
    ToolCallRecord,
)
from ash.safety.guard import SafetyGuard
from ash.ui.headless import HeadlessUI


class FailingThenSuccessfulCodingLoop:
    def __init__(
        self, root: Path, *, tool: str = "replace_file_content",
        dispatched: bool = True, executed: bool = True,
        opaque: bool = True, fail_count: int = 1,
    ) -> None:
        self.project_root = root
        self.session_store = SessionStore(root / "sessions.db")
        self.safety_guard = SafetyGuard(project_root=root)
        self.current_session = self.session_store.create_session(
            str(root), model="openai/gpt-6-luna", permission_mode="auto_edit"
        )
        self.original_session_id = self.current_session.session_id
        self.model_attempts = 0
        self._tool = tool
        self._dispatched = dispatched
        self._executed = executed
        self._opaque = opaque
        self._fail_count = fail_count
        self.inputs: list[str] = []
        self.recovered_calls = 0
        self.observed_wall_budget: float | None = None

    @asynccontextmanager
    async def wall_time_budget(self, seconds: float):
        self.observed_wall_budget = seconds
        yield

    async def start_session(self, session_id: str | None = None):
        if session_id is not None:
            self.current_session = self.session_store.load_session(session_id)
        return self.current_session

    async def aclose(self) -> None:
        return None

    @property
    def provider(self):
        return SimpleNamespace(
            capabilities=SimpleNamespace(vision=False),
            count_tokens=lambda text: len(text),
        )

    @property
    def active_model_id(self) -> str:
        return "openai/gpt-6-luna"

    @property
    def last_turn_usage(self) -> dict[str, int]:
        return {"prompt_tokens": 120, "completion_tokens": 10}

    _last_context_tokens = 4096
    _last_turn_stop_reason = "completed"
    recovered_turns = 0
    recovery_summary = None

    async def _recover_current_session_before_turn(self) -> None:
        self.recovered_calls += 1
        # The ordinary on-start recovery skips idle interrupted turns.
        from ash.core.checkpoints import recover_interrupted_turns

        recover_interrupted_turns(
            self.session_store, self.safety_guard,
            self.current_session.session_id,
        )

    async def run_turn(self, input_text: str) -> str:
        self.inputs.append(input_text)
        self.model_attempts += 1
        if self.model_attempts > self._fail_count:
            return "Completed after verifying actual workspace"

        turn_id = f"turn-{self.model_attempts}"
        self.session_store.start_turn(
            self.current_session.session_id, turn_id, input_text
        )
        self.session_store.save_tool_call(
            self.current_session.session_id,
            ToolCallRecord(
                call_id=f"call-{self.model_attempts}",
                tool_name=self._tool,
                arguments={"file_path": "module.py"},
                approved=True,
                dispatched=self._dispatched,
                executed=self._executed,
                success=True if self._executed else None,
                result="known successful edit" if self._executed else None,
                error=None,
                timestamp=datetime.now(timezone.utc),
            ),
            turn_id=turn_id,
        )
        self.session_store.interrupt_turn(turn_id)
        raise ContextBudgetExceededError(
            72_904, 59_141, protected_current_turn=True,
            provider_replay_state_preserved=self._opaque,
        )


@pytest.mark.asyncio
async def test_automatic_continuation_uses_fresh_child_and_original_user_task(
    tmp_path: Path,
) -> None:
    loop = FailingThenSuccessfulCodingLoop(tmp_path)
    original = "Fix the two modules; preserve auth on same-origin redirects"
    result = await run_with_context_continuation(
        loop, original, max_continuations=1
    )
    assert result.response == "Completed after verifying actual workspace"
    assert result.context_continuations == 1
    assert result.session_id != loop.original_session_id
    assert loop.model_attempts == 2
    assert loop.recovered_calls == 1
    assert loop.inputs[0] == original
    assert "Inspect the actual workspace" in loop.inputs[1]
    child = loop.session_store.load_session(result.session_id)
    parent = loop.session_store.load_session(loop.original_session_id)
    assert child.parent_session_id == parent.session_id
    assert child.permission_mode == "auto_edit"
    assert child.tool_calls == []
    assert len(child.messages) == 1
    assert original in child.messages[0].content
    assert "not imported or authorized for replay" in child.messages[0].content.lower()
    assert [call.call_id for call in parent.tool_calls] == ["call-1"]
    assert parent.tool_calls[0].success is True
    assert loop.session_store.interrupted_recovery_reports(parent.session_id)[0][
        "status"
    ] == "interrupted"


@pytest.mark.asyncio
async def test_automatic_continuation_refuses_even_known_external_commands(
    tmp_path: Path,
) -> None:
    loop = FailingThenSuccessfulCodingLoop(tmp_path, tool="run_command")
    with pytest.raises(SessionForkConflictError, match="without external actions"):
        await run_with_context_continuation(
            loop, "Fix the issue", max_continuations=1
        )
    assert len(loop.session_store.session_tree(loop.original_session_id)) == 1
    assert loop.model_attempts == 1


@pytest.mark.asyncio
async def test_automatic_continuation_refuses_ambiguous_dispatched_effect(
    tmp_path: Path,
) -> None:
    loop = FailingThenSuccessfulCodingLoop(
        tmp_path, tool="run_command", executed=False
    )
    with pytest.raises(SessionForkConflictError, match="quiescent"):
        await run_with_context_continuation(
            loop, "Fix the issue", max_continuations=1
        )
    assert len(loop.session_store.session_tree(loop.original_session_id)) == 1
    assert loop.model_attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("opaque,max_continuations", [(False, 1), (True, 0)])
async def test_unrecoverable_context_failures_are_not_silently_retried(
    tmp_path: Path, opaque: bool, max_continuations: int,
) -> None:
    loop = FailingThenSuccessfulCodingLoop(tmp_path, opaque=opaque)
    with pytest.raises(ContextBudgetExceededError):
        await run_with_context_continuation(
            loop, "Fix the issue", max_continuations=max_continuations
        )
    assert loop.model_attempts == 1
    assert loop.recovered_calls == 0


@pytest.mark.asyncio
async def test_automatic_continuation_has_bounded_attempt_count(tmp_path: Path) -> None:
    loop = FailingThenSuccessfulCodingLoop(tmp_path, fail_count=5)
    with pytest.raises(ContextBudgetExceededError):
        await run_with_context_continuation(
            loop, "Fix the issue", max_continuations=2
        )
    assert loop.model_attempts == 3
    assert loop.recovered_calls == 2
    assert len(loop.session_store.session_tree(loop.original_session_id)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("task", ["  ", "x" * 8193])
async def test_continuation_refuses_unusable_original_task(
    tmp_path: Path, task: str,
) -> None:
    loop = FailingThenSuccessfulCodingLoop(tmp_path)
    with pytest.raises(ValueError, match="bounded user task"):
        await run_with_context_continuation(
            loop, task, max_continuations=1
        )
    assert loop.model_attempts == 0


@pytest.mark.parametrize("count", [0, 1, 2])
def test_cli_exposes_bounded_opt_in(count: int) -> None:
    parser = argparse.ArgumentParser()
    configure_parser(parser, "test")
    args = parser.parse_args(["-p", "fix the code", "--context-continuations", str(count)])
    assert args.context_continuations == count


def test_cli_context_continuation_disabled_by_default() -> None:
    parser = argparse.ArgumentParser()
    configure_parser(parser, "test")
    assert parser.parse_args(["-p", "hello"]).context_continuations == 0
    with pytest.raises(SystemExit):
        parser.parse_args(["-p", "hello", "--context-continuations", "3"])


def test_cli_wall_budget_is_opt_in() -> None:
    parser = argparse.ArgumentParser()
    configure_parser(parser, "test")
    assert parser.parse_args(["-p", "hello"]).turn_time_budget is None
    assert parser.parse_args([
        "-p", "hello", "--turn-time-budget", "300"
    ]).turn_time_budget == 300


@pytest.mark.parametrize("value", ["0", "29", "3601"])
def test_cli_rejects_wall_budget_outside_allowed_range(value: str) -> None:
    from ash.cli import main

    with pytest.raises(SystemExit) as error:
        main(["-p", "hello", "--turn-time-budget", value])
    assert error.value.code == 2


@pytest.mark.asyncio
async def test_headless_wall_budget_wraps_one_shot_and_reports_opt_in(
    tmp_path: Path,
) -> None:
    loop = FailingThenSuccessfulCodingLoop(tmp_path, fail_count=0)
    output = io.StringIO()
    ui = HeadlessUI(output_format="json", stream=output)
    result = await _bootstrap_and_headless(
        loop, SimpleNamespace(attachment_token_budget=1024),
        prompt="Complete a bounded coding task", session_id=None,
        ui=ui, turn_time_budget=300,
    )
    assert result == 0
    assert loop.observed_wall_budget == 300
    event = json.loads(output.getvalue().splitlines()[0])
    assert event["type"] == "turn.completed"
    assert event["turn_time_budget_seconds"] == 300


@pytest.mark.asyncio
async def test_headless_json_reports_child_session_after_safe_context_restart(
    tmp_path: Path,
) -> None:
    loop = FailingThenSuccessfulCodingLoop(tmp_path)
    output = io.StringIO()
    ui = HeadlessUI(output_format="json", stream=output)
    exit_code = await _bootstrap_and_headless(
        loop,
        SimpleNamespace(attachment_token_budget=1024),
        prompt="Fix both modules and run the test",
        session_id=None,
        ui=ui,
        context_continuations=1,
    )
    assert exit_code == 0
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(rows) == 1
    assert rows[0]["type"] == "turn.completed"
    assert rows[0]["session_id"] != loop.original_session_id
    assert rows[0]["context_continuations"] == 1
    assert rows[0]["response"] == "Completed after verifying actual workspace"
