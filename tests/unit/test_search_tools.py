import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ash.safety.guard import SafetyGuard
from ash.tools import search as search_module
from ash.tools.base import ToolOutputBudget
from ash.tools.filesystem import ReadFileTool
from ash.tools.search import GlobFilesTool, ListDirectoryTool, SearchTextArgs, SearchTextTool


@pytest.mark.asyncio
async def test_list_directory_is_bounded(tmp_path) -> None:
    for index in range(4):
        (tmp_path / f"{index}.txt").write_text(str(index))
    result = await ListDirectoryTool(SafetyGuard(tmp_path)).run(max_results=2)
    assert result.success is True
    assert result.truncated is True
    assert "truncated after 2" in result.output


@pytest.mark.asyncio
async def test_list_directory_caps_after_deterministic_path_order(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a_first.py", "b_second.py", "c_middle.py", "z_last.py"):
        (tmp_path / name).write_text("", encoding="utf-8")
    original = search_module.list_scoped_directory

    def reverse_entries(directory, guard):
        metadata, children = original(directory, guard)
        return metadata, list(reversed(children))

    monkeypatch.setattr(search_module, "list_scoped_directory", reverse_entries)

    result = await ListDirectoryTool(SafetyGuard(tmp_path)).run(max_results=2)

    assert result.success is True
    assert result.truncated is True
    assert result.output == "a_first.py\nb_second.py\n[truncated after 2 entries]"


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
async def test_recursive_list_dir_skips_generated_only_for_broad_discovery(tmp_path) -> None:
    objects = tmp_path / ".git" / "objects"
    objects.mkdir(parents=True)
    for n in range(220):
        (objects / f"{n:03d}").write_text("generated")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "feature.py").write_text("feature")
    (tmp_path / "tests" / "test_feature.py").write_text("test")
    listing = ListDirectoryTool(SafetyGuard(tmp_path))

    broad = await listing.run(recursive=True, max_results=200)
    assert broad.success and not broad.truncated
    assert "src/feature.py" in broad.output
    assert "tests/test_feature.py" in broad.output
    assert ".git/objects/000" not in broad.output
    assert "skipped generated directories: .git" in broad.output

    explicit = await listing.run(directory_path=".git", recursive=True, max_results=20)
    assert explicit.success and explicit.truncated
    assert ".git/objects/000" in explicit.output

    nonrecursive = await listing.run(recursive=False)
    assert nonrecursive.success
    assert ".git/" in nonrecursive.output


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
async def test_scoped_discovery_paths_can_be_read_from_workspace_root(tmp_path) -> None:
    package = tmp_path / "pkg"
    (package / "sub").mkdir(parents=True)
    (package / "app.py").write_text("needle app\n", encoding="utf-8")
    (package / "sub" / "util.py").write_text("needle util\n", encoding="utf-8")
    guard = SafetyGuard(tmp_path)
    for tool, field_name in (
        (ListDirectoryTool(guard), "directory_path"),
        (GlobFilesTool(guard), "pattern"),
        (SearchTextTool(guard), "directory_path"),
        (ReadFileTool(guard), "file_path"),
    ):
        assert (
            "project-relative"
            in tool.json_schema()["properties"][field_name]["description"].lower()
        )

    listed = await ListDirectoryTool(guard).run(directory_path="pkg")
    recursive = await ListDirectoryTool(guard).run(directory_path="pkg", recursive=True)
    globbed = await GlobFilesTool(guard).run(pattern="**/*.py", directory_path="pkg")
    scoped_glob = await GlobFilesTool(guard).run(pattern="app.py", directory_path="pkg")
    searched = await SearchTextTool(guard).run(
        pattern="needle", directory_path="pkg", fixed_strings=True
    )
    scoped_search = await SearchTextTool(guard).run(
        pattern="needle", directory_path="pkg", glob="app.py", fixed_strings=True
    )

    assert listed.output == "pkg/app.py\npkg/sub/"
    assert recursive.output == "pkg/app.py\npkg/sub/\npkg/sub/util.py"
    assert globbed.output == "pkg/app.py\npkg/sub/util.py"
    assert scoped_glob.output == "pkg/app.py"
    assert searched.output == "pkg/app.py:1:needle app\npkg/sub/util.py:1:needle util"
    assert scoped_search.output == "pkg/app.py:1:needle app"
    for path in globbed.output.splitlines():
        result = await ReadFileTool(guard).run(file_path=path)
        assert result.success is True, (path, result.error)
        assert "needle" in result.output


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
async def test_workspace_globs_respect_directory_segments_and_recursive_zero_depth(
    tmp_path,
) -> None:
    (tmp_path / "src" / "nested").mkdir(parents=True)
    for relative in ("top.py", "src/direct.py", "src/nested/deep.py"):
        target = tmp_path / relative
        target.write_text("needle\n", encoding="utf-8")
    guard = SafetyGuard(tmp_path)
    glob_tool = GlobFilesTool(guard)
    search_tool = SearchTextTool(guard)

    direct = await glob_tool.run(pattern="src/*.py")
    recursive = await glob_tool.run(pattern="src/**/*.py")
    top = await glob_tool.run(pattern="*.py")
    scoped = await glob_tool.run(pattern="*.py", directory_path="src")
    searched = await search_tool.run(pattern="needle", glob="src/**/*.py")

    assert direct.output == "src/direct.py"
    assert recursive.output == "src/direct.py\nsrc/nested/deep.py"
    assert top.output == "top.py"
    assert scoped.output == "src/direct.py"
    assert searched.output == ("src/direct.py:1:needle\nsrc/nested/deep.py:1:needle")


