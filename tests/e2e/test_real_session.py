"""End-to-end test for the Sprint 15 final-verification phase.

Drives a full multi-turn Ash session through the real AshLoop, exercising
the tool surface (file writes, command runs), the V9 git auto-commit
flow, and the persisted session restore. The provider is a
deterministic fake so the test runs offline; the filesystem, sandbox
manager, and git layer are all real.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, AsyncGenerator, Callable

import pytest

from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.providers.base import StreamChunk
from ash.safety.guard import SafetyGuard
from ash.sandbox import SANDBOX_TIER_SCOPED, SandboxManager
from ash.tools.command import RunCommandTool
from ash.tools.filesystem import ReadFileTool, ReplaceFileContentTool, WriteFileTool
from ash.ui.terminal import TerminalUI


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class SessionProvider:
    """A provider that scripts a multi-step session, turn by turn."""

    def __init__(self, scripts: list[list[str]]) -> None:
        self._scripts = [list(s) for s in scripts]
        self._call_count = 0
        self.received_messages: list[list[dict[str, Any]]] = []

    @property
    def model_name(self) -> str:
        return "e2e-fake"

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def stream_chat(
        self, messages: list[dict[str, Any]], temperature: float = 0.0, tools=None
    ) -> AsyncGenerator[StreamChunk, None]:
        self.received_messages.append(list(messages))
        if self._call_count >= len(self._scripts):
            yield StreamChunk(content="<response>done</response>", is_done=True)
            return
        script = self._scripts[self._call_count]
        self._call_count += 1
        for fragment in script:
            yield StreamChunk(content=fragment)
        yield StreamChunk(content="", is_done=True)


def _silent_console() -> Any:
    from rich.console import Console

    return Console(file=io.StringIO(), force_terminal=False, width=120)


def _make_ui(approval_yes: bool = True) -> TerminalUI:
    if approval_yes:
        return TerminalUI(safety_tier="auto_approve", console=_silent_console())
    return TerminalUI(safety_tier="dry_run", console=_silent_console())


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _git_init(workspace: Path) -> None:
    """Initialise a real git repo with a user identity and one seed commit."""

    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "ash@test"], cwd=workspace, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Ash Test"], cwd=workspace, check=True
    )
    (workspace / "README.md").write_text("# e2e\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=workspace, check=True)


def _git_log(workspace: Path) -> list[str]:
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=workspace, capture_output=True, text=True
    )
    return log.stdout.strip().splitlines()


def _latest_session_id(db_path: Path) -> str:
    """Return the session_id of the most recently created session."""

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT session_id FROM sessions ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise AssertionError(f"no session row in {db_path}")
    return row["session_id"]


def _make_loop(
    workspace: Path,
    db_path: Path,
    provider: SessionProvider,
    *,
    auto_commit: bool = False,
    ui: TerminalUI | None = None,
) -> AshLoop:
    guard = SafetyGuard(project_root=workspace)
    store = SessionStore(db_path)
    manager = SandboxManager(
        workspace_root=workspace, preferred_tier=SANDBOX_TIER_SCOPED
    )
    tools: dict[str, Any] = {
        ReadFileTool(guard).name: ReadFileTool(guard),
        WriteFileTool(guard).name: WriteFileTool(guard),
        ReplaceFileContentTool(guard).name: ReplaceFileContentTool(guard),
        RunCommandTool(guard, sandbox_manager=manager).name: RunCommandTool(
            guard, sandbox_manager=manager
        ),
    }
    active_ui = ui if ui is not None else _make_ui()
    return AshLoop(
        session_store=store,
        provider=provider,
        safety_guard=guard,
        ui=active_ui,
        project_root=workspace,
        tools=tools,
        auto_commit=auto_commit,
        safety_tier=active_ui.safety_tier,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_e2e_session_writes_file_and_runs_command(tmp_path: Path) -> None:
    """Multi-turn session: writes a Python module, runs pytest, asserts
    both effects on disk and the SQLite trail."""

    workspace = tmp_path / "project"
    workspace.mkdir()
    _git_init(workspace)
    db_path = tmp_path / "s.db"

    provider = SessionProvider(
        scripts=[
            # Call 1: model emits the write_file tool call.
            [
                "<thought>write a module</thought>",
                '<call_tool name="write_file">',
                '<arg name="file_path">module.py</arg>',
                '<arg name="content">VALUE = 42\n</arg>',
                '<arg name="overwrite">false</arg>',
                "</call_tool>",
            ],
            # Call 2: model emits the run_command tool call.
            [
                "<thought>run it</thought>",
                '<call_tool name="run_command">',
                '<arg name="command_line">python3 -c "import module; print(module.VALUE)"</arg>',
                '<arg name="cwd">.</arg>',
                "</call_tool>",
            ],
            # Call 3: terminal response.
            [
                "<response>all done</response>",
            ],
        ]
    )

    async def driver() -> str:
        loop = _make_loop(workspace, db_path, provider)
        await loop.start_session()
        return await loop.run_turn("start the session")

    response = asyncio.run(driver())
    assert "all done" in response

    # File was written.
    written = (workspace / "module.py").read_text(encoding="utf-8")
    assert "VALUE = 42" in written

    # Session was persisted.
    session = SessionStore(db_path).load_session(_latest_session_id(db_path))
    assert any(
        m.role == "user" and "start the session" in m.content for m in session.messages
    )
    assert any(m.role == "assistant" for m in session.messages)
    tool_records = [
        r for r in session.tool_calls if r.tool_name in {"write_file", "run_command"}
    ]
    assert len(tool_records) >= 2
    for record in tool_records:
        assert record.executed is True
        assert record.approved is True
        assert record.error is None


def test_e2e_session_auto_commits_each_turn(tmp_path: Path) -> None:
    """When auto_commit is on, every turn's file changes land in git."""

    workspace = tmp_path / "project"
    workspace.mkdir()
    _git_init(workspace)
    (workspace / "unrelated.txt").write_text("leave me alone\n")
    db_path = tmp_path / "s.db"

    provider = SessionProvider(
        scripts=[
            # Call 1: write turn1
            [
                '<call_tool name="write_file">',
                '<arg name="file_path">turn1.txt</arg>',
                '<arg name="content">turn-one\n</arg>',
                '<arg name="overwrite">false</arg>',
                "</call_tool>",
            ],
            # Call 2: final response of turn 1
            ["<response>wrote turn 1</response>"],
            # Call 3: write turn2
            [
                '<call_tool name="write_file">',
                '<arg name="file_path">turn2.txt</arg>',
                '<arg name="content">turn-two\n</arg>',
                '<arg name="overwrite">false</arg>',
                "</call_tool>",
            ],
            # Call 4: final response of turn 2
            ["<response>wrote turn 2</response>"],
        ]
    )

    async def driver() -> list[str]:
        loop = _make_loop(workspace, db_path, provider, auto_commit=True)
        await loop.start_session()
        await loop.run_turn("turn 1")
        await loop.run_turn("turn 2")
        return _git_log(workspace)

    commits = asyncio.run(driver())
    # Seed + two auto-commits.
    assert len(commits) == 3
    assert sum("turn complete" in c for c in commits) == 2
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "?? unrelated.txt" in status.stdout


