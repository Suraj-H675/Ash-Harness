import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ash.safety.guard import SafetyGuard
from ash.tools import search as search_module
from ash.tools.search import GlobFilesTool, ListDirectoryTool, SearchTextTool


@pytest.mark.asyncio
async def test_list_directory_is_bounded(tmp_path) -> None:
    for index in range(4):
        (tmp_path / f"{index}.txt").write_text(str(index))
    result = await ListDirectoryTool(SafetyGuard(tmp_path)).run(max_results=2)
    assert result.success is True
    assert result.truncated is True
    assert "truncated after 2" in result.output


@pytest.mark.asyncio
async def test_list_directory_caps_recursive_depth(tmp_path) -> None:
    deep_file = tmp_path / "one" / "two" / "three" / "four" / "five.txt"
    deep_file.parent.mkdir(parents=True)
    deep_file.write_text("deep")

    result = await ListDirectoryTool(SafetyGuard(tmp_path)).run(
        recursive=True,
        max_results=100,
    )

    assert result.success is True
    assert "one/two/three/four/" in result.output
    assert "one/two/three/four/five.txt" not in result.output
    assert result.truncated is True
    assert "[workspace directory listing incomplete]" in result.output


@pytest.mark.asyncio
async def test_list_directory_marks_unreadable_descendants_incomplete(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "hidden.txt").write_text("hidden")
    original = search_module.list_scoped_directory

    def list_with_denial(directory, guard):
        if directory == tmp_path / "nested":
            raise PermissionError("no access to nested directory")
        return original(directory, guard)

    monkeypatch.setattr(search_module, "list_scoped_directory", list_with_denial)
    result = await ListDirectoryTool(SafetyGuard(tmp_path)).run(recursive=True)
    assert result.success is True
    assert result.truncated is True
    assert "[workspace directory listing incomplete]" in result.output
    assert "nested/hidden.txt" not in result.output


@pytest.mark.asyncio
async def test_recursive_list_directory_allows_other_tasks_to_run(tmp_path) -> None:
    for index in range(200):
        (tmp_path / f"file_{index:03}.txt").write_text("content")

    other_task_ran = asyncio.Event()

    async def other_task() -> None:
        await asyncio.sleep(0)
        other_task_ran.set()

    observer = asyncio.create_task(other_task())
    result = await ListDirectoryTool(SafetyGuard(tmp_path)).run(
        recursive=True, max_results=200
    )
    progressed_during_scan = other_task_ran.is_set()
    await observer

    assert result.success is True
    assert result.truncated is False
    assert progressed_during_scan


