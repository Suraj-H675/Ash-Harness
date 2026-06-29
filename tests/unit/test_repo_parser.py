"""Unit tests for the tree-sitter Python symbol extractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from repo.parser import SymbolExtractor


def test_extract_imports_functions_classes_and_methods(tmp_path: Path) -> None:
    src = """import os
from sys import path

class Foo:
    def bar(self):
        return 1
    def baz(self):
        return 2

def top_level():
    pass
"""
    p = tmp_path / "sample.py"
    p.write_text(src)

    extractor = SymbolExtractor()
    symbols = extractor.extract(p)

    by_name = {(s.name, s.kind, s.parent): s for s in symbols}

    assert ("import os", "import", None) in by_name
    assert ("from sys import path", "import_from", None) in by_name
    assert ("Foo", "class", None) in by_name
    assert ("bar", "method", "Foo") in by_name
    assert ("baz", "method", "Foo") in by_name
    assert ("top_level", "function", None) in by_name


def test_extract_handles_nested_definitions(tmp_path: Path) -> None:
    src = """def outer():
    def inner():
        return 1
    return inner
"""
    p = tmp_path / "nested.py"
    p.write_text(src)

    symbols = SymbolExtractor().extract(p)
    names = {s.name for s in symbols}
    assert "outer" in names
    assert "inner" in names


def test_extract_returns_empty_for_missing_file(tmp_path: Path) -> None:
    extractor = SymbolExtractor()
    assert extractor.extract(tmp_path / "does_not_exist.py") == []


def test_extract_preserves_line_numbers(tmp_path: Path) -> None:
    src = "\n\nimport os\n\n\ndef foo():\n    pass\n"
    p = tmp_path / "lines.py"
    p.write_text(src)

    symbols = SymbolExtractor().extract(p)
    foo = next(s for s in symbols if s.name == "foo")
    assert foo.start_line == 6
    assert foo.end_line == 7


@pytest.mark.parametrize(
    ("filename", "source", "expected"),
    [
        (
            "main.js",
            'import { value } from "./dep.js";\n'
            "export class Widget { render() {} }\n"
            "const build = () => {};\n",
            {
                ("Widget", "class", None, None),
                ("render", "method", "Widget", None),
                ("build", "function", None, None),
                (
                    'import { value } from "./dep.js";',
                    "import",
                    None,
                    "path:./dep.js",
                ),
            },
        ),
        (
            "main.ts",
            'import { value } from "./dep";\n'
            "interface Shape { area(): number }\n"
            "function build(): void {}\n",
            {
                ("Shape", "interface", None, None),
                ("area", "method", "Shape", None),
                ("build", "function", None, None),
            },
        ),
        (
            "main.tsx",
            "export function App() { return <main /> }\n",
            {("App", "function", None, None)},
        ),
        (
            "main.go",
            'package main\nimport "example.com/app/pkg"\n'
            "type Widget struct{}\n"
            "func (w Widget) Render() {}\n",
            {
                ('"example.com/app/pkg"', "import", None, "go:example.com/app/pkg"),
                ("Widget", "type", None, None),
                ("Render", "method", "Widget", None),
            },
        ),
        (
            "main.rs",
            "use crate::dep::Thing;\nstruct Widget;\n"
            "impl Widget { fn render(&self) {} }\n",
            {
                ("use crate::dep::Thing;", "import", None, "rust:crate::dep::Thing"),
                ("Widget", "struct", None, None),
                ("render", "method", "Widget", None),
            },
        ),
        (
            "Main.java",
            "import com.example.Dep;\n"
            "class Widget { void render() {} }\ninterface Shape {}\n",
            {
                ("import com.example.Dep;", "import", None, "java:com.example.Dep"),
                ("Widget", "class", None, None),
                ("render", "method", "Widget", None),
                ("Shape", "interface", None, None),
            },
        ),
        (
            "main.c",
            '#include "dep.h"\nstruct Widget {};\nvoid render(void) {}\n',
            {
                ('#include "dep.h"', "import", None, "include:dep.h"),
                ("Widget", "struct", None, None),
                ("render", "function", None, None),
            },
        ),
        (
            "main.cpp",
            '#include "dep.hpp"\nclass Widget { void render(); };\nvoid build() {}\n',
            {
                ('#include "dep.hpp"', "import", None, "include:dep.hpp"),
                ("Widget", "class", None, None),
                ("render", "method", "Widget", None),
                ("build", "function", None, None),
            },
        ),
        (
            "Main.cs",
            "using Example.Dep;\n"
            "namespace App { class Widget { void Render() {} } interface Shape {} }\n",
            {
                ("using Example.Dep;", "import", None, "csharp:Example.Dep"),
                ("Widget", "class", None, None),
                ("Render", "method", "Widget", None),
                ("Shape", "interface", None, None),
            },
        ),
    ],
)
def test_extracts_supported_source_languages(
    tmp_path: Path,
    filename: str,
    source: str,
    expected: set[tuple[str, str, str | None, str | None]],
) -> None:
    path = tmp_path / filename
    path.write_text(source)

    symbols = SymbolExtractor().extract(path)
    actual = {(item.name, item.kind, item.parent, item.reference) for item in symbols}

    assert expected <= actual


def test_extract_returns_empty_for_unsupported_language(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("not source code")
    assert SymbolExtractor().extract(path) == []


@pytest.mark.parametrize(
    ("filename", "source", "expected_line"),
    [
        (
            "sample.py",
            'def target():\n    return "target"\n\n# target\ntarget()\n',
            5,
        ),
        (
            "sample.ts",
            'function target() { return "target"; }\n// target\ntarget();\n',
            3,
        ),
        (
            "sample.c",
            'void target(void) {}\nconst char *text = "target";\ntarget();\n',
            3,
        ),
    ],
)
def test_find_references_excludes_declarations_comments_and_strings(
    tmp_path: Path,
    filename: str,
    source: str,
    expected_line: int,
) -> None:
    path = tmp_path / filename
    path.write_text(source)

    matches = SymbolExtractor().find_references(path, "target")

    assert [(item.start_line, item.start_column) for item in matches] == [
        (expected_line, 1)
    ]


def test_find_references_supports_case_insensitive_matching(tmp_path: Path) -> None:
    path = tmp_path / "sample.cs"
    path.write_text("class Example { void Run() { RUN(); } }\n")

    matches = SymbolExtractor().find_references(path, "run", case_sensitive=False)

    assert [item.name for item in matches] == ["RUN"]
