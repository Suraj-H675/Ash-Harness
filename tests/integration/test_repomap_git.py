"""Integration tests for Sprint 9: PPR repo map and git auto-commits."""

from __future__ import annotations

import asyncio
import io
import subprocess
from pathlib import Path

import pytest

from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.providers.base import StreamChunk
from ash.repo.repomap import RepoMap
from ash.safety.guard import SafetyGuard
from ash.tools.git import AutoCommitTool
from ash.tools.filesystem import ReadFileTool, WriteFileTool
from ash.tools.symbols import FindReferencesTool, FindSymbolTool
from ash.ui.terminal import TerminalUI


# --- fakes (mirroring tests/integration/test_loop.py patterns) -------------


class FakeProvider:
    def __init__(self, scripts: list[list[str]]) -> None:
        self._scripts = [list(s) for s in scripts]
        self._call_count = 0
        self.received_messages: list[list[dict]] = []

    @property
    def model_name(self) -> str:
        return "fake-model"

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


def _make_ui() -> TerminalUI:
    return TerminalUI(safety_tier="auto_approve", console=_silent_console())


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def git_workspace(workspace: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "ash@test"], cwd=workspace, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Ash Test"], cwd=workspace, check=True
    )
    return workspace


@pytest.fixture
def safety_guard(workspace: Path) -> SafetyGuard:
    return SafetyGuard(project_root=workspace)


@pytest.fixture
def session_store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.db")


# --- tests ------------------------------------------------------------------


def test_repo_map_injected_into_system_prompt(
    workspace: Path, safety_guard: SafetyGuard, session_store: SessionStore
) -> None:
    (workspace / "alpha.py").write_text("import beta\ndef a(): pass\n")
    (workspace / "beta.py").write_text("def b(): pass\n")

    repo_map = RepoMap(workspace)
    provider = FakeProvider(scripts=[["<response>ok</response>"]])
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=workspace,
        repo_map=repo_map,
    )

    asyncio.run(loop.run_turn("hi"))

    system_message = provider.received_messages[0][0]
    assert "## Repository Map" in system_message["content"]
    assert "alpha.py" in system_message["content"]


def test_multilanguage_repo_map_injected_into_system_prompt(
    workspace: Path, safety_guard: SafetyGuard, session_store: SessionStore
) -> None:
    (workspace / "client.ts").write_text(
        'import { request } from "./transport";\n'
        "export interface Client {}\n"
        "export function connect() {}\n"
    )
    (workspace / "transport.ts").write_text("export function request() {}\n")
    provider = FakeProvider(scripts=[["<response>ok</response>"]])
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=workspace,
        repo_map=RepoMap(workspace),
    )

    asyncio.run(loop.run_turn("inspect the TypeScript client"))

    system_content = provider.received_messages[0][0]["content"]
    assert "client.ts" in system_content
    assert "Client" in system_content
    assert "connect" in system_content
    assert "transport.ts" in system_content


def test_structural_navigation_tools_execute_in_agent_loop(
    workspace: Path, safety_guard: SafetyGuard, session_store: SessionStore
) -> None:
    (workspace / "service.py").write_text(
        "class Service:\n    pass\n\ndef build():\n    return Service()\n"
    )
    repo_map = RepoMap(workspace)
    symbol_tool = FindSymbolTool(safety_guard, repo_map)
    references_tool = FindReferencesTool(safety_guard, repo_map)
    provider = FakeProvider(
        scripts=[
            [
                '<call_tool name="find_symbol">'
                '<arg name="query">Service</arg>'
                "</call_tool>"
                '<call_tool name="find_references">'
                '<arg name="query">Service</arg>'
                "</call_tool>"
            ],
            ["<response>navigation complete</response>"],
        ]
    )
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=workspace,
        repo_map=repo_map,
        tools={
            symbol_tool.name: symbol_tool,
            references_tool.name: references_tool,
        },
    )

    response = asyncio.run(loop.run_turn("locate Service"))

    assert response == "navigation complete"
    loaded = session_store.load_session(loop.current_session.session_id)
    tool_messages = [
        message.content for message in loaded.messages if message.role == "tool"
    ]
    assert len(tool_messages) == 2
    assert "service.py:1: class Service [python]" in tool_messages[0]
    assert "service.py:5:12: Service [python]" in tool_messages[1]


