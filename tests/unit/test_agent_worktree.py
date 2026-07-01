from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from agents.worktree import WorktreeError, WorktreeManager


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "file.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "file.txt")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "initial",
    )
    return root


def test_worktree_agent_commits_branch_without_mutating_lead(
    repository: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def run():
        lease = await manager.create("coder-1")
        (lease.path / "file.txt").write_text("worker\n", encoding="utf-8")
        commit = await manager.commit_changes(lease, message="agent change")
        await manager.remove(lease, keep_branch=True)
        return lease, commit

    lease, commit = asyncio.run(run())

    assert commit
    assert repository.joinpath("file.txt").read_text(encoding="utf-8") == "base\n"
    assert not lease.path.exists()
    assert _git(repository, "show", f"{lease.branch}:file.txt") == "worker"


def test_worktree_agent_branch_can_be_applied_and_removed(
    repository: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def run():
        lease = await manager.create("coder-apply")
        (lease.path / "file.txt").write_text("applied\n", encoding="utf-8")
        await manager.commit_changes(lease, message="agent change")
        await manager.remove(lease, keep_branch=True)
        branches = await manager.list_agent_branches()
        commit = await manager.apply_branch(lease.branch)
        return lease, branches, commit

    lease, branches, commit = asyncio.run(run())

    assert (lease.branch, commit) in branches
    assert repository.joinpath("file.txt").read_text(encoding="utf-8") == "applied\n"
    result = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{lease.branch}"],
        cwd=repository,
        check=False,
    )
    assert result.returncode != 0


def test_worktree_without_changes_removes_branch(
    repository: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def run():
        lease = await manager.create("reviewer-1")
        assert await manager.commit_changes(lease, message="unused") is None
        await manager.remove(lease, keep_branch=False)
        return lease

    lease = asyncio.run(run())
    result = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{lease.branch}"],
        cwd=repository,
        check=False,
    )
    assert result.returncode != 0


def test_worktree_rejects_dirty_lead(repository: Path, tmp_path: Path) -> None:
    (repository / "file.txt").write_text("dirty\n", encoding="utf-8")
    manager = WorktreeManager(repository, tmp_path / "agents")

    with pytest.raises(WorktreeError, match="clean lead worktree"):
        asyncio.run(manager.create("coder-1"))


def test_worktree_rejects_unsafe_agent_id(repository: Path, tmp_path: Path) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    with pytest.raises(WorktreeError, match="agent_id"):
        asyncio.run(manager.create("../escape"))


def test_worktree_apply_rejects_dirty_lead(repository: Path, tmp_path: Path) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def prepare():
        lease = await manager.create("coder-dirty")
        (lease.path / "file.txt").write_text("worker\n", encoding="utf-8")
        await manager.commit_changes(lease, message="agent change")
        await manager.remove(lease, keep_branch=True)
        return lease

    lease = asyncio.run(prepare())
    (repository / "local.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(WorktreeError, match="clean lead worktree"):
        asyncio.run(manager.apply_branch(lease.branch))