def test_e2e_session_respects_safety_tier_dry_run(tmp_path: Path) -> None:
    """A dry_run safety tier denies every tool call — the file is NOT
    written, the tool record reflects the denial, and the loop still
    emits a final text response."""

    workspace = tmp_path / "project"
    workspace.mkdir()
    _git_init(workspace)
    db_path = tmp_path / "s.db"

    provider = SessionProvider(
        scripts=[
            # Call 1: tool call (will be denied)
            [
                '<call_tool name="write_file">',
                '<arg name="file_path">should_not_exist.txt</arg>',
                '<arg name="content">nope\n</arg>',
                '<arg name="overwrite">false</arg>',
                "</call_tool>",
            ],
            # Call 2: model emits the final response acknowledging denial
            ["<response>I tried but was denied</response>"],
        ]
    )

    async def driver() -> str:
        loop = _make_loop(workspace, db_path, provider, ui=_make_ui(approval_yes=False))
        await loop.start_session()
        return await loop.run_turn("please write the file")

    response = asyncio.run(driver())
    assert "denied" in response.lower() or "tried" in response.lower()
    assert not (workspace / "should_not_exist.txt").exists()

    session = SessionStore(db_path).load_session(_latest_session_id(db_path))
    write_record = next(
        (r for r in session.tool_calls if r.tool_name == "write_file"), None
    )
    assert write_record is not None
    assert write_record.approved is False
    assert write_record.executed is False
    assert write_record.error == "dry-run mode forbids side effects"