def test_repo_map_prioritizes_successfully_read_file(
    workspace: Path, safety_guard: SafetyGuard, session_store: SessionStore
) -> None:
    for name in ["a", "b", "c", "d", "e", "z_active"]:
        (workspace / f"{name}.py").write_text(f"def {name}(): pass\n")
    provider = FakeProvider(
        scripts=[
            [
                '<call_tool name="read_file"><arg name="file_path">'
                "z_active.py</arg></call_tool>"
            ],
            ["<response>done</response>"],
        ]
    )
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=workspace,
        repo_map=RepoMap(workspace),
        tools={"read_file": ReadFileTool(safety_guard)},
    )

    asyncio.run(loop.run_turn("read the active file"))

    first_system = provider.received_messages[0][0]["content"]
    second_system = provider.received_messages[1][0]["content"]
    assert "z_active.py" not in first_system
    assert "z_active.py" in second_system


def test_repo_map_refreshes_after_successful_write(
    workspace: Path, safety_guard: SafetyGuard, session_store: SessionStore
) -> None:
    for name in ["a", "b", "c", "d", "e"]:
        (workspace / f"{name}.py").write_text(f"def {name}(): pass\n")
    provider = FakeProvider(
        scripts=[
            [
                '<call_tool name="write_file">'
                '<arg name="file_path">z_new.py</arg>'
                '<arg name="content">def fresh(): pass\n</arg>'
                "</call_tool>"
            ],
            ["<response>done</response>"],
        ]
    )
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=workspace,
        repo_map=RepoMap(workspace),
        tools={"write_file": WriteFileTool(safety_guard)},
    )

    asyncio.run(loop.run_turn("create a new file"))

    assert (workspace / "z_new.py").exists()
    first_system = provider.received_messages[0][0]["content"]
    second_system = provider.received_messages[1][0]["content"]
    assert "z_new.py" not in first_system
    assert "z_new.py" in second_system
    assert "fresh" in second_system


def test_auto_commit_skips_unrelated_preexisting_work_without_tool_edits(
    git_workspace: Path, safety_guard: SafetyGuard, session_store: SessionStore
) -> None:
    # Make a tracked file, then a fresh untracked file before Ash edits anything.
    (git_workspace / "tracked.py").write_text("x = 1\n")
    subprocess.run(
        ["git", "add", "tracked.py"], cwd=git_workspace, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"],
        cwd=git_workspace,
        check=True,
        capture_output=True,
    )

    (git_workspace / "new_file.py").write_text("y = 2\n")

    provider = FakeProvider(scripts=[["<response>done</response>"]])
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=git_workspace,
        auto_commit=True,
    )

    asyncio.run(loop.run_turn("change it"))

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=git_workspace, capture_output=True, text=True
    )
    commits = log.stdout.strip().splitlines()
    assert len(commits) == 1
    assert "seed" in commits[0]
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=git_workspace,
        capture_output=True,
        text=True,
    )
    assert "?? new_file.py" in status.stdout


def test_auto_commit_disabled_by_default(
    git_workspace: Path, safety_guard: SafetyGuard, session_store: SessionStore
) -> None:
    (git_workspace / "seed.py").write_text("z = 1\n")
    subprocess.run(
        ["git", "add", "seed.py"], cwd=git_workspace, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"],
        cwd=git_workspace,
        check=True,
        capture_output=True,
    )
    (git_workspace / "untracked.py").write_text("w = 2\n")

    provider = FakeProvider(scripts=[["<response>ok</response>"]])
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=git_workspace,
    )

    asyncio.run(loop.run_turn("noop"))

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=git_workspace, capture_output=True, text=True
    )
    commits = log.stdout.strip().splitlines()
    # Only the seed commit should exist; auto_commit defaults to off.
    assert len(commits) == 1
    assert "seed" in commits[0]
    assert "turn complete" not in log.stdout


def test_auto_commit_tool_rejects_out_of_scope_path(
    git_workspace: Path, safety_guard: SafetyGuard
) -> None:
    tool = AutoCommitTool(safety_guard)
    result = asyncio.run(tool.run(message="bad", paths=["/etc/passwd"]))
    assert result.success is False
    assert "out-of-scope" in (result.error or "")
