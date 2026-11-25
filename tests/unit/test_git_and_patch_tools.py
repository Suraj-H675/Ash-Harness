import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from ash.safety.guard import SafetyGuard
from ash.safety.git import read_only_git_args
from ash.safety.scoped_io import workspace_mutation_lock
from ash.sandbox import SandboxManager, SandboxResult, SandboxTier
from ash.sandbox.process_utils import ProcessOutputLimitExceeded, communicate_process
from ash.tools.base import ToolExecutionOutcome
from ash.tools.git import (
    GIT_OUTPUT_LIMIT_EXIT,
    AutoCommitArgs,
    AutoCommitTool,
    GitDiffTool,
    GitLogTool,
    GitStatusTool,
    _git_result,
    _run_git,
    _run_git_result,
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


@pytest.mark.asyncio
async def test_model_oriented_patch_updates_two_modules_and_respects_dry_run(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("def value():\n    return 1\n")
    second.write_text("def value():\n    return 2\n")
    patch_text = """*** Begin Patch
*** Update File: first.py
@@ def value():
-    return 1
+    return 11
*** Update File: second.py
@@ def value():
-    return 2
+    return 22
*** End Patch"""
    tool = ApplyPatchTool(SafetyGuard(tmp_path))
    dry = await tool.run(patch=patch_text, dry_run=True)
    assert dry.success, dry.error
    assert first.read_text() == "def value():\n    return 1\n"
    assert second.read_text() == "def value():\n    return 2\n"
    applied = await tool.run(patch=patch_text)
    assert applied.success, applied.error
    assert first.read_text() == "def value():\n    return 11\n"
    assert second.read_text() == "def value():\n    return 22\n"


@pytest.mark.asyncio
async def test_model_oriented_patch_add_delete_and_preserve_line_endings(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original.txt"
    windows = tmp_path / "windows.txt"
    original.write_text("remove me\n")
    windows.write_bytes(b"first\r\nsecond\r\n")
    patch_text = """*** Begin Patch
*** Add File: added.txt
+new content
*** Delete File: original.txt
*** Update File: windows.txt
@@
-second
+replacement
*** End Patch"""
    result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(patch=patch_text)
    assert result.success, result.error
    assert (tmp_path / "added.txt").read_text() == "new content\n"
    assert not original.exists()
    assert windows.read_bytes() == b"first\r\nreplacement\r\n"


@pytest.mark.asyncio
async def test_model_oriented_patch_rejects_ambiguous_and_out_of_scope_edits(
    tmp_path: Path,
) -> None:
    target = tmp_path / "module.py"
    target.write_text("value = 1\nvalue = 1\n")
    tool = ApplyPatchTool(SafetyGuard(tmp_path))
    ambiguous = """*** Begin Patch
*** Update File: module.py
@@
-value = 1
+value = 2
*** End Patch"""
    response = await tool.run(patch=ambiguous)
    assert not response.success
    assert "ambiguous" in (response.error or "").lower()
    assert target.read_text() == "value = 1\nvalue = 1\n"

    traversal = """*** Begin Patch
*** Update File: ../outside.py
@@
-value = 1
+value = 2
*** End Patch"""
    refused = await tool.run(patch=traversal)
    assert not refused.success
    assert target.read_text() == "value = 1\nvalue = 1\n"


@pytest.mark.asyncio
async def test_model_oriented_patch_preserves_missing_final_newline_and_multiple_hunks(
    tmp_path: Path,
) -> None:
    target = tmp_path / "module.txt"
    target.write_bytes(b"alpha\nbeta\ngamma\ndelta")
    result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(
        patch="""*** Begin Patch
*** Update File: module.txt
@@
-alpha
+ALPHA
@@
-delta
+DELTA
*** End Patch"""
    )
    assert result.success, result.error
    assert target.read_bytes() == b"ALPHA\nbeta\ngamma\nDELTA"


@pytest.mark.parametrize(
    "raw_content,patch_body",
    [
        (b"\x00\x01", "*** Delete File: module.txt"),
        (b"\xff\xfe", "*** Delete File: module.txt"),
        (b"first\nsecond\n", "*** Update File: module.txt\n@@\n-missing\n+replacement"),
        (b"first\n", "*** Update File: module.txt\n*** Move to: moved.txt\n@@\n-first\n+other"),
        (b"first\n", "*** Update File: module\x1b[31m.txt\n@@\n-first\n+other"),
        (b"first\n", "*** Update File: module.txt\n@@\n-first\n+other\n*** Add File: ./module.txt\n+other"),
    ],
)
@pytest.mark.asyncio
async def test_model_patch_refuses_unsafe_or_stale_source_without_writes(
    tmp_path: Path, raw_content: bytes, patch_body: str
) -> None:
    target = tmp_path / "module.txt"
    target.write_bytes(raw_content)
    result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(
        patch=f"*** Begin Patch\n{patch_body}\n*** End Patch"
    )
    assert not result.success
    assert result.outcome is ToolExecutionOutcome.COMPLETED
    assert target.read_bytes() == raw_content
    assert not (tmp_path / "moved.txt").exists()


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="requires unprivileged POSIX filesystem permissions",
)
@pytest.mark.asyncio
async def test_real_git_patch_failure_after_first_file_edit_is_unknown(
    tmp_path: Path,
) -> None:
    """`git apply --check` can pass, yet apply can fail after editing a file."""
    await _init_repo(tmp_path)
    first = tmp_path / "first.py"
    first.write_text("value = 1\n", encoding="utf-8")
    locked_dir = tmp_path / "z_locked"
    locked_dir.mkdir()
    second = locked_dir / "second.py"
    second.write_text("value = 1\n", encoding="utf-8")
    patch_text = """--- a/first.py
+++ b/first.py
@@ -1 +1 @@
-value = 1
+value = 2
--- a/z_locked/second.py
+++ b/z_locked/second.py
@@ -1 +1 @@
-value = 1
+value = 2
"""
    tool = ApplyPatchTool(SafetyGuard(tmp_path))
    locked_dir.chmod(0o555)
    try:
        dry_run = await tool.run(patch=patch_text, dry_run=True)
        assert dry_run.success is True
        assert first.read_text() == second.read_text() == "value = 1\n"
        result = await tool.run(patch=patch_text)
    finally:
        locked_dir.chmod(0o755)

    assert first.read_text() == "value = 2\n"
    assert second.read_text() == "value = 1\n"
    assert result.success is False
    assert result.outcome is ToolExecutionOutcome.UNKNOWN
    assert "may have changed" in (result.error or "")
    assert "inspect" in (result.error or "").lower()


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="requires unprivileged POSIX filesystem permissions",
)
@pytest.mark.asyncio
async def test_coding_loop_inspects_partial_patch_instead_of_replaying(
    tmp_path: Path,
) -> None:
    """The provider sees an ambiguous real partial write and checks both files."""
    import io
    import shlex
    import sys

    from ash.core.loop import AshLoop
    from ash.core.session import SessionStore
    from ash.providers.base import ProviderABC, StreamChunk
    from ash.providers.capabilities import ProviderCapabilities
    from ash.tools.command import RunCommandTool
    from ash.tools.filesystem import ReadFileTool
    from ash.ui.headless import HeadlessUI

    await _init_repo(tmp_path)
    first = tmp_path / "first.py"
    first.write_text("value = 1\n", encoding="utf-8")
    locked_dir = tmp_path / "z_locked"
    locked_dir.mkdir()
    second = locked_dir / "second.py"
    second.write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "verify_partial.py").write_text(
        "from pathlib import Path\n"
        "assert Path('first.py').read_text() == 'value = 2\\n'\n"
        "assert Path('z_locked/second.py').read_text() == 'value = 1\\n'\n"
        "print('PARTIAL-PATCH-CONFIRMED')\n",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} -B verify_partial.py"
    patch_text = """--- a/first.py
+++ b/first.py
@@ -1 +1 @@
-value = 1
+value = 2
--- a/z_locked/second.py
+++ b/z_locked/second.py
@@ -1 +1 @@
-value = 1
+value = 2
"""

    class Provider(ProviderABC):
        model_name = "scripted-partial-git-patch"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.requests = 0

        def count_tokens(self, text: str) -> int:
            return max(1, len(str(text)) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.requests += 1
            history = repr(messages)
            if self.requests == 1:
                calls = [{"id": "one-partial-patch", "name": "apply_patch",
                          "arguments": {"patch": patch_text}}]
            elif self.requests == 2:
                assert "Tool outcome is ambiguous" in history
                assert "workspace may have changed partially" in history
                assert first.read_text() == "value = 2\n"
                assert second.read_text() == "value = 1\n"
                calls = [
                    {"id": "inspect-first", "name": "read_file",
                     "arguments": {"file_path": "first.py"}},
                    {"id": "inspect-second", "name": "read_file",
                     "arguments": {"file_path": "z_locked/second.py"}},
                ]
            elif self.requests == 3:
                assert "1: value = 2" in history
                assert "1: value = 1" in history
                calls = [{"id": "verify-partial", "name": "run_command",
                          "arguments": {"command_line": command}}]
            else:
                assert self.requests == 4
                assert "PARTIAL-PATCH-CONFIRMED" in history
                yield StreamChunk(
                    content="Partial patch confirmed; second file still needs repair.",
                    is_done=True,
                )
                return
            yield StreamChunk(is_done=True, native_tool_calls=calls)

    async def approve(name: str, arguments: dict[str, object]) -> bool:
        if name == "run_command":
            assert arguments["command_line"] == command
        return True

    guard = SafetyGuard(tmp_path)
    store = SessionStore(tmp_path / "partial-patch-loop.db")
    provider = Provider()
    loop = AshLoop(
        store, provider, guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path,
        tools={
            "apply_patch": ApplyPatchTool(guard),
            "read_file": ReadFileTool(guard),
            "run_command": RunCommandTool(guard),
        },
        on_tool_approval=approve,
    )
    locked_dir.chmod(0o555)
    try:
        session = await loop.start_session()
        result = await loop.run_turn("Update both modules and verify the result")
        assert result == "Partial patch confirmed; second file still needs repair."
        assert provider.requests == 4
        assert first.read_text() == "value = 2\n"
        assert second.read_text() == "value = 1\n"
        calls = store.load_session(session.session_id).tool_calls
        assert [call.tool_name for call in calls] == [
            "apply_patch", "read_file", "read_file", "run_command"
        ]
        assert calls[0].executed and calls[0].dispatched
        assert calls[0].success is None
        assert all(call.success is True for call in calls[1:])
        assert "PARTIAL-PATCH-CONFIRMED" in (calls[3].result or "")
        assert store.verify_audit_log(session.session_id) == []
        assert any(
            entry.result == "FAILURE"
            and entry.details.get("call_id") == "one-partial-patch"
            and entry.details.get("ambiguous") is True
            for entry in store.list_audit_logs(session.session_id)
        )
    finally:
        locked_dir.chmod(0o755)
        await loop.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True])
