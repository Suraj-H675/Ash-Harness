import pytest

from ash.safety.guard import SafetyGuard
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
    monkeypatch.setattr("ash.tools.search.shutil.which", lambda _: None)
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
    monkeypatch.setattr("ash.tools.search.shutil.which", lambda _: None)
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
async def test_search_text_fallback_bounds_workspace_scan(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("ash.tools.search.shutil.which", lambda _: None)
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
async def test_search_text_python_fallback_streams_lines(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("ash.tools.search.shutil.which", lambda _: None)
    (tmp_path / "notes.txt").write_text("first\nneedle\nlast\n")

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
    )

    assert result.success is True
    assert result.output == "notes.txt:2:needle"