@pytest.mark.asyncio
async def test_workspace_globs_accept_explicit_current_directory_segments(
    tmp_path: Path,
) -> None:
    package = tmp_path / "src" / "sub"
    package.mkdir(parents=True)
    (tmp_path / "src" / "direct.py").write_text("needle\n")
    (package / "nested.py").write_text("needle\n")
    guard = SafetyGuard(tmp_path)

    found = await GlobFilesTool(guard).run(pattern="./src/./**/*.py")
    searched = await SearchTextTool(guard).run(
        pattern="needle", glob="./src/**/*.py", fixed_strings=True
    )
    parent_traversal_pattern = await GlobFilesTool(guard).run(
        pattern="./../src/**/*.py"
    )

    assert found.output == "src/direct.py\nsrc/sub/nested.py"
    assert searched.output == (
        "src/direct.py:1:needle\nsrc/sub/nested.py:1:needle"
    )
    assert parent_traversal_pattern.output == ""


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
    if tool_type is SearchTextTool:
        assert "No matches found" in result.output
    else:
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
async def test_search_text_files_only_finds_broad_unique_files_without_repeated_lines(
    tmp_path,
) -> None:
    (tmp_path / "alpha.py").write_text("header\n" + "needle\n" * 40)
    (tmp_path / "beta.py").write_text("needle first\nneedle second\n")
    (tmp_path / "gamma.py").write_text("header\nneedle here\n")
    (tmp_path / "unrelated.py").write_text("not relevant\n")
    tool = SearchTextTool(SafetyGuard(tmp_path))

    # Existing mode spends the whole match limit on just one source file.
    legacy = await tool.run(pattern="needle", fixed_strings=True, max_results=2)
    assert legacy.success and legacy.truncated
    assert legacy.output.count("alpha.py:") == 2
    assert "beta.py" not in legacy.output

    broad = await tool.run(
        pattern="needle", fixed_strings=True, max_results=2, files_only=True
    )
    assert broad.success and broad.truncated
    assert broad.output.splitlines() == [
        "alpha.py:2",
        "beta.py:1",
        "[truncated after 2 matching files]",
    ]
    complete = await tool.run(
        pattern="needle", fixed_strings=True, max_results=3, files_only=True
    )
    assert complete.success and not complete.truncated
    assert complete.output.splitlines() == [
        "alpha.py:2", "beta.py:1", "gamma.py:2",
    ]
    assert len(complete.output) < len(legacy.output)


