"""Unit tests for the tree-sitter Python symbol extractor."""

from __future__ import annotations

from pathlib import Path

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
