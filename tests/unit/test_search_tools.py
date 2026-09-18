import os
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
async def test_search_text_returns_file_and_line(tmp_path) -> None:
    (tmp_path / "app.py").write_text("first\nneedle here\n")
    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
    )
    assert result.success is True
    assert "app.py:2:needle here" in result.output


@pytest.mark.asyncio
async def test_search_text_supports_regex_and_case_insensitive_matching(tmp_path) -> None:
    (tmp_path / "app.py").write_text("first\nAlpha123\n", encoding="utf-8")

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern=r"alpha\d+",
        case_sensitive=False,
    )

    assert result.success is True
    assert result.output == "app.py:2:Alpha123"


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
async def test_search_text_fallback_skips_oversized_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("ash.tools.search.MAX_SEARCH_FILE_BYTES", 32)
    (tmp_path / "large.txt").write_text("needle " + "x" * 40)

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
    )

    assert result.success is True
    assert result.output == ""


@pytest.mark.asyncio
async def test_search_text_fallback_bounds_workspace_scan(tmp_path, monkeypatch) -> None:
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