@pytest.mark.asyncio
async def test_search_text_files_only_keeps_regex_case_glob_and_single_file_contract(
    tmp_path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("no match\nNeedle\nneedle again\n")
    (tmp_path / "src" / "b.txt").write_text("needle\n")
    (tmp_path / "src" / "c.py").write_text("NEEDLE\n")
    tool = SearchTextTool(SafetyGuard(tmp_path))
    scoped = await tool.run(
        pattern="needle", directory_path="src", glob="**/*.py",
        case_sensitive=False, files_only=True,
    )
    assert scoped.success and not scoped.truncated
    assert scoped.output.splitlines() == ["src/a.py:2", "src/c.py:1"]
    single = await tool.run(
        pattern="n[e]+dle", directory_path="src/a.py", files_only=True,
    )
    assert single.success and not single.truncated
    assert single.output == "src/a.py:3"
    invalid = await tool.run(pattern="[", files_only=True)
    assert invalid.success is False and (invalid.error or "").startswith("Invalid regex:")
    conflict = await tool.run(
        pattern="needle", files_only=True, context_lines=1,
    )
    assert conflict.success is False
    assert "context_lines" in (conflict.error or "")


@pytest.mark.asyncio
async def test_search_text_files_only_preserves_scope_and_bytes_limit(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    for i in range(12):
        (workspace / f"module_{i:02d}.py").write_text("needle\n")
    outside = tmp_path / "secret.py"
    outside.write_text("EXTERNAL_SECRET_NEEDLE\n")
    tool = SearchTextTool(SafetyGuard(workspace))
    refused = await tool.run(
        pattern="EXTERNAL_SECRET_NEEDLE", directory_path="../secret.py",
        files_only=True, fixed_strings=True,
    )
    assert refused.success is False
    assert "outside project scope" in (refused.error or "")
    assert refused.output == ""
    # Identical existing search capture enforcement applies in file-only mode.
    monkeypatch.setattr(search_module, "MAX_SEARCH_CAPTURE_BYTES", 45)
    limited = await tool.run(
        pattern="needle", files_only=True, fixed_strings=True,
    )
    assert limited.success and limited.truncated
    assert limited.output.startswith("module_00.py:1\nmodule_01.py:1\n")
    assert "module_00.py:1:needle" not in limited.output
    assert "module_11.py:1" not in limited.output
    assert "[search output capture truncated after 45 bytes]" in limited.output
    assert "EXTERNAL_SECRET_NEEDLE" not in limited.output


@pytest.mark.asyncio
async def test_search_text_opt_in_context_merges_adjacent_match_windows(tmp_path) -> None:
    (tmp_path / "app.py").write_text(
        "header\nbefore\nneedle first\nbetween\nneedle second\nafter\ntail\n"
    )
    tool = SearchTextTool(SafetyGuard(tmp_path))
    result = await tool.run(pattern="needle", fixed_strings=True, context_lines=1)

    assert result.success is True
    assert result.truncated is False
    assert result.output.splitlines() == [
        "app.py-2-before",
        "app.py:3:needle first",
        "app.py-4-between",
        "app.py:5:needle second",
        "app.py-6-after",
    ]
    # Context rows never consume the match count or change default results.
    limited = await tool.run(
        pattern="needle", fixed_strings=True, context_lines=1, max_results=1
    )
    assert limited.output.splitlines() == [
        "app.py-2-before",
        "app.py:3:needle first",
        "app.py-4-between",
        "[truncated after 1 matches]",
    ]
    assert limited.truncated is True
    default = await tool.run(pattern="needle", fixed_strings=True)
    assert default.output == "app.py:3:needle first\napp.py:5:needle second"


def _parse_grouped_search_output(output: str):
    groups = []
    current = None
    header_pattern = re.compile(r"^(.*) \(lines (\d+)-(\d+)\)$")
    row_pattern = re.compile(r"^(> |  )(\d+): (.*)$")
    for line in output.splitlines():
        header = header_pattern.match(line)
        if header:
            if current is not None:
                groups.append(current)
            current = {
                "path": header.group(1),
                "start": int(header.group(2)),
                "end": int(header.group(3)),
                "rows": [],
            }
            continue
        if line == "...":
            if current is not None:
                groups.append(current)
                current = None
            continue
        row = row_pattern.match(line)
        if row and current is not None:
            current["rows"].append(
                (int(row.group(2)), row.group(1) == "> ", row.group(3))
            )
    if current is not None:
        groups.append(current)

    matches = []
    for group in groups:
        rows = group["rows"]
        assert rows, "range headers must have at least one numbered row"
        assert any(is_match for _, is_match, _ in rows), (
            "range blocks must contain a matching line"
        )
        line_numbers = [line_number for line_number, _, _ in rows]
        assert line_numbers == list(range(group["start"], group["end"] + 1))
        matches.extend(
            (group["path"], line_number)
            for line_number, is_match, _ in rows
            if is_match
        )
    return matches, groups


def _make_grouped_fixture(root: Path, shape: str) -> list[tuple[str, int]]:
    if shape == "compact-repeated-names":
        expected = []
        for module in (
            "packages/runtime/providers/cache/backend.py",
            "packages/runtime/providers/cache/restore.py",
            "packages/runtime/providers/cache/warmup.py",
        ):
            path = root / module
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "# repeated-name fixture\n"
                "class CacheStore:\n"
                "    def read(self): return None\n"
                "def create_cache(): return CacheStore()\n"
                "# unrelated separator\n"
                "def restore_cache(store): return CacheStore()\n"
                "# trailing comment\n",
                encoding="utf-8",
            )
            expected.extend((module, line) for line in (2, 4, 6))
        return expected

    if shape == "overlapping-context":
        relative = "src/deeply/nested/storage/backends/cache/provider/store.py"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# overlapping context fixture"]
        for index in range(15):
            lines.extend(
                (
                    f"def use_store_{index}():",
                    f"    return CacheStore({index})",
                    f"    # context for repeated CacheStore name {index}",
                )
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return [
            (relative, line)
            for index in range(15)
            for line in (3 + 3 * index, 4 + 3 * index)
        ]

    raise AssertionError(f"unknown fixture shape: {shape}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shape", "capture_cap"),
    [
        ("compact-repeated-names", 1_024),
        ("overlapping-context", 2_922),
    ],
)
async def test_search_text_grouped_context_preserves_fixture_matches_and_budget(
    tmp_path,
    shape: str,
    capture_cap: int,
) -> None:
    expected = _make_grouped_fixture(tmp_path, shape)
    tool = SearchTextTool(SafetyGuard(tmp_path))
    common = {
        "pattern": "CacheStore",
        "fixed_strings": True,
        "context_lines": 2,
        "max_output_bytes": 2_000_000,
    }

    legacy = await tool.run(**common)
    grouped = await tool.run(**common, grouped_context=True)
    limited = await tool.run(
        **{**common, "max_output_bytes": capture_cap},
        grouped_context=True,
    )

    grouped_matches, groups = _parse_grouped_search_output(grouped.output)
    limited_matches, limited_groups = _parse_grouped_search_output(limited.output)
    assert grouped.success and not grouped.truncated
    assert grouped_matches == expected
    assert limited.success and not limited.truncated
    assert limited_matches == expected
    assert all(
        any(is_match for _, is_match, _ in block["rows"])
        for block in groups + limited_groups
    )
    assert len(grouped.output.encode("utf-8")) <= int(
        len(legacy.output.encode("utf-8")) * 0.9
    )


@pytest.mark.asyncio
async def test_search_text_grouped_context_sheds_context_before_provider_drops_hits(
    tmp_path,
) -> None:
    source = tmp_path / "deep" / "cache.py"
    source.parent.mkdir()
    lines = []
    for index in range(6):
        lines.extend(
            (
                "x" * 300,
                f"needle_{index} = {index}",
                "y" * 300,
            )
        )
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")

    budget_inputs = []

    def accepts(result) -> bool:
        budget_inputs.append(result.output)
        return len(result.output.encode("utf-8")) <= 1_200

    budget = ToolOutputBudget(
        accepts=accepts,
        can_dispatch=True,
    )
    tool = SearchTextTool(SafetyGuard(tmp_path))

    with tool.output_budget_context(budget):
        result = await tool.run(
            pattern="needle_",
            fixed_strings=True,
            context_lines=1,
            grouped_context=True,
            max_output_bytes=20_000,
        )

    matches, groups = _parse_grouped_search_output(result.output)
    assert result.success and result.truncated
    assert matches == [("deep/cache.py", line) for line in (2, 5, 8, 11, 14, 17)]
    assert "context omitted" in result.output
    assert all(len(block["rows"]) == 1 for block in groups)
    assert len(result.output.encode("utf-8")) <= 1_200
    assert len(budget_inputs[0].encode("utf-8")) > 1_200
    assert "  1: " in budget_inputs[0]


@pytest.mark.asyncio
async def test_search_text_grouped_context_sheds_context_before_capture_drops_hits(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "deep").mkdir()
    (tmp_path / "deep" / "cache.py").write_text(
        "x" * 510 + "\nneedle\n" + "y" * 510 + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(search_module, "MAX_SEARCH_CONTEXT_CAPTURE_BYTES", 600)

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
        context_lines=1,
        grouped_context=True,
        max_output_bytes=1_024,
    )

    matches, groups = _parse_grouped_search_output(result.output)
    assert matches == [("deep/cache.py", 2)]
    assert len(groups) == 1 and len(groups[0]["rows"]) == 1
    assert "context omitted" in result.output
    assert "search output capture truncated" not in result.output
    assert result.truncated
    assert len(result.output.encode("utf-8")) <= 600


@pytest.mark.asyncio
async def test_grouped_hit_only_search_uses_explicit_output_cap_above_context_cap(
    tmp_path,
) -> None:
    (tmp_path / "large.py").write_text(
        "".join(f"needle_{index} " + "x" * 8_000 + "\n" for index in range(4)),
        encoding="utf-8",
    )

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle_",
        fixed_strings=True,
        context_lines=0,
        grouped_context=True,
        max_output_bytes=100_000,
    )

    matches, _ = _parse_grouped_search_output(result.output)
    assert matches == [("large.py", line) for line in range(1, 5)]
    assert len(result.output.encode("utf-8")) > 24_576
    assert result.truncated is False