@pytest.mark.asyncio
async def test_list_directory_does_not_descend_into_directory_swapped_to_symlink(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    nested = workspace / "docs"
    nested.mkdir()
    (nested / "safe.txt").write_text("safe\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SECRET_FILENAME.txt").write_text("secret\n", encoding="utf-8")
    real_list_scoped_directory = search_module.list_scoped_directory
    swapped = False

    def list_then_swap(path, guard):
        nonlocal swapped
        result = real_list_scoped_directory(path, guard)
        if Path(path) == workspace and not swapped:
            swapped = True
            nested.rename(workspace / "docs-saved")
            try:
                nested.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return result

    monkeypatch.setattr(search_module, "list_scoped_directory", list_then_swap)

    result = await ListDirectoryTool(SafetyGuard(workspace)).run(
        directory_path=".",
        recursive=True,
        max_results=20,
    )

    assert swapped is True
    assert "SECRET_FILENAME.txt" not in result.output


@pytest.mark.asyncio
async def test_glob_files_returns_relative_matches(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('x')")
    (tmp_path / "README.md").write_text("readme")
    result = await GlobFilesTool(SafetyGuard(tmp_path)).run(pattern="**/*.py")
    assert result.output == "src/app.py"


@pytest.mark.asyncio
async def test_double_star_glob_includes_top_level_and_nested_files(tmp_path) -> None:
    (tmp_path / "root.py").write_text("needle\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "nested.py").write_text("needle\n")
    guard = SafetyGuard(tmp_path)

    glob_result = await GlobFilesTool(guard).run(pattern="**/*.py")
    search_result = await SearchTextTool(guard).run(
        pattern="needle", glob="**/*.py", fixed_strings=True
    )

    assert glob_result.output == "pkg/nested.py\nroot.py"
    assert glob_result.truncated is False
    assert set(search_result.output.splitlines()) == {
        "pkg/nested.py:1:needle",
        "root.py:1:needle",
    }
    assert search_result.truncated is False


@pytest.mark.asyncio
async def test_glob_files_reports_depth_limit_incomplete(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "hidden.py").write_text("needle")
    monkeypatch.setattr(search_module, "MAX_GLOB_DEPTH", 1)

    result = await GlobFilesTool(SafetyGuard(tmp_path)).run(pattern="**/*.py")
    assert result.success is True
    assert result.truncated is True
    assert result.output == "[workspace glob incomplete]"


@pytest.mark.asyncio
async def test_glob_files_marks_unreadable_file_metadata_incomplete(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "unreadable.py").write_text("content")
    original = search_module.stat_scoped_path

    def stat_with_denial(path, guard):
        if path == tmp_path / "unreadable.py":
            raise PermissionError("unable to stat file")
        return original(path, guard)

    monkeypatch.setattr(search_module, "stat_scoped_path", stat_with_denial)
    result = await GlobFilesTool(SafetyGuard(tmp_path)).run(pattern="**/*.py")
    assert result.success is True
    assert result.truncated is True
    assert result.output == "[workspace glob incomplete]"


@pytest.mark.asyncio
async def test_glob_files_bounds_workspace_scan(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("ash.tools.search.MAX_GLOB_SCAN_ENTRIES", 2)
    for index in range(3):
        (tmp_path / f"{index}.txt").write_text(str(index))

    result = await GlobFilesTool(SafetyGuard(tmp_path)).run(
        pattern="**/*.missing",
        max_results=2_000,
    )

    assert result.success is True
    assert result.truncated is True
    assert "workspace scan truncated after 2 entries" in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_type", "arguments"),
    [
        (GlobFilesTool, {"pattern": "*.absent"}),
        (SearchTextTool, {"pattern": "missing-token", "fixed_strings": True}),
    ],
    ids=["glob_files", "search_text"],
)
async def test_workspace_scans_allow_other_tasks_to_run(
    tmp_path, tool_type, arguments
) -> None:
    for index in range(200):
        (tmp_path / f"file_{index:03}.txt").write_text("ordinary text\n")

    other_task_ran = asyncio.Event()

    async def other_task() -> None:
        await asyncio.sleep(0)
        other_task_ran.set()

    observer = asyncio.create_task(other_task())
    result = await tool_type(SafetyGuard(tmp_path)).run(**arguments)
    progressed_during_scan = other_task_ran.is_set()
    await observer

    assert result.success is True
    assert result.truncated is False
    assert result.output == ""
    assert progressed_during_scan


@pytest.mark.asyncio
async def test_search_text_returns_file_and_line(tmp_path) -> None:
    (tmp_path / "app.py").write_text("first\nneedle here\n")
    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
    )
    assert result.success is True
    assert "app.py:2:needle here" in result.output


@pytest.mark.asyncio
async def test_search_text_supports_regex_and_case_insensitive_matching(
    tmp_path,
) -> None:
    (tmp_path / "app.py").write_text("first\nAlpha123\n", encoding="utf-8")

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern=r"alpha\d+",
        case_sensitive=False,
    )

    assert result.success is True
    assert result.output == "app.py:2:Alpha123"


@pytest.mark.asyncio
async def test_search_text_marks_aggregate_regex_budget_exhaustion(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "notes.txt").write_text("needle\nneedle\n", encoding="utf-8")
    monkeypatch.setattr(search_module, "MAX_SEARCH_REGEX_MATCH_SECONDS", 0.05)
    monkeypatch.setattr(search_module, "MAX_SEARCH_REGEX_TOTAL_SECONDS", 0.05)
    clock_values = iter((1.0, 1.06))
    monkeypatch.setattr(search_module, "monotonic", lambda: next(clock_values))

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(pattern="needle")

    assert result.success is True
    assert result.truncated is True
    assert result.output == (
        "notes.txt:1:needle\n"
        "[workspace search incomplete: regex time budget exhausted]"
    )


def test_search_text_catastrophic_regex_returns_incomplete_result(tmp_path) -> None:
    (tmp_path / "hostile.txt").write_text("a" * 128 + "!\n", encoding="utf-8")
    child = """
import asyncio, json, sys
from pathlib import Path
from ash.safety.guard import SafetyGuard
from ash.tools.search import SearchTextTool
result = asyncio.run(SearchTextTool(SafetyGuard(Path(sys.argv[1]))).run(pattern=r'(a|aa)+$'))
print(json.dumps({"success": result.success, "truncated": result.truncated, "output": result.output}))
"""
    source_root = str(Path(__file__).resolve().parents[2] / "src")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_root, env.get("PYTHONPATH")) if part
    )

    result = subprocess.run(
        [sys.executable, "-c", child, str(tmp_path)],
        capture_output=True,
        check=True,
        env=env,
        text=True,
        timeout=3,
    )

    payload = json.loads(result.stdout)
    assert payload["success"] is True
    assert payload["output"] in {
        "",
        "[workspace search incomplete: regex time budget exhausted]",
    }
    assert payload["truncated"] is bool(payload["output"])


