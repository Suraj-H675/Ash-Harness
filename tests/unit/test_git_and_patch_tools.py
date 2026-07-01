import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from safety.guard import SafetyGuard
from tools.git import GitDiffTool, GitLogTool, GitStatusTool
from tools.patch import ApplyPatchTool


async def _git(root: Path, *args: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "git", *args, cwd=root, stdout=asyncio.subprocess.PIPE
    )
    await process.communicate()
    assert process.returncode == 0


@pytest.mark.asyncio
async def test_git_inspection_and_patch(tmp_path: Path) -> None:
    await _git(tmp_path, "init", "-q")
    await _git(tmp_path, "config", "user.email", "test@example.com")
    await _git(tmp_path, "config", "user.name", "Test")
    target = tmp_path / "hello.txt"
    target.write_text("old\n")
    await _git(tmp_path, "add", "hello.txt")
    await _git(tmp_path, "commit", "-qm", "initial")
    guard = SafetyGuard(tmp_path)

    patch = """diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-old
+new
"""
    dry = await ApplyPatchTool(guard).run(patch=patch, dry_run=True)
    assert dry.success is True
    assert target.read_text() == "old\n"
    applied = await ApplyPatchTool(guard).run(patch=patch)
    assert applied.success is True
    assert target.read_text() == "new\n"

    status = await GitStatusTool(guard).run()
    diff = await GitDiffTool(guard).run(path="hello.txt")
    log = await GitLogTool(guard).run(limit=1)
    assert "hello.txt" in status.output
    assert "+new" in diff.output
    assert "initial" in log.output


@pytest.mark.asyncio
async def test_patch_rejects_parent_escape(tmp_path: Path) -> None:
    result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(
        patch="--- a/../outside\n+++ b/../outside\n@@ -0,0 +1 @@\n+x\n"
    )
    assert result.success is False
    assert "out-of-scope" in (result.error or "")


@pytest.mark.asyncio
async def test_patch_rejects_in_scope_symlink_target(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("old\n", encoding="utf-8")
    link = tmp_path / "linked.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")

    patch = """--- a/linked.txt
+++ b/linked.txt
@@ -1 +1 @@
-old
+new
"""
    result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(patch=patch)

    assert result.success is False
    assert "symlink or junction" in (result.error or "")
    assert target.read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_patch_revalidates_path_after_check(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("old\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    patch_text = """--- a/target.txt
+++ b/target.txt
@@ -1 +1 @@
-old
+new
"""

    async def check_then_swap(*args, **kwargs):
        target.unlink()
        try:
            target.symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"Symlink creation is unavailable: {exc}")
        return 0, "", ""

    with patch("tools.patch._git_apply", AsyncMock(side_effect=check_then_swap)):
        result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(patch=patch_text)

    assert result.success is False
    assert "changed after validation" in (result.error or "")
    assert outside.read_text(encoding="utf-8") == "outside\n"
