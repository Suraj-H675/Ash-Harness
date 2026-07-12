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