@pytest.mark.asyncio
async def test_search_text_reports_per_match_timeout_without_losing_earlier_results(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "notes.txt").write_text("needle\nslow\n", encoding="utf-8")

    class TimedMatcher:
        def search(self, line: str, *, timeout: float):
            if line.startswith("slow"):
                raise TimeoutError
            return True

    monkeypatch.setattr(search_module.regex_engine, "compile", lambda *_: TimedMatcher())
    result = await SearchTextTool(SafetyGuard(tmp_path)).run(pattern="needle")

    assert result.success is True
    assert result.truncated is True
    assert result.output == (
        "notes.txt:1:needle\n"
        "[workspace search incomplete: regex time budget exhausted]"
    )


@pytest.mark.asyncio
async def test_search_text_applies_glob_filter(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "docs" / "notes.md").write_text("needle\n", encoding="utf-8")

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
        glob="**/*.py",
    )

    assert result.success is True
    assert result.output == "src/app.py:1:needle"


@pytest.mark.asyncio
async def test_search_text_fixed_strings_escape_regex_metacharacters(tmp_path) -> None:
    (tmp_path / "notes.txt").write_text(
        "value [literal]\n",
        encoding="utf-8",
    )

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="[literal]",
        fixed_strings=True,
    )

    assert result.success is True
    assert result.output == "notes.txt:1:value [literal]"


@pytest.mark.asyncio
async def test_search_text_reports_invalid_regex(tmp_path) -> None:
    (tmp_path / "notes.txt").write_text("anything\n", encoding="utf-8")

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(pattern="[")

    assert result.success is False
    assert result.error is not None
    assert result.error.startswith("Invalid regex:")


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
@pytest.mark.asyncio
async def test_search_text_does_not_execute_workspace_shadowed_ripgrep(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "workspace-rg-ran"
    fake_rg = tmp_path / "rg"
    fake_rg.write_text(
        f"#!/bin/sh\nprintf ran > {marker}\nexit 0\n",
        encoding="utf-8",
    )
    fake_rg.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    (tmp_path / "notes.txt").write_text("needle\n", encoding="utf-8")

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle", fixed_strings=True
    )

    assert result.success is True
    assert result.output == "notes.txt:1:needle"
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
@pytest.mark.asyncio
async def test_search_text_does_not_delegate_workspace_paths_to_host_ripgrep(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("needle\n", encoding="utf-8")
    host_bin = tmp_path / "host-bin"
    host_bin.mkdir()
    marker = tmp_path / "host-rg-ran"
    fake_rg = host_bin / "rg"
    fake_rg.write_text(
        f"#!/bin/sh\nprintf ran > {marker}\nexit 0\n",
        encoding="utf-8",
    )
    fake_rg.chmod(0o755)
    monkeypatch.setenv("PATH", str(host_bin))

    result = await SearchTextTool(SafetyGuard(workspace)).run(
        pattern="needle",
        fixed_strings=True,
    )

    assert result.success is True
    assert result.output == "notes.txt:1:needle"
    assert not marker.exists()


@pytest.mark.asyncio
async def test_search_text_does_not_follow_root_swapped_after_validation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    nested = workspace / "docs"
    nested.mkdir()
    (nested / "safe.txt").write_text("harmless\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.txt").write_text(
        "CWD_SECRET_NEEDLE\n",
        encoding="utf-8",
    )
    guard = SafetyGuard(workspace)
    real_validate_path = guard.validate_path
    swapped = False

    def validate_then_swap(path):
        nonlocal swapped
        resolved = real_validate_path(path)
        if path == "docs" and not swapped:
            swapped = True
            nested.rename(workspace / "docs-saved")
            try:
                nested.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return resolved

    monkeypatch.setattr(guard, "validate_path", validate_then_swap)

    result = await SearchTextTool(guard).run(
        pattern="CWD_SECRET_NEEDLE",
        directory_path="docs",
        fixed_strings=True,
    )

    assert swapped is True
    assert "CWD_SECRET_NEEDLE" not in result.output


@pytest.mark.asyncio
async def test_search_text_does_not_mark_exact_limit_truncated(tmp_path) -> None:
    (tmp_path / "ok.py").write_text("needle\n")

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
        max_results=1,
    )

    assert result.success is True
    assert result.truncated is False
    assert result.output == "ok.py:1:needle"