def test_e2e_session_persists_through_db_restore(tmp_path: Path) -> None:
    """A second loop instance loaded with the original session_id sees
    the messages and tool records persisted by the first."""

    workspace = tmp_path / "project"
    workspace.mkdir()
    _git_init(workspace)
    db_path = tmp_path / "s.db"

    provider1 = SessionProvider(
        scripts=[
            # Call 1: tool call
            [
                '<call_tool name="write_file">',
                '<arg name="file_path">persisted.txt</arg>',
                '<arg name="content">first loop\n</arg>',
                '<arg name="overwrite">false</arg>',
                "</call_tool>",
            ],
            # Call 2: final response
            ["<response>first loop done</response>"],
        ]
    )

    async def first_loop() -> str:
        loop = _make_loop(workspace, db_path, provider1)
        await loop.start_session()
        session_id = loop.current_session.session_id
        await loop.run_turn("do it")
        return session_id

    session_id = asyncio.run(first_loop())

    provider2 = SessionProvider(scripts=[["<response>second loop</response>"]])

    async def second_loop() -> str:
        loop = _make_loop(workspace, db_path, provider2)
        await loop.start_session(session_id)
        return await loop.run_turn("do it again")

    response = asyncio.run(second_loop())
    assert "second loop" in response

    # The restored session has the original 4 messages from loop 1
    # (user, assistant-empty, tool, assistant-final) plus the 2 from
    # loop 2 (user, assistant) = 6 total.
    session = SessionStore(db_path).load_session(session_id)
    assert len(session.messages) == 6
    assert session.messages[0].role == "user"
    assert session.messages[0].content == "do it"
    assert session.messages[-1].role == "assistant"
    assert "second loop" in session.messages[-1].content