@pytest.mark.asyncio
async def test_grouped_hit_only_capture_does_not_claim_context_was_omitted(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "all_hits.py").write_text(
        "".join(f"needle_{index} " + "x" * 350 + "\n" for index in range(3)),
        encoding="utf-8",
    )
    monkeypatch.setattr(search_module, "MAX_SEARCH_CONTEXT_CAPTURE_BYTES", 1_024)

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle_",
        fixed_strings=True,
        context_lines=1,
        grouped_context=True,
        max_output_bytes=1_024,
    )

    matches, groups = _parse_grouped_search_output(result.output)
    assert matches == [("all_hits.py", 1), ("all_hits.py", 2)]
    assert all(all(is_match for _, is_match, _ in group["rows"]) for group in groups)
    assert "search output capture truncated" in result.output
    assert "context omitted" not in result.output
    assert result.truncated is True


@pytest.mark.asyncio
async def test_grouped_provider_fallback_does_not_claim_absent_context_was_dropped(
    tmp_path,
) -> None:
    (tmp_path / "all_hits.py").write_text(
        "".join(f"needle_{index:02d} = {index}\n" for index in range(40)),
        encoding="utf-8",
    )
    tool = SearchTextTool(SafetyGuard(tmp_path))
    arguments = {
        "pattern": "needle_",
        "fixed_strings": True,
        "context_lines": 1,
        "grouped_context": True,
        "max_output_bytes": 100_000,
    }
    full = await tool.run(**arguments)
    expected = [("all_hits.py", line) for line in range(1, 41)]
    assert _parse_grouped_search_output(full.output)[0] == expected

    limit = len(full.output.encode("utf-8")) - 1
    budget = ToolOutputBudget(
        accepts=lambda result: len(result.output.encode("utf-8")) <= limit,
        can_dispatch=True,
    )
    with tool.output_budget_context(budget):
        result = await tool.run(**arguments)

    matches, groups = _parse_grouped_search_output(result.output)
    assert result.success and result.truncated
    assert matches == expected[: len(matches)]
    assert 0 < len(matches) < len(expected)
    assert all(all(is_match for _, is_match, _ in group["rows"]) for group in groups)
    assert "context budget truncated search results" in result.output
    assert "context omitted" not in result.output


@pytest.mark.asyncio
async def test_grouped_hit_collection_counts_path_storage_once_per_file(tmp_path) -> None:
    directory_names = ["d" * 140 for _ in range(20)]
    relative = "/".join([*directory_names, "cache.py"])
    source = tmp_path / relative
    source.parent.mkdir(parents=True)
    source.write_text("needle\n" * 700, encoding="utf-8")

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
        context_lines=0,
        grouped_context=True,
        max_results=1_000,
        max_output_bytes=100_000,
    )

    matches, groups = _parse_grouped_search_output(result.output)
    assert matches == [(relative, line) for line in range(1, 701)]
    assert len(groups) == 1
    assert result.truncated is False


@pytest.mark.asyncio
async def test_search_text_grouped_context_zero_matches_and_incompatible_files_only(
    tmp_path,
) -> None:
    (tmp_path / "app.py").write_text("no matching content\n", encoding="utf-8")
    tool = SearchTextTool(SafetyGuard(tmp_path))
    grouped_schema = tool.json_schema()["properties"]["grouped_context"]

    assert grouped_schema["type"] == "boolean"
    assert "default" not in grouped_schema

    zero = await tool.run(
        pattern="missing",
        fixed_strings=True,
        context_lines=0,
        grouped_context=True,
    )
    incompatible = await tool.run(
        pattern="missing",
        fixed_strings=True,
        files_only=True,
        grouped_context=True,
    )

    assert zero.success and "No matches found" in zero.output and not zero.truncated
    assert incompatible.success is False
    assert incompatible.output == ""
    assert "grouped_context" in (incompatible.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("grouped_context", [False, True])
async def test_search_text_zero_matches_returns_bounded_correction_hint(
    tmp_path, grouped_context: bool
) -> None:
    (tmp_path / "app.py").write_text("existing marker\n", encoding="utf-8")

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="missing-marker",
        fixed_strings=True,
        directory_path=".",
        grouped_context=grouped_context,
    )

    assert result.success is True
    assert result.error is None
    assert result.truncated is False
    assert "No matches found" in result.output
    assert "adjust the pattern or directory_path" in result.output
    assert len(result.output.encode("utf-8")) <= 120


@pytest.mark.asyncio
async def test_search_text_grouped_context_keeps_regex_and_literal_matching(
    tmp_path,
) -> None:
    (tmp_path / "app.py").write_text(
        "CacheStore CacheClient\nCacheStore\nCacheClient\n", encoding="utf-8"
    )
    tool = SearchTextTool(SafetyGuard(tmp_path))

    regex_result = await tool.run(
        pattern="Cache(Store|Client)",
        context_lines=0,
        grouped_context=True,
    )
    literal_result = await tool.run(
        pattern="CacheStore",
        fixed_strings=True,
        context_lines=0,
        grouped_context=True,
    )

    assert _parse_grouped_search_output(regex_result.output)[0] == [
        ("app.py", 1),
        ("app.py", 2),
        ("app.py", 3),
    ]
    assert _parse_grouped_search_output(literal_result.output)[0] == [
        ("app.py", 1),
        ("app.py", 2),
    ]