async def test_git_diff_discloses_untracked_source_omitted_from_review(
    tmp_path: Path, staged: bool,
) -> None:
    await _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    (tmp_path / "tracked.py").write_text("old = 1\n", encoding="utf-8")
    await _git(tmp_path, "add", "tracked.py", ".gitignore")
    await _git(tmp_path, "commit", "-qm", "initial")
    (tmp_path / "new_source.py").write_text("new = 2\n", encoding="utf-8")
    guard = SafetyGuard(tmp_path)

    untracked_only = await GitDiffTool(guard).run(staged=staged)
    scoped = await GitDiffTool(guard).run(path="new_source.py", staged=staged)

    for result in (untracked_only, scoped):
        assert result.success and not result.truncated
        assert "untracked" in result.output.lower()
        assert "git_status" in result.output
        assert "workspace_diff" in result.output
        assert "new = 2" not in result.output

    tracked_only = await GitDiffTool(guard).run(path="tracked.py", staged=staged)
    assert tracked_only.success
    assert "untracked" not in tracked_only.output.lower()

    (tmp_path / "tracked.py").write_text("old = 3\n", encoding="utf-8")
    if staged:
        await _git(tmp_path, "add", "tracked.py")
    mixed = await GitDiffTool(guard).run(staged=staged)
    assert "+old = 3" in mixed.output
    assert "-old = 1" in mixed.output
    assert "untracked" in mixed.output.lower()

    (tmp_path / "new_source.py").unlink()
    (tmp_path / "ignored.tmp").write_text("private build output\n", encoding="utf-8")
    tracked_diff = await GitDiffTool(guard).run(staged=staged)
    assert "+old = 3" in tracked_diff.output
    assert "untracked" not in tracked_diff.output.lower()


@pytest.mark.asyncio
async def test_git_diff_bounds_warning_when_review_nears_output_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.tools.git as git_tools
    from ash.tools.base import ToolResult

    async def long_diff(*args, **kwargs):
        del args, kwargs
        return ToolResult(success=True, output="a" * 390)

    async def untracked_status(*args, **kwargs):
        del args, kwargs
        return 0, "?? new.py\0", ""

    monkeypatch.setattr(git_tools, "DEFAULT_GIT_OUTPUT_LIMIT", 400)
    monkeypatch.setattr(git_tools, "_git_result", long_diff)
    monkeypatch.setattr(git_tools, "_run_git", untracked_status)

    result = await GitDiffTool(SafetyGuard(tmp_path)).run()
    assert result.success and result.truncated
    assert len(result.output) <= 400
    assert "output truncated" in result.output
    assert "untracked" in result.output
    assert "new.py" not in result.output


