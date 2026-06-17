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
    assert result.output == "2: two\n3: three"
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
    assert (project_root / "src" / "app.py").read_text(encoding="utf-8") == "print('again')\n"


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
        f"{shlex.quote('from pathlib import Path; print(Path(\"marker.txt\").read_text())')}"
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
        f"{shlex.quote('from pathlib import Path; print(Path(\"marker.txt\").read_text())')}"
    )

    result = await RunCommandTool(guard).run(command_line=command)

    assert result.success is True
    assert result.output.strip() == "from-root"


@pytest.mark.asyncio
async def test_run_command_enforces_timeout(guard: SafetyGuard) -> None:
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(2)')}"

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
        await RunCommandTool(guard).run(command_line="Get-Content 'C:\\Program Files (x86)\\app.txt'")


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


def test_auto_commit_tool_is_in_default_tools():
    """auto_commit should be in the default tools dict."""
    from __main__ import _build_tools
    from safety.guard import SafetyGuard
    from pathlib import Path

    guard = SafetyGuard(project_root=Path("/tmp"))
    tools = _build_tools(guard)
    assert "auto_commit" in tools, "auto_commit must be in default tools dict"
    assert tools["auto_commit"].name == "auto_commit"

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
        cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path, check=True, capture_output=True
    )

    # Create a file and commit
    (tmp_path / "test.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=tmp_path, check=True, capture_output=True
    )

    # Write a new file
    (tmp_path / "new.txt").write_text("world")

    guard = SafetyGuard(project_root=tmp_path)
    tool = AutoCommitTool(guard)
    result = await tool.run(message="add new file", paths=["new.txt"])

    assert result.success, f"auto_commit failed: {result.error}"
    assert "commit" in result.output.lower() or "create" in result.output.lower()