def test_e2e_explorer_cancelled_turn_preserves_completed_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, str], None],
) -> None:
    """Cancel after a real write and inspect durable state through fresh CLI processes."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    scratch_tmp = tmp_path / "tmp"
    scratch_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch_tmp))

    workspace = tmp_path / "project"
    workspace.mkdir()
    db_directory = tmp_path / "db"
    db_directory.mkdir()
    db_path = db_directory / "sessions.db"

    source_root = Path(__file__).resolve().parents[2]
    sanitized_env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(scratch_tmp),
        "PATH": os.defpath,
        "PYTHONPATH": str(source_root / "src"),
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=workspace,
            env=sanitized_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "ash@test")
    git("config", "user.name", "Ash Test")
    (workspace / "README.md").write_text("# e2e\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-q", "-m", "seed")

    class CancelAfterWriteProvider(SessionProvider):
        def __init__(self) -> None:
            super().__init__(scripts=[])
            self.second_request_started = asyncio.Event()

        async def stream_chat(
            self,
            messages: list[dict[str, Any]],
            temperature: float = 0.0,
            tools=None,
        ) -> AsyncGenerator[StreamChunk, None]:
            self.received_messages.append(list(messages))
            self._call_count += 1
            if self._call_count == 1:
                for fragment in (
                    '<call_tool name="write_file">',
                    '<arg name="file_path">completed.txt</arg>',
                    '<arg name="content">completed-before-cancel\n</arg>',
                    '<arg name="overwrite">false</arg>',
                    "</call_tool>",
                ):
                    yield StreamChunk(content=fragment)
                yield StreamChunk(content="", is_done=True)
                return
            if self._call_count == 2:
                self.second_request_started.set()
                await asyncio.Event().wait()
                return
            raise AssertionError("the cancelled turn unexpectedly requested another response")

        async def aclose(self) -> None:
            return None

    provider = CancelAfterWriteProvider()

    async def cancel_turn() -> str:
        loop = _make_loop(workspace, db_path, provider)
        from ash.core.checkpoints import FileCheckpointMiddleware
        from ash.core.secret_middleware import SecretRedactionMiddleware

        def checkpoint_context() -> tuple[str, str, str] | None:
            if loop.current_session is None or loop.turn_context is None:
                return None
            return (
                loop.current_session.session_id,
                loop.turn_context.turn_id,
                str(loop.turn_context.get("tool_call_id", "")),
            )

        loop.tool_middlewares.extend(
            [
                FileCheckpointMiddleware(
                    loop.session_store, loop.safety_guard, checkpoint_context
                ),
                SecretRedactionMiddleware(),
            ]
        )
        await asyncio.wait_for(loop.start_session(), timeout=5)
        assert loop.current_session is not None
        session_id = loop.current_session.session_id
        turn = asyncio.create_task(loop.run_turn("write the fixture file"))
        try:
            await asyncio.wait_for(provider.second_request_started.wait(), timeout=5)
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(turn, timeout=5)
        finally:
            if not turn.done():
                turn.cancel()
            await asyncio.wait_for(
                asyncio.gather(turn, return_exceptions=True), timeout=5
            )
            await asyncio.wait_for(loop.aclose(), timeout=5)
        return session_id

    session_id = asyncio.run(cancel_turn())

    written_bytes = b"completed-before-cancel\n"
    written_path = workspace / "completed.txt"
    assert written_path.read_bytes() == written_bytes
    digest = hashlib.sha256(written_bytes).hexdigest()

    session = SessionStore(db_path).load_session(session_id)
    write_calls = [call for call in session.tool_calls if call.tool_name == "write_file"]
    assert len(write_calls) == 1
    write_call = write_calls[0]
    assert write_call.approved is True
    assert write_call.executed is True
    assert write_call.error is None
    assert provider._call_count == 2

    store = SessionStore(db_path)
    checkpoints = store.latest_file_checkpoints(session_id)
    assert len(checkpoints) == 1
    assert checkpoints[0]["call_id"] == write_call.call_id
    assert checkpoints[0]["existed"] == 0
    assert checkpoints[0]["after_sha256"] == digest
    assert store.started_turns(session_id) == []
    reports = store.interrupted_recovery_reports(session_id)
    assert len(reports) == 1
    report = reports[0]
    assert report["status"] == "interrupted"
    assert report["compensated_calls"] == []
    assert report["unknown_calls"] == []
    assert report["unresolved_files"] == []
    assert report["recovered_calls"] == []
    assert store.verify_audit_log(session_id) == []
    assert any(
        audit.details.get("call_id") == write_call.call_id
        and audit.result == "SUCCESS"
        for audit in store.list_audit_logs(session_id)
    )

    event_types = [
        item.event["type"] for item in store.list_runtime_events(session_id, limit=100)
    ]
    completed_index = event_types.index("tool.completed")
    request_cancelled_index = event_types.index("model.request.cancelled")
    turn_cancelled_index = event_types.index("turn.cancelled")
    assert completed_index < request_cancelled_index < turn_cancelled_index

    def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "ash",
                "--db-directory",
                str(db_directory),
                *arguments,
            ],
            cwd=workspace,
            env=sanitized_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    recovery_result = run_cli("sessions", "recovery", "--session", session_id, "--json")
    assert recovery_result.returncode == 0, recovery_result.stderr
    recovery_payload = json.loads(recovery_result.stdout)
    assert len(recovery_payload["reports"]) == 1
    cli_report = recovery_payload["reports"][0]
    assert cli_report["status"] == "interrupted"
    assert cli_report["turn_id"] == report["turn_id"]
    assert cli_report["compensated_calls"] == []
    assert cli_report["unknown_calls"] == []
    assert cli_report["unresolved_files"] == []

    audit_result = run_cli("audit", "verify", "--session", session_id, "--json")
    assert audit_result.returncode == 0, audit_result.stderr
    audit_payload = json.loads(audit_result.stdout)
    assert audit_payload["ok"] is True
    assert audit_payload["errors"] == []

    git_status = git("status", "--short", "--untracked-files=all").splitlines()
    assert git_status == ["?? completed.txt"]
    commits = git("log", "--oneline").splitlines()
    assert len(commits) == 1
    assert commits[0].endswith(" seed")

    record_property(
        "ash.explorer.evidence",
        json.dumps(
            {
                "schema_version": 1,
                "scenario": "cancel_after_completed_write",
                "session_id": session_id,
                "file_sha256": digest,
                "tool_call": {
                    "approved": write_call.approved,
                    "executed": write_call.executed,
                    "succeeded": write_call.error is None,
                },
                "recovery_status": report["status"],
                "recovery_counts": {
                    "compensated": len(report["compensated_calls"]),
                    "unknown": len(report["unknown_calls"]),
                    "unresolved": len(report["unresolved_files"]),
                    "recovered": len(report["recovered_calls"]),
                },
                "runtime_event_order": [
                    event_types[completed_index],
                    event_types[request_cancelled_index],
                    event_types[turn_cancelled_index],
                ],
                "audit_valid": audit_payload["ok"],
                "git_status": git_status,
                "commit_count": len(commits),
                "cli_exit_codes": {
                    "recovery": recovery_result.returncode,
                    "audit_verify": audit_result.returncode,
                },
                "reproduction_command": (
                    'evidence_dir="$(mktemp -d "${TMPDIR:-/tmp}/ash-explorer.XXXXXX")" && '
                    "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q "
                    "--timeout=45 --timeout-method=thread "
                    "-p no:cacheprovider -o junit_family=xunit1 "
                    "tests/e2e/test_real_session.py::"
                    "test_e2e_explorer_cancelled_turn_preserves_completed_write "
                    '--junitxml="$evidence_dir/results.xml"'
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