@pytest.mark.asyncio
async def test_multi_file_apply_patch_reports_python_syntax_without_undoing_edit(
    tmp_path: Path,
) -> None:
    """Bulk edits deserve the same advisory feedback as direct file edits."""
    await _init_repo(tmp_path)
    valid = tmp_path / "valid.py"
    invalid = tmp_path / "invalid.py"
    valid.write_text("def answer():\n    return 1\n", encoding="utf-8")
    invalid.write_text("def other():\n    return 1\n", encoding="utf-8")
    patch = """diff --git a/valid.py b/valid.py
--- a/valid.py
+++ b/valid.py
@@ -1,2 +1,2 @@
 def answer():
-    return 1
+    return 2
diff --git a/invalid.py b/invalid.py
--- a/invalid.py
+++ b/invalid.py
@@ -1,2 +1,2 @@
 def other():
-    return 1
+    return (
"""
    result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(patch=patch)
    assert result.success is True
    assert valid.read_text(encoding="utf-8") == "def answer():\n    return 2\n"
    assert invalid.read_text(encoding="utf-8") == "def other():\n    return (\n"
    assert result.diagnostic_summary == {"python_syntax_errors": 1}
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0]["path"] == "invalid.py"
    assert result.diagnostics[0]["line"] == 2
    assert result.diagnostics[0]["code"] == "SyntaxError"
    assert "syntax check" in result.output.lower()


@pytest.mark.asyncio
async def test_apply_patch_syntax_feedback_is_not_emitted_on_dry_run(
    tmp_path: Path,
) -> None:
    await _init_repo(tmp_path)
    target = tmp_path / "script.py"
    target.write_text("value = 1\n", encoding="utf-8")
    patch = """--- a/script.py
+++ b/script.py
@@ -1 +1 @@
-value = 1
+def invalid(:
"""
    result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(
        patch=patch, dry_run=True
    )
    assert result.success is True
    assert not result.diagnostics
    assert target.read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.asyncio
async def test_multi_file_patch_syntax_advisories_bound_number_of_python_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.tools.patch as patch_module

    await _init_repo(tmp_path)
    monkeypatch.setattr(patch_module, "MAX_PATCH_PYTHON_SYNTAX_FILES", 1)
    patch = ""
    for name in ("first.py", "second.py"):
        (tmp_path / name).write_text("value = 1\n", encoding="utf-8")
        patch += (
            f"--- a/{name}\n+++ b/{name}\n"
            "@@ -1 +1 @@\n-value = 1\n+def invalid(:\n"
        )

    result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(patch=patch)

    assert result.success
    assert result.diagnostic_summary == {"python_syntax_errors": 1}
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0]["path"] == "first.py"
    assert result.truncated
    assert "1 further path(s) not checked" in result.output
    for name in ("first.py", "second.py"):
        assert (tmp_path / name).read_text(encoding="utf-8") == "def invalid(:\n"


