from pathlib import Path

import pytest

from ash.repo.repomap import RepoMap
from ash.safety.guard import SafetyGuard
from ash.tools.symbols import FindReferencesTool, FindSymbolTool


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "service.py").write_text(
        "class Service:\n"
        "    def connect(self):\n"
        "        return None\n\n"
        "def run():\n"
        "    Service().connect()\n"
    )
    (tmp_path / "client.ts").write_text(
        "export class Service {}\nexport function run() { new Service(); }\n"
    )
    return tmp_path


@pytest.mark.asyncio
async def test_find_symbol_returns_structural_definitions(workspace: Path) -> None:
    guard = SafetyGuard(workspace)
    result = await FindSymbolTool(guard, RepoMap(workspace)).run(query="Service")

    assert result.success
    assert "client.ts:1: class Service [typescript]" in result.output
    assert "service.py:1: class Service [python]" in result.output
    assert result.truncated is False


@pytest.mark.asyncio
async def test_find_references_excludes_definition_sites(workspace: Path) -> None:
    guard = SafetyGuard(workspace)
    result = await FindReferencesTool(guard, RepoMap(workspace)).run(query="Service")

    assert result.success
    assert "client.ts:2" in result.output
    assert "service.py:6" in result.output
    assert "client.ts:1" not in result.output
    assert "service.py:1" not in result.output


@pytest.mark.asyncio
async def test_symbol_tools_support_globs_case_folding_and_limits(
    workspace: Path,
) -> None:
    guard = SafetyGuard(workspace)
    tool = FindSymbolTool(guard, RepoMap(workspace))

    filtered = await tool.run(
        query="service",
        case_sensitive=False,
        path_glob="*.ts",
    )
    limited = await tool.run(
        query="service",
        case_sensitive=False,
        max_results=1,
    )

    assert "client.ts" in filtered.output
    assert "service.py" not in filtered.output
    assert limited.truncated is True
    assert len(limited.output.splitlines()) == 1


@pytest.mark.asyncio
async def test_symbol_tools_report_empty_results_as_success(workspace: Path) -> None:
    guard = SafetyGuard(workspace)
    repo_map = RepoMap(workspace)

    definition = await FindSymbolTool(guard, repo_map).run(query="Missing")
    reference = await FindReferencesTool(guard, repo_map).run(query="Missing")

    assert definition.success and "No definitions found" in definition.output
    assert reference.success and "No references found" in reference.output


@pytest.mark.asyncio
async def test_symbol_navigation_reports_incomplete_repository_index(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("def present(): pass\n")
    (tmp_path / "b.py").write_text("def another(): pass\n")
    (tmp_path / "z.py").write_text(
        "def MissingFromIndex(): pass\ndef run():\n    MissingFromIndex()\n"
    )
    guard = SafetyGuard(tmp_path)
    repo_map = RepoMap(tmp_path, max_files=2)

    for tool in (FindSymbolTool(guard, repo_map), FindReferencesTool(guard, repo_map)):
        missing = await tool.run(query="MissingFromIndex")
        assert missing.success
        assert missing.truncated is True
        assert "No" in missing.output and "in indexed files" in missing.output
        assert "indexed" in missing.output
        assert "search_text" in missing.output

        present = await tool.run(query="present")
        assert present.success
        assert present.truncated is True
        if isinstance(tool, FindSymbolTool):
            assert "a.py" in present.output
        else:
            assert "No references found in indexed files" in present.output
        assert "indexed" in present.output

    complete = await FindSymbolTool(guard, RepoMap(tmp_path)).run(
        query="MissingFromIndex"
    )
    assert complete.success and not complete.truncated
    assert "z.py" in complete.output

    (tmp_path / "z.py").unlink()
    refreshed = await FindSymbolTool(guard, repo_map).run(query="missing")
    assert refreshed.success and not refreshed.truncated
    assert "index incomplete" not in refreshed.output


def test_ranked_new_active_file_marks_index_incomplete(tmp_path: Path) -> None:
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("def known(): pass\n")
    repo_map = RepoMap(tmp_path, max_files=2)
    assert not repo_map.index_incomplete

    active = tmp_path / "z.py"
    active.write_text("def added(): pass\n")
    repo_map.rank([active])
    assert repo_map.index_incomplete
