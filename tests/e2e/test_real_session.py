"""End-to-end test for the Sprint 15 final-verification phase.

Drives a full multi-turn Ash session through the real AshLoop, exercising
the tool surface (file writes, command runs), the V9 git auto-commit
flow, and the persisted session restore. The provider is a
deterministic fake so the test runs offline; the filesystem, sandbox
manager, and git layer are all real.
"""

from __future__ import annotations

import asyncio
import io
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, AsyncGenerator


from core.loop import AshLoop
from core.session import SessionStore
from providers.base import StreamChunk
from safety.guard import SafetyGuard
from sandbox import SANDBOX_TIER_SCOPED, SandboxManager
from tools.command import RunCommandTool
from tools.filesystem import ReadFileTool, ReplaceFileContentTool, WriteFileTool
from ui.terminal import TerminalUI


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
        self, messages: list[dict[str, Any]], temperature: float = 0.0
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
    return AshLoop(
        session_store=store,
        provider=provider,
        safety_guard=guard,
        ui=ui if ui is not None else _make_ui(),
        project_root=workspace,
        tools=tools,
        auto_commit=auto_commit,
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
    assert write_record.error == "Denied by user"


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
