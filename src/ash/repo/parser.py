"""Tree-sitter symbol extraction for repository-map source languages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tree_sitter import Language, Node, Parser
import tree_sitter_c as tsc
import tree_sitter_cpp as tscpp
import tree_sitter_c_sharp as tscsharp
import tree_sitter_go as tsgo
import tree_sitter_java as tsjava
import tree_sitter_javascript as tsjavascript
import tree_sitter_python as tspython
import tree_sitter_rust as tsrust
import tree_sitter_typescript as tstypescript


MAX_SOURCE_FILE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Symbol:
    """A named source construct or import discovered in one file."""

    name: str
    kind: str
    file_path: str
    start_line: int
    end_line: int
    parent: str | None = None
    reference: str | None = None
    language: str = "python"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "file_path": self.file_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "parent": self.parent,
            "reference": self.reference,
            "language": self.language,
        }


@dataclass(frozen=True)
class SourceLocation:
    """A precise source occurrence returned by structural code navigation."""

    name: str
    file_path: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    language: str


@dataclass(frozen=True)
class LanguageSpec:
    name: str
    factory: Callable[[], Any]
    class_nodes: dict[str, str]
    function_nodes: dict[str, str]
    import_nodes: dict[str, str]
    parent_nodes: frozenset[str] = frozenset()


_PYTHON = LanguageSpec(
    name="python",
    factory=tspython.language,
    class_nodes={"class_definition": "class"},
    function_nodes={"function_definition": "function"},
    import_nodes={"import_statement": "import", "import_from_statement": "import_from"},
)
_JAVASCRIPT = LanguageSpec(
    name="javascript",
    factory=tsjavascript.language,
    class_nodes={"class_declaration": "class"},
    function_nodes={
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "method_definition": "method",
    },
    import_nodes={"import_statement": "import", "export_statement": "import_from"},
)
_TYPESCRIPT = LanguageSpec(
    name="typescript",
    factory=tstypescript.language_typescript,
    class_nodes={
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "type_alias_declaration": "type",
    },
    function_nodes={
        **_JAVASCRIPT.function_nodes,
        "function_signature": "function",
        "method_signature": "method",
    },
    import_nodes=_JAVASCRIPT.import_nodes,
)
_TSX = LanguageSpec(
    name="tsx",
    factory=tstypescript.language_tsx,
    class_nodes=_TYPESCRIPT.class_nodes,
    function_nodes=_TYPESCRIPT.function_nodes,
    import_nodes=_TYPESCRIPT.import_nodes,
)
_GO = LanguageSpec(
    name="go",
    factory=tsgo.language,
    class_nodes={"type_spec": "type"},
    function_nodes={
        "function_declaration": "function",
        "method_declaration": "method",
    },
    import_nodes={"import_spec": "import"},
)
_RUST = LanguageSpec(
    name="rust",
    factory=tsrust.language,
    class_nodes={
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
        "type_item": "type",
    },
    function_nodes={"function_item": "function"},
    import_nodes={"use_declaration": "import", "mod_item": "import"},
    parent_nodes=frozenset({"impl_item"}),
)
_JAVA = LanguageSpec(
    name="java",
    factory=tsjava.language,
    class_nodes={
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "record_declaration": "record",
        "annotation_type_declaration": "interface",
    },
    function_nodes={
        "method_declaration": "method",
        "constructor_declaration": "method",
    },
    import_nodes={"import_declaration": "import"},
)
_C = LanguageSpec(
    name="c",
    factory=tsc.language,
    class_nodes={
        "struct_specifier": "struct",
        "union_specifier": "type",
        "enum_specifier": "enum",
        "type_definition": "type",
    },
    function_nodes={"function_definition": "function"},
    import_nodes={"preproc_include": "import"},
)
_CPP = LanguageSpec(
    name="cpp",
    factory=tscpp.language,
    class_nodes={
        "class_specifier": "class",
        "struct_specifier": "struct",
        "union_specifier": "type",
        "enum_specifier": "enum",
        "type_definition": "type",
    },
    function_nodes={"function_definition": "function"},
    import_nodes={"preproc_include": "import"},
)
_CSHARP = LanguageSpec(
    name="csharp",
    factory=tscsharp.language,
    class_nodes={
        "class_declaration": "class",
        "interface_declaration": "interface",
        "struct_declaration": "struct",
        "record_declaration": "record",
        "enum_declaration": "enum",
        "delegate_declaration": "type",
    },
    function_nodes={
        "method_declaration": "method",
        "constructor_declaration": "method",
        "destructor_declaration": "method",
        "local_function_statement": "function",
    },
    import_nodes={"using_directive": "import"},
)


LANGUAGE_BY_SUFFIX: dict[str, LanguageSpec] = {
    ".py": _PYTHON,
    ".pyi": _PYTHON,
    ".js": _JAVASCRIPT,
    ".jsx": _JAVASCRIPT,
    ".mjs": _JAVASCRIPT,
    ".cjs": _JAVASCRIPT,
    ".ts": _TYPESCRIPT,
    ".mts": _TYPESCRIPT,
    ".cts": _TYPESCRIPT,
    ".tsx": _TSX,
    ".go": _GO,
    ".rs": _RUST,
    ".java": _JAVA,
    ".c": _C,
    ".h": _CPP,
    ".cc": _CPP,
    ".cpp": _CPP,
    ".cxx": _CPP,
    ".hh": _CPP,
    ".hpp": _CPP,
    ".hxx": _CPP,
    ".cs": _CSHARP,
}
SOURCE_SUFFIXES = frozenset(LANGUAGE_BY_SUFFIX)
IDENTIFIER_NODE_TYPES = frozenset(
    {
        "identifier",
        "type_identifier",
        "field_identifier",
        "property_identifier",
        "namespace_identifier",
        "package_identifier",
        "shorthand_property_identifier",
    }
)


class SymbolExtractor:
    """Detect a source language and extract definitions/imports with Tree-sitter."""

    def __init__(self) -> None:
        self._parsers: dict[str, Parser] = {}

    def extract(self, file_path: Path) -> list[Symbol]:
        parsed = self._parse_file(file_path)
        if parsed is None:
            return []
        spec, root = parsed
        symbols: list[Symbol] = []
        self._collect(
            root,
            file_path=file_path,
            spec=spec,
            parent=None,
            out=symbols,
        )
        return symbols

    def find_references(
        self,
        file_path: Path,
        name: str,
        *,
        case_sensitive: bool = True,
    ) -> list[SourceLocation]:
        """Find identifier references, excluding structural declaration names."""

        parsed = self._parse_file(file_path)
        if parsed is None:
            return []
        spec, root = parsed
        expected = name if case_sensitive else name.casefold()
        matches: list[SourceLocation] = []
        for node in [root, *self._descendants(root)]:
            if node.type not in IDENTIFIER_NODE_TYPES:
                continue
            value = self._text(node)
            comparable = value if case_sensitive else value.casefold()
            if comparable != expected or self._is_declaration_name(node, spec):
                continue
            matches.append(
                SourceLocation(
                    name=value,
                    file_path=str(file_path),
                    start_line=node.start_point[0] + 1,
                    start_column=node.start_point[1] + 1,
                    end_line=node.end_point[0] + 1,
                    end_column=node.end_point[1] + 1,
                    language=spec.name,
                )
            )
        return matches

    def _parse_file(self, file_path: Path) -> tuple[LanguageSpec, Node] | None:
        spec = LANGUAGE_BY_SUFFIX.get(file_path.suffix.casefold())
        if spec is None:
            return None
        try:
            if file_path.stat().st_size > MAX_SOURCE_FILE_BYTES:
                return None
            with file_path.open("rb") as handle:
                source = handle.read(MAX_SOURCE_FILE_BYTES + 1)
        except OSError:
            return None
        if len(source) > MAX_SOURCE_FILE_BYTES:
            return None
        parser = self._parsers.get(spec.name)
        if parser is None:
            parser = Parser(Language(spec.factory()))
            self._parsers[spec.name] = parser
        return spec, parser.parse(source).root_node

    def _collect(
        self,
        node: Node,
        *,
        file_path: Path,
        spec: LanguageSpec,
        parent: str | None,
        out: list[Symbol],
    ) -> None:
        node_type = node.type
        child_parent = parent

        class_kind = spec.class_nodes.get(node_type)
        if class_kind is not None:
            name = self._definition_name(node)
            if name:
                out.append(
                    self._symbol(node, file_path, spec, name, class_kind, parent)
                )
                child_parent = name

        function_kind = spec.function_nodes.get(node_type)
        if function_kind is not None:
            name = self._definition_name(node)
            if name:
                method_parent = parent or self._receiver_parent(node, spec.name)
                kind = function_kind
                if spec.name in {"python", "rust"} and method_parent is not None:
                    kind = "method"
                out.append(
                    self._symbol(
                        node,
                        file_path,
                        spec,
                        name,
                        kind,
                        method_parent if kind == "method" else parent,
                    )
                )

        import_kind = spec.import_nodes.get(node_type)
        if import_kind is not None:
            reference = self._import_reference(node, spec.name)
            # Export statements without a source are declarations, not imports.
            if spec.name not in {"javascript", "typescript", "tsx"} or reference:
                out.append(
                    self._symbol(
                        node,
                        file_path,
                        spec,
                        self._text(node).strip(),
                        import_kind,
                        parent,
                        reference=reference,
                    )
                )

        if node_type in spec.parent_nodes:
            child_parent = (
                self._text(node.child_by_field_name("type")).strip() or parent
            )

        if spec.name == "cpp" and node_type == "field_declaration" and parent:
            declarator = node.child_by_field_name("declarator")
            declarator_types = (
                {
                    declarator.type,
                    *(item.type for item in self._descendants(declarator)),
                }
                if declarator is not None
                else set()
            )
            if declarator is not None and "function_declarator" in declarator_types:
                name = self._declarator_name(declarator)
                if name:
                    out.append(
                        self._symbol(node, file_path, spec, name, "method", parent)
                    )

        if (
            spec.name in {"javascript", "typescript", "tsx"}
            and node_type == "variable_declarator"
        ):
            value = node.child_by_field_name("value")
            name_node = node.child_by_field_name("name")
            if value is not None and value.type in {
                "arrow_function",
                "function_expression",
                "generator_function",
            }:
                name = self._text(name_node).strip()
                if name:
                    out.append(
                        self._symbol(
                            node,
                            file_path,
                            spec,
                            name,
                            "method" if parent else "function",
                            parent,
                        )
                    )

        for child in node.named_children:
            self._collect(
                child,
                file_path=file_path,
                spec=spec,
                parent=child_parent,
                out=out,
            )

    @classmethod
    def _definition_name(cls, node: Node) -> str | None:
        name = cls._definition_name_node(node)
        return cls._text(name).strip() or None

    @classmethod
    def _definition_name_node(cls, node: Node) -> Node | None:
        name = node.child_by_field_name("name")
        if name is not None:
            return name
        declarator = node.child_by_field_name("declarator")
        if declarator is not None:
            return cls._declarator_name_node(declarator)
        for descendant in cls._descendants(node):
            if descendant.type in IDENTIFIER_NODE_TYPES:
                return descendant
        return None

    @classmethod
    def _declarator_name(cls, node: Node) -> str | None:
        name = cls._declarator_name_node(node)
        return cls._text(name).strip() or None

    @classmethod
    def _declarator_name_node(cls, node: Node) -> Node | None:
        current: Node | None = node
        while current is not None:
            if current.type in {"identifier", "field_identifier", "type_identifier"}:
                return current
            next_node = current.child_by_field_name("declarator")
            if next_node is None:
                break
            current = next_node
        for descendant in cls._descendants(node):
            if descendant.type in {"identifier", "field_identifier"}:
                return descendant
        return None

    @classmethod
    def _is_declaration_name(cls, node: Node, spec: LanguageSpec) -> bool:
        parent = node.parent
        while parent is not None:
            if (
                parent.type in spec.class_nodes
                or parent.type in spec.function_nodes
                or parent.type == "variable_declarator"
            ):
                declared = cls._definition_name_node(parent)
                if (
                    declared is not None
                    and declared.start_byte == node.start_byte
                    and declared.end_byte == node.end_byte
                ):
                    return True
                return False
            parent = parent.parent
        return False

    @classmethod
    def _receiver_parent(cls, node: Node, language: str) -> str | None:
        if language != "go":
            return None
        receiver = node.child_by_field_name("receiver")
        if receiver is None:
            return None
        candidates = [
            cls._text(descendant).strip()
            for descendant in cls._descendants(receiver)
            if descendant.type == "type_identifier"
        ]
        return candidates[-1] if candidates else None

    @classmethod
    def _import_reference(cls, node: Node, language: str) -> str | None:
        if language in {"javascript", "typescript", "tsx"}:
            source = node.child_by_field_name("source")
            value = cls._unquote(cls._text(source))
            return f"path:{value}" if value else None
        if language == "go":
            value = cls._unquote(cls._text(node.child_by_field_name("path")))
            return f"go:{value}" if value else None
        if language == "rust":
            if node.type == "mod_item":
                name = cls._text(node.child_by_field_name("name")).strip()
                return f"rust:self::{name}" if name else None
            argument = cls._text(node.child_by_field_name("argument")).strip()
            return f"rust:{argument}" if argument else None
        if language == "java":
            text = cls._text(node).strip().removeprefix("import ").removesuffix(";")
            text = text.removeprefix("static ").removesuffix(".*")
            return f"java:{text}" if text else None
        if language in {"c", "cpp"}:
            value = cls._unquote(cls._text(node.child_by_field_name("path")))
            return f"include:{value}" if value else None
        if language == "csharp":
            text = cls._text(node).strip().removeprefix("using ").removesuffix(";")
            if "=" in text or text.startswith("static "):
                return None
            return f"csharp:{text}" if text else None
        return None

    @staticmethod
    def _unquote(value: str) -> str:
        value = value.strip()
        if len(value) >= 2 and value[0] in {'"', "'", "<"}:
            closing = ">" if value[0] == "<" else value[0]
            if value[-1] == closing:
                return value[1:-1]
        return value

    @staticmethod
    def _text(node: Node | None) -> str:
        if node is None or node.text is None:
            return ""
        return node.text.decode("utf-8", errors="replace")

    @staticmethod
    def _descendants(node: Node) -> list[Node]:
        descendants: list[Node] = []
        stack = list(reversed(node.named_children))
        while stack:
            current = stack.pop()
            descendants.append(current)
            stack.extend(reversed(current.named_children))
        return descendants

    @staticmethod
    def _symbol(
        node: Node,
        file_path: Path,
        spec: LanguageSpec,
        name: str,
        kind: str,
        parent: str | None,
        *,
        reference: str | None = None,
    ) -> Symbol:
        return Symbol(
            name=name,
            kind=kind,
            file_path=str(file_path),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            parent=parent,
            reference=reference,
            language=spec.name,
        )
