import asyncio
import codecs
import hashlib
import shlex
import sys
from pathlib import Path

import pytest

from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.tools.command import (
    MAX_COMMAND_CWD_CHARS,
    MAX_COMMAND_INPUT_CHARS,
    RunCommandArgs,
    RunCommandTool,
    decode_stream,
    quote_powershell_literal_path,
)
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
