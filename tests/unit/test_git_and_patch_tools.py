import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from safety.guard import SafetyGuard
from sandbox.process_utils import communicate_process
from tools.git import AutoCommitTool, GitDiffTool, GitLogTool, GitStatusTool
from tools.patch import ApplyPatchTool


async def _git(root: Path, *args: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "git", *args, cwd=root, stdout=asyncio.subprocess.PIPE
    )
    await communicate_process(process)
    assert process.returncode == 0


async def _init_repo(root: Path) -> None:
    await _git(root, "init", "-q")
    await _git(root, "config", "user.email", "test@example.com")
    await _git(root, "config", "user.name", "Test")


@pytest.mark.asyncio
async def test_git_inspection_and_patch(tmp_path: Path) -> None:
    await _init_repo(tmp_path)
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
async def test_git_inspection_reports_process_timeout(tmp_path: Path) -> None:
    with patch("tools.git.communicate_process", side_effect=asyncio.TimeoutError):
        result = await GitStatusTool(SafetyGuard(tmp_path)).run()

    assert result.success is False
    assert result.output == ""
    assert "timed out after 30 seconds" in (result.error or "")


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


@pytest.mark.asyncio
async def test_auto_commit_requires_explicit_paths(tmp_path: Path) -> None:
    await _init_repo(tmp_path)
    target = tmp_path / "tracked.txt"
    target.write_text("old\n")
    await _git(tmp_path, "add", "tracked.txt")
    await _git(tmp_path, "commit", "-qm", "initial")
    target.write_text("new\n")
    (tmp_path / "untracked.txt").write_text("do not include\n")

    result = await AutoCommitTool(SafetyGuard(tmp_path)).run(message="unsafe")

    assert result.success is False
    assert "explicit path scope" in (result.error or "")
    status = await GitStatusTool(SafetyGuard(tmp_path)).run()
    assert " M tracked.txt" in status.output
    assert "?? untracked.txt" in status.output


@pytest.mark.asyncio
async def test_auto_commit_refuses_unrelated_prestaged_paths(tmp_path: Path) -> None:
    await _init_repo(tmp_path)
    scoped = tmp_path / "scoped.txt"
    unrelated = tmp_path / "unrelated.txt"
    scoped.write_text("old\n")
    unrelated.write_text("old\n")
    await _git(tmp_path, "add", "scoped.txt", "unrelated.txt")
    await _git(tmp_path, "commit", "-qm", "initial")
    scoped.write_text("new\n")
    unrelated.write_text("new\n")
    await _git(tmp_path, "add", "unrelated.txt")

    result = await AutoCommitTool(SafetyGuard(tmp_path)).run(
        message="scoped only",
        paths=["scoped.txt"],
    )

    assert result.success is False
    assert "pre-staged paths outside explicit scope" in (result.error or "")
    log = await GitLogTool(SafetyGuard(tmp_path)).run(limit=1)
    assert "initial" in log.output


@pytest.mark.asyncio
async def test_auto_commit_surfaces_hook_failure_output(tmp_path: Path) -> None:
    await _init_repo(tmp_path)
    target = tmp_path / "tracked.txt"
    target.write_text("old\n")
    await _git(tmp_path, "add", "tracked.txt")
    await _git(tmp_path, "commit", "-qm", "initial")
    target.write_text("new\n")
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho hook failed >&2\nexit 1\n")
    hook.chmod(0o755)

    result = await AutoCommitTool(SafetyGuard(tmp_path)).run(
        message="should fail",
        paths=["tracked.txt"],
    )

    assert result.success is False
    assert "git commit failed with exit code" in (result.error or "")
    assert "hook failed" in (result.error or "")


@pytest.mark.asyncio
async def test_auto_commit_scrubs_hook_environment_except_explicit_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX Git hook environment test")
    await _init_repo(tmp_path)
    target = tmp_path / "tracked.txt"
    target.write_text("old\n")
    await _git(tmp_path, "add", "tracked.txt")
    await _git(tmp_path, "commit", "-qm", "initial")
    target.write_text("new\n")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    monkeypatch.setenv("BUILD_CHANNEL", "nightly")
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        "printf '%s|%s' \"${UNRELATED_SECRET-unset}\" "
        '"${BUILD_CHANNEL-unset}" > hook-env.txt\n'
    )
    hook.chmod(0o755)

    result = await AutoCommitTool(
        SafetyGuard(tmp_path), environment_allowlist=["BUILD_CHANNEL"]
    ).run(message="scrubbed hook", paths=["tracked.txt"])

    assert result.success is True
    assert (tmp_path / "hook-env.txt").read_text() == "unset|nightly"


@pytest.mark.asyncio
async def test_auto_commit_refuses_staged_secret_without_echoing_value(
    tmp_path: Path,
) -> None:
    await _init_repo(tmp_path)
    target = tmp_path / "config.env"
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz"
    target.write_text(f"OPENAI_API_KEY={secret}\n")

    result = await AutoCommitTool(SafetyGuard(tmp_path)).run(
        message="unsafe secret",
        paths=["config.env"],
    )

    assert result.success is False
    assert "Potential secret detected" in (result.error or "")
    assert "config.env:1" in (result.error or "")
    assert "provider API key" in (result.error or "")
    assert secret not in (result.error or "")
    assert result.output == "Changes remain staged for inspection."
    status = await GitStatusTool(SafetyGuard(tmp_path)).run()
    assert "A  config.env" in status.output
