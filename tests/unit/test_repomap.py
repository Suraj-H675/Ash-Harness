"""Unit tests for the repo map and Personalized PageRank math."""

from __future__ import annotations

import math
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock

import pytest

from repo.repomap import RepoMap, calculate_personalized_pagerank


def test_pagerank_concentrates_on_teleport_node() -> None:
    matrix = [[0, 1, 0], [1, 0, 0], [0, 0, 0]]
    v = calculate_personalized_pagerank(matrix, teleport_indices=[0])
    assert abs(sum(v) - 1.0) < 1e-6
    assert v[0] > v[1] > v[2]


def test_pagerank_handles_dangling_nodes() -> None:
    # Node 2 has no outbound edges — must not produce NaN.
    matrix = [[0, 1, 0], [1, 0, 0], [0, 0, 0]]
    v = calculate_personalized_pagerank(matrix, teleport_indices=[0, 1, 2])
    assert all(math.isfinite(value) for value in v)
    assert abs(sum(v) - 1.0) < 1e-6


def test_pagerank_empty_matrix_returns_empty() -> None:
    out = calculate_personalized_pagerank([], teleport_indices=[])
    assert out == []


def test_pagerank_no_teleport_defaults_to_uniform() -> None:
    matrix = [[0, 1], [1, 0]]
    v = calculate_personalized_pagerank(matrix, teleport_indices=[])
    # Symmetric two-node graph with no teleport bias — both nodes share score.
    assert abs(v[0] - v[1]) < 1e-6


def test_pagerank_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="square"):
        calculate_personalized_pagerank([[0, 0, 0], [0, 0, 0]], teleport_indices=[])
    with pytest.raises(ValueError, match="alpha"):
        calculate_personalized_pagerank(
            [[0, 0], [0, 0]], teleport_indices=[], alpha=1.0
        )


def test_pagerank_deduplicates_teleport_indices() -> None:
    matrix = [[0, 0], [0, 0]]
    duplicated = calculate_personalized_pagerank(matrix, teleport_indices=[0, 0, 1])
    unique = calculate_personalized_pagerank(matrix, teleport_indices=[0, 1])
    assert duplicated == pytest.approx(unique)


def test_repomap_discovers_python_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def hello(): pass\n")
    (tmp_path / "b.py").write_text("import a\n")
    (tmp_path / "ignored.txt").write_text("not python")

    rm = RepoMap(tmp_path)
    names = sorted(p.name for p in (node.path for node in rm.files))
    assert names == ["a.py", "b.py"]


def test_repomap_rank_emphasizes_active_file(tmp_path: Path) -> None:
    (tmp_path / "active.py").write_text("def hot(): pass\n")
    (tmp_path / "neighbor.py").write_text("import active\ndef used(): pass\n")
    (tmp_path / "isolated.py").write_text("def alone(): pass\n")

    rm = RepoMap(tmp_path)
    ranked = rm.rank([tmp_path / "active.py"])
    by_path = {p.name: score for p, score in ranked}

    assert by_path["active.py"] > by_path["neighbor.py"]
    assert by_path["active.py"] > by_path["isolated.py"]


