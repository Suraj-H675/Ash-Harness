import hashlib
import shlex
import sys
from pathlib import Path

import pytest

from safety.guard import SafetyGuard, SafetyViolation
from tools.command import RunCommandTool, decode_stream, quote_powershell_literal_path
from tools.filesystem import (
    BINARY_FILE_ERROR,
    EXISTS_ERROR,
    ReadFileTool,
    ReplaceFileContentTool,
    ReplaceFileEditsTool,
    WholeEditTool,
    WriteFileTool,
)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def guard(project_root: Path) -> SafetyGuard:
    return SafetyGuard(project_root)


@pytest.mark.asyncio
async def test_read_file_returns_numbered_line_slice(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    target = project_root / "notes.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = await ReadFileTool(guard).run(
        file_path="notes.txt",
        start_line=2,
        end_line=3,
    )

    assert result.success is True
    digest = hashlib.sha256("one\ntwo\nthree\n".encode("utf-8")).hexdigest()
    assert result.output == (
        f"[read_file metadata: path={target}; sha256={digest}; total_file_lines=3]\n"
        "2: two\n3: three"
    )
    assert result.error is None
    assert result.truncated is False


@pytest.mark.asyncio
async def test_read_file_blocks_binary_null_byte(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    target = project_root / "image.bin"
    target.write_bytes(b"ASH\x00binary")

    result = await ReadFileTool(guard).run(file_path="image.bin")

    assert result.success is False
    assert result.error == BINARY_FILE_ERROR


@pytest.mark.asyncio
async def test_read_file_truncation_reports_follow_up_range(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    target = project_root / "large.txt"
    target.write_text(
        "\n".join(f"line {index}" for index in range(1, 806)) + "\n",
        encoding="utf-8",
    )

    result = await ReadFileTool(guard).run(file_path="large.txt")

    assert result.success is True
    assert result.truncated is True
    assert "[read_file metadata:" in result.output
    assert "800: line 800" in result.output
    assert "801: line 801" not in result.output
    assert (
        "[read_file truncated: requested_lines=1-805; returned_lines=1-800; "
        "total_file_lines=805; omitted_lines=5; next_start_line=801]"
        in result.output
    )


@pytest.mark.asyncio
async def test_read_file_truncation_metadata_respects_start_line(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    target = project_root / "large.txt"
    target.write_text(
        "\n".join(f"line {index}" for index in range(1, 1001)) + "\n",
        encoding="utf-8",
    )

    result = await ReadFileTool(guard).run(
        file_path="large.txt",
        start_line=101,
        end_line=950,
    )

    assert result.success is True
    assert result.truncated is True
    assert "[read_file metadata:" in result.output
    assert "900: line 900" in result.output
    assert "901: line 901" not in result.output
    assert (
        "[read_file truncated: requested_lines=101-950; returned_lines=101-900; "
        "total_file_lines=1000; omitted_lines=50; next_start_line=901]"
        in result.output
    )


@pytest.mark.asyncio
async def test_write_file_creates_parent_directories_and_respects_overwrite(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    tool = WriteFileTool(guard)

    created = await tool.run(file_path="src/app.py", content="print('ok')\n")
    blocked = await tool.run(file_path="src/app.py", content="print('again')\n")
    overwritten = await tool.run(
        file_path="src/app.py",
        content="print('again')\n",
        overwrite=True,
    )

    assert created.success is True
    assert blocked.success is False
    assert blocked.error == EXISTS_ERROR
    assert overwritten.success is True
    assert (project_root / "src" / "app.py").read_text(
        encoding="utf-8"
    ) == "print('again')\n"
    assert list((project_root / "src").glob(".app.py.*.tmp")) == []


@pytest.mark.asyncio
async def test_write_file_atomic_write_preserves_exact_newlines(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    target = project_root / "script.txt"
    content = "one\r\ntwo\r\n"

    result = await WriteFileTool(guard).run(
        file_path="script.txt",
        content=content,
        overwrite=True,
    )

    assert result.success is True
    assert target.read_bytes() == content.encode("utf-8")
    assert list(project_root.glob(".script.txt.*.tmp")) == []


@pytest.mark.asyncio
async def test_write_file_blocks_paths_outside_project(
    guard: SafetyGuard,
) -> None:
    with pytest.raises(SafetyViolation):
        await WriteFileTool(guard).run(file_path="../outside.txt", content="nope")


@pytest.mark.asyncio
async def test_replace_file_content_confined_to_line_bounds_and_normalizes_crlf(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    target = project_root / "doc.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")

    result = await ReplaceFileContentTool(guard).run(
        file_path="doc.txt",
        start_line=2,
        end_line=2,
        target_content="two\r\n",
        replacement_content="TWO",
    )

    assert result.success is True
    assert target.read_text(encoding="utf-8") == "one\nTWO\nthree\n"
    assert list(target.parent.glob(".doc.txt.*.tmp")) == []


@pytest.mark.asyncio
async def test_replace_file_content_accepts_matching_expected_sha256(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    target = project_root / "doc.txt"
    original = "one\ntwo\nthree\n"
    target.write_text(original, encoding="utf-8")
    expected_sha256 = hashlib.sha256(original.encode("utf-8")).hexdigest()

    result = await ReplaceFileContentTool(guard).run(
        file_path="doc.txt",
        start_line=2,
        end_line=2,
        target_content="two",
        replacement_content="TWO",
        expected_sha256=expected_sha256,
    )

    assert result.success is True
    assert target.read_text(encoding="utf-8") == "one\nTWO\nthree\n"


@pytest.mark.asyncio
async def test_replace_file_content_rejects_stale_expected_sha256(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    target = project_root / "doc.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = await ReplaceFileContentTool(guard).run(
        file_path="doc.txt",
        start_line=2,
        end_line=2,
        target_content="two",
        replacement_content="TWO",
        expected_sha256="0" * 64,
    )

    assert result.success is False
    assert "File changed since it was read" in (result.error or "")
    assert "actual_sha256=" in (result.error or "")
    assert target.read_text(encoding="utf-8") == "one\ntwo\nthree\n"


@pytest.mark.asyncio
async def test_replace_file_edits_applies_multiple_ranges_atomically(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    target = project_root / "doc.txt"
    target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    result = await ReplaceFileEditsTool(guard).run(
        file_path="doc.txt",
        edits=[
            {
                "start_line": 1,
                "end_line": 1,
                "target_content": "one",
                "replacement_content": "ONE",
            },
            {
                "start_line": 3,
                "end_line": 4,
                "target_content": "three\nfour",
                "replacement_content": "THREE\nFOUR",
            },
        ],
    )

    assert result.success is True
    assert target.read_text(encoding="utf-8") == "ONE\ntwo\nTHREE\nFOUR\n"


@pytest.mark.asyncio
async def test_replace_file_edits_rejects_mismatch_without_partial_write(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    target = project_root / "doc.txt"
    original = "one\ntwo\nthree\n"
    target.write_text(original, encoding="utf-8")

    result = await ReplaceFileEditsTool(guard).run(
        file_path="doc.txt",
        edits=[
            {
                "start_line": 1,
                "end_line": 1,
                "target_content": "one",
                "replacement_content": "ONE",
            },
            {
                "start_line": 3,
                "end_line": 3,
                "target_content": "wrong",
                "replacement_content": "THREE",
            },
        ],
    )

    assert result.success is False
    assert "edit 2 target_content does not match" in (result.error or "")
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_replace_file_edits_rejects_overlapping_ranges(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    target = project_root / "doc.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = await ReplaceFileEditsTool(guard).run(
        file_path="doc.txt",
        edits=[
            {
                "start_line": 1,
                "end_line": 2,
                "target_content": "one\ntwo",
                "replacement_content": "ONE\nTWO",
            },
            {
                "start_line": 2,
                "end_line": 3,
                "target_content": "two\nthree",
                "replacement_content": "TWO\nTHREE",
            },
        ],
    )

    assert result.success is False
    assert "overlaps an earlier edit" in (result.error or "")
    assert target.read_text(encoding="utf-8") == "one\ntwo\nthree\n"


@pytest.mark.asyncio
async def test_replace_file_content_does_not_search_outside_requested_bounds(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    target = project_root / "doc.txt"
    target.write_text("target\nmiddle\ntarget\n", encoding="utf-8")

    result = await ReplaceFileContentTool(guard).run(
        file_path="doc.txt",
        start_line=2,
        end_line=2,
        target_content="target",
        replacement_content="changed",
    )

    assert result.success is False
    assert "target_content does not match" in (result.error or "")
    assert target.read_text(encoding="utf-8") == "target\nmiddle\ntarget\n"


@pytest.mark.asyncio
async def test_run_command_executes_with_scoped_cwd(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    marker = project_root / "marker.txt"
    marker.write_text("hello", encoding="utf-8")
    command = (
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote('from pathlib import Path; print(Path("marker.txt").read_text())')}"
    )

    result = await RunCommandTool(guard).run(command_line=command, cwd=".")

    assert result.success is True
    assert result.output.strip() == "hello"
    assert result.error is None


@pytest.mark.asyncio
async def test_run_command_defaults_to_project_root_cwd(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    marker = project_root / "marker.txt"
    marker.write_text("from-root", encoding="utf-8")
    command = (
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote('from pathlib import Path; print(Path("marker.txt").read_text())')}"
    )

    result = await RunCommandTool(guard).run(command_line=command)

    assert result.success is True
    assert result.output.strip() == "from-root"


@pytest.mark.asyncio
async def test_run_command_scrubs_secret_environment(
    project_root: Path,
    guard: SafetyGuard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "super-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    script = (
        "import os; "
        "print(os.getenv('SECRET_TOKEN', 'missing')); "
        "print(os.getenv('ANTHROPIC_API_KEY', 'missing')); "
        "print(bool(os.getenv('PATH'))); "
        "print(os.getenv('ASH_WORKSPACE_ROOT', ''))"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    result = await RunCommandTool(guard).run(command_line=command)

    assert result.success is True
    lines = result.output.splitlines()
    assert lines[0] == "missing"
    assert lines[1] == "missing"
    assert lines[2] == "True"
    assert lines[3] == str(project_root)


@pytest.mark.asyncio
async def test_run_command_enforces_timeout(guard: SafetyGuard) -> None:
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(2)')}"
    )

    result = await RunCommandTool(guard).run(command_line=command, timeout_seconds=1)

    assert result.success is False
    assert result.error == "Error: Command timed out after 1 seconds."


@pytest.mark.asyncio
async def test_run_command_blocks_unsafe_commands(guard: SafetyGuard) -> None:
    with pytest.raises(SafetyViolation):
        await RunCommandTool(guard).run(command_line="rm -rf /")


@pytest.mark.asyncio
async def test_run_command_requires_literal_path_for_windows_file_cmdlets(
    guard: SafetyGuard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ash.tools.command.platform.system", lambda: "Windows")

    with pytest.raises(SafetyViolation, match="-LiteralPath"):
        await RunCommandTool(guard).run(
            command_line="Get-Content 'C:\\Program Files (x86)\\app.txt'"
        )


def test_decode_stream_falls_back_to_cp1252() -> None:
    assert decode_stream(b"\x93quoted\x94") == "\u201cquoted\u201d"


def test_quote_powershell_literal_path_escapes_single_quotes() -> None:
    assert quote_powershell_literal_path("C:\\Users\\O'Brien\\file.txt") == (
        "-LiteralPath 'C:\\Users\\O''Brien\\file.txt'"
    )


@pytest.mark.asyncio
async def test_whole_edit_tool(tmp_path: Path) -> None:
    test_file = tmp_path / "big.py"
    test_file.write_text("old content")

    guard = SafetyGuard(project_root=tmp_path)
    tool = WholeEditTool(guard)
    result = await tool.run(
        file_path=str(test_file),
        content="new content\nwith more lines\n" * 100,
        reason="major refactor",
    )

    assert result.success, f"whole_edit failed: {result.error}"
    assert test_file.read_text() == "new content\nwith more lines\n" * 100
    assert list(tmp_path.glob(".big.py.*.tmp")) == []


@pytest.mark.asyncio
async def test_whole_edit_blocks_paths_outside_project(guard: SafetyGuard) -> None:
    with pytest.raises(SafetyViolation):
        await WholeEditTool(guard).run(file_path="../outside.txt", content="nope")


def test_auto_commit_tool_is_in_default_tools():
    """auto_commit should be in the default tools dict."""
    from ash.__main__ import _build_tools
    from safety.guard import SafetyGuard
    from pathlib import Path

    guard = SafetyGuard(project_root=Path("/tmp"))
    tools = _build_tools(guard)
    assert "auto_commit" in tools, "auto_commit must be in default tools dict"
    assert tools["auto_commit"].name == "auto_commit"
    assert "replace_file_edits" in tools
    assert tools["replace_file_edits"].name == "replace_file_edits"


@pytest.mark.asyncio
async def test_auto_commit_tool_runs_successfully(tmp_path):
    """AutoCommitTool should create a commit when called with valid args."""
    from tools.git import AutoCommitTool
    from safety.guard import SafetyGuard
    import subprocess

    # Initialize a git repo
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Create a file and commit
    (tmp_path / "test.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    # Write a new file
    (tmp_path / "new.txt").write_text("world")

    guard = SafetyGuard(project_root=tmp_path)
    tool = AutoCommitTool(guard)
    result = await tool.run(message="add new file", paths=["new.txt"])

    assert result.success, f"auto_commit failed: {result.error}"
    assert "commit" in result.output.lower() or "create" in result.output.lower()
