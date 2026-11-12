"""Owner-selected post-verification completion stops extra model requests."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel

from ash.config import AshConfig
from ash.cli import _bootstrap_and_headless
from ash.core.loop import AshLoop
from ash.core.session import Message, SessionStore, ToolCallRecord
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult
from ash.tools.filesystem import ReplaceFileContentTool
from ash.ui.headless import HeadlessUI


COMMAND = "python3 -B qa_visible.py"


class CheckArgs(BaseModel):
    command_line: str


class SyntheticCheckTool(BaseTool):
    name = "run_command"
    description = "Synthetic, non-shell test tool for isolated unit tests"
    args_schema = CheckArgs

    def __init__(self, guard: SafetyGuard, *, passes: bool = True):
        super().__init__(guard)
        self.passes = passes
        self.executions = 0

    async def run(self, **kwargs):
        CheckArgs(**kwargs)
        self.executions += 1
        return ToolResult(
            success=self.passes,
            output="Synthetic test passed" if self.passes else "Synthetic test failed",
            error=None if self.passes else "Synthetic failure",
            exit_code=0 if self.passes else 1,
        )


class EditThenCheckProvider(ProviderABC):
    model_name = "synthetic-checkpoint"
    _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

    def __init__(self, *, command: str = COMMAND):
        self.calls = 0
        self.command = command

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                is_done=True,
                native_tool_calls=[{
                    "id": "edit-1",
                    "name": "replace_file_content",
                    "arguments": {
                        "file_path": "module.py",
                        "start_line": 1,
                        "end_line": 1,
                        "target_content": "before = 0",
                        "replacement_content": "after = 1",
                    },
                }],
            )
        elif self.calls == 2:
            yield StreamChunk(
                is_done=True,
                native_tool_calls=[{
                    "id": "check-1",
                    "name": "run_command",
                    "arguments": {"command_line": self.command},
                }],
            )
        else:
            yield StreamChunk(content="Normal provider final report", is_done=True)


def make_loop(
    tmp_path: Path, *, provider=None, passes: bool = True,
) -> tuple[AshLoop, SyntheticCheckTool]:
    (tmp_path / "module.py").write_text("before = 0\n")
    guard = SafetyGuard(tmp_path)
    command = SyntheticCheckTool(guard, passes=passes)
    config = AshConfig(
        model="local/synthetic-checkpoint",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        provider or EditThenCheckProvider(),
        guard,
        HeadlessUI(output_format="text", stream=io.StringIO()),
        tmp_path,
        tools={
            "replace_file_content": ReplaceFileContentTool(guard),
            "run_command": command,
        },
        safety_tier="auto_approve",
        config=config,
    )
    return loop, command


@pytest.mark.asyncio
async def test_exact_owner_checkpoint_finishes_without_third_model_request(
    tmp_path: Path,
) -> None:
    provider = EditThenCheckProvider()
    loop, command = make_loop(tmp_path, provider=provider)
    try:
        session = await loop.start_session()
        loop._owner_report_after_command = COMMAND
        result = await loop.run_turn("Repair the file and run the exact test")
        assert provider.calls == 2
        assert command.executions == 1
        assert (tmp_path / "module.py").read_text() == "after = 1\n"
        assert loop._last_turn_owner_checkpoint
        assert loop._last_turn_stop_reason == "owner_command_checkpoint"
        assert "Owner-selected verification checkpoint" in result
        assert COMMAND not in result  # Do not echo arbitrary command contents.
        assert "not a claim that all other requirements" in result
        saved = loop.session_store.load_session(session.session_id)
        assert len(saved.tool_calls) == 2
        assert all(call.success for call in saved.tool_calls)
        assert saved.messages[-1].metadata["owner_command_checkpoint"] is True
        assert loop.session_store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_one_shot_cli_emits_structured_owner_checkpoint(tmp_path: Path) -> None:
    provider = EditThenCheckProvider()
    loop, command = make_loop(tmp_path, provider=provider)
    output = io.StringIO()
    ui = HeadlessUI(output_format="json", stream=output)
    loop.ui = ui
    result = await _bootstrap_and_headless(
        loop,
        SimpleNamespace(attachment_token_budget=1024),
        prompt="Fix the source then run the authorized check",
        session_id=None,
        ui=ui,
        report_after_command=COMMAND,
    )
    assert result == 0
    assert provider.calls == 2
    assert command.executions == 1
    assert loop._owner_report_after_command is None
    report = json.loads(output.getvalue())
    assert report["type"] == "turn.completed"
    assert report["owner_command_checkpoint"] is True
    assert "hidden checks" in report["response"]


def test_cli_owner_checkpoint_is_explicit_and_rejects_resume_modes() -> None:
    import argparse

    from ash.cli import main
    from ash.cli_parser import configure_parser

    parser = argparse.ArgumentParser()
    configure_parser(parser, "test")
    assert parser.parse_args(["-p", "hi"]).report_after_command is None
    assert parser.parse_args([
        "-p", "hi", "--report-after-command", COMMAND
    ]).report_after_command == COMMAND
    for invalid in (
        ["--report-after-command", COMMAND],
        ["--session", "any", "-p", "hi", "--report-after-command", COMMAND],
        ["-p", "hi", "--report-after-command", ""],
        ["-p", "hi", "--report-after-command", COMMAND, "--json-schema", "/tmp/schema.json"],
    ):
        with pytest.raises(SystemExit) as exc:
            main(invalid)
        assert exc.value.code == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_command,tool_command,passes", [
    (None, COMMAND, True),
    ("python3 other_test.py", COMMAND, True),
    (COMMAND, "python3 other_test.py", True),
    (COMMAND, COMMAND, False),
])
async def test_mismatch_failure_or_disabled_checkpoint_keeps_normal_model_flow(
    tmp_path: Path, owner_command: str | None, tool_command: str,
    passes: bool,
) -> None:
    provider = EditThenCheckProvider(command=tool_command)
    loop, command = make_loop(tmp_path, provider=provider, passes=passes)
    try:
        await loop.start_session()
        loop._owner_report_after_command = owner_command
        response = await loop.run_turn("Repair and test")
        assert response == "Normal provider final report"
        assert provider.calls == 3
        assert command.executions == 1
        assert not loop._last_turn_owner_checkpoint
    finally:
        await loop.aclose()


@pytest.mark.parametrize("case", [
    "test_before_edit", "failed_read", "unknown_action", "unapproved",
    "undispatched", "wrong_command", "wrong_cwd", "no_edit", "other_project",
])
def test_durable_owner_checkpoint_rejects_unverified_action_history(
    tmp_path: Path, case: str,
) -> None:
    store = SessionStore(tmp_path / "records.db")
    session = store.create_session(str(tmp_path))
    turn_id = "owner-check"
    store.start_turn(session.session_id, turn_id, "Owner task")
    edit_name = "replace_file_content"
    records = [
        (edit_name, {"file_path": "module.py"}),
        ("run_command", {"command_line": COMMAND}),
    ]
    if case == "test_before_edit":
        records.reverse()
    elif case == "failed_read":
        records.insert(1, ("read_file", {"file_path": "module.py"}))
    elif case == "unknown_action":
        records.insert(1, ("mcp__external__side_effect", {}))
    elif case == "no_edit":
        records.pop(0)
    elif case == "wrong_command":
        records[-1] = ("run_command", {"command_line": "other test"})
    elif case == "wrong_cwd":
        records[-1] = ("run_command", {"command_line": COMMAND, "cwd": "subdir"})
    for idx, (name, arguments) in enumerate(records):
        store.save_tool_call(
            session.session_id,
            ToolCallRecord(
                call_id=f"call-{idx}", tool_name=name, arguments=arguments,
                approved=case != "unapproved" or idx != 0,
                dispatched=case != "undispatched" or idx != 0,
                executed=case != "undispatched" or idx != 0,
                success=case != "failed_read" or name != "read_file",
                error="failure" if case == "failed_read" and name == "read_file" else None,
                timestamp=datetime.now(timezone.utc),
            ),
            turn_id=turn_id,
        )
    result = store.owner_command_checkpoint_evidence(
        session.session_id, turn_id,
        expected_project_path=(tmp_path / "elsewhere" if case == "other_project" else tmp_path),
        command_line=COMMAND,
    )
    assert result is None


def test_owner_checkpoint_rejects_an_assistant_call_missing_its_tool_result(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "incomplete.db")
    session = store.create_session(str(tmp_path))
    turn_id = "incomplete-turn"
    store.start_turn(session.session_id, turn_id, "Fix and test")
    for index, (name, arguments) in enumerate((
        ("replace_file_content", {"file_path": "module.py"}),
        ("run_command", {"command_line": COMMAND}),
    )):
        store.save_tool_call(
            session.session_id,
            ToolCallRecord(
                call_id=f"complete-{index}", tool_name=name,
                arguments=arguments, approved=True, dispatched=True,
                executed=True, success=True, timestamp=datetime.now(timezone.utc),
            ),
            turn_id=turn_id,
        )
    store.save_message(
        session.session_id,
        Message(
            role="assistant", content="",
            timestamp=datetime.now(timezone.utc),
            metadata={"tool_calls": [
                {"call_id": "unfinished-tool", "name": "read_file",
                 "arguments": {"file_path": "module.py"}}
            ]},
        ),
        turn_id=turn_id,
    )
    assert store.owner_command_checkpoint_evidence(
        session.session_id, turn_id,
        expected_project_path=tmp_path,
        command_line=COMMAND,
    ) is None
