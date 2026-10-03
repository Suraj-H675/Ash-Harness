import asyncio
import codecs
import hashlib
import os
import shlex
import sys
from pathlib import Path

import pytest

from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.tools.command import (
    MAX_COMMAND_CWD_CHARS,
    MAX_COMMAND_INPUT_CHARS,
    MAX_COMMAND_TIMEOUT_SECONDS,
    RunCommandArgs,
    RunCommandTool,
    decode_stream,
)
from ash.tools.base import ToolResult
from ash.tools.filesystem import (
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


def test_run_command_schema_rejects_oversized_command_and_cwd() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RunCommandArgs(command_line="x" * (MAX_COMMAND_INPUT_CHARS + 1))
    with pytest.raises(ValidationError):
        RunCommandArgs(command_line="echo ok", cwd="x" * (MAX_COMMAND_CWD_CHARS + 1))
    with pytest.raises(ValidationError):
        RunCommandArgs(
            command_line="echo ok",
            timeout_seconds=MAX_COMMAND_TIMEOUT_SECONDS + 1,
        )


def test_tool_result_structured_metadata_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.tools.base as base_module

    with pytest.raises(ValueError, match="at most 256 items"):
        ToolResult(success=True, output="ok", diagnostics=[{}] * 257)

    monkeypatch.setattr(base_module, "MAX_TOOL_RESULT_STRUCTURED_BYTES", 128)
    with pytest.raises(ValueError, match="structured metadata exceeds 128"):
        ToolResult(
            success=True,
            output="ok",
            citations=[{"url": "https://example.com/" + "x" * 256}],
        )

    monkeypatch.setattr(base_module, "MAX_TOOL_RESULT_IMAGE_DATA_BYTES", 8)
    with pytest.raises(ValueError, match="image data exceeds 8"):
        ToolResult(
            success=True,
            output="ok",
            image_blocks=[
                {"type": "image", "media_type": "image/png", "data": "123456789"}
            ],
        )

    monkeypatch.setattr(base_module, "MAX_TOOL_RESULT_TEXT_BYTES", 8)
    with pytest.raises(ValueError, match="tool-result text exceeds 8"):
        ToolResult(success=True, output="123456789")

    with pytest.raises(ValueError, match="structured metadata must be serializable"):
        ToolResult(
            success=True,
            output="ok",
            diagnostics=[{"value": object()}],
        )

    with pytest.raises(ValueError, match="structured metadata must be serializable"):
        ToolResult(
            success=True,
            output="ok",
            diagnostics=[{"value": float("nan")}],
        )


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
        f"[read_file metadata: path={target}; sha256={digest}; encoding=utf-8; "
        "total_file_lines=3]\n"
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
async def test_read_file_rejects_oversized_text(
    project_root: Path,
    guard: SafetyGuard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ash.tools.filesystem.MAX_TEXT_FILE_BYTES", 4)
    (project_root / "large.txt").write_bytes(b"12345")

    result = await ReadFileTool(guard).run(file_path="large.txt")

    assert result.success is False
    assert result.error == "Error: file exceeds 4 bytes"


@pytest.mark.asyncio
async def test_write_file_rejects_oversized_text(
    project_root: Path,
    guard: SafetyGuard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ash.tools.filesystem.MAX_TEXT_FILE_BYTES", 4)

    result = await WriteFileTool(guard).run(file_path="large.txt", content="12345")

    assert result.success is False
    assert result.error == "Error: content exceeds 4 bytes"
    assert not (project_root / "large.txt").exists()


@pytest.mark.asyncio
async def test_replace_file_rejects_oversized_text_without_mutating(
    project_root: Path,
    guard: SafetyGuard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ash.tools.filesystem.MAX_TEXT_FILE_BYTES", 4)
    target = project_root / "large.txt"
    target.write_bytes(b"12345")

    result = await ReplaceFileContentTool(guard).run(
        file_path="large.txt",
        start_line=1,
        end_line=1,
        target_content="12345",
        replacement_content="small",
    )

    assert result.success is False
    assert result.error == "Error: file exceeds 4 bytes"
    assert target.read_bytes() == b"12345"


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
        "total_file_lines=805; omitted_lines=5; next_start_line=801]" in result.output
    )


@pytest.mark.asyncio
async def test_run_command_extracts_bounded_structured_diagnostics(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    command = (
        "printf '%s\\n' "
        "'src/app.py:12:5: error: expected expression' "
        "'src/app.py:20: [E501] line too long' "
        "'FAILED tests/unit/test_app.py::test_app - AssertionError'"
    )

    result = await RunCommandTool(guard).run(command_line=command)

    paths = [diagnostic["path"] for diagnostic in result.diagnostics]
    assert result.success is True
    assert len(result.diagnostics) == 3
    assert "src/app.py" in paths
    assert any(diagnostic["code"] == "E501" for diagnostic in result.diagnostics)
    assert any(
        diagnostic["symbol"] == "test_app"
        and diagnostic["path"] == "tests/unit/test_app.py"
        for diagnostic in result.diagnostics
    )


@pytest.mark.skipif(os.name != "posix", reason="PTY execution is POSIX-only")
@pytest.mark.asyncio
async def test_run_command_pty_has_real_controlling_terminal(
    guard: SafetyGuard,
) -> None:
    script = (
        "import os; "
        "print('tty=' + ','.join(str(os.isatty(fd)) for fd in (0,1,2))); "
        "print('foreground=' + str(os.tcgetpgrp(0) == os.getpgrp()))"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    result = await RunCommandTool(guard).run(command_line=command, pty=True)

    assert result.success is True
    assert "tty=True,True,True" in result.output
    assert "foreground=True" in result.output


@pytest.mark.skipif(os.name != "posix", reason="PTY execution is POSIX-only")
@pytest.mark.asyncio
async def test_run_command_pty_enforces_timeout(guard: SafetyGuard) -> None:
    command = (
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote('import time; print(\"ready\", flush=True); time.sleep(30)')}"
    )

    result = await RunCommandTool(guard).run(
        command_line=command,
        timeout_seconds=1,
        pty=True,
    )

    assert result.success is False
    assert result.error == "Error: Command timed out after 1 seconds."


@pytest.mark.skipif(os.name != "posix", reason="PTY execution is POSIX-only")
@pytest.mark.asyncio
async def test_run_command_pty_enforces_output_capture_limit(
    guard: SafetyGuard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.tools.command as command_module

    monkeypatch.setattr(command_module, "MAX_COMMAND_OUTPUT_CHARS", 64)
    command = (
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote('import sys; sys.stdout.write(\"x\" * 4096); sys.stdout.flush()')}"
    )

    result = await RunCommandTool(guard).run(command_line=command, pty=True)

    assert result.truncated is True
    assert len(result.output) < 256
    assert "Process output capture limit reached" in result.output


@pytest.mark.skipif(os.name != "posix", reason="PTY execution is POSIX-only")
@pytest.mark.asyncio
async def test_run_command_pty_concurrent_spawns_complete(
    guard: SafetyGuard,
) -> None:
    script = "import os; print(os.isatty(0) and os.tcgetpgrp(0) == os.getpgrp())"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    results = await asyncio.gather(
        *(RunCommandTool(guard).run(command_line=command, pty=True) for _ in range(8))
    )

    assert all(result.success for result in results)
    assert all("True" in result.output for result in results)


@pytest.mark.skipif(os.name != "posix", reason="PTY execution is POSIX-only")
@pytest.mark.asyncio
async def test_run_command_pty_redacts_provider_key(
    guard: SafetyGuard,
) -> None:
    provider_key = "xai-" + "a" * 80
    script = f"print({provider_key!r}, flush=True)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    result = await RunCommandTool(guard).run(command_line=command, pty=True)

    assert result.success is True
    assert provider_key not in result.output
    assert "[REDACTED]" in result.output


@pytest.mark.asyncio
async def test_run_command_bounds_diagnostic_count(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    lines = [
        f"src/file-{index}.py:{index}: error: bad {index}" for index in range(60)
    ]
    quoted_lines = " ".join(f"'{line}'" for line in lines)
    command = f"printf '%s\\n' {quoted_lines}"

    result = await RunCommandTool(guard).run(command_line=command)

    assert len(result.diagnostics) == 50


@pytest.mark.asyncio
async def test_run_command_aggregates_framework_summaries(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    output = "\n".join(
        [
            "FAILED tests/unit/test_a.py::test_one - AssertionError",
            "FAILED tests/unit/test_b.py::test_two",
            "=========== 2 failed, 7 passed, 1 error in 0.12s ===========",
            "Found 3 errors in 2 files (checked 10 source files)",
            "1 fixable with the --fix option.",
        ]
    )
    command = f"cat <<'ASH_DIAGNOSTICS'\n{output}\nASH_DIAGNOSTICS"

    result = await RunCommandTool(guard).run(command_line=command)

    assert result.success is True
    assert result.diagnostic_summary == {
        "pytest_failed": 2,
        "pytest_passed": 7,
        "pytest_errors": 3,
        "mypy_error_count": 3,
        "ruff_fixable": 1,
    }


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
        "total_file_lines=1000; omitted_lines=50; next_start_line=901]" in result.output
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
async def test_read_file_reports_invalid_utf8_without_traceback(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    (project_root / "legacy.txt").write_bytes(b"valid prefix\xff")

    result = await ReadFileTool(guard).run(file_path="legacy.txt")

    assert result.success is False
    assert result.error == "Error: File is not valid UTF-8 text."


@pytest.mark.parametrize(
    ("label", "codec", "bom"),
    [
        ("utf-8-sig", "utf-8", codecs.BOM_UTF8),
        ("utf-16-le", "utf-16-le", codecs.BOM_UTF16_LE),
        ("utf-16-be", "utf-16-be", codecs.BOM_UTF16_BE),
        ("utf-32-le", "utf-32-le", codecs.BOM_UTF32_LE),
        ("utf-32-be", "utf-32-be", codecs.BOM_UTF32_BE),
    ],
)
@pytest.mark.asyncio
async def test_read_file_supports_bom_tagged_unicode(
    project_root: Path,
    guard: SafetyGuard,
    label: str,
    codec: str,
    bom: bytes,
) -> None:
    target = project_root / "unicode.txt"
    raw = bom + "alpha\nbeta\n".encode(codec)
    target.write_bytes(raw)

    result = await ReadFileTool(guard).run(file_path="unicode.txt")

    assert result.success is True
    assert f"sha256={hashlib.sha256(raw).hexdigest()}" in result.output
    assert f"encoding={label}" in result.output
    assert "1: alpha" in result.output
    assert "2: beta" in result.output


@pytest.mark.parametrize(
    ("codec", "bom"),
    [
        ("utf-8", codecs.BOM_UTF8),
        ("utf-16-le", codecs.BOM_UTF16_LE),
        ("utf-16-be", codecs.BOM_UTF16_BE),
        ("utf-32-le", codecs.BOM_UTF32_LE),
        ("utf-32-be", codecs.BOM_UTF32_BE),
    ],
)
@pytest.mark.asyncio
async def test_replace_file_content_preserves_bom_encoding(
    project_root: Path,
    guard: SafetyGuard,
    codec: str,
    bom: bytes,
) -> None:
    target = project_root / "unicode.txt"
    target.write_bytes(bom + "one\ntwo\n".encode(codec))

    result = await ReplaceFileContentTool(guard).run(
        file_path="unicode.txt",
        start_line=2,
        end_line=2,
        target_content="two",
        replacement_content="TWO",
    )

    assert result.success is True
    raw = target.read_bytes()
    assert raw.startswith(bom)
    assert raw[len(bom) :].decode(codec) == "one\nTWO\n"


@pytest.mark.asyncio
async def test_write_file_overwrite_preserves_bom_encoding(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    target = project_root / "unicode.txt"
    target.write_bytes(codecs.BOM_UTF16_BE + "old\n".encode("utf-16-be"))

    result = await WriteFileTool(guard).run(
        file_path="unicode.txt",
        content="new\n",
        overwrite=True,
    )

    assert result.success is True
    raw = target.read_bytes()
    assert raw.startswith(codecs.BOM_UTF16_BE)
    assert raw[len(codecs.BOM_UTF16_BE) :].decode("utf-16-be") == "new\n"


@pytest.mark.asyncio
async def test_write_file_blocks_paths_outside_project(
    guard: SafetyGuard,
) -> None:
    with pytest.raises(SafetyViolation):
        await WriteFileTool(guard).run(file_path="../outside.txt", content="nope")


@pytest.mark.asyncio
async def test_write_file_does_not_clobber_file_created_during_write(
    project_root: Path,
    guard: SafetyGuard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.tools import filesystem

    target = project_root / "new.txt"
    real_write = filesystem.atomic_write_scoped_bytes

    def racing_write(*args, **kwargs):
        target.write_text("created concurrently", encoding="utf-8")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(filesystem, "atomic_write_scoped_bytes", racing_write)
    result = await WriteFileTool(guard).run(file_path="new.txt", content="agent")

    assert result.success is False
    assert result.error == EXISTS_ERROR
    assert target.read_text(encoding="utf-8") == "created concurrently"


@pytest.mark.asyncio
async def test_write_file_blocks_parent_symlink_swap_race(
    project_root: Path,
    guard: SafetyGuard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = project_root / "src"
    parent.mkdir()
    outside = project_root.parent / "outside"
    outside.mkdir()
    original_validate = guard.validate_mutation_path
    calls = 0

    def racing_validate(path):
        nonlocal calls
        result = original_validate(path)
        calls += 1
        if calls == 3:
            parent.rename(project_root / "src-original")
            try:
                parent.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"Symlink creation is unavailable: {exc}")
        return result

    monkeypatch.setattr(guard, "validate_mutation_path", racing_validate)
    result = await WriteFileTool(guard).run(file_path="src/new.txt", content="agent")

    assert result.success is False
    assert not (outside / "new.txt").exists()


@pytest.mark.asyncio
async def test_edit_detects_file_change_during_atomic_write(
    project_root: Path,
    guard: SafetyGuard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.tools import filesystem

    target = project_root / "doc.txt"
    target.write_text("old\n", encoding="utf-8")
    real_write = filesystem.atomic_write_scoped_bytes

    def racing_write(*args, **kwargs):
        target.write_text("concurrent\n", encoding="utf-8")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(filesystem, "atomic_write_scoped_bytes", racing_write)
    result = await ReplaceFileContentTool(guard).run(
        file_path="doc.txt",
        start_line=1,
        end_line=1,
        target_content="old",
        replacement_content="new",
    )

    assert result.success is False
    assert "changed during edit" in (result.error or "")
    assert target.read_text(encoding="utf-8") == "concurrent\n"


@pytest.mark.asyncio
async def test_atomic_overwrite_preserves_executable_mode(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    target = project_root / "script.sh"
    target.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    target.chmod(0o755)

    result = await WriteFileTool(guard).run(
        file_path="script.sh",
        content="#!/bin/sh\nexit 0\n",
        overwrite=True,
    )

    assert result.success is True
    assert target.stat().st_mode & 0o777 == 0o755


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
    script = 'from pathlib import Path; print(Path("marker.txt").read_text())'
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

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
    script = 'from pathlib import Path; print(Path("marker.txt").read_text())'
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    result = await RunCommandTool(guard).run(command_line=command)

    assert result.success is True
    assert result.output.strip() == "from-root"


@pytest.mark.asyncio
async def test_run_command_streams_redacted_output_with_invocation_context(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    script = (
        "import sys,time; "
        "print('first', flush=True); "
        "time.sleep(0.2); "
        "print('token=supersecretvalue', file=sys.stderr, flush=True)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    tool = RunCommandTool(guard)
    events: list[dict[str, object]] = []
    tool.set_event_sink(events.append)

    with tool.event_context({"call_id": "call-1", "tool": "run_command"}):
        pending = asyncio.create_task(tool.run(command_line=command))
        for _ in range(20):
            if events:
                break
            await asyncio.sleep(0.02)
        assert pending.done() is False
        assert events[0] == {
            "call_id": "call-1",
            "tool": "run_command",
            "type": "tool.output",
            "stream": "stdout",
            "delta": "first\n",
        }
        result = await pending

    assert result.success is True
    streamed = "".join(str(event["delta"]) for event in events)
    assert "supersecretvalue" not in streamed
    assert "[REDACTED]" in streamed
    assert {event["stream"] for event in events} == {"stdout", "stderr"}


@pytest.mark.asyncio
async def test_run_command_redacts_bare_provider_keys_from_final_result(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    provider_key = "xai-" + "a" * 80
    script = (
        "import sys; "
        f"print({provider_key!r}); "
        f"print({provider_key!r}, file=sys.stderr)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    result = await RunCommandTool(guard).run(command_line=command)

    assert result.success is True
    assert provider_key not in result.output
    assert provider_key not in (result.error or "")
    assert "[REDACTED]" in result.output
    assert "[REDACTED]" in (result.error or "")


@pytest.mark.asyncio
async def test_run_command_bounds_live_output(
    guard: SafetyGuard,
) -> None:
    script = "import sys; sys.stdout.write('word ' * 25000); sys.stdout.flush()"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    tool = RunCommandTool(guard)
    events: list[dict[str, object]] = []
    tool.set_event_sink(events.append)

    result = await tool.run(command_line=command)

    streamed = "".join(str(event["delta"]) for event in events)
    assert result.truncated is True
    assert len(streamed) < 100100
    assert "Live command output truncated" in streamed


@pytest.mark.asyncio
async def test_run_command_handles_output_capture_limit_without_escaping(
    project_root: Path,
    guard: SafetyGuard,
) -> None:
    script = "import sys; sys.stdout.write('x' * 120000)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    result = await RunCommandTool(guard).run(command_line=command)

    assert result.success is True
    assert result.truncated is True
    assert "Process output capture limit reached" in result.output


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
async def test_run_command_forwards_only_explicitly_allowlisted_environment(
    guard: SafetyGuard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUILD_CHANNEL", "nightly")
    monkeypatch.setenv("UNLISTED_SECRET", "must-not-leak")
    script = (
        "import os; "
        "print(os.getenv('BUILD_CHANNEL', 'missing')); "
        "print(os.getenv('UNLISTED_SECRET', 'missing'))"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    result = await RunCommandTool(guard, environment_allowlist=["BUILD_CHANNEL"]).run(
        command_line=command
    )

    assert result.success is True
    assert result.output.splitlines() == ["nightly", "missing"]


@pytest.mark.asyncio
async def test_run_command_strips_desktop_session_authority_by_default(
    guard: SafetyGuard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop_env = {
        "DISPLAY": ":77",
        "WAYLAND_DISPLAY": "wayland-9",
        "XAUTHORITY": "/tmp/xauthority",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/session-bus",
    }
    for name, value in desktop_env.items():
        monkeypatch.setenv(name, value)
    script = (
        "import os; "
        "names=('DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS'); "
        "print('|'.join(os.getenv(name, 'missing') for name in names))"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    default_result = await RunCommandTool(guard).run(command_line=command)
    opted_in_result = await RunCommandTool(
        guard,
        environment_allowlist=desktop_env,
    ).run(command_line=command)

    assert default_result.success is True
    assert default_result.output.strip() == "missing|missing|missing|missing"
    assert opted_in_result.success is True
    assert (
        opted_in_result.output.strip()
        == ":77|wayland-9|/tmp/xauthority|unix:path=/tmp/session-bus"
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable PATH semantics")
@pytest.mark.asyncio
async def test_run_command_workspace_path_requires_explicit_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    host_bin = tmp_path / "host-bin"
    workspace.mkdir()
    host_bin.mkdir()
    workspace_helper = workspace / "helper"
    host_helper = host_bin / "helper"
    workspace_helper.write_text("#!/bin/sh\nprintf WORKSPACE\n", encoding="utf-8")
    host_helper.write_text("#!/bin/sh\nprintf HOST\n", encoding="utf-8")
    workspace_helper.chmod(0o755)
    host_helper.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(workspace), str(host_bin))))
    guard = SafetyGuard(workspace)

    default_result = await RunCommandTool(guard, project_root=workspace).run(
        command_line="helper"
    )
    opted_in_result = await RunCommandTool(
        guard,
        project_root=workspace,
        environment_allowlist=["PATH"],
    ).run(command_line="helper")

    assert default_result.success is True
    assert default_result.output == "HOST"
    assert opted_in_result.success is True
    assert opted_in_result.output == "WORKSPACE"


@pytest.mark.asyncio
async def test_run_command_enforces_timeout(guard: SafetyGuard) -> None:
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(2)')}"
    )

    result = await RunCommandTool(guard).run(command_line=command, timeout_seconds=1)

    assert result.success is False
    assert result.error == "Error: Command timed out after 1 seconds."


@pytest.mark.asyncio
async def test_run_command_cleans_process_tree_after_unexpected_io_failure(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    import ash.tools.command as command_module

    class FakeProcess:
        returncode = None

    process = FakeProcess()
    cleaned = False

    @contextmanager
    def launch_context():
        yield SimpleNamespace(
            argv=("/bin/sh", "-c", "printf ok"),
            pass_fds=(),
            cwd=str(project_root),
        )

    async def spawn(*args, **kwargs):
        del args, kwargs
        return process

    async def fail_communicate(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("command stream failed")

    async def cleanup(target, *, plan=None, grace_seconds=1.0):
        nonlocal cleaned
        del plan, grace_seconds
        assert target is process
        cleaned = True
        return None, False

    monkeypatch.setattr(
        command_module,
        "prepare_process_tree",
        lambda *args, **kwargs: SimpleNamespace(spawn_options={}),
    )
    monkeypatch.setattr(
        command_module,
        "prepare_scoped_process_launch",
        lambda *args, **kwargs: launch_context(),
    )
    monkeypatch.setattr(command_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(command_module, "communicate_process", fail_communicate)
    monkeypatch.setattr(
        command_module,
        "settle_process_tree_after_cancellation",
        cleanup,
    )
    tool = RunCommandTool(SafetyGuard(project_root), project_root=project_root)

    with pytest.raises(RuntimeError, match="command stream failed"):
        await tool._run_scoped(
            "printf ok",
            5,
            None,
            env={"PATH": os.defpath},
            stream_callback=lambda *_args: None,  # type: ignore[arg-type]
            expected_cwd_identity=None,
        )

    assert cleaned is True


@pytest.mark.asyncio
async def test_run_command_does_not_spawn_without_tree_preflight(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.tools import command as command_module
    from ash.sandbox.process_utils import ProcessTreeUnavailable

    def unavailable(*args: object, **kwargs: object) -> object:
        raise ProcessTreeUnavailable("taskkill unavailable")

    monkeypatch.setattr(command_module, "prepare_process_tree", unavailable)
    monkeypatch.setattr(
        command_module.asyncio,
        "create_subprocess_shell",
        lambda *args, **kwargs: pytest.fail("command must not launch"),
    )

    result = await RunCommandTool(SafetyGuard(project_root)).run(
        command_line="printf unsafe",
    )

    assert result.success is False
    assert "command was not started" in (result.error or "")


@pytest.mark.asyncio
async def test_run_command_blocks_unsafe_commands(guard: SafetyGuard) -> None:
    with pytest.raises(SafetyViolation):
        await RunCommandTool(guard).run(command_line="rm -rf /")


@pytest.mark.asyncio
async def test_run_command_blocks_wrapped_dynamic_executable_before_launch(
    guard: SafetyGuard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ash.tools.command.asyncio.create_subprocess_shell",
        lambda *args, **kwargs: pytest.fail("blocked command must not launch"),
    )

    with pytest.raises(SafetyViolation, match="dynamic executable expansion"):
        await RunCommandTool(guard).run(
            command_line='cmd=rm; timeout 5 env "$cmd" -rf /'
        )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_run_command_refuses_workspace_replaced_after_tool_creation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-saved"
    replacement = tmp_path / "replacement"
    workspace.mkdir()
    replacement.mkdir()
    tool = RunCommandTool(SafetyGuard(workspace), project_root=workspace)
    workspace.rename(saved)
    replacement.rename(workspace)

    result = await tool.run(command_line="printf unsafe > marker.txt")

    assert result.success is False
    assert "working directory identity changed" in (result.error or "")
    assert not (saved / "marker.txt").exists()
    assert not (workspace / "marker.txt").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_run_command_cwd_swap_cannot_escape_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = workspace / "work"
    cwd.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    saved = workspace / "work-saved"
    guard = SafetyGuard(workspace)
    real_validate_path = guard.validate_path
    swapped = False

    def validate_then_swap(path):
        nonlocal swapped
        resolved = real_validate_path(path)
        if path == "work" and not swapped:
            swapped = True
            cwd.rename(saved)
            try:
                cwd.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return resolved

    monkeypatch.setattr(guard, "validate_path", validate_then_swap)

    with pytest.raises(SafetyViolation, match="outside project scope"):
        await RunCommandTool(guard, project_root=workspace).run(
            command_line="printf safe > marker.txt",
            cwd="work",
        )

    assert swapped is True
    assert not (outside / "marker.txt").exists()
    assert not (saved / "marker.txt").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_run_command_uses_held_cwd_after_path_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox import process_utils as process_utils_module

    real_prepare = process_utils_module._prepare_posix_cwd_launch

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = workspace / "work"
    cwd.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    saved = workspace / "work-saved"
    swapped = False

    def prepare_after_swap(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            cwd.rename(saved)
            try:
                cwd.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr(
        process_utils_module,
        "_prepare_posix_cwd_launch",
        prepare_after_swap,
    )

    result = await RunCommandTool(
        SafetyGuard(workspace), project_root=workspace
    ).run(
        command_line="printf safe > marker.txt",
        cwd="work",
    )

    assert swapped is True
    assert result.success is True
    assert not (outside / "marker.txt").exists()
    assert (saved / "marker.txt").read_text(encoding="utf-8") == "safe"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor cwd")
@pytest.mark.asyncio
async def test_run_command_fails_closed_without_descriptor_cwd(
    project_root: Path,
    guard: SafetyGuard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox import process_utils as process_utils_module
    from ash.sandbox.process_utils import ProcessTreeUnavailable

    def unavailable(*args, **kwargs):
        raise ProcessTreeUnavailable("race-resistant cwd launch unavailable")

    monkeypatch.setattr(
        process_utils_module,
        "_prepare_posix_cwd_launch",
        unavailable,
    )
    monkeypatch.setattr(
        "ash.tools.command.asyncio.create_subprocess_shell",
        lambda *args, **kwargs: pytest.fail("command must not launch"),
    )

    result = await RunCommandTool(guard, project_root=project_root).run(
        command_line="printf unsafe"
    )

    assert result.success is False
    assert "race-resistant cwd launch unavailable" in (result.error or "")


def test_decode_stream_falls_back_to_cp1252() -> None:
    assert decode_stream(b"\x93quoted\x94") == "\u201cquoted\u201d"


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
    from ash.safety.guard import SafetyGuard
    from pathlib import Path

    guard = SafetyGuard(project_root=Path("/tmp"))
    tools = _build_tools(guard)
    assert "auto_commit" in tools, "auto_commit must be in default tools dict"
    assert tools["auto_commit"].name == "auto_commit"
    assert "replace_file_edits" in tools
    assert tools["replace_file_edits"].name == "replace_file_edits"


def test_default_command_tools_receive_environment_allowlist(tmp_path: Path) -> None:
    from ash.__main__ import _build_tools
    from ash.config import AshConfig

    tools = _build_tools(
        SafetyGuard(project_root=tmp_path),
        runtime_config=AshConfig(command_env_allowlist=["BUILD_CHANNEL"]),
    )

    assert tools["run_command"].environment_allowlist == ("BUILD_CHANNEL",)
    assert tools["background_process"].environment_allowlist == ("BUILD_CHANNEL",)
    assert tools["auto_commit"].environment_allowlist == ("BUILD_CHANNEL",)


def test_default_auto_commit_tool_receives_runtime_sandbox(tmp_path: Path) -> None:
    from ash.__main__ import _build_tools
    from ash.safety.guard import SafetyGuard

    manager = object()
    tools = _build_tools(
        SafetyGuard(project_root=tmp_path),
        sandbox_manager=manager,
    )

    assert tools["auto_commit"].sandbox_manager is manager


@pytest.mark.asyncio
async def test_auto_commit_tool_runs_successfully(tmp_path):
    """AutoCommitTool should create a commit when called with valid args."""
    from ash.tools.git import AutoCommitTool
    from ash.safety.guard import SafetyGuard
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