@pytest.mark.asyncio
async def test_search_text_five_context_lines_remain_bounded(tmp_path, monkeypatch) -> None:
    """Allow a useful nearby source window without lifting the output cap."""
    path = tmp_path / "app.py"
    path.write_text(
        "".join("needle\n" if index == 7 else f"line {index}\n" for index in range(1, 14))
    )
    tool = SearchTextTool(SafetyGuard(tmp_path))
    result = await tool.run(pattern="needle", fixed_strings=True, context_lines=5)
    assert result.success and not result.truncated
    assert result.output.splitlines() == [
        *(f"app.py-{index}-line {index}" for index in range(2, 7)),
        "app.py:7:needle",
        *(f"app.py-{index}-line {index}" for index in range(8, 13)),
    ]
    monkeypatch.setattr(search_module, "MAX_SEARCH_CONTEXT_CAPTURE_BYTES", 64)
    limited = await tool.run(
        pattern="needle", fixed_strings=True, context_lines=5,
        max_output_bytes=2_000_000,
    )
    assert limited.success and limited.truncated
    assert "[search output capture truncated after 64 bytes]" in limited.output
    assert "app.py-12-line 12" not in limited.output


@pytest.mark.asyncio
async def test_search_text_ten_context_lines_preserve_results_under_fixed_byte_cap(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "module.py"
    path.write_text(
        "".join("needle\n" if index == 13 else f"line {index}\n"
                for index in range(1, 26))
    )
    tool = SearchTextTool(SafetyGuard(tmp_path))
    window = await tool.run(pattern="needle", fixed_strings=True, context_lines=10)
    assert window.success and not window.truncated
    assert window.output.splitlines() == [
        *(f"module.py-{index}-line {index}" for index in range(3, 13)),
        "module.py:13:needle",
        *(f"module.py-{index}-line {index}" for index in range(14, 24)),
    ]
    direct = await tool.run(pattern="needle", fixed_strings=True)
    assert direct.success and direct.output == "module.py:13:needle"

    monkeypatch.setattr(search_module, "MAX_SEARCH_CONTEXT_CAPTURE_BYTES", 64)
    truncated = await tool.run(
        pattern="needle", fixed_strings=True, context_lines=10,
        max_output_bytes=2_000_000,
    )
    assert truncated.success and truncated.truncated
    assert "[search output capture truncated after 64 bytes]" in truncated.output
    assert "module.py-23-line 23" not in truncated.output
    with pytest.raises(ValueError, match="less than or equal to 10"):
        SearchTextArgs(pattern="needle", context_lines=11)


@pytest.mark.asyncio
async def test_search_text_context_previews_are_bounded_and_reported(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search_module, "MAX_SEARCH_CONTEXT_PREVIEW_CHARS", 12)
    (tmp_path / "app.py").write_text(
        "a" * 300 + "\nneedle\n" + "b" * 300 + "\n"
    )
    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle", fixed_strings=True, context_lines=1
    )
    assert result.output.splitlines() == [
        "app.py-1-" + "a" * 12 + " [context preview truncated]",
        "app.py:2:needle",
        "app.py-3-" + "b" * 12 + " [context preview truncated]",
    ]
    assert result.truncated is True


@pytest.mark.asyncio
async def test_search_text_context_caps_total_output_without_changing_default(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search_module, "MAX_SEARCH_CONTEXT_CAPTURE_BYTES", 48)
    (tmp_path / "app.py").write_text("before\nneedle\nafter\nneedle\n")
    tool = SearchTextTool(SafetyGuard(tmp_path))

    contextual = await tool.run(pattern="needle", context_lines=1)
    assert contextual.truncated is True
    assert "[search output capture truncated after 48 bytes]" in contextual.output
    assert "app.py:4:needle" not in contextual.output
    ordinary = await tool.run(pattern="needle")
    assert ordinary.truncated is False
    assert ordinary.output == "app.py:2:needle\napp.py:4:needle"


@pytest.mark.asyncio
async def test_search_text_defaults_to_bounded_capture_with_explicit_larger_output(
    tmp_path,
) -> None:
    (tmp_path / "large.py").write_text(
        "".join(f"needle_{index:03d} " + "x" * 300 + "\n" for index in range(200))
    )
    tool = SearchTextTool(SafetyGuard(tmp_path))

    bounded = await tool.run(pattern="needle", fixed_strings=True)
    assert bounded.truncated is True
    assert "[search output capture truncated after 16384 bytes]" in bounded.output
    assert "large.py:1:needle_000" in bounded.output
    assert "large.py:200:needle_199" not in bounded.output
    assert len(bounded.output.encode("utf-8")) < 17_000

    explicit = await tool.run(
        pattern="needle", fixed_strings=True, max_output_bytes=100_000
    )
    assert explicit.truncated is False
    assert "large.py:200:needle_199" in explicit.output
    assert len(explicit.output) > 60_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_type", "arguments"),
    [
        (ListDirectoryTool, {"max_results": 20}),
        (GlobFilesTool, {"pattern": "**/*.py", "max_results": 20}),
    ],
)
async def test_directory_navigation_honors_provider_output_budget(
    tmp_path, tool_type, arguments: dict[str, object]
) -> None:
    for index in range(20):
        (tmp_path / f"module_{index:02}.py").write_text(
            f"VALUE_{index} = {index}\n", encoding="utf-8"
        )
    tool = tool_type(SafetyGuard(tmp_path))
    budget = ToolOutputBudget(
        accepts=lambda result: len(result.output) <= 180,
        can_dispatch=True,
    )

    with tool.output_budget_context(budget):
        result = await tool.run(**arguments)

    assert result.success is True
    assert result.truncated is True
    assert "context budget truncated" in result.output
    assert len(result.output) <= 180


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_type", "arguments", "limit_marker"),
    [
        (
            ListDirectoryTool,
            {"max_results": 2},
            "[truncated after 2 entries]",
        ),
        (
            GlobFilesTool,
            {"pattern": "**/*.py", "max_results": 2},
            "[truncated after 2 matches]",
        ),
    ],
)
async def test_directory_context_truncation_does_not_claim_displayed_limit(
    tmp_path, tool_type, arguments: dict[str, object], limit_marker: str
) -> None:
    for index in range(3):
        filename = f"module_{index}_{'x' * 100}.py"
        (tmp_path / filename).write_text("VALUE = 1\n", encoding="utf-8")
    tool = tool_type(SafetyGuard(tmp_path))
    budget = ToolOutputBudget(
        accepts=lambda result: len(result.output) <= 220,
        can_dispatch=True,
    )

    with tool.output_budget_context(budget):
        result = await tool.run(**arguments)

    assert result.success is True
    assert result.truncated is True
    assert "context budget truncated" in result.output
    assert "max_results=2 also reached" in result.output
    assert limit_marker not in result.output


