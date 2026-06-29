"""Integration tests for Sprint 9: PPR repo map and git auto-commits."""

from __future__ import annotations

import asyncio
import io
import subprocess
from pathlib import Path

import pytest

from core.loop import AshLoop
from core.session import SessionStore
from providers.base import StreamChunk
from repo.repomap import RepoMap
from safety.guard import SafetyGuard
from tools.git import AutoCommitTool
from tools.filesystem import ReadFileTool, WriteFileTool
from ui.terminal import TerminalUI


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


def test_auto_commit_creates_commit_on_turn_completion(
    git_workspace: Path, safety_guard: SafetyGuard, session_store: SessionStore
) -> None:
    # Make a tracked file, then a fresh untracked file.
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
    assert len(commits) == 2
    assert "turn complete" in commits[0]
    # The auto-commit must include the new file.
    show = subprocess.run(
        ["git", "show", "--stat", "HEAD"],
        cwd=git_workspace,
        capture_output=True,
        text=True,
    )
    assert "new_file.py" in show.stdout


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
