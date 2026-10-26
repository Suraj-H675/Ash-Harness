from pathlib import Path
import shutil
import subprocess

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


@pytest.mark.asyncio
async def test_explicit_symbol_path_finds_file_outside_repo_map_limit(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("def early(): pass\n")
    (tmp_path / "b.py").write_text("def other(): pass\n")
    target = tmp_path / "target.py"
    target.write_text("def Wanted(): pass\ndef use():\n    Wanted()\n")
    repo_map = RepoMap(tmp_path, max_files=2)
    indexed = [node.path for node in repo_map.files]
    assert target not in indexed
    guard = SafetyGuard(tmp_path)

    definition = await FindSymbolTool(guard, repo_map).run(
        query="Wanted", path_glob="target.py"
    )
    reference = await FindReferencesTool(guard, repo_map).run(
        query="Wanted", path_glob="target.py"
    )
    assert "target.py:1: function Wanted" in definition.output
    assert "target.py:3:5: Wanted" in reference.output
    assert not definition.truncated and not reference.truncated
    assert "index incomplete" not in definition.output
    assert "index incomplete" not in reference.output
    assert [node.path for node in repo_map.files] == indexed

    absent = await FindSymbolTool(guard, repo_map).run(
        query="Absent", path_glob="target.py"
    )
    assert absent.output == "No definitions found for 'Absent'."
    assert not absent.truncated

    indexed_file = await FindSymbolTool(guard, repo_map).run(
        query="early", path_glob="a.py"
    )
    assert "a.py:1: function early" in indexed_file.output
    assert not indexed_file.truncated

    wildcard = await FindSymbolTool(guard, repo_map).run(
        query="Wanted", path_glob="*.py"
    )
    assert wildcard.truncated
    assert "index incomplete" in wildcard.output

    unmatched_spelling = await FindSymbolTool(guard, repo_map).run(
        query="Wanted", path_glob="./target.py"
    )
    assert unmatched_spelling.truncated
    assert "in indexed files" in unmatched_spelling.output

    target.write_text("def Renamed(): pass\ndef use():\n    Renamed()\n")
    updated = await FindSymbolTool(guard, repo_map).run(
        query="Renamed", path_glob="target.py"
    )
    assert "target.py:1: function Renamed" in updated.output


@pytest.mark.asyncio
async def test_explicit_symbol_path_respects_excluded_directories(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("def early(): pass\n")
    hidden = tmp_path / "generated"
    hidden.mkdir()
    (hidden / "secret.py").write_text("def Secret(): pass\n")
    repo_map = RepoMap(tmp_path, max_files=1, exclude_patterns=["generated/"])
    result = await FindSymbolTool(SafetyGuard(tmp_path), repo_map).run(
        query="Secret", path_glob="generated/secret.py"
    )
    assert "function Secret" not in result.output


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
async def test_explicit_symbol_path_respects_gitignore(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("ignored.py\n")
    (tmp_path / "a.py").write_text("def indexed(): pass\n")
    (tmp_path / "ignored.py").write_text("def Hidden(): pass\n")
    repo_map = RepoMap(tmp_path, max_files=1)

    result = await FindSymbolTool(SafetyGuard(tmp_path), repo_map).run(
        query="Hidden", path_glob="ignored.py"
    )
    assert "function Hidden" not in result.output


@pytest.mark.asyncio
async def test_explicit_symbol_path_rejects_external_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text("def early(): pass\n")
    outside = tmp_path / "outside.py"
    outside.write_text(
        "def OutsideSecret(): pass\ndef caller():\n    OutsideSecret()\n"
    )
    (workspace / "target.py").symlink_to(outside)
    repo_map = RepoMap(workspace, max_files=1)
    guard = SafetyGuard(workspace)

    for tool in (FindSymbolTool(guard, repo_map), FindReferencesTool(guard, repo_map)):
        result = await tool.run(query="OutsideSecret", path_glob="target.py")
        assert "outside.py" not in result.output
        assert "function OutsideSecret" not in result.output
        assert "target.py:" not in result.output


def test_ranked_new_active_file_marks_index_incomplete(tmp_path: Path) -> None:
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("def known(): pass\n")
    repo_map = RepoMap(tmp_path, max_files=2)
    assert not repo_map.index_incomplete

    active = tmp_path / "z.py"
    active.write_text("def added(): pass\n")
    repo_map.rank([active])
    assert repo_map.index_incomplete
