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
async def test_glob_files_returns_relative_matches(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('x')")
    (tmp_path / "README.md").write_text("readme")
    result = await GlobFilesTool(SafetyGuard(tmp_path)).run(pattern="**/*.py")
    assert result.output == "src/app.py"


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
async def test_search_text_bounds_oversized_rg_output(tmp_path) -> None:
    (tmp_path / "large.txt").write_text("needle " + "x" * 2_100_000)

    result = await SearchTextTool(SafetyGuard(tmp_path)).run(
        pattern="needle",
        fixed_strings=True,
    )

    assert result.success is True
    assert result.truncated is True
    assert "search output capture truncated" in result.output


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