@pytest.mark.asyncio
async def test_search_text_honors_provider_output_budget(tmp_path) -> None:
    for index in range(10):
        (tmp_path / f"module_{index:02}.py").write_text(
            f"needle_{index} = '{'x' * 256}'\n", encoding="utf-8"
        )
    tool = SearchTextTool(SafetyGuard(tmp_path))
    budget = ToolOutputBudget(
        accepts=lambda result: len(result.output) <= 600,
        can_dispatch=True,
    )

    with tool.output_budget_context(budget):
        result = await tool.run(pattern="needle", fixed_strings=True)

    assert result.success is True
    assert result.truncated is True
    assert "context budget truncated search results" in result.output
    assert "module_00.py:1" in result.output
    assert len(result.output) <= 600


@pytest.mark.asyncio
async def test_search_context_truncation_does_not_claim_displayed_match_limit(
    tmp_path,
) -> None:
    for index in range(3):
        filename = f"source_{index}_{'x' * 60}.py"
        (tmp_path / filename).write_text(
            f"needle {'x' * 35}\n", encoding="utf-8"
        )
    tool = SearchTextTool(SafetyGuard(tmp_path))
    budget = ToolOutputBudget(
        accepts=lambda result: len(result.output) <= 215,
        can_dispatch=True,
    )

    with tool.output_budget_context(budget):
        result = await tool.run(
            pattern="needle",
            fixed_strings=True,
            max_results=2,
        )

    assert result.success is True
    assert result.truncated is True
    assert "context budget truncated search results" in result.output
    assert "max_results=2 also reached" in result.output
    assert "[truncated after 2 matches]" not in result.output


@pytest.mark.asyncio
async def test_search_text_long_unicode_match_still_exposes_location_with_budget(
    tmp_path,
) -> None:
    (tmp_path / "wide.py").write_text("needle " + "🪐" * 20_000 + "\n")
    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle", fixed_strings=True
    )
    assert result.success is True
    assert result.truncated is True
    assert result.output.startswith("wide.py:1:needle ")
    assert "[line preview truncated]" in result.output
    assert len(result.output.encode("utf-8")) < 10_000


@pytest.mark.asyncio
async def test_context_search_hard_cap_still_applies_to_explicit_large_budget(
    tmp_path,
) -> None:
    (tmp_path / "many.py").write_text(
        "".join(f"needle {index}: " + "x" * 300 + "\n" for index in range(200))
    )
    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle", fixed_strings=True,
        context_lines=1, max_output_bytes=100_000,
    )
    assert result.success is True
    assert result.truncated is True
    assert "[search output capture truncated after 24576 bytes]" in result.output
    assert len(result.output.encode("utf-8")) < 25_000


@pytest.mark.asyncio
async def test_search_text_late_match_keeps_relevant_excerpt_and_column(tmp_path) -> None:
    (tmp_path / "minified.js").write_text("x" * 30_000 + "NEEDLE" + "y" * 5_000)
    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="NEEDLE", fixed_strings=True
    )
    assert result.success is True
    assert result.truncated is True
    assert result.output.startswith("minified.js:1:[column 29921] ")
    assert "NEEDLE" in result.output
    assert "[line preview truncated]" in result.output
    assert len(result.output.encode("utf-8")) < 10_000

    grouped = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="NEEDLE",
        fixed_strings=True,
        context_lines=0,
        grouped_context=True,
    )
    assert grouped.success is True
    assert grouped.truncated is True
    assert grouped.output.startswith("minified.js (lines 1-1)\n> 1: [column 29921] ")
    assert "NEEDLE" in grouped.output
    assert "[line preview truncated]" in grouped.output


