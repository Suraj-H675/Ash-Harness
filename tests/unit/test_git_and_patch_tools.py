import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from ash.safety.guard import SafetyGuard
from ash.sandbox import SandboxManager, SandboxResult
from ash.sandbox.process_utils import ProcessOutputLimitExceeded, communicate_process
from ash.tools.git import (
    GIT_OUTPUT_LIMIT_EXIT,
    AutoCommitArgs,
    AutoCommitTool,
    GitDiffTool,
    GitLogTool,
    GitStatusTool,
    _git_result,
    _run_git,
    git_dirty_paths,
)
from ash.tools.patch import ApplyPatchTool
from ash.tools.patch import _git_apply


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
async def test_git_dirty_paths_covers_index_worktree_and_untracked(tmp_path: Path) -> None:
    await _init_repo(tmp_path)
    staged = tmp_path / "staged.txt"
    unstaged = tmp_path / "unstaged.txt"
    staged.write_text("old staged\n")
    unstaged.write_text("old unstaged\n")
    await _git(tmp_path, "add", "staged.txt", "unstaged.txt")
    await _git(tmp_path, "commit", "-qm", "initial")

    staged.write_text("new staged\n")
    unstaged.write_text("new unstaged\n")
    (tmp_path / "space name.txt").write_text("untracked\n")
    await _git(tmp_path, "add", "staged.txt")

    dirty = await git_dirty_paths(tmp_path)

    assert dirty == {"staged.txt", "unstaged.txt", "space name.txt"}


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
@pytest.mark.asyncio
async def test_git_tools_do_not_execute_workspace_shadowed_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _init_repo(tmp_path)
    marker = tmp_path / "workspace-git-ran"
    fake_git = tmp_path / "git"
    fake_git.write_text(
        f"#!/bin/sh\nprintf ran > {marker}\nprintf FAKE_GIT_RAN\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    result = await GitStatusTool(SafetyGuard(tmp_path)).run()

    assert result.success is True
    assert "FAKE_GIT_RAN" not in result.output
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
@pytest.mark.asyncio
async def test_read_only_git_disables_repository_extensions(tmp_path: Path) -> None:
    await _init_repo(tmp_path)
    tracked = tmp_path / "hello.txt"
    tracked.write_text("old\n", encoding="utf-8")
    await _git(tmp_path, "add", "hello.txt")
    await _git(tmp_path, "commit", "-qm", "initial")

    marker = tmp_path / "git-extension-ran"
    extension = tmp_path / "git-extension.sh"
    extension.write_text(
        f"#!/bin/sh\nprintf ran >> {marker}\nprintf converted\n",
        encoding="utf-8",
    )
    extension.chmod(0o755)
    await _git(tmp_path, "config", "core.fsmonitor", str(extension))

    status = await GitStatusTool(SafetyGuard(tmp_path)).run()
    assert status.success is True
    assert not marker.exists()

    await _git(tmp_path, "config", "diff.leak.textconv", str(extension))
    (tmp_path / ".gitattributes").write_text("hello.txt diff=leak\n", encoding="utf-8")
    tracked.write_text("new\n", encoding="utf-8")
    diff = await GitDiffTool(SafetyGuard(tmp_path)).run()

    assert diff.success is True
    assert "+new" in diff.output
    assert "converted" not in diff.output
    assert not marker.exists()
    local_config = await asyncio.create_subprocess_exec(
        "git",
        "config",
        "--local",
        "--get",
        "core.fsmonitor",
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await local_config.communicate()
    assert stdout.decode().strip() == str(extension)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
@pytest.mark.asyncio
async def test_patch_tool_does_not_execute_workspace_shadowed_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _init_repo(tmp_path)
    target = tmp_path / "hello.txt"
    target.write_text("old\n", encoding="utf-8")
    await _git(tmp_path, "add", "hello.txt")
    await _git(tmp_path, "commit", "-qm", "initial")
    marker = tmp_path / "workspace-git-ran"
    fake_git = tmp_path / "git"
    fake_git.write_text(
        f"#!/bin/sh\nprintf ran > {marker}\nexit 0\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    patch_text = """diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-old
+new
"""

    result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(
        patch=patch_text, dry_run=True
    )

    assert result.success is True
    assert not marker.exists()


@pytest.mark.asyncio
async def test_git_inspection_reports_process_timeout(tmp_path: Path) -> None:
    with patch("ash.tools.git.communicate_process", side_effect=asyncio.TimeoutError):
        result = await GitStatusTool(SafetyGuard(tmp_path)).run()

    assert result.success is False
    assert result.output == ""
    assert "timed out after 30 seconds" in (result.error or "")


@pytest.mark.asyncio
async def test_git_capture_limit_is_reported_as_truncated_read_only_output(
    tmp_path: Path,
) -> None:
    with patch(
        "ash.tools.git._run_git",
        AsyncMock(
            return_value=(GIT_OUTPUT_LIMIT_EXIT, "partial", "output exceeded")
        ),
    ):
        result = await _git_result(tmp_path, ["diff"])

    assert result.success is True
    assert result.truncated is True
    assert "partial" in result.output
    assert "output truncated" in result.output


@pytest.mark.asyncio
async def test_git_capture_limit_returns_a_bounded_failure_to_callers(
    tmp_path: Path,
) -> None:
    process = Mock(returncode=0)
    with (
        patch("ash.tools.git.asyncio.create_subprocess_exec", AsyncMock(return_value=process)),
        patch(
            "ash.tools.git.communicate_process",
            AsyncMock(
                side_effect=ProcessOutputLimitExceeded(
                    "too much", stdout=b"partial", stderr=b""
                )
            ),
        ),
    ):
        code, stdout, stderr = await _run_git(tmp_path, ["status"])

    assert code == GIT_OUTPUT_LIMIT_EXIT
    assert stdout == "partial"
    assert "exceeded" in stderr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend_name", "expected_executable"),
    [("bubblewrap", None), ("docker", "git")],
)
async def test_run_git_uses_supplied_sandbox_manager(
    tmp_path: Path,
    backend_name: str,
    expected_executable: str | None,
) -> None:
    manager = Mock(backend_name=backend_name)
    manager.run = AsyncMock(
        return_value=SandboxResult(
            exit_code=0,
            stdout="sandboxed",
            stderr="",
            tier=2,
            backend_name=backend_name,
            fallback_used=False,
            duration_seconds=0.01,
        )
    )

    code, stdout, stderr = await _run_git(
        tmp_path,
        ["status", "--short"],
        sandbox_manager=manager,
    )

    assert code == 0
    assert stdout == "sandboxed"
    assert stderr == ""
    command = manager.run.await_args.args[0]
    if expected_executable is None:
        assert Path(command[0]).stem.casefold() == "git"
        assert Path(command[0]).is_absolute()
    else:
        assert command[0] == expected_executable


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_run_git_refuses_workspace_swap_during_executable_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.tools.git as git_module

    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-saved"
    replacement = tmp_path / "replacement"
    workspace.mkdir()
    replacement.mkdir()
    await _init_repo(workspace)
    await _init_repo(replacement)
    real_resolve = git_module.resolve_host_executable
    swapped = False

    def resolve_then_swap(name: str, *, workspace_root: Path, cwd: Path):
        nonlocal swapped
        resolved = real_resolve(name, workspace_root=workspace_root, cwd=cwd)
        if not swapped:
            swapped = True
            workspace.rename(saved)
            replacement.rename(workspace)
        return resolved

    monkeypatch.setattr(git_module, "resolve_host_executable", resolve_then_swap)

    code, stdout, stderr = await _run_git(
        workspace,
        ["rev-parse", "--show-toplevel"],
    )

    assert swapped is True
    assert code == 126
    assert stdout == ""
    assert "working directory identity changed" in stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_run_git_with_manager_refuses_replaced_workspace_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-saved"
    replacement = tmp_path / "replacement"
    workspace.mkdir()
    replacement.mkdir()
    await _init_repo(workspace)
    await _init_repo(replacement)
    manager = SandboxManager(workspace_root=workspace, backend_preference="direct")
    workspace.rename(saved)
    replacement.rename(workspace)

    code, stdout, stderr = await _run_git(
        workspace,
        ["rev-parse", "--show-toplevel"],
        sandbox_manager=manager,
    )

    assert code == 126
    assert stdout == ""
    assert "working directory identity changed" in stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_run_git_cwd_swap_cannot_escape_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox import process_utils as process_utils_module

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    saved = tmp_path / "workspace-saved"
    workspace.mkdir()
    outside.mkdir()
    await _init_repo(workspace)
    await _init_repo(outside)
    real_prepare = process_utils_module.prepare_process_tree
    swapped = False

    def prepare_then_swap(*args, **kwargs):
        nonlocal swapped
        plan = real_prepare(*args, **kwargs)
        if not swapped:
            swapped = True
            workspace.rename(saved)
            try:
                workspace.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"Symlink creation is unavailable: {exc}")
        return plan

    monkeypatch.setattr("ash.tools.git.prepare_process_tree", prepare_then_swap)

    code, stdout, stderr = await _run_git(workspace, ["rev-parse", "--show-toplevel"])

    assert swapped is True
    assert code == 0, stderr
    assert Path(stdout.strip()).resolve() == saved.resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_git_apply_refuses_workspace_swap_during_executable_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.tools.patch as patch_module

    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-saved"
    replacement = tmp_path / "replacement"
    workspace.mkdir()
    replacement.mkdir()
    await _init_repo(workspace)
    await _init_repo(replacement)
    real_resolve = patch_module.resolve_host_executable
    swapped = False

    def resolve_then_swap(name: str, *, workspace_root: Path, cwd: Path):
        nonlocal swapped
        resolved = real_resolve(name, workspace_root=workspace_root, cwd=cwd)
        if not swapped:
            swapped = True
            workspace.rename(saved)
            replacement.rename(workspace)
        return resolved

    monkeypatch.setattr(patch_module, "resolve_host_executable", resolve_then_swap)
    patch_text = """diff --git a/marker.txt b/marker.txt
new file mode 100644
--- /dev/null
+++ b/marker.txt
@@ -0,0 +1 @@
+unsafe
"""

    code, stdout, stderr = await _git_apply(workspace, patch_text, check=False)

    assert swapped is True
    assert code == 126
    assert stdout == ""
    assert "working directory identity changed" in stderr
    assert not (saved / "marker.txt").exists()
    assert not (workspace / "marker.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_git_apply_cwd_swap_cannot_escape_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox import process_utils as process_utils_module

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    saved = tmp_path / "workspace-saved"
    workspace.mkdir()
    outside.mkdir()
    real_prepare = process_utils_module.prepare_process_tree
    swapped = False

    def prepare_then_swap(*args, **kwargs):
        nonlocal swapped
        plan = real_prepare(*args, **kwargs)
        if not swapped:
            swapped = True
            workspace.rename(saved)
            try:
                workspace.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"Symlink creation is unavailable: {exc}")
        return plan

    monkeypatch.setattr("ash.tools.patch.prepare_process_tree", prepare_then_swap)
    patch_text = """diff --git a/marker.txt b/marker.txt
new file mode 100644
--- /dev/null
+++ b/marker.txt
@@ -0,0 +1 @@
+safe
"""

    code, stdout, stderr = await _git_apply(workspace, patch_text, check=False)

    assert swapped is True
    assert code == 0, stderr
    assert not (outside / "marker.txt").exists()
    assert (saved / "marker.txt").read_text(encoding="utf-8") == "safe\n"


@pytest.mark.asyncio
async def test_run_git_fails_closed_when_stable_cwd_is_unavailable(
    tmp_path: Path,
) -> None:
    from ash.sandbox.process_utils import ProcessTreeUnavailable

    with (
        patch(
            "ash.tools.git.prepare_scoped_process_launch",
            side_effect=ProcessTreeUnavailable("stable cwd unavailable"),
        ),
        patch("ash.tools.git.asyncio.create_subprocess_exec", AsyncMock()) as create,
    ):
        code, stdout, stderr = await _run_git(tmp_path, ["status"])

    assert code == 126
    assert stdout == ""
    assert "stable cwd unavailable" in stderr
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_git_apply_fails_closed_when_stable_cwd_is_unavailable(
    tmp_path: Path,
) -> None:
    from ash.sandbox.process_utils import ProcessTreeUnavailable

    patch_text = """diff --git a/marker.txt b/marker.txt
new file mode 100644
--- /dev/null
+++ b/marker.txt
@@ -0,0 +1 @@
+safe
"""
    with (
        patch(
            "ash.tools.patch.prepare_scoped_process_launch",
            side_effect=ProcessTreeUnavailable("stable cwd unavailable"),
        ),
        patch("ash.tools.patch.asyncio.create_subprocess_exec", AsyncMock()) as create,
    ):
        code, stdout, stderr = await _git_apply(tmp_path, patch_text, check=False)

    assert code == 126
    assert stdout == ""
    assert "stable cwd unavailable" in stderr
    create.assert_not_awaited()


@pytest.mark.skipif(os.name == "nt", reason="POSIX Git hook fixture")
@pytest.mark.asyncio
async def test_auto_commit_contains_git_hook_inside_runtime_sandbox(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await _init_repo(workspace)
    target = workspace / "tracked.txt"
    target.write_text("old\n", encoding="utf-8")
    await _git(workspace, "add", "tracked.txt")
    await _git(workspace, "commit", "-qm", "initial")
    target.write_text("new\n", encoding="utf-8")
    outside = tmp_path / "outside-marker"
    hook = workspace / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        f"#!/bin/sh\nprintf escaped > {outside} 2>/dev/null || true\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    manager = SandboxManager(workspace_root=workspace, backend_preference="native")
    if not manager.is_fully_isolated():
        pytest.skip("full native sandbox is unavailable on this host")

    result = await AutoCommitTool(
        SafetyGuard(workspace), sandbox_manager=manager
    ).run(message="sandboxed hook", paths=["tracked.txt"])

    assert result.success is True
    assert not outside.exists()


@pytest.mark.asyncio
async def test_patch_rejects_parent_escape(tmp_path: Path) -> None:
    result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(
        patch="--- a/../outside\n+++ b/../outside\n@@ -0,0 +1 @@\n+x\n"
    )
    assert result.success is False
    assert "out-of-scope" in (result.error or "")


@pytest.mark.asyncio
async def test_patch_rejects_oversized_input_before_spawning_git(tmp_path: Path) -> None:
    result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(
        patch="x" * (8 * 1024 * 1024 + 1)
    )

    assert result.success is False
    assert "exceeds" in (result.error or "")


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

    with patch("ash.tools.patch._git_apply", AsyncMock(side_effect=check_then_swap)):
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


def test_auto_commit_argument_schema_rejects_empty_and_oversized_inputs() -> None:
    with pytest.raises(ValueError):
        AutoCommitArgs(message="scoped", paths=[""])
    with pytest.raises(ValueError):
        AutoCommitArgs(message="scoped", paths=["x" * 4097])
    with pytest.raises(ValueError):
        AutoCommitArgs(message="x" * 65_537)


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
    assert "Git index already contains staged paths" in (result.error or "")
    log = await GitLogTool(SafetyGuard(tmp_path)).run(limit=1)
    assert "initial" in log.output


@pytest.mark.asyncio
async def test_auto_commit_refuses_prestaged_path_inside_scope(tmp_path: Path) -> None:
    await _init_repo(tmp_path)
    target = tmp_path / "tracked.txt"
    target.write_text("old\n")
    await _git(tmp_path, "add", "tracked.txt")
    await _git(tmp_path, "commit", "-qm", "initial")
    target.write_text("user staged\n")
    await _git(tmp_path, "add", "tracked.txt")

    result = await AutoCommitTool(SafetyGuard(tmp_path)).run(
        message="must preserve user staging",
        paths=["tracked.txt"],
    )

    assert result.success is False
    assert "Git index already contains staged paths" in (result.error or "")
    status = await GitStatusTool(SafetyGuard(tmp_path)).run()
    assert "M  tracked.txt" in status.output
    log = await GitLogTool(SafetyGuard(tmp_path)).run(limit=1)
    assert "initial" in log.output


@pytest.mark.asyncio
async def test_owned_auto_commit_refuses_change_racing_with_git_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import ash.tools.git as git_tools

    await _init_repo(tmp_path)
    target = tmp_path / "tracked.txt"
    target.write_text("old\n")
    await _git(tmp_path, "add", "tracked.txt")
    await _git(tmp_path, "commit", "-qm", "initial")
    target.write_text("ash edit\n")
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    original_run_git = git_tools._run_git
    changed = False

    async def mutate_before_add(cwd, args, *positional, **kwargs):
        nonlocal changed
        if list(args[:2]) == ["add", "--"] and not changed:
            changed = True
            target.write_text("late user edit\n")
        return await original_run_git(cwd, args, *positional, **kwargs)

    monkeypatch.setattr(git_tools, "_run_git", mutate_before_add)
    result = await AutoCommitTool(SafetyGuard(tmp_path)).run_owned(
        message="owned edit",
        paths=["tracked.txt"],
        expected_sha256={"tracked.txt": expected},
    )

    assert changed is True
    assert result.success is False
    assert "changed after Ash's last edit" in (result.error or "")
    assert target.read_text() == "late user edit\n"
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
async def test_owned_auto_commit_does_not_run_hook_that_mutates_owned_file(
    tmp_path: Path,
) -> None:
    import hashlib

    await _init_repo(tmp_path)
    target = tmp_path / "tracked.txt"
    target.write_text("old\n")
    await _git(tmp_path, "add", "tracked.txt")
    await _git(tmp_path, "commit", "-qm", "initial")
    target.write_text("ash owned\n")
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        "printf 'hook mutated\\n' > tracked.txt\n"
        "git add -- tracked.txt\n"
    )
    hook.chmod(0o755)

    result = await AutoCommitTool(SafetyGuard(tmp_path)).run_owned(
        message="owned edit",
        paths=["tracked.txt"],
        expected_sha256={"tracked.txt": expected},
    )

    assert result.success is True
    process = await asyncio.create_subprocess_exec(
        "git",
        "show",
        "HEAD:tracked.txt",
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await communicate_process(process)
    assert process.returncode == 0
    assert stdout.decode() == "ash owned\n"
    assert target.read_text() == "ash owned\n"


@pytest.mark.asyncio
async def test_owned_auto_commit_does_not_absorb_hook_staged_unrelated_path(
    tmp_path: Path,
) -> None:
    import hashlib

    await _init_repo(tmp_path)
    target = tmp_path / "tracked.txt"
    unrelated = tmp_path / "unrelated.txt"
    target.write_text("old\n")
    unrelated.write_text("user old\n")
    await _git(tmp_path, "add", "tracked.txt", "unrelated.txt")
    await _git(tmp_path, "commit", "-qm", "initial")
    target.write_text("ash owned\n")
    unrelated.write_text("user concurrent\n")
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\ngit add -- unrelated.txt\n")
    hook.chmod(0o755)

    result = await AutoCommitTool(SafetyGuard(tmp_path)).run_owned(
        message="owned edit",
        paths=["tracked.txt"],
        expected_sha256={"tracked.txt": expected},
    )

    assert result.success is True
    process = await asyncio.create_subprocess_exec(
        "git",
        "show",
        "--format=",
        "--name-only",
        "HEAD",
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await communicate_process(process)
    assert process.returncode == 0
    assert stdout.decode().splitlines() == ["tracked.txt"]
    assert unrelated.read_text() == "user concurrent\n"


@pytest.mark.asyncio
async def test_explicit_auto_commit_hook_cannot_expand_path_scope(tmp_path: Path) -> None:
    await _init_repo(tmp_path)
    target = tmp_path / "tracked.txt"
    unrelated = tmp_path / "unrelated.txt"
    target.write_text("old\n")
    unrelated.write_text("user old\n")
    await _git(tmp_path, "add", "tracked.txt", "unrelated.txt")
    await _git(tmp_path, "commit", "-qm", "initial")
    target.write_text("explicit edit\n")
    unrelated.write_text("user concurrent\n")
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\ngit add -- unrelated.txt\n")
    hook.chmod(0o755)

    result = await AutoCommitTool(SafetyGuard(tmp_path)).run(
        message="explicit edit",
        paths=["tracked.txt"],
    )

    assert result.success is False
    assert "outside explicit scope" in (result.error or "")
    process = await asyncio.create_subprocess_exec(
        "git",
        "show",
        "--format=",
        "--name-only",
        "HEAD",
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await communicate_process(process)
    assert process.returncode == 0
    assert stdout.decode().splitlines() == ["tracked.txt", "unrelated.txt"]
    status = await GitStatusTool(SafetyGuard(tmp_path)).run()
    assert "M  unrelated.txt" in status.output


@pytest.mark.asyncio
async def test_owned_auto_commit_supports_unborn_repository(tmp_path: Path) -> None:
    import hashlib

    await _init_repo(tmp_path)
    target = tmp_path / "first.txt"
    target.write_text("first owned commit\n")
    expected = hashlib.sha256(target.read_bytes()).hexdigest()

    result = await AutoCommitTool(SafetyGuard(tmp_path)).run_owned(
        message="first owned commit",
        paths=["first.txt"],
        expected_sha256={"first.txt": expected},
    )

    assert result.success is True
    process = await asyncio.create_subprocess_exec(
        "git",
        "show",
        "HEAD:first.txt",
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await communicate_process(process)
    assert process.returncode == 0
    assert stdout.decode() == "first owned commit\n"


@pytest.mark.asyncio
async def test_explicit_auto_commit_rescans_secret_injected_by_hook(
    tmp_path: Path,
) -> None:
    await _init_repo(tmp_path)
    target = tmp_path / "config.env"
    target.write_text("SAFE=value\n")
    await _git(tmp_path, "add", "config.env")
    await _git(tmp_path, "commit", "-qm", "initial")
    target.write_text("SAFE=changed\n")
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz"
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f"printf 'OPENAI_API_KEY={secret}\\n' > config.env\n"
        "git add -- config.env\n"
    )
    hook.chmod(0o755)

    result = await AutoCommitTool(SafetyGuard(tmp_path)).run(
        message="hook injected secret",
        paths=["config.env"],
    )

    assert result.success is False
    assert "Potential secret detected" in (result.error or "")
    assert secret not in (result.error or "")
    log = await GitLogTool(SafetyGuard(tmp_path)).run(limit=1)
    assert "initial" in log.output


@pytest.mark.asyncio
async def test_owned_auto_commit_refuses_concurrent_head_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import ash.tools.git as git_tools

    await _init_repo(tmp_path)
    target = tmp_path / "tracked.txt"
    target.write_text("old\n")
    await _git(tmp_path, "add", "tracked.txt")
    await _git(tmp_path, "commit", "-qm", "initial")
    target.write_text("ash owned\n")
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    original_run_git = git_tools._run_git
    moved = False

    async def move_head_before_update(cwd, args, *positional, **kwargs):
        nonlocal moved
        if list(args[:2]) == ["update-ref", "HEAD"] and not moved:
            moved = True
            await _git(tmp_path, "commit", "--allow-empty", "-qm", "concurrent user")
        return await original_run_git(cwd, args, *positional, **kwargs)

    monkeypatch.setattr(git_tools, "_run_git", move_head_before_update)
    result = await AutoCommitTool(SafetyGuard(tmp_path)).run_owned(
        message="owned edit",
        paths=["tracked.txt"],
        expected_sha256={"tracked.txt": expected},
    )

    assert moved is True
    assert result.success is False
    assert "git update-ref failed" in (result.error or "")
    log = await GitLogTool(SafetyGuard(tmp_path)).run(limit=1)
    assert "concurrent user" in log.output


@pytest.mark.asyncio
async def test_explicit_auto_commit_preserves_commit_hook_sequence(
    tmp_path: Path,
) -> None:
    await _init_repo(tmp_path)
    target = tmp_path / "tracked.txt"
    target.write_text("old\n")
    await _git(tmp_path, "add", "tracked.txt")
    await _git(tmp_path, "commit", "-qm", "initial")
    target.write_text("new\n")
    marker = tmp_path / "hook-order.txt"
    hooks = tmp_path / ".git" / "hooks"
    (hooks / "pre-commit").write_text(
        "#!/bin/sh\nprintf 'pre\\n' >> hook-order.txt\n"
    )
    (hooks / "prepare-commit-msg").write_text(
        "#!/bin/sh\n"
        "printf 'prepare:%s\\n' \"$2\" >> hook-order.txt\n"
        "printf '\\nprepared-by-hook\\n' >> \"$1\"\n"
    )
    (hooks / "commit-msg").write_text(
        "#!/bin/sh\n"
        "grep -q 'prepared-by-hook' \"$1\" || exit 9\n"
        "printf 'commit-msg\\n' >> hook-order.txt\n"
    )
    (hooks / "post-commit").write_text(
        "#!/bin/sh\nprintf 'post\\n' >> hook-order.txt\nexit 7\n"
    )
    for name in ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit"):
        hook = hooks / name
        hook.chmod(0o755)

    result = await AutoCommitTool(SafetyGuard(tmp_path)).run(
        message="explicit hooks",
        paths=["tracked.txt"],
    )

    assert result.success is True
    assert "post-commit hook exited with code 7" in result.output
    assert marker.read_text().splitlines() == [
        "pre",
        "prepare:message",
        "commit-msg",
        "post",
    ]
    process = await asyncio.create_subprocess_exec(
        "git",
        "log",
        "-1",
        "--format=%B",
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await communicate_process(process)
    assert process.returncode == 0
    assert "prepared-by-hook" in stdout.decode()


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