@pytest.mark.asyncio
async def test_search_text_fallback_does_not_read_file_swapped_to_external_symlink(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("harmless text\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("TOP_SECRET_NEEDLE\n", encoding="utf-8")
    real_read_scoped_bytes = search_module.read_scoped_bytes
    swapped = False

    def read_after_swap(path, guard, **kwargs):
        nonlocal swapped
        if Path(path) == target and not swapped:
            swapped = True
            target.unlink()
            try:
                target.symlink_to(outside)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return real_read_scoped_bytes(path, guard, **kwargs)

    monkeypatch.setattr(
        search_module,
        "read_scoped_bytes",
        read_after_swap,
    )

    result = await SearchTextTool(SafetyGuard(workspace)).run(
        pattern="TOP_SECRET_NEEDLE",
        fixed_strings=True,
    )

    assert swapped is True
    assert "TOP_SECRET_NEEDLE" not in result.output


@pytest.mark.asyncio
async def test_search_text_rejects_out_of_scope_directory(tmp_path) -> None:
    with pytest.raises(Exception, match="outside project scope"):
        await SearchTextTool(SafetyGuard(tmp_path)).run(
            pattern="x",
            directory_path="../outside",
        )


@pytest.mark.asyncio
async def test_search_text_bounds_oversized_fallback_output(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("ash.tools.search.MAX_SEARCH_CAPTURE_BYTES", 8_192)
    lines = ["needle " + "x" * 120 for _ in range(1_000)]
    (tmp_path / "large.txt").write_text("\n".join(lines))

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
        max_results=2_000,
    )

    assert result.success is True
    assert result.truncated is True
    assert "search output capture truncated" in result.output


@pytest.mark.asyncio
async def test_search_text_fallback_bounds_long_line(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("ash.tools.search.MAX_SEARCH_CAPTURE_BYTES", 8_192)
    (tmp_path / "large.txt").write_text("needle " + "x" * 200_000)

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
    )

    assert result.success is True
    assert result.truncated is True
    assert "search output capture truncated" in result.output
    assert len(result.output) < 10_000


@pytest.mark.asyncio
async def test_search_text_marks_oversized_files_incomplete(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("ash.tools.search.MAX_SEARCH_FILE_BYTES", 32)
    (tmp_path / "large.txt").write_text("needle " + "x" * 40)

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
    )

    assert result.success is True
    assert result.truncated is True
    assert "workspace search incomplete" in result.output


@pytest.mark.asyncio
async def test_search_text_matches_regex_across_long_logical_line(tmp_path) -> None:
    line = "BEGIN" + "x" * 70_000 + "END"
    (tmp_path / "long.txt").write_text(line + "\n", encoding="utf-8")

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="BEGIN.*END",
        fixed_strings=False,
    )

    assert result.success is True
    assert result.truncated is True
    assert result.output.startswith(
        "long.txt:1:" + line[: search_module.MAX_SEARCH_LINE_CHARS]
    )
    assert "[line preview truncated]" in result.output


@pytest.mark.asyncio
async def test_search_text_fixed_string_can_match_line_ending(tmp_path) -> None:
    (tmp_path / "ending.txt").write_text("needle\n", encoding="utf-8")

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle\n",
        fixed_strings=True,
    )

    assert result.success is True
    assert result.output == "ending.txt:1:needle"
    assert result.truncated is False


@pytest.mark.asyncio
async def test_search_text_reports_long_line_preview_truncation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ash.tools.search.MAX_SEARCH_LINE_CHARS", 16)
    (tmp_path / "long.txt").write_text("needle" + "x" * 40 + "\n", encoding="utf-8")

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
    )

    assert result.success is True
    assert result.truncated is True
    assert result.output == "long.txt:1:needlexxxxxxxxxx [line preview truncated]"


@pytest.mark.asyncio
async def test_search_text_marks_depth_omissions_incomplete(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ash.tools.search.MAX_SEARCH_DEPTH", 1)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "needle.txt").write_text("needle\n", encoding="utf-8")

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
    )

    assert result.success is True
    assert result.truncated is True
    assert result.output == "[workspace search incomplete]"


@pytest.mark.asyncio
async def test_search_text_marks_directory_enumeration_errors_incomplete(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "needle.txt").write_text("needle\n", encoding="utf-8")

    def fail_list(_path, _guard):
        raise PermissionError("injected directory denial")

    monkeypatch.setattr(search_module, "list_scoped_directory", fail_list)
    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
    )

    assert result.success is True
    assert result.truncated is True
    assert result.output == "[workspace search incomplete]"


@pytest.mark.asyncio
async def test_search_text_fallback_bounds_workspace_scan(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("ash.tools.search.MAX_SEARCH_SCAN_ENTRIES", 2)
    for index in range(3):
        (tmp_path / f"{index}.txt").write_text(str(index))

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
        max_results=2_000,
    )

    assert result.success is True
    assert result.truncated is True
    assert "workspace scan truncated after 2 entries" in result.output


@pytest.mark.asyncio
async def test_search_text_streams_lines(tmp_path) -> None:
    (tmp_path / "notes.txt").write_text("first\nneedle\nlast\n")

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
    )

    assert result.success is True
    assert result.output == "notes.txt:2:needle"