def test_repomap_render_includes_top_symbols(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import b\ndef hello(): pass\nclass Foo: pass\n")
    (tmp_path / "b.py").write_text("def world(): pass\n")

    rm = RepoMap(tmp_path)
    ranked = rm.rank([tmp_path / "a.py"])
    rendered = rm.render(ranked, top_files=2, symbols_per_file=4)

    assert "## Repository Map" in rendered
    assert "hello" in rendered
    assert "world" in rendered


def test_repomap_skips_ignored_directories(tmp_path: Path) -> None:
    (tmp_path / "keep.py").write_text("def keep(): pass\n")
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "skip.py").write_text("def skip(): pass\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "skip2.py").write_text("def skip2(): pass\n")

    rm = RepoMap(tmp_path)
    names = {p.name for p in (n.path for n in rm.files)}
    assert names == {"keep.py"}


def test_repomap_excludes_node_modules(tmp_path: Path) -> None:
    from repo.repomap import RepoMap

    (tmp_path / "src.py").write_text("x = 1")
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    (node_modules / "dep.js").write_text("module.exports = {}")

    repo_map = RepoMap(project_root=tmp_path, exclude_patterns=["node_modules/**"])
    ranked = repo_map.rank([tmp_path])
    paths = [str(p) for p in ranked]
    assert not any("node_modules" in p for p in paths)
    assert any("src.py" in p for p in paths)


def test_repomap_applies_exclusions_before_file_limit(tmp_path: Path) -> None:
    generated = tmp_path / "a_generated"
    generated.mkdir()
    (generated / "ignored.py").write_text("def ignored(): pass\n")
    (tmp_path / "z_kept.py").write_text("def kept(): pass\n")

    repo_map = RepoMap(
        tmp_path,
        max_files=1,
        exclude_patterns=["a_generated/**"],
    )

    assert [node.path.name for node in repo_map.files] == ["z_kept.py"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_repomap_honors_gitignore_for_untracked_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("generated.py\n")
    (tmp_path / "generated.py").write_text("def generated(): pass\n")
    (tmp_path / "kept.py").write_text("def kept(): pass\n")

    repo_map = RepoMap(tmp_path)

    assert [node.path.name for node in repo_map.files] == ["kept.py"]


def test_repomap_refresh_reuses_unchanged_parse_results(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("def first(): pass\n")
    second.write_text("def second(): pass\n")
    repo_map = RepoMap(tmp_path)
    extract = Mock(wraps=repo_map._extractor.extract)
    repo_map._extractor.extract = extract

    repo_map.refresh()
    assert extract.call_count == 0

    first.write_text("def first_changed(): pass\n")
    repo_map.refresh()

    assert extract.call_count == 1
    assert extract.call_args.args[0] == first


def test_repomap_resolves_relative_imports(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "__init__.py").write_text("")
    active = package / "main.py"
    active.write_text("from . import helper\ndef main(): pass\n")
    helper = package / "helper.py"
    helper.write_text("def help_main(): pass\n")
    isolated = tmp_path / "isolated.py"
    isolated.write_text("def isolated(): pass\n")

    ranked = dict(RepoMap(tmp_path).rank([active]))

    assert ranked[helper.resolve()] > ranked[isolated.resolve()]


def test_repomap_discovers_and_renders_multiple_languages(tmp_path: Path) -> None:
    (tmp_path / "api.go").write_text("package api\nfunc Serve() {}\n")
    (tmp_path / "model.rs").write_text("pub struct Model;\n")
    (tmp_path / "view.tsx").write_text(
        "interface Props {}\nexport function View() { return <main /> }\n"
    )

    repo_map = RepoMap(tmp_path)
    names = {node.path.name for node in repo_map.files}
    rendered = repo_map.render(repo_map.rank([]))

    assert names == {"api.go", "model.rs", "view.tsx"}
    assert "Serve" in rendered
    assert "Model" in rendered
    assert "View" in rendered
    assert "Props" in rendered


def test_repomap_ranks_javascript_relative_dependency(tmp_path: Path) -> None:
    active = tmp_path / "main.ts"
    active.write_text('import { helper } from "./helper";\nexport function main() {}\n')
    helper = tmp_path / "helper.ts"
    helper.write_text("export function helper() {}\n")
    isolated = tmp_path / "isolated.ts"
    isolated.write_text("export function isolated() {}\n")

    ranked = dict(RepoMap(tmp_path).rank([active]))

    assert ranked[helper.resolve()] > ranked[isolated.resolve()]


def test_repomap_prefers_same_language_for_extensionless_import(tmp_path: Path) -> None:
    active = tmp_path / "main.ts"
    active.write_text('import { helper } from "./helper";\n')
    typescript = tmp_path / "helper.ts"
    typescript.write_text("export function helper() {}\n")
    javascript = tmp_path / "helper.js"
    javascript.write_text("export function helper() {}\n")

    ranked = dict(RepoMap(tmp_path).rank([active]))

    assert ranked[typescript.resolve()] > ranked[javascript.resolve()]


def test_repomap_resolves_dotted_javascript_basename(tmp_path: Path) -> None:
    active = tmp_path / "main.ts"
    active.write_text('import { helper } from "./helper.test";\n')
    dependency = tmp_path / "helper.test.ts"
    dependency.write_text("export function helper() {}\n")
    isolated = tmp_path / "isolated.ts"
    isolated.write_text("export function isolated() {}\n")

    ranked = dict(RepoMap(tmp_path).rank([active]))

    assert ranked[dependency.resolve()] > ranked[isolated.resolve()]


def test_repomap_resolves_java_source_root_import(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "main" / "java" / "com" / "example"
    source_root.mkdir(parents=True)
    active = source_root / "Main.java"
    active.write_text("import com.example.Dep;\nclass Main {}\n")
    dependency = source_root / "Dep.java"
    dependency.write_text("class Dep {}\n")
    isolated = tmp_path / "Isolated.java"
    isolated.write_text("class Isolated {}\n")

    ranked = dict(RepoMap(tmp_path).rank([active]))

    assert ranked[dependency.resolve()] > ranked[isolated.resolve()]


def test_repomap_dot_graph_follows_import_direction(tmp_path: Path) -> None:
    source = tmp_path / "main.ts"
    source.write_text('import { helper } from "./helper";\n')
    dependency = tmp_path / "helper.ts"
    dependency.write_text("export function helper() {}\n")
    repo_map = RepoMap(tmp_path)

    graph = repo_map.to_dot_graph([source, dependency])

    assert '"main.ts" -> "helper.ts";' in graph
    assert '"helper.ts" -> "main.ts";' not in graph


def test_repomap_finds_definitions_and_references_across_languages(
    tmp_path: Path,
) -> None:
    (tmp_path / "service.py").write_text(
        "def connect():\n    pass\n\ndef run():\n    connect()\n"
    )
    (tmp_path / "client.ts").write_text(
        "export function connect() {}\nexport function run() { connect(); }\n"
    )
    repo_map = RepoMap(tmp_path)

    definitions = repo_map.find_definitions("connect")
    references = repo_map.find_references("connect")

    assert {(Path(item.file_path).name, item.start_line) for item in definitions} == {
        ("client.ts", 1),
        ("service.py", 1),
    }
    assert {(Path(item.file_path).name, item.start_line) for item in references} == {
        ("client.ts", 2),
        ("service.py", 5),
    }


def test_repomap_symbol_queries_support_path_globs_and_case_folding(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "Main.java").write_text("class Connection {}\n")
    (tmp_path / "Connection.cs").write_text("class Connection {}\n")
    repo_map = RepoMap(tmp_path)

    matches = repo_map.find_definitions(
        "connection",
        case_sensitive=False,
        path_glob="src/*.java",
    )

    assert [(Path(item.file_path).name, item.language) for item in matches] == [
        ("Main.java", "java")
    ]