@pytest.mark.parametrize("patch_format", ["unified", "model"])
@pytest.mark.asyncio
async def test_coding_loop_repairs_syntax_after_multi_file_patch(
    tmp_path: Path, patch_format: str,
) -> None:
    import io

    from ash.core.loop import AshLoop
    from ash.core.session import SessionStore
    from ash.providers.base import ProviderABC, StreamChunk
    from ash.providers.capabilities import ProviderCapabilities
    from ash.tools.filesystem import ReplaceFileContentTool
    from ash.ui.headless import HeadlessUI

    await _init_repo(tmp_path)
    (tmp_path / "first.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "second.py").write_text("value = 1\n", encoding="utf-8")
    patch = """--- a/first.py
+++ b/first.py
@@ -1 +1 @@
-value = 1
+value = 2
--- a/second.py
+++ b/second.py
@@ -1 +1 @@
-value = 1
+def broken(:
"""
    if patch_format == "model":
        patch = """*** Begin Patch
*** Update File: first.py
@@
-value = 1
+value = 2
*** Update File: second.py
@@
-value = 1
+def broken(:
*** End Patch"""

    class RepairFromPatchFeedback(ProviderABC):
        model_name = "patch-feedback"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.calls = 0

        def count_tokens(self, text):
            return max(1, len(str(text)) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.calls += 1
            if self.calls == 1:
                tool_call = {
                    "call_id": "patch-two-files",
                    "name": "apply_patch", "arguments": {"patch": patch},
                }
            elif self.calls == 2:
                assert "python_syntax_errors" in repr(messages)
                assert "second.py" in repr(messages)
                assert "SyntaxError" in repr(messages)
                tool_call = {
                    "call_id": "repair-second-file",
                    "name": "replace_file_content",
                    "arguments": {
                        "file_path": "second.py",
                        "target_content": "def broken(:",
                        "replacement_content": "value = 3",
                    },
                }
            else:
                yield StreamChunk(content="Both modules corrected", is_done=True)
                return
            yield StreamChunk(is_done=True, native_tool_calls=[tool_call])

    async def approve(_name: str, _arguments: dict[str, object]) -> bool:
        return True

    guard = SafetyGuard(tmp_path)
    store = SessionStore(tmp_path / "coding-loop.db")
    provider = RepairFromPatchFeedback()
    loop = AshLoop(
        store, provider, guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path,
        tools={
            "apply_patch": ApplyPatchTool(guard),
            "replace_file_content": ReplaceFileContentTool(guard),
        },
        on_tool_approval=approve,
    )
    try:
        session = await loop.start_session()
        assert await loop.run_turn("Update both modules and correct errors") == (
            "Both modules corrected"
        )
        calls = store.load_session(session.session_id).tool_calls
        assert [call.tool_name for call in calls] == [
            "apply_patch", "replace_file_content"
        ]
        assert all(call.success is True for call in calls)
        assert provider.calls == 3
        assert (tmp_path / "first.py").read_text(encoding="utf-8") == "value = 2\n"
        assert (tmp_path / "second.py").read_text(encoding="utf-8") == "value = 3\n"
        assert store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_apply_patch_explains_malformed_model_patch(tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    target.write_text("old\n")
    tool = ApplyPatchTool(SafetyGuard(tmp_path))
    assert "unified diff" in tool.json_schema()["properties"]["patch"]["description"]

    result = await tool.run(
        patch="*** Begin Patch\n*** Update File: hello.txt\n@@\nold\n+new\n*** End Patch\n"
    )

    assert result.success is False
    assert "expected an @@ context or prefixed change line" in (result.error or "")
    assert target.read_text() == "old\n"


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.asyncio
async def test_apply_patch_explains_corrupt_hunk_counts(
    tmp_path: Path, dry_run: bool
) -> None:
    await _init_repo(tmp_path)
    target = tmp_path / "hello.txt"
    target.write_text("old\n")
    await _git(tmp_path, "add", "hello.txt")
    await _git(tmp_path, "commit", "-qm", "initial")

    result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(
        patch="--- a/hello.txt\n+++ b/hello.txt\n@@ -1,2 +1,2 @@\n-old\n+new\n",
        dry_run=dry_run,
    )

    assert result.success is False
    assert result.outcome is ToolExecutionOutcome.COMPLETED
    assert "corrupt patch at" in (result.error or "")
    assert "context lines count on both sides" in (result.error or "")
    assert "replace_file_edits" in (result.error or "")
    assert target.read_text() == "old\n"


@pytest.mark.asyncio
async def test_direct_apply_patch_refuses_competing_ash_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    await _init_repo(tmp_path)
    target = tmp_path / "hello.txt"
    target.write_text("old\n", encoding="utf-8")
    await _git(tmp_path, "add", "hello.txt")
    await _git(tmp_path, "commit", "-qm", "initial")
    patch_text = """diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-old
+new
"""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def owner() -> None:
        with workspace_mutation_lock():
            entered.set()
            await release.wait()

    owner_task = asyncio.create_task(owner())
    await entered.wait()
    try:
        result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(patch=patch_text)
    finally:
        release.set()
        await owner_task

    assert result.success is False
    assert "another Ash workspace mutation" in (result.error or "")
    assert target.read_text(encoding="utf-8") == "old\n"


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

    await _git(tmp_path, "config", "filter.leak.clean", str(extension))
    (tmp_path / ".gitattributes").write_text(
        "hello.txt diff=leak filter=leak\n",
        encoding="utf-8",
    )
    marker.unlink(missing_ok=True)

    status_with_filter = await GitStatusTool(SafetyGuard(tmp_path)).run()
    diff_with_filter = await GitDiffTool(SafetyGuard(tmp_path)).run()

    assert status_with_filter.success is True
    assert diff_with_filter.success is True
    assert "+new" in diff_with_filter.output
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


def test_read_only_git_filter_overrides_cover_process_and_complex_driver_names() -> None:
    args = read_only_git_args(
        ["status", "--short"],
        filter_drivers=("foo.bar baz",),
    )

    assert "--work-tree=." in args
    assert "log.showSignature=false" in args
    assert "filter.foo.bar baz.clean=" in args
    assert "filter.foo.bar baz.smudge=" in args
    assert "filter.foo.bar baz.process=" in args
    assert "filter.foo.bar baz.required=false" in args


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
@pytest.mark.asyncio
async def test_read_only_git_neutralizes_filters_from_included_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    await _init_repo(workspace)
    tracked = workspace / "hello.txt"
    tracked.write_text("old\n", encoding="utf-8")
    await _git(workspace, "add", "hello.txt")
    await _git(workspace, "commit", "-qm", "initial")

    marker = tmp_path / "included-filter-ran"
    extension = tmp_path / "included-filter.sh"
    extension.write_text(
        f"#!/bin/sh\nprintf ran >> {marker}\ncat\n",
        encoding="utf-8",
    )
    extension.chmod(0o755)
    included = home / "ash-filter-include.conf"
    included.write_text(
        "[filter \"included.driver\"]\n"
        f"    clean = {extension}\n"
        f"    smudge = {extension}\n",
        encoding="utf-8",
    )
    (home / ".gitconfig").write_text(
        f"[include]\n    path = {included}\n",
        encoding="utf-8",
    )
    (workspace / ".gitattributes").write_text(
        "hello.txt filter=included.driver\n",
        encoding="utf-8",
    )
    tracked.write_text("new\n", encoding="utf-8")

    status = await GitStatusTool(SafetyGuard(workspace)).run()
    diff = await GitDiffTool(SafetyGuard(workspace)).run()

    assert status.success is True
    assert diff.success is True
    assert "+new" in diff.output
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config_key", "command"),
    [
        ("core.excludesFile", ["status", "--short"]),
        ("core.attributesFile", ["status", "--short"]),
        ("diff.orderFile", ["diff", "--"]),
        ("mailmap.file", ["log", "-1", "--pretty=format:%an"]),
        ("include.path", ["status", "--short"]),
        ("includeIf.onbranch:main.path", ["status", "--short"]),
    ],
)
async def test_read_only_git_refuses_repository_host_path_indirections(
    tmp_path: Path,
    config_key: str,
    command: list[str],
) -> None:
    await _init_repo(tmp_path)
    outside = tmp_path.parent / f"outside-{config_key.replace('.', '-')}"
    outside.write_text("synthetic host data\n", encoding="utf-8")
    await _git(tmp_path, "config", config_key, str(outside))

    code, stdout, stderr = await _run_git(tmp_path, command, read_only=True)

    assert code == 126
    assert stdout == ""
    assert "untrusted repository Git config" in stderr
    assert config_key.casefold() in stderr.casefold()


@pytest.mark.asyncio
async def test_read_only_git_preserves_global_excludes_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    outside_ignore = tmp_path / "global-ignore"
    outside_ignore.write_text("global-hidden.tmp\n", encoding="utf-8")
    (home / ".gitconfig").write_text(
        f"[core]\n    excludesFile = {outside_ignore}\n",
        encoding="utf-8",
    )
    await _init_repo(workspace)
    (workspace / "global-hidden.tmp").write_text("hidden\n", encoding="utf-8")
    (workspace / "visible.tmp").write_text("visible\n", encoding="utf-8")

    code, stdout, stderr = await _run_git(
        workspace,
        ["status", "--short"],
        read_only=True,
    )

    assert code == 0, stderr
    assert "global-hidden.tmp" not in stdout
    assert "visible.tmp" in stdout


@pytest.mark.asyncio
async def test_read_only_git_pins_worktree_to_selected_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    await _init_repo(workspace)
    tracked = workspace / "tracked.txt"
    tracked.write_text("same\n", encoding="utf-8")
    await _git(workspace, "add", "tracked.txt")
    await _git(workspace, "commit", "-qm", "initial")
    (outside / "tracked.txt").write_text("outside replacement\n", encoding="utf-8")
    await _git(workspace, "config", "core.worktree", str(outside))

    code, stdout, stderr = await _run_git(
        workspace,
        ["status", "--short"],
        read_only=True,
    )

    assert code == 0, stderr
    assert stdout == ""


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
@pytest.mark.asyncio
async def test_read_only_git_log_never_runs_repository_signature_verifier(
    tmp_path: Path,
) -> None:
    await _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("signed content\n", encoding="utf-8")
    await _git(tmp_path, "add", "tracked.txt")

    tree_process = await asyncio.create_subprocess_exec(
        "git",
        "write-tree",
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
    )
    tree_stdout, _ = await tree_process.communicate()
    assert tree_process.returncode == 0
    tree = tree_stdout.decode().strip()
    commit_payload = (
        f"tree {tree}\n"
        "author Test <test@example.com> 1700000000 +0000\n"
        "committer Test <test@example.com> 1700000000 +0000\n"
        "gpgsig -----BEGIN PGP SIGNATURE-----\n"
        " fake-signature\n"
        " -----END PGP SIGNATURE-----\n"
        "\n"
        "signed-looking\n"
    ).encode()
    commit_process = await asyncio.create_subprocess_exec(
        "git",
        "hash-object",
        "-t",
        "commit",
        "-w",
        "--stdin",
        cwd=tmp_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    commit_stdout, _ = await commit_process.communicate(commit_payload)
    assert commit_process.returncode == 0
    commit = commit_stdout.decode().strip()
    await _git(tmp_path, "update-ref", "HEAD", commit)

    marker = tmp_path / "signature-verifier-ran"
    verifier = tmp_path / "fake-gpg"
    verifier.write_text(
        f"#!/bin/sh\nprintf ran >> {marker}\nexit 1\n",
        encoding="utf-8",
    )
    verifier.chmod(0o755)
    await _git(tmp_path, "config", "log.showSignature", "true")
    await _git(tmp_path, "config", "gpg.program", str(verifier))

    result = await GitLogTool(SafetyGuard(tmp_path)).run(limit=1)

    assert result.success is True
    assert "signed-looking" in result.output
    assert not marker.exists()


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
@pytest.mark.parametrize(
    ("timeout_on_apply", "expected_outcome"),
    [
        (False, ToolExecutionOutcome.COMPLETED),
        (True, ToolExecutionOutcome.UNKNOWN),
    ],
)
async def test_patch_timeout_outcome_tracks_mutation_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeout_on_apply: bool,
    expected_outcome: ToolExecutionOutcome,
) -> None:
    import ash.tools.patch as patch_module

    patch_text = """diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-old
+new
"""

    async def timeout_at_selected_stage(
        _cwd: Path,
        _patch: str,
        *,
        check: bool,
    ) -> tuple[int, str, str]:
        if check == timeout_on_apply:
            return 0, "", ""
        failure = asyncio.TimeoutError()
        failure.add_note("Process-tree cleanup failed: synthetic cleanup failure")
        raise failure

    monkeypatch.setattr(patch_module, "_git_apply", timeout_at_selected_stage)

    result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(patch=patch_text)

    assert result.success is False
    assert result.outcome is expected_outcome
    assert "timed out after 30 seconds" in (result.error or "")
    assert "process-tree cleanup failed: synthetic cleanup failure" in (
        result.error or ""
    ).casefold()
    if timeout_on_apply:
        assert "Inspect the affected files before retrying" in (result.error or "")
    else:
        assert "no files were changed" in (result.error or "")


@pytest.mark.asyncio
async def test_patch_output_limit_after_dispatch_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.tools.patch as patch_module

    async def exceed_output_limit(
        _cwd: Path,
        _patch: str,
        *,
        check: bool,
    ) -> tuple[int, str, str]:
        if check:
            return 0, "", ""
        return patch_module.PATCH_OUTPUT_LIMIT_EXIT, "partial", "output exceeded"

    monkeypatch.setattr(patch_module, "_git_apply", exceed_output_limit)
    patch_text = """diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-old
+new
"""

    result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(patch=patch_text)

    assert result.success is False
    assert result.outcome is ToolExecutionOutcome.UNKNOWN
    assert result.truncated is True
    assert "workspace may have changed" in (result.error or "")
    assert "output exceeded" in (result.error or "")


@pytest.mark.asyncio
async def test_git_inspection_reports_process_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.tools.git as git_module
    import ash.sandbox.process_utils as process_utils

    async def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        from ash.sandbox.process_utils import ProcessTreeTerminationError

        raise ProcessTreeTerminationError("synthetic cleanup failure")

    monkeypatch.setattr(process_utils, "terminate_process_tree", fail_cleanup)
    process = SimpleNamespace(pid=4321, returncode=None)
    monkeypatch.setattr(
        git_module.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    monkeypatch.setattr(
        git_module,
        "communicate_process",
        AsyncMock(side_effect=asyncio.TimeoutError),
    )
    result = await GitStatusTool(SafetyGuard(tmp_path)).run()

    assert result.success is False
    assert result.output == ""
    assert "timed out after 30 seconds" in (result.error or "")
    assert "process-tree cleanup failed: synthetic cleanup failure" in (
        result.error or ""
    )
    assert result.outcome is ToolExecutionOutcome.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["git", "patch"])
async def test_git_timeout_cleanup_survives_second_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    import ash.sandbox.process_utils as process_utils
    import ash.tools.git as git_module
    import ash.tools.patch as patch_module

    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    calls = 0

    async def fake_spawn(*_args, **_kwargs):
        return SimpleNamespace(pid=12345, returncode=None)

    async def timed_out(*_args, **_kwargs):
        raise asyncio.TimeoutError

    async def fake_terminate(_process, *, plan, grace_seconds=1.0):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        finished.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(git_module, "communicate_process", timed_out)
    monkeypatch.setattr(patch_module, "communicate_process", timed_out)
    monkeypatch.setattr(process_utils, "terminate_process_tree", fake_terminate)

    if operation == "git":
        git = shutil.which("git")
        assert git is not None
        runner = git_module._run_prepared_git_result(
            tmp_path,
            git,
            ["status"],
            {"PATH": os.environ.get("PATH", os.defpath)},
            (),
            expected_cwd_identity=None,
            sandbox_manager=None,
        )
    else:
        runner = patch_module._git_apply(tmp_path, "", check=True)
    task = asyncio.create_task(runner)
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.05)
        assert not done, "Git subprocess cleanup stopped after another interrupt"
    finally:
        release.set()
        if not task.done():
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    assert calls == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX Git outcome regression")
@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_stage", ["update-ref", "post-commit"])
async def test_auto_commit_reports_ambiguous_timeout_after_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeout_stage: str,
) -> None:
    import ash.tools.git as git_module

    await _init_repo(tmp_path)
    target = tmp_path / "owned.txt"
    target.write_text("before\n", encoding="utf-8")
    await _git(tmp_path, "add", "owned.txt")
    await _git(tmp_path, "commit", "-qm", "initial")
    target.write_text("after\n", encoding="utf-8")
    post_commit_ran = tmp_path / "post-commit-ran"
    post_commit = tmp_path / ".git" / "hooks" / "post-commit"
    post_commit.write_text(
        f"#!/bin/sh\nprintf ran > '{post_commit_ran}'\n",
        encoding="utf-8",
    )
    post_commit.chmod(0o755)

    real_git = shutil.which("git")
    assert real_git is not None
    real_run_git_result = git_module._run_git_result

    async def report_lost_response(workspace_root, args, *run_args, **run_kwargs):
        result = await real_run_git_result(
            workspace_root, args, *run_args, **run_kwargs
        )
        mutation_completed = (
            args[:1] == ["update-ref"]
            if timeout_stage == "update-ref"
            else (
                args[:3] == ["hook", "run", "--ignore-missing"]
                and "post-commit" in args
            )
        )
        if mutation_completed:
            return git_module._GitCommandResult(
                result.returncode,
                result.stdout,
                result.stderr,
                timed_out=True,
                output_truncated=result.output_truncated,
            )
        return result

    monkeypatch.setattr(git_module, "_run_git_result", report_lost_response)

    result = await AutoCommitTool(SafetyGuard(tmp_path)).run(
        message="capture owned change",
        paths=["owned.txt"],
    )

    assert result.success is False
    assert result.outcome is ToolExecutionOutcome.UNKNOWN
    assert "inspect" in (result.error or "").lower()
    if timeout_stage == "update-ref":
        assert "update-ref" in (result.error or "")
        assert "HEAD may have changed" in (result.error or "")
    else:
        assert "post-commit hook" in (result.error or "")
        assert "side effects may be incomplete" in (result.error or "")
        assert "Commit " in result.output
        assert "created." in result.output
        assert post_commit_ran.read_text(encoding="utf-8") == "ran"
    head_subject = subprocess.run(
        [real_git, "log", "-1", "--format=%s"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head_subject == "capture owned change"


@pytest.mark.asyncio
async def test_git_runner_preserves_sandbox_timeout_signal(tmp_path: Path) -> None:
    class TimedOutSandbox:
        backend_name = "test"

        async def run(self, *_args, **_kwargs) -> SandboxResult:
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr="Command timed out after 30 seconds.",
                tier=SandboxTier(0),
                backend_name=self.backend_name,
                timed_out=True,
            )

    result = await _run_git_result(
        tmp_path,
        ["update-ref", "HEAD", "new", "old"],
        sandbox_manager=TimedOutSandbox(),  # type: ignore[arg-type]
    )

    assert result.returncode == -1
    assert result.timed_out is True
    assert result.interrupted is True


@pytest.mark.asyncio
async def test_run_git_cleans_process_tree_after_unexpected_io_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    import ash.tools.git as git_module

    class FakeProcess:
        returncode = None

    process = FakeProcess()
    cleaned = False

    @contextmanager
    def launch_context():
        yield SimpleNamespace(argv=("git", "status"), pass_fds=(), cwd=str(tmp_path))

    async def spawn(*args, **kwargs):
        del args, kwargs
        return process

    async def fail_communicate(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("git stream failed")

    async def cleanup(target, *, plan=None, grace_seconds=1.0):
        nonlocal cleaned
        del plan, grace_seconds
        assert target is process
        cleaned = True
        return None, False

    monkeypatch.setattr(
        git_module,
        "prepare_scoped_process_launch",
        lambda *args, **kwargs: launch_context(),
    )
    monkeypatch.setattr(
        git_module,
        "prepare_process_tree",
        lambda *args, **kwargs: SimpleNamespace(spawn_options={}),
    )
    monkeypatch.setattr(git_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(git_module, "communicate_process", fail_communicate)
    monkeypatch.setattr(
        git_module,
        "settle_process_tree_after_cancellation",
        cleanup,
    )

    with pytest.raises(RuntimeError, match="git stream failed"):
        await git_module._run_prepared_git(
            tmp_path,
            "git",
            ("status",),
            {},
            (),
            expected_cwd_identity=None,
            sandbox_manager=None,
        )

    assert cleaned is True


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
    metadata = os.stat(tmp_path)
    assert manager.run.await_args.kwargs["expected_cwd_identity"] == (
        metadata.st_dev,
        metadata.st_ino,
    )
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
async def test_read_only_git_refuses_workspace_swap_after_filter_discovery(
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
    real_run_prepared = git_module._run_prepared_git
    calls = 0

    async def run_then_swap(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = await real_run_prepared(*args, **kwargs)
        if calls == 1:
            workspace.rename(saved)
            replacement.rename(workspace)
        return result

    monkeypatch.setattr(git_module, "_run_prepared_git", run_then_swap)

    try:
        code, stdout, stderr = await _run_git(
            workspace,
            ["status", "--short"],
            read_only=True,
        )
    finally:
        if workspace.exists() and not replacement.exists():
            workspace.rename(replacement)
        if saved.exists() and not workspace.exists():
            saved.rename(workspace)

    assert calls == 2
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


@pytest.mark.asyncio
async def test_git_apply_cleans_process_tree_after_unexpected_io_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    import ash.tools.patch as patch_module

    class FakeProcess:
        returncode = None

    process = FakeProcess()
    cleaned = False

    @contextmanager
    def launch_context():
        yield SimpleNamespace(
            argv=("git", "apply", "-"),
            pass_fds=(),
            cwd=str(tmp_path),
        )

    async def spawn(*args, **kwargs):
        del args, kwargs
        return process

    async def fail_communicate(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("git apply stream failed")

    async def cleanup(target, *, plan=None, grace_seconds=1.0):
        nonlocal cleaned
        del plan, grace_seconds
        assert target is process
        cleaned = True
        return None, False

    monkeypatch.setattr(
        patch_module,
        "resolve_host_executable",
        lambda *args, **kwargs: "git",
    )
    monkeypatch.setattr(
        patch_module,
        "prepare_scoped_process_launch",
        lambda *args, **kwargs: launch_context(),
    )
    monkeypatch.setattr(
        patch_module,
        "prepare_process_tree",
        lambda *args, **kwargs: SimpleNamespace(spawn_options={}),
    )
    monkeypatch.setattr(patch_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(patch_module, "communicate_process", fail_communicate)
    monkeypatch.setattr(
        patch_module,
        "settle_process_tree_after_cancellation",
        cleanup,
    )

    patch_text = """diff --git a/x.txt b/x.txt
new file mode 100644
--- /dev/null
+++ b/x.txt
@@ -0,0 +1 @@
+x
"""
    with pytest.raises(RuntimeError, match="git apply stream failed"):
        await _git_apply(tmp_path, patch_text, check=False)

    assert cleaned is True


@pytest.mark.asyncio
async def test_git_apply_scrubs_parent_git_and_provider_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.tools.patch as patch_module

    monkeypatch.setenv("GIT_DIR", "/tmp/outside-git-dir")
    monkeypatch.setenv("GIT_WORK_TREE", "/tmp/outside-worktree")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/tmp/outside-gitconfig")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-provider-secret")
    process = SimpleNamespace(returncode=0)
    create = AsyncMock(return_value=process)
    monkeypatch.setattr(patch_module.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(
        patch_module,
        "communicate_process",
        AsyncMock(return_value=(b"", b"")),
    )
    patch_text = """diff --git a/marker.txt b/marker.txt
new file mode 100644
--- /dev/null
+++ b/marker.txt
@@ -0,0 +1 @@
+safe
"""

    code, stdout, stderr = await _git_apply(tmp_path, patch_text, check=True)

    assert (code, stdout, stderr) == (0, "", "")
    call = create.await_args
    assert call is not None
    argv = tuple(str(item) for item in call.args)
    environment = call.kwargs["env"]
    assert "--work-tree=." in argv
    assert "--no-pager" in argv
    assert "GIT_DIR" not in environment
    assert "GIT_WORK_TREE" not in environment
    assert "GIT_CONFIG_GLOBAL" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


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
    original_run_git = git_tools._run_git_result
    changed = False

    async def mutate_before_add(cwd, args, *positional, **kwargs):
        nonlocal changed
        if list(args[:2]) == ["add", "--"] and not changed:
            changed = True
            target.write_text("late user edit\n")
        return await original_run_git(cwd, args, *positional, **kwargs)

    monkeypatch.setattr(git_tools, "_run_git_result", mutate_before_add)
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
    original_run_git = git_tools._run_git_result
    moved = False

    async def move_head_before_update(cwd, args, *positional, **kwargs):
        nonlocal moved
        if list(args[:2]) == ["update-ref", "HEAD"] and not moved:
            moved = True
            await _git(tmp_path, "commit", "--allow-empty", "-qm", "concurrent user")
        return await original_run_git(cwd, args, *positional, **kwargs)

    monkeypatch.setattr(git_tools, "_run_git_result", move_head_before_update)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("bad_context", ["stale", "ambiguous"])
async def test_model_patch_names_failed_file_and_hunk_without_mutation(
    tmp_path: Path, dry_run: bool, bad_context: str,
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("first = 1\n")
    original = "start = 1\nvalue = 2\nvalue = 2\n"
    second.write_text(original)
    missing = "not_present = 3" if bad_context == "stale" else "value = 2"
    result = await ApplyPatchTool(SafetyGuard(tmp_path)).run(
        patch=("*** Begin Patch\n*** Update File: first.py\n@@\n"
               "-first = 1\n+first = 10\n*** Update File: second.py\n@@\n"
               f"-start = 1\n+start = 10\n@@\n-{missing}\n+value = 20\n*** End Patch"),
        dry_run=dry_run,
    )
    assert not result.success
    error = result.error or ""
    assert "second.py" in error and "hunk 2" in error
    assert "no files changed" in error.lower()
    assert ("not found" if bad_context == "stale" else "ambiguous") in error.lower()
    assert missing not in error
    assert first.read_text() == "first = 1\n"
    assert second.read_text() == original


@pytest.mark.asyncio
async def test_precision_edit_journeys_preserve_exact_source_and_recover_safely(
    tmp_path: Path,
) -> None:
    import hashlib
    import re
    import shlex
    import stat
    import sys

    from ash.tools.command import RunCommandTool
    from ash.tools.filesystem import ReadFileTool, ReplaceFileContentTool

    guard = SafetyGuard(tmp_path)
    reader = ReadFileTool(guard)
    replacer = ReplaceFileContentTool(guard)
    patcher = ApplyPatchTool(guard)
    runner = RunCommandTool(guard)

    repeated_path = tmp_path / "repeated.py"
    repeated_before = (
        'def first():\r\n    return "café"\r\n\r\n'
        'def second():\r\n    return "café"\r\n'
    ).encode("utf-8")
    repeated_path.write_bytes(repeated_before)

    repeated_read = await reader.run(file_path="repeated.py")
    assert repeated_read.success
    repeated_mode = stat.S_IMODE(repeated_path.stat().st_mode)
    repeated_digest_match = re.search(r"sha256=([0-9a-f]{64})", repeated_read.output)
    assert repeated_digest_match is not None
    repeated_digest = repeated_digest_match.group(1)
    assert "2:     return \"café\"" in repeated_read.output
    assert "5:     return \"café\"" in repeated_read.output

    ambiguous = await replacer.run(
        file_path="repeated.py",
        target_content='    return "café"',
        replacement_content='    return "taxed"',
        expected_sha256=repeated_digest,
    )
    assert not ambiguous.success
    assert "multiple full-line blocks" in (ambiguous.error or "")
    assert "Specify both start_line and end_line" in (ambiguous.error or "")
    assert repeated_path.read_bytes() == repeated_before

    precise = await replacer.run(
        file_path="repeated.py",
        start_line=2,
        end_line=2,
        target_content='    return "café"',
        replacement_content='    return "taxed"',
        expected_sha256=repeated_digest,
    )
    repeated_after = (
        'def first():\r\n    return "taxed"\r\n\r\n'
        'def second():\r\n    return "café"\r\n'
    ).encode("utf-8")
    assert precise.success, precise.error
    assert repeated_path.read_bytes() == repeated_after
    assert stat.S_IMODE(repeated_path.stat().st_mode) == repeated_mode
    assert hashlib.sha256(repeated_before).hexdigest() != hashlib.sha256(
        repeated_after
    ).hexdigest()
    repeated_script = (
        "from pathlib import Path; scope = {}; source = Path('repeated.py').read_text(); "
        "exec(compile(source, 'repeated.py', 'exec'), scope); "
        "assert scope['first']() == 'taxed'; assert scope['second']() == 'café'"
    )
    repeated_check = await runner.run(
        command_line=f"{shlex.quote(sys.executable)} -c {shlex.quote(repeated_script)}",
        cwd=".",
    )
    assert repeated_check.success, repeated_check.error

    shifted_path = tmp_path / "shifted.py"
    shifted_before = 'def render():\n\tlabel = "café"\n\treturn label\n'.encode(
        "utf-8"
    )
    shifted_path.write_bytes(shifted_before)
    shifted_read = await reader.run(file_path="shifted.py")
    assert shifted_read.success
    assert "3: \treturn label" in shifted_read.output
    shifted_mode = stat.S_IMODE(shifted_path.stat().st_mode)
    shifted_digest_match = re.search(r"sha256=([0-9a-f]{64})", shifted_read.output)
    assert shifted_digest_match is not None
    shifted_digest = shifted_digest_match.group(1)

    external_source = b"# external editor inserted this line\n" + shifted_before
    shifted_path.write_bytes(external_source)
    stale = await replacer.run(
        file_path="shifted.py",
        start_line=3,
        end_line=3,
        target_content="\treturn label",
        replacement_content="\treturn label + '!'",
        expected_sha256=shifted_digest,
    )
    assert not stale.success
    assert "File changed since it was read" in (stale.error or "")
    assert shifted_path.read_bytes() == external_source

    refreshed = await reader.run(file_path="shifted.py")
    assert refreshed.success
    assert "4: \treturn label" in refreshed.output
    refreshed_digest_match = re.search(r"sha256=([0-9a-f]{64})", refreshed.output)
    assert refreshed_digest_match is not None
    refreshed_digest = refreshed_digest_match.group(1)
    assert refreshed_digest != shifted_digest
    model_patch = (
        "*** Begin Patch\n*** Update File: shifted.py\n@@ def render():\n"
        '-\treturn label\n+\treturn label + "✓"\n*** End Patch'
    )
    corrected = await patcher.run(patch=model_patch)
    shifted_after = (
        b"# external editor inserted this line\n"
        b"def render():\n\tlabel = \"caf\xc3\xa9\"\n\treturn label + \"\xe2\x9c\x93\"\n"
    )
    assert corrected.success, corrected.error
    assert shifted_path.read_bytes() == shifted_after
    assert stat.S_IMODE(shifted_path.stat().st_mode) == shifted_mode
    assert hashlib.sha256(external_source).hexdigest() != hashlib.sha256(
        shifted_after
    ).hexdigest()
    shifted_script = (
        "from pathlib import Path; scope = {}; source = Path('shifted.py').read_text(); "
        "exec(compile(source, 'shifted.py', 'exec'), scope); "
        "assert scope['render']() == 'café✓'"
    )
    shifted_check = await runner.run(
        command_line=f"{shlex.quote(sys.executable)} -c {shlex.quote(shifted_script)}",
        cwd=".",
    )
    assert shifted_check.success, shifted_check.error


@pytest.mark.asyncio
async def test_model_patch_hunk_errors_report_bounded_verified_recovery_info(
    tmp_path: Path,
) -> None:
    import hashlib

    stale_path = tmp_path / "stale.py"
    stale_source = (
        "def render(item):\n"
        "    return item.name\n"
        "# unrelated diagnostic marker\n"
    )
    stale_path.write_text(stale_source, encoding="utf-8")
    stale_before = stale_path.read_bytes()
    stale = await ApplyPatchTool(SafetyGuard(tmp_path)).run(
        patch=(
            "*** Begin Patch\n*** Update File: stale.py\n@@\n"
            " def render(item):\n-    return item.label\n"
            "+    return item.name.upper()\n*** End Patch"
        )
    )
    stale_error = stale.error or ""
    assert not stale.success
    assert stale.outcome is ToolExecutionOutcome.COMPLETED
    assert "stale.py: hunk 1" in stale_error
    assert f"current_sha256={hashlib.sha256(stale_before).hexdigest()}" in stale_error
    assert "current_file_lines=3" in stale_error
    assert "verified_hunk_line_refs=hunk:1->file:1" in stale_error
    assert "Read stale.py and rebuild this hunk" in stale_error
    assert "Do not retry the unchanged patch" in stale_error
    assert "unrelated diagnostic marker" not in stale_error
    assert len(stale_error.encode("utf-8")) < 800
    assert stale_path.read_bytes() == stale_before

    repeated_path = tmp_path / "repeated.py"
    repeated_source = (
        "def first():\n    return 1\n\n"
        "def second():\n    return 1\n"
    )
    repeated_path.write_text(repeated_source, encoding="utf-8")
    repeated_before = repeated_path.read_bytes()
    repeated = await ApplyPatchTool(SafetyGuard(tmp_path)).run(
        patch=(
            "*** Begin Patch\n*** Update File: repeated.py\n@@\n"
            "-    return 1\n+    return 2\n*** End Patch"
        )
    )
    repeated_error = repeated.error or ""
    assert not repeated.success
    assert repeated.outcome is ToolExecutionOutcome.COMPLETED
    assert "repeated.py: hunk 1" in repeated_error
    assert "verified_exact_hunk_matches=2" in repeated_error
    assert "exact_match_start_lines=2,5" in repeated_error
    assert "do not choose among matches automatically" in repeated_error
    assert "def first" not in repeated_error
    assert "def second" not in repeated_error
    assert len(repeated_error.encode("utf-8")) < 800
    assert repeated_path.read_bytes() == repeated_before

    many_repeated = tmp_path / "many-repeated.py"
    many_repeated_source = "".join("value = 1\n" for _ in range(12))
    many_repeated.write_text(many_repeated_source, encoding="utf-8")
    many_repeated_before = many_repeated.read_bytes()
    many_ambiguous = await ApplyPatchTool(SafetyGuard(tmp_path)).run(
        patch=(
            "*** Begin Patch\n*** Update File: many-repeated.py\n@@\n"
            "-value = 1\n+value = 2\n*** End Patch"
        )
    )
    many_error = many_ambiguous.error or ""
    assert not many_ambiguous.success
    assert "verified_exact_hunk_matches=12" in many_error
    assert "exact_match_start_lines=1,2,3,4,+8 more" in many_error
    assert len(many_error.encode("utf-8")) < 800
    assert many_repeated.read_bytes() == many_repeated_before


@pytest.mark.asyncio
async def test_coding_loop_repairs_stale_model_patch_from_bounded_feedback(
    tmp_path: Path,
) -> None:
    import hashlib
    import io

    from ash.core.loop import AshLoop
    from ash.core.session import SessionStore
    from ash.providers.base import ProviderABC, StreamChunk
    from ash.providers.capabilities import ProviderCapabilities
    from ash.tools.filesystem import ReadFileTool
    from ash.ui.headless import HeadlessUI

    target = tmp_path / "module.py"
    current_source = "def render(item):\n    return item.name\n"
    target.write_text(current_source, encoding="utf-8")
    untouched = tmp_path / "untouched.txt"
    untouched.write_text("sentinel\n", encoding="utf-8")
    untouched_before = untouched.read_bytes()
    current_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    stale_patch = (
        "*** Begin Patch\n*** Update File: module.py\n@@\n"
        " def render(item):\n-    return item.label\n"
        "+    return item.name.upper()\n*** End Patch"
    )
    corrected_patch = (
        "*** Begin Patch\n*** Update File: module.py\n@@\n"
        " def render(item):\n-    return item.name\n"
        "+    return item.name.upper()\n*** End Patch"
    )

    class RepairFromBoundedPatchFeedback(ProviderABC):
        model_name = "bounded-patch-repair"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.calls = 0

        def count_tokens(self, text):
            return max(1, len(str(text)) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            del temperature, tools
            self.calls += 1
            history = repr(messages)
            if self.calls == 1:
                call = {
                    "id": "stale-patch",
                    "name": "apply_patch",
                    "arguments": {"patch": stale_patch},
                }
            elif self.calls == 2:
                assert "current_sha256=" + current_digest in history
                assert "Read module.py and rebuild this hunk" in history
                assert "Do not retry the unchanged patch" in history
                assert target.read_text(encoding="utf-8") == current_source
                call = {
                    "id": "refresh-module",
                    "name": "read_file",
                    "arguments": {"file_path": "module.py"},
                }
            elif self.calls == 3:
                assert current_digest in history
                assert "2:     return item.name" in history
                call = {
                    "id": "repair-module",
                    "name": "apply_patch",
                    "arguments": {"patch": corrected_patch},
                }
            else:
                assert self.calls == 4
                yield StreamChunk(content="Module safely repaired", is_done=True)
                return
            yield StreamChunk(is_done=True, native_tool_calls=[call])

    async def approve(_name: str, _arguments: dict[str, object]) -> bool:
        return True

    guard = SafetyGuard(tmp_path)
    store = SessionStore(tmp_path / "patch-repair-loop.db")
    provider = RepairFromBoundedPatchFeedback()
    loop = AshLoop(
        store,
        provider,
        guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path,
        tools={
            "apply_patch": ApplyPatchTool(guard),
            "read_file": ReadFileTool(guard),
        },
        on_tool_approval=approve,
    )
    try:
        session = await loop.start_session()
        assert await loop.run_turn("Repair the module safely") == "Module safely repaired"
        calls = store.load_session(session.session_id).tool_calls
        assert [call.tool_name for call in calls] == [
            "apply_patch",
            "read_file",
            "apply_patch",
        ]
        assert calls[0].success is False
        assert "Do not retry the unchanged patch" in (calls[0].error or "")
        assert "unrelated diagnostic marker" not in (calls[0].error or "")
        assert calls[1].success is True
        assert calls[2].success is True
        assert provider.calls == 4
        assert target.read_text(encoding="utf-8") == (
            "def render(item):\n    return item.name.upper()\n"
        )
        assert untouched.read_bytes() == untouched_before
        assert store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()




@pytest.mark.asyncio
async def test_model_patch_feedback_uses_original_header_for_long_path(
    tmp_path: Path,
) -> None:
    from ash.tools.patch import ApplyPatchTool

    guard = SafetyGuard(tmp_path)
    path_parts = ("directory_" + "x" * 90,) * 3
    relative_path = Path(*path_parts) / "target.py"
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True)
    original = b"def render():\n    return 1\n"
    target.write_bytes(original)
    result = await ApplyPatchTool(guard).run(
        patch=(
            "*** Begin Patch\n"
            f"*** Update File: {relative_path.as_posix()}\n@@\n"
            " def render():\n-    return 2\n+    return 3\n"
            "*** End Patch"
        )
    )

    error = result.error or ""
    assert not result.success
    assert "path display truncated" in error
    assert "Read the exact path from the failed Update File header" in error
    assert f"Read {error.split(': hunk 1:', 1)[0]}" not in error
    assert len(error.encode("utf-8")) < 800
    assert target.read_bytes() == original
