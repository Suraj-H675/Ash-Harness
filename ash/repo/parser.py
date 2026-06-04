"""Tree-sitter-based Python symbol extractor for the Ash repo map.

The extractor parses a single Python source file and yields a flat list of
:class:`Symbol` records covering classes, functions, methods, and
imports. It is intentionally narrow — the repo map only needs *signatures*
to populate context, not full AST fidelity.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Language, Node, Parser
import tree_sitter_python as tspython


@dataclass(frozen=True)
class Symbol:
    """A single named construct in a Python source file."""

    name: str
    kind: str  # "class" | "function" | "method" | "import" | "import_from"
    file_path: str
    start_line: int  # 1-indexed, inclusive
    end_line: int  # 1-indexed, inclusive
    parent: str | None = None  # enclosing class name for methods

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "file_path": self.file_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "parent": self.parent,
        }


class SymbolExtractor:
    """Parse Python files and emit :class:`Symbol` records via tree-sitter."""

    def __init__(self) -> None:
        self._parser = Parser(Language(tspython.language()))

    def extract(self, file_path: Path) -> list[Symbol]:
        """Parse ``file_path`` and return all named symbols it contains."""

        try:
            source = file_path.read_bytes()
        except OSError:
            return []

        tree = self._parser.parse(source)
        if tree.root_node is None or tree.root_node.has_error:
            # Best-effort: still walk the tree so partial parses contribute.
            pass

        symbols: list[Symbol] = []
        for node in tree.root_node.children:
            self._collect(node, file_path, parent=None, out=symbols)
        return symbols

    # --- internal walk ---------------------------------------------------

    def _collect(
        self,
        node: Node,
        file_path: Path,
        *,
        parent: str | None,
        out: list[Symbol],
    ) -> None:
        kind = node.type

        if kind == "class_definition":
            name = self._first_child_name(node, "identifier")
            if name is not None:
                out.append(
                    Symbol(
                        name=name,
                        kind="class",
                        file_path=str(file_path),
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        parent=parent,
                    )
                )
                # Recurse into the class body to find methods.
                body = self._child_by_field(node, "body")
                if body is not None:
                    for child in body.children:
                        self._collect(child, file_path, parent=name, out=out)
                return

        elif kind == "function_definition":
            name = self._first_child_name(node, "identifier")
            if name is not None:
                out.append(
                    Symbol(
                        name=name,
                        kind="method" if parent is not None else "function",
                        file_path=str(file_path),
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        parent=parent,
                    )
                )
            # Recurse into the body so nested defs are still found.
            body = self._child_by_field(node, "body")
            if body is not None:
                for child in body.children:
                    self._collect(child, file_path, parent=parent, out=out)
            return

        elif kind == "import_statement":
            out.append(
                Symbol(
                    name=self._render_import(node),
                    kind="import",
                    file_path=str(file_path),
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    parent=parent,
                )
            )
            return

        elif kind == "import_from_statement":
            out.append(
                Symbol(
                    name=self._render_import_from(node),
                    kind="import_from",
                    file_path=str(file_path),
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    parent=parent,
                )
            )
            return

        # Walk into block-like containers so nested defs are still found.
        if kind in {"if_statement", "for_statement", "while_statement", "try_statement",
                    "with_statement", "match_statement", "elif_clause", "else_clause",
                    "decorated_definition", "expression_statement"}:
            for child in node.children:
                self._collect(child, file_path, parent=parent, out=out)
            return

        # `decorated_definition` carries the inner def/class as its first
        # named child — descend into whatever it holds.
        for child in node.children:
            if child.type in {"class_definition", "function_definition",
                              "decorated_definition"}:
                self._collect(child, file_path, parent=parent, out=out)

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _first_child_name(node: Node, child_type: str) -> str | None:
        for child in node.children:
            if child.type == child_type:
                return child.text.decode("utf-8", errors="replace")
        return None

    @staticmethod
    def _child_by_field(node: Node, field: str) -> Node | None:
        return node.child_by_field_name(field)

    @staticmethod
    def _render_import(node: Node) -> str:
        """Best-effort string for ``import a.b.c as d``."""

        text = node.text.decode("utf-8", errors="replace")
        return text.strip()

    @staticmethod
    def _render_import_from(node: Node) -> str:
        """Best-effort string for ``from a.b import c, d as e``."""

        text = node.text.decode("utf-8", errors="replace")
        return text.strip()
