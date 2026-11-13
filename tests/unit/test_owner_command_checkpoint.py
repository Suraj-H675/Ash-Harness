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


@pytest.mark.asyncio
@pytest.mark.parametrize("case,expect_success", [
    ("unverified", False),
    ("passed", True),
    ("wrong_command", False),
    ("failed_check", False),
    ("passed_then_edit", False),
    ("failed_then_repaired_and_passed", True),
])
async def test_owner_required_check_exposes_durable_verified_success_not_model_claim(
    tmp_path: Path, case: str, expect_success: bool,
) -> None:
    """A normal model final is not proof the requested command passed."""

    class RequiredCheckProvider(ProviderABC):
        model_name = "required-check-provider"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.calls = 0

        def count_tokens(self, text: str) -> int:
            return max(1, len(str(text)) // 4)

        async def stream_chat(self, messages, temperature=0, tools=None):
            self.calls += 1
            if self.calls == 1:
                assert any(
                    "Ash owner-required verification:" in str(message.get("content", ""))
                    and COMMAND in str(message.get("content", ""))
                    for message in messages
                )
            steps: list[tuple[str, str]] = [("edit", "")]
            if case == "unverified":
                steps = [("edit", "")]
            elif case == "failed_then_repaired_and_passed":
                steps = [("edit", ""), ("check", COMMAND), ("edit", ""), ("check", COMMAND)]
            elif case == "passed_then_edit":
                steps = [("edit", ""), ("check", COMMAND), ("edit", "")]
            else:
                steps.append(("check", "python3 wrong.py" if case == "wrong_command" else COMMAND))
            if self.calls > len(steps):
                yield StreamChunk(content="Everything passed", is_done=True)
                return
            step, command = steps[self.calls - 1]
            if step == "edit":
                tool = "replace_file_content"
                arguments: dict[str, object] = {
                    "file_path": "module.py",
                    "target_content": "before = 0" if self.calls == 1 else "after = 1",
                    "replacement_content": "after = 1" if self.calls == 1 else "after = 2",
                }
            else:
                tool = "run_command"
                arguments = {"command_line": command}
            yield StreamChunk(is_done=True, native_tool_calls=[{
                "id": f"required-{self.calls}", "name": tool, "arguments": arguments,
            }])

    provider = RequiredCheckProvider()
    loop, check = make_loop(tmp_path, provider=provider, passes=case != "failed_check")
    if case == "failed_then_repaired_and_passed":
        original_run = check.run

        async def fail_once(**kwargs):
            check.passes = check.executions > 0
            return await original_run(**kwargs)

        check.run = fail_once  # type: ignore[method-assign]
    stream = io.StringIO()
    loop.ui = HeadlessUI(output_format="json", stream=stream)
    code = await _bootstrap_and_headless(
        loop, SimpleNamespace(attachment_token_budget=1024),
        prompt="Fix and verify this module", session_id=None,
        ui=loop.ui, require_check_command=COMMAND,
    )
    assert code == (0 if expect_success else 1)
    response = json.loads(stream.getvalue())
    assert response["required_check_satisfied"] is expect_success
    assert response["type"] == "turn.completed"
    if not expect_success:
        assert "Required verification" in response["response"]
    # The model's own text is never accepted as verification evidence.
    assert "Everything passed" in response["response"] or expect_success
    if case == "passed":
        assert provider.calls == 3  # Final model completes the whole task.
    if case == "failed_then_repaired_and_passed":
        assert provider.calls == 5  # Earlier failure requires a real recheck.
    saved = loop.session_store.load_session(response["session_id"])
    assert all(
        "Ash owner-required verification:" not in message.content
        for message in saved.messages
    )



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


def test_cli_required_check_is_explicit_and_rejects_ambiguous_modes() -> None:
    import argparse
    from ash.cli import main
    from ash.cli_parser import configure_parser

    parser = argparse.ArgumentParser()
    configure_parser(parser, "test")
    assert parser.parse_args(["-p", "fix code"]).require_check_command is None
    assert parser.parse_args([
        "-p", "fix code", "--require-check-command", COMMAND
    ]).require_check_command == COMMAND
    for invalid in (
        ["--require-check-command", COMMAND],
        ["-p", "fix", "--session", "prior", "--require-check-command", COMMAND],
        ["-p", "fix", "--require-check-command", ""],
        ["-p", "fix", "--require-check-command", "a" * 4097],
        ["-p", "fix", "--require-check-command", COMMAND, "--json-schema", "/tmp/a.json"],
        ["-p", "fix", "--require-check-command", COMMAND, "--context-continuations", "1"],
        ["-p", "fix", "--require-check-command", COMMAND, "--report-after-command", COMMAND],
    ):
        with pytest.raises(SystemExit) as error:
            main(invalid)
        assert error.value.code == 2


@pytest.mark.parametrize("case,expected", [
    ("pass", True),
    ("wrong_command", False),
    ("wrong_cwd", False),
    ("wrong_project", False),
    ("failed", False),
    ("no_exit_status", False),
    ("denied", False),
    ("unapproved", False),
    ("unknown_effect", False),
    ("edit_after_check", False),
    ("shell_after_check", False),
    ("failed_then_passed", True),
    ("unknown_then_passed", False),
    ("false_success_flag", False),
    ("read_after_check", True),
    ("earlier_unrelated_command", True),
])
def test_required_check_durable_evidence_rejects_unsafe_or_stale_proof(
    tmp_path: Path, case: str, expected: bool,
) -> None:
    store = SessionStore(tmp_path / "required-evidence.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "only-turn", "Fix and verify")
    calls: list[tuple[str, dict[str, object], bool, int | None]] = [
        ("replace_file_content", {"file_path": "module.py"}, True, None),
        ("run_command", {"command_line": COMMAND}, True, 0),
    ]
    if case == "wrong_command":
        calls[-1] = ("run_command", {"command_line": "python3 other.py"}, True, 0)
    if case == "wrong_cwd":
        calls[-1] = ("run_command", {"command_line": COMMAND, "cwd": "elsewhere"}, True, 0)
    if case == "failed":
        calls[-1] = ("run_command", {"command_line": COMMAND}, False, 1)
    if case == "no_exit_status":
        calls[-1] = ("run_command", {"command_line": COMMAND}, True, None)
    if case == "unknown_effect":
        calls.append(("mcp__other__action", {}, True, None))
    if case == "edit_after_check":
        calls.append(("write_file", {"file_path": "module.py"}, True, None))
    if case == "shell_after_check":
        calls.append(("run_command", {"command_line": "echo later"}, True, 0))
    if case == "failed_then_passed":
        calls.insert(1, ("run_command", {"command_line": COMMAND}, False, 1))
    if case == "unknown_then_passed":
        calls.insert(1, ("run_command", {"command_line": COMMAND}, False, None))
    if case == "false_success_flag":
        calls.insert(1, ("run_command", {"command_line": COMMAND}, False, 0))
    if case == "read_after_check":
        calls.append(("git_diff", {}, True, None))
    if case == "earlier_unrelated_command":
        calls.insert(1, ("run_command", {"command_line": "true"}, True, 0))
    for index, (name, arguments, success, exit_code) in enumerate(calls):
        store.save_tool_call(
            session.session_id,
            ToolCallRecord(
                call_id=f"record-{index}", tool_name=name, arguments=arguments,
                approved=case != "unapproved" or index != 1,
                dispatched=case != "denied" or index != 1,
                executed=case != "denied" or index != 1,
                success=success, exit_code=exit_code,
                timestamp=datetime.now(timezone.utc),
            ),
            turn_id="only-turn",
        )
    store.complete_turn("only-turn")
    assert store.required_check_command_succeeded(
        session.session_id,
        expected_project_path=(
            tmp_path / "elsewhere" if case == "wrong_project" else tmp_path
        ),
        command_line=COMMAND,
    ) is expected


@pytest.mark.parametrize("exit_code,accepted", [(0, True), (1, False), (None, False)])
def test_optional_owner_checkpoint_requires_confirmed_zero_exit_status(
    tmp_path: Path, exit_code: int | None, accepted: bool,
) -> None:
    store = SessionStore(tmp_path / "legacy-checkpoint-exit.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "active", "Fix and test")
    for index, (name, args) in enumerate((
        ("replace_file_content", {"file_path": "module.py"}),
        ("run_command", {"command_line": COMMAND}),
    )):
        store.save_tool_call(
            session.session_id,
            ToolCallRecord(
                call_id=f"checkpoint-{index}", tool_name=name,
                arguments=args, approved=True, dispatched=True, executed=True,
                success=True, exit_code=exit_code if index == 1 else None,
                timestamp=datetime.now(timezone.utc),
            ),
            turn_id="active",
        )
    evidence = store.owner_command_checkpoint_evidence(
        session.session_id,
        "active",
        expected_project_path=tmp_path,
        command_line=COMMAND,
    )
    assert (evidence == 1) is accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("passes", [True, False])
async def test_required_check_real_workspace_command_is_run_once(
    tmp_path: Path, passes: bool,
) -> None:
    from ash.tools.command import RunCommandTool

    expected_source = "after = 1\n" if passes else "after = 2\n"
    (tmp_path / "qa_visible.py").write_text(
        "from pathlib import Path\n"
        f"assert Path('module.py').read_text() == {expected_source!r}\n",
        encoding="utf-8",
    )
    loop, _ = make_loop(tmp_path)
    loop.tools["run_command"] = RunCommandTool(loop.safety_guard)
    output = io.StringIO()
    loop.ui = HeadlessUI(output_format="stream-json", stream=output)
    loop.ui.set_event_enricher(loop._envelope_event)
    code = await _bootstrap_and_headless(
        loop,
        SimpleNamespace(attachment_token_budget=1024),
        prompt="Fix the source and verify with the owner command",
        session_id=None,
        ui=loop.ui,
        require_check_command=COMMAND,
    )
    assert code == (0 if passes else 1)
    records = [json.loads(line) for line in output.getvalue().splitlines() if line]
    completed = [item for item in records if item["type"] == "turn.completed"]
    assert len(completed) == 1
    assert completed[0]["required_check_satisfied"] is passes
    if not passes:
        assert "Required verification not established" in completed[0]["response"]
    assert loop.session_store.verify_audit_log(completed[0]["session_id"]) == []
    assert (tmp_path / "module.py").read_text() == "after = 1\n"


@pytest.mark.asyncio
async def test_required_check_does_not_auto_commit_before_evidence_is_final(
    tmp_path: Path,
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for key, value in (("user.name", "Ash Test"), ("user.email", "ash@example.invalid")):
        subprocess.run(["git", "config", key, value], cwd=tmp_path, check=True)
    loop, command = make_loop(tmp_path)
    subprocess.run(["git", "add", "--", "module.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_path, check=True)
    loop.auto_commit = True
    stream = io.StringIO()
    loop.ui = HeadlessUI(output_format="json", stream=stream)
    result = await _bootstrap_and_headless(
        loop,
        SimpleNamespace(attachment_token_budget=1024),
        prompt="Make the change and run the operator's required test",
        session_id=None, ui=loop.ui, require_check_command=COMMAND,
    )
    assert result == 0
    assert json.loads(stream.getvalue())["required_check_satisfied"] is True
    assert command.executions == 1
    commits = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=tmp_path,
        check=True, capture_output=True, text=True,
    )
    assert commits.stdout.strip() == "1"
    diff = subprocess.run(
        ["git", "diff", "--", "module.py"], cwd=tmp_path,
        check=True, capture_output=True, text=True,
    )
    assert "+after = 1" in diff.stdout


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
