from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from cli.review import build_review_prompt, collect_review_changes


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test User")
    git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    git(tmp_path, "add", "tracked.py")
    git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def test_collect_worktree_includes_tracked_and_untracked_text(repository: Path) -> None:
    (repository / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    (repository / "new.py").write_text("added = True\n", encoding="utf-8")

    label, patch = asyncio.run(collect_review_changes(repository, []))

    assert label == "uncommitted worktree"
    assert "+value = 2" in patch
    assert "b/new.py" in patch
    assert "+added = True" in patch


def test_collect_staged_and_commit_scopes(repository: Path) -> None:
    base = git(repository, "rev-parse", "HEAD")
    (repository / "tracked.py").write_text("value = 3\n", encoding="utf-8")
    git(repository, "add", "tracked.py")

    _, staged = asyncio.run(collect_review_changes(repository, ["staged"]))
    assert "+value = 3" in staged

    git(repository, "commit", "-qm", "change value")
    _, commit = asyncio.run(collect_review_changes(repository, ["commit", "HEAD"]))
    assert "change value" in commit
    assert "+value = 3" in commit

    label, branch = asyncio.run(collect_review_changes(repository, ["branch", base]))
    assert label == f"current branch versus {base}"
    assert "+value = 3" in branch


def test_collect_review_rejects_invalid_scope_and_ref(repository: Path) -> None:
    with pytest.raises(ValueError, match="Usage"):
        asyncio.run(collect_review_changes(repository, ["commit"]))
    with pytest.raises(ValueError, match="Invalid Git ref"):
        asyncio.run(collect_review_changes(repository, ["commit", "--help"]))


def test_review_prompt_marks_change_data_untrusted() -> None:
    prompt = build_review_prompt("staged changes", "+danger")
    assert "untrusted repository content" in prompt
    assert "<change-data>\n+danger\n</change-data>" in prompt