@pytest.mark.asyncio
async def test_search_text_accepts_a_single_file_path(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("first\nneedle here\n")
    (tmp_path / "src" / "other.py").write_text("needle elsewhere\n")
    tool = SearchTextTool(SafetyGuard(tmp_path))

    result = await tool.run(
        pattern="needle", directory_path="src/app.py", fixed_strings=True
    )

    assert result.success is True
    assert result.output == "src/app.py:2:needle here"
    assert result.truncated is False

    matching = await tool.run(
        pattern="needle", directory_path="src/app.py", glob="**/*.py"
    )
    basename = await tool.run(
        pattern="needle", directory_path="src/app.py", glob="*.py"
    )
    project_relative = await tool.run(
        pattern="needle", directory_path="src/app.py", glob="src/*.py"
    )
    excluded = await tool.run(
        pattern="needle", directory_path="src/app.py", glob="**/*.md"
    )
    assert matching.output == result.output
    assert basename.output == result.output
    assert project_relative.output == result.output
    assert excluded.success is True
    assert "No matches found" in excluded.output


@pytest.mark.parametrize(
    ("context_lines", "files_only"),
    [(0, False), (2, False), (0, True)],
)
@pytest.mark.asyncio
async def test_search_text_single_file_preserves_scoped_read_protection(
    tmp_path, monkeypatch: pytest.MonkeyPatch, context_lines: int, files_only: bool,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("harmless\n")
    outside = tmp_path / "outside.py"
    outside.write_text("PRIVATE_NEEDLE\n")
    original = search_module.read_scoped_bytes

    def replace_with_symlink(path, guard, **kwargs):
        if Path(path) == target:
            target.unlink()
            try:
                target.symlink_to(outside)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return original(path, guard, **kwargs)

    monkeypatch.setattr(search_module, "read_scoped_bytes", replace_with_symlink)
    result = await SearchTextTool(SafetyGuard(workspace)).run(
        pattern="PRIVATE_NEEDLE", directory_path="app.py",
        fixed_strings=True, context_lines=context_lines,
        files_only=files_only,
    )

    assert result.success is True
    assert "PRIVATE_NEEDLE" not in result.output
    assert result.truncated is True


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
            return re.search("needle", line)

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
    assert "fixed_strings=true" in result.error


@pytest.mark.asyncio
async def test_search_text_single_file_still_rejects_outside_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.py").write_text("secret\n")

    result = await SearchTextTool(SafetyGuard(workspace)).run(
        pattern="secret", directory_path="../outside.py"
    )
    assert result.success is False
    assert "outside project scope" in (result.error or "")
    assert "project-relative" in (result.error or "")
    assert "secret" not in result.output


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
    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="x", directory_path="../outside"
    )
    assert result.success is False
    assert "outside project scope" in (result.error or "")
    assert "project-relative" in (result.error or "")


@pytest.mark.parametrize("tool_name", ["list_dir", "glob_files", "search_text"])
@pytest.mark.asyncio
async def test_discovery_tools_return_actionable_scope_refusal_without_exception(
    tmp_path: Path, tool_name: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("PRIVATE_CONTENT\n", encoding="utf-8")
    guard = SafetyGuard(workspace)
    tool, kwargs = {
        "list_dir": (ListDirectoryTool(guard), {"directory_path": "../"}),
        "glob_files": (
            GlobFilesTool(guard),
            {"directory_path": "../", "pattern": "**/*.py"},
        ),
        "search_text": (
            SearchTextTool(guard),
            {"directory_path": "../secret.py", "pattern": "PRIVATE_CONTENT"},
        ),
    }[tool_name]

    result = await tool.run(**kwargs)

    assert result.success is False
    assert "outside project scope" in (result.error or "")
    assert "project-relative" in (result.error or "")
    assert "/workspace" not in result.output
    assert "PRIVATE_CONTENT" not in result.output
    assert outside.read_text(encoding="utf-8") == "PRIVATE_CONTENT\n"


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
    assert result.output.startswith("large.txt:1:needle")
    assert "[line preview truncated]" in result.output
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
        "long.txt:1:" + line[: search_module.DEFAULT_SEARCH_CAPTURE_BYTES // 2]
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


@pytest.mark.asyncio
@pytest.mark.parametrize("generated_name", [".venv", "node_modules"])
async def test_workspace_wide_search_skips_generated_dependencies_but_keeps_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, generated_name: str
) -> None:
    monkeypatch.setattr(search_module, "MAX_GLOB_SCAN_ENTRIES", 12)
    monkeypatch.setattr(search_module, "MAX_SEARCH_SCAN_ENTRIES", 12)
    dependencies = tmp_path / generated_name / "lib"
    dependencies.mkdir(parents=True)
    for index in range(24):
        (dependencies / f"package_{index:03}.py").write_text(
            "dependency_only_token\n", encoding="utf-8"
        )
    source = tmp_path / "src"
    source.mkdir()
    (source / "app.py").write_text("application_unique_token\n", encoding="utf-8")
    guard = SafetyGuard(tmp_path)

    filenames = await GlobFilesTool(guard).run(pattern="**/*.py")
    contents = await SearchTextTool(guard).run(
        pattern="application_unique_token", fixed_strings=True
    )
    for result in (filenames, contents):
        assert result.success and not result.truncated
        assert "src/app.py" in result.output
        assert "package_" not in result.output
        assert f"[skipped generated directories: {generated_name}" in result.output
        assert "directory_path" in result.output

    # An explicitly requested subtree remains discoverable, even if generated.
    dependency_files = await GlobFilesTool(guard).run(
        pattern="**/*.py", directory_path=generated_name
    )
    dependency_matches = await SearchTextTool(guard).run(
        pattern="dependency_only_token",
        fixed_strings=True,
        directory_path=generated_name,
    )
    # Explicit searches still respect their configured scan budget.
    assert dependency_files.truncated and "package_000.py" in dependency_files.output
    assert (
        dependency_matches.truncated
        and "package_000.py:1" in dependency_matches.output
    )
    exact_file = await SearchTextTool(guard).run(
        pattern="dependency_only_token",
        fixed_strings=True,
        directory_path=f"{generated_name}/lib/package_000.py",
    )
    assert not exact_file.truncated and "package_000.py:1" in exact_file.output

    directory = await ListDirectoryTool(guard).run()
    assert f"{generated_name}/" in directory.output


@pytest.mark.asyncio
@pytest.mark.parametrize("generated_name", ["__pycache__", ".venv", "node_modules"])
async def test_narrow_source_search_skips_nested_generated_but_explicit_root_can_search(
    tmp_path: Path, generated_name: str,
) -> None:
    """Narrowing search to src must not re-enable generated descendants."""
    source = tmp_path / "src" / "pkg"
    source.mkdir(parents=True)
    (source / "actual.py").write_text("real_source_marker\n", encoding="utf-8")
    generated = source / generated_name / "nested"
    generated.mkdir(parents=True)
    (generated / "shadow.py").write_text("generated_only_marker\n", encoding="utf-8")
    nested_cache = generated.parent / "__pycache__"
    nested_cache.mkdir(exist_ok=True)
    (nested_cache / "explicit.py").write_text(
        "explicit_generated_marker\n", encoding="utf-8"
    )
    # Binary caches should not create misleading 'incomplete' results for a
    # source-only query, but an explicitly selected cache remains inspectable.
    (generated / "compiled.pyc").write_bytes(b"\x00\xff\x00invalid")
    guard = SafetyGuard(tmp_path)

    paths = await GlobFilesTool(guard).run(pattern="**/*.py", directory_path="src")
    source_search = await SearchTextTool(guard).run(
        pattern="real_source_marker", fixed_strings=True, directory_path="src"
    )
    grouped_source_search = await SearchTextTool(guard).run(
        pattern="real_source_marker",
        fixed_strings=True,
        directory_path="src",
        grouped_context=True,
    )
    absent = await SearchTextTool(guard).run(
        pattern="generated_only_marker", fixed_strings=True,
        directory_path="src",
    )
    for result in (paths, source_search, grouped_source_search, absent):
        assert result.success and not result.truncated
        assert "shadow.py" not in result.output
        assert "workspace search incomplete" not in result.output
        assert f"skipped generated directories: {generated_name}" in result.output
    assert "src/pkg/actual.py" in paths.output
    assert "src/pkg/actual.py:1:real_source_marker" in source_search.output
    assert _parse_grouped_search_output(grouped_source_search.output)[0] == [
        ("src/pkg/actual.py", 1)
    ]

    # Explicitly requesting the generated subtree opts into its contents.
    relative_generated = f"src/pkg/{generated_name}"
    explicit_glob = await GlobFilesTool(guard).run(
        pattern="**/*.py", directory_path=relative_generated
    )
    explicit_text = await SearchTextTool(guard).run(
        pattern="generated_only_marker", fixed_strings=True,
        directory_path=relative_generated,
        glob="**/*.py",
    )
    explicit_grouped_text = await SearchTextTool(guard).run(
        pattern="generated_only_marker",
        fixed_strings=True,
        directory_path=relative_generated,
        glob="**/*.py",
        grouped_context=True,
    )
    assert explicit_glob.success and "shadow.py" in explicit_glob.output
    assert "explicit.py" in explicit_glob.output
    assert explicit_text.success and "shadow.py:1:generated_only_marker" in explicit_text.output
    assert _parse_grouped_search_output(explicit_grouped_text.output)[0] == [
        ("src/pkg/" + generated_name + "/nested/shadow.py", 1)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("files_only", [False, True])
async def test_coding_loop_narrow_source_discovery_avoids_generated_cache(
    tmp_path: Path, files_only: bool,
) -> None:
    """Real agent search results keep source discovery reliable without retries."""
    import io

    from ash.core.loop import AshLoop
    from ash.core.session import SessionStore
    from ash.providers.base import ProviderABC, StreamChunk
    from ash.providers.capabilities import ProviderCapabilities
    from ash.ui.headless import HeadlessUI

    source = tmp_path / "src"
    source.mkdir()
    (source / "app.py").write_text("real_source_marker\n", encoding="utf-8")
    generated = source / "__pycache__"
    generated.mkdir()
    (generated / "shadow.py").write_text("generated_only_marker\n", encoding="utf-8")
    (generated / "app.cpython.pyc").write_bytes(b"\xff\x00junk")

    class SearchProvider(ProviderABC):
        model_name = "scripted-narrow-code-search"
        _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

        def __init__(self) -> None:
            self.requests = 0

        def count_tokens(self, text: str) -> int:
            return max(1, len(str(text)) // 4)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.requests += 1
            history = repr(messages)
            if self.requests == 1:
                call = {"id": "find-marker", "name": "search_text", "arguments": {
                    "pattern": "real_source_marker", "fixed_strings": True,
                    "directory_path": "src", "files_only": files_only,
                }}
            elif self.requests == 2:
                expected = (
                    "src/app.py:1" if files_only
                    else "src/app.py:1:real_source_marker"
                )
                assert expected in history
                if files_only:
                    assert "src/app.py:1:real_source_marker" not in history
                assert "skipped generated directories: __pycache__" in history
                assert "workspace search incomplete" not in history
                assert "shadow.py" not in history
                call = {"id": "list-source", "name": "glob_files", "arguments": {
                    "pattern": "**/*.py", "directory_path": "src",
                }}
            else:
                assert self.requests == 3
                assert "src/app.py" in history
                assert "shadow.py" not in history
                yield StreamChunk(content="Found the real source module.", is_done=True)
                return
            yield StreamChunk(is_done=True, native_tool_calls=[call])

    async def approve(_name: str, _args: dict[str, object]) -> bool:
        return True

    guard = SafetyGuard(tmp_path)
    store = SessionStore(tmp_path / "search-loop.db")
    provider = SearchProvider()
    loop = AshLoop(
        store, provider, guard,
        HeadlessUI(output_format="stream-json", stream=io.StringIO()),
        tmp_path,
        tools={
            "search_text": SearchTextTool(guard),
            "glob_files": GlobFilesTool(guard),
        },
        on_tool_approval=approve,
    )
    try:
        session = await loop.start_session()
        result = await loop.run_turn("Find the source module, not its caches")
        assert result == "Found the real source module."
        assert provider.requests == 3
        calls = store.load_session(session.session_id).tool_calls
        assert [call.tool_name for call in calls] == ["search_text", "glob_files"]
        assert all(call.success is True for call in calls)
        assert store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()
