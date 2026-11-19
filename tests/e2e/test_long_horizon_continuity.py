"""Offline coding continuity through the actual session CLI and tool runtime."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ash.config import AshConfig
from ash.context.history import ContextBudgetExceededError
from ash.core.loop import AshLoop
from ash.core.session import SessionStore, ToolCallRecord
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.safety.guard import SafetyGuard
from ash.tools.command import RunCommandTool
from ash.tools.filesystem import ReadFileTool, ReplaceFileContentTool
from ash.ui.headless import HeadlessUI


def call(call_id, name, **arguments):
    return {"id": call_id, "name": name, "arguments": arguments}


class ScriptedProvider(ProviderABC):
    model_name = "scripted"
    _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

    def __init__(self, steps):
        self.steps = iter(steps)
        self.requests = []

    def count_tokens(self, text):
        return max(1, len(text) // 4)

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.requests.append(messages)
        yield next(self.steps)(messages)


def cli(workspace, db_dir, *args):
    env = {
        "PATH": os.defpath,
        "HOME": str(workspace.parent / "home"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "NO_COLOR": "1",
    }
    result = subprocess.run(
        [sys.executable, "-m", "ash", "--db-directory", str(db_dir), *args, "--json"],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert not result.stderr, result.stderr
    return result.returncode, json.loads(result.stdout)


def coding_loop(config, store, provider, stream):
    guard = SafetyGuard(config.workspace_root)

    async def approve(_name, _arguments):
        return True

    return AshLoop(
        store,
        provider,
        guard,
        HeadlessUI(output_format="stream-json", stream=stream),
        config.workspace_root,
        config=config,
        system_prompt="Work on the checkout.",
        tools={
            "read_file": ReadFileTool(guard),
            "replace_file_content": ReplaceFileContentTool(guard),
            "run_command": RunCommandTool(guard),
        },
        on_tool_approval=approve,
    )


@pytest.fixture
def offline_workspace(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (tmp_path / "home").mkdir()
    config = AshConfig(
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        model="custom/scripted",
        max_context_tokens=8_000,
        max_completion_tokens=256,
        max_turn_total_tokens=0,
        memory_backend="off",
        repo_map_enabled=False,
    )
    return config, SessionStore(config.db_directory / "sessions.db")


@pytest.mark.asyncio
async def test_cli_context_restart_continues_verified_multi_file_work(
    offline_workspace,
):
    config, store = offline_workspace
    workspace = config.workspace_root
    (workspace / "pricing.py").write_text("subtotal = 195\n")
    (workspace / "taxes.py").write_text("total = 101\n")
    (workspace / "test_checkout.py").write_text(
        "from pricing import subtotal\nfrom taxes import total\n\n"
        "def test_pricing():\n    assert subtotal == 90\n\n"
        "def test_taxes():\n    assert total == 99\n"
    )
    check = f"{shlex.quote(sys.executable)} -m pytest -q --tb=short test_checkout.py"
    state = [
        {
            "type": "reasoning",
            "id": "rs_continuity",
            "summary": [],
            "encrypted_content": "r" * 34_000,
        }
    ]

    def read_pair(_messages):
        return StreamChunk(
            is_done=True,
            native_tool_calls=[
                call("read-pricing", "read_file", file_path="pricing.py"),
                call("read-taxes", "read_file", file_path="taxes.py"),
            ],
        )

    def initial_check(messages):
        history = repr(messages)
        assert "subtotal = 195" in history and "total = 101" in history
        return StreamChunk(
            is_done=True,
            native_tool_calls=[
                call(
                    "initial-check",
                    "run_command",
                    command_line=check,
                    timeout_seconds=10,
                ),
            ],
        )

    def first_edit(messages):
        assert '"pytest_failed": 2' in repr(messages)
        return StreamChunk(
            is_done=True,
            provider_state=state,
            native_tool_calls=[
                call(
                    "edit-pricing",
                    "replace_file_content",
                    file_path="pricing.py",
                    target_content="subtotal = 195",
                    replacement_content="subtotal = 90",
                ),
            ],
        )

    original_provider = ScriptedProvider([read_pair, initial_check, first_edit])
    stream = io.StringIO()
    first = coding_loop(config, store, original_provider, stream)
    try:
        parent = await first.start_session()
        with pytest.raises(ContextBudgetExceededError) as failure:
            await first.run_turn("Repair pricing and taxes and verify both")
        assert failure.value.provider_replay_state_preserved
        assert failure.value.required_tokens > failure.value.limit
        assert len(original_provider.requests) == 3
    finally:
        await first.aclose()

    assert (workspace / "pricing.py").read_text() == "subtotal = 90\n"
    assert (workspace / "taxes.py").read_text() == "total = 101\n"
    original = store.load_session(parent.session_id)
    assert any(m.metadata.get("provider_state") == state for m in original.messages)
    assert [
        c.exit_code for c in original.tool_calls if c.tool_name == "run_command"
    ] == [1]
    assert not any(
        json.loads(line)["type"] == "turn.completed"
        for line in stream.getvalue().splitlines()
    )

    code, restarted = cli(
        workspace,
        config.db_directory,
        "sessions",
        "restart-context",
        "--session",
        parent.session_id,
        "--handoff",
        "Pricing is partially repaired. Inspect both files, fix taxes and run test_checkout.py.",
    )
    assert code == 0, restarted
    child_id = restarted["session_id"]
    assert restarted["parent_session_id"] == parent.session_id
    code, activity = cli(
        workspace,
        config.db_directory,
        "sessions",
        "activity",
        "--session",
        parent.session_id,
    )
    assert code == 0
    assert activity["last_incomplete_turn_reason"] == "opaque_context"
    child = store.load_session(child_id)
    assert child.parent_session_id == parent.session_id
    assert len(child.messages) == 1 and not child.tool_calls
    assert not child.messages[0].metadata.get("provider_state")

    def inspect_restart(messages):
        assert "Pricing is partially repaired" in repr(messages)
        assert "rs_continuity" not in repr(messages)
        assert "edit-pricing" not in repr(messages)
        return read_pair(messages)

    def second_edit(messages):
        history = repr(messages)
        assert "subtotal = 90" in history and "total = 101" in history
        return StreamChunk(
            is_done=True,
            native_tool_calls=[
                call(
                    "edit-taxes",
                    "replace_file_content",
                    file_path="taxes.py",
                    target_content="total = 101",
                    replacement_content="total = 99",
                ),
            ],
        )

    def final_check(messages):
        assert "edit-taxes" in repr(messages)
        return StreamChunk(
            is_done=True,
            native_tool_calls=[
                call(
                    "final-check", "run_command", command_line=check, timeout_seconds=10
                ),
            ],
        )

    def finish(messages):
        assert '"pytest_passed": 2' in repr(messages)
        return StreamChunk(is_done=True, content="Both checkout tests passed.")

    continuing_provider = ScriptedProvider(
        [inspect_restart, second_edit, final_check, finish]
    )
    resumed = coding_loop(
        config, SessionStore(store.db_path), continuing_provider, io.StringIO()
    )
    try:
        await resumed.start_session(child_id)
        assert (
            await resumed.run_turn("Continue the handoff")
            == "Both checkout tests passed."
        )
    finally:
        await resumed.aclose()

    assert (workspace / "pricing.py").read_text() == "subtotal = 90\n"
    assert (workspace / "taxes.py").read_text() == "total = 99\n"
    assert len(continuing_provider.requests) == 4
    original_after = store.load_session(parent.session_id)
    assert original_after.messages == original.messages
    assert original_after.tool_calls == original.tool_calls
    child_calls = store.load_session(child_id).tool_calls
    assert [c.exit_code for c in child_calls if c.tool_name == "run_command"] == [0]
    assert [
        c.call_id for c in child_calls if c.tool_name == "replace_file_content"
    ] == ["edit-taxes"]
    assert store.verify_audit_log(parent.session_id) == []
    assert store.verify_audit_log(child_id) == []


@pytest.mark.asyncio
async def test_cli_refuses_context_restart_after_ambiguous_command(offline_workspace):
    config, store = offline_workspace
    workspace = config.workspace_root
    marker = workspace / "effect.txt"
    script = (
        "import time\n"
        "with open('effect.txt', 'a') as f: f.write('ran once\\n')\n"
        "time.sleep(30)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    provider = ScriptedProvider(
        [
            lambda _messages: StreamChunk(
                is_done=True,
                native_tool_calls=[
                    call("ambiguous-command", "run_command", command_line=command),
                ],
            ),
        ]
    )
    loop = coding_loop(config, store, provider, io.StringIO())
    try:
        parent = await loop.start_session()
        turn = asyncio.create_task(loop.run_turn("Run the command once"))
        args = (
            "sessions",
            "restart-context",
            "--session",
            parent.session_id,
            "--handoff",
            "Inspect whether the previous operation happened.",
        )
        try:
            async with asyncio.timeout(10):
                while not marker.exists():
                    await asyncio.sleep(0.02)
            code, busy = cli(workspace, config.db_directory, *args)
            assert code != 0
            assert "active in another Ash process" in busy["error"]["message"]
        finally:
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await turn
    finally:
        await loop.aclose()

    assert len(provider.requests) == 1
    assert marker.read_text() == "ran once\n"
    code, refused = cli(workspace, config.db_directory, *args)
    assert code != 0
    assert refused["error"]["category"] == "session"
    assert "sessions recovery" in refused["error"]["remedy"]
    assert len(store.session_tree(parent.session_id)) == 1
    assert [c.call_id for c in store.load_session(parent.session_id).tool_calls] == [
        "ambiguous-command"
    ]
    assert marker.read_text() == "ran once\n"
    reports = store.interrupted_recovery_reports(parent.session_id)
    assert reports[0]["unknown_calls"] == [
        {"call_id": "ambiguous-command", "tool": "run_command"}
    ]
    code, activity = cli(
        workspace,
        config.db_directory,
        "sessions",
        "activity",
        "--session",
        parent.session_id,
    )
    assert code == 0
    assert activity["last_incomplete_turn_reason"] == "cancelled"


@pytest.mark.parametrize(
    "status, known_outcome",
    [
        ("working", False),
        ("completed", False),
        ("completed", True),
    ],
)
def test_cli_context_restart_keeps_durable_mcp_tasks_for_runtime_recovery(
    offline_workspace,
    status,
    known_outcome,
):
    config, store = offline_workspace
    source = store.create_session(str(config.workspace_root))
    store.start_turn(source.session_id, "remote-work", "Start the remote operation")
    store.save_mcp_task(
        task_id="fixture-task",
        session_id=source.session_id,
        turn_id="remote-work",
        call_id="fixture-call",
        server_name="fixture",
        remote_tool_name="submit",
        contract_fingerprint="contract",
        server_fingerprint="server",
        protocol_version="2025-11-25",
        task={"status": status},
        answered_inputs={},
    )
    if known_outcome:
        store.save_tool_call(
            source.session_id,
            ToolCallRecord(
                call_id="fixture-call",
                tool_name="mcp__fixture__submit",
                approved=True,
                dispatched=True,
                executed=True,
                success=True,
                result="completed",
                arguments={},
                timestamp=datetime.now(timezone.utc),
            ),
            turn_id="remote-work",
        )
        store.complete_turn("remote-work")
    code, reply = cli(
        config.workspace_root,
        config.db_directory,
        "sessions",
        "restart-context",
        "--session",
        source.session_id,
        "--handoff",
        "Inspect the remote operation.",
    )
    assert [r["task_id"] for r in store.list_mcp_tasks(source.session_id)] == [
        "fixture-task"
    ]
    if known_outcome:
        assert code == 0, reply
        assert not store.load_session(reply["session_id"]).tool_calls
    else:
        assert code != 0
        assert "persisted MCP tasks" in reply["error"]["message"]
        assert [r["turn_id"] for r in store.started_turns(source.session_id)] == [
            "remote-work"
        ]
        assert len(store.session_tree(source.session_id)) == 1
