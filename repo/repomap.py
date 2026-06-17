"""Repository map and Personalized PageRank (PPR) for Ash context.

Given a workspace, the map:
  1. Walks Python files, parses each with the tree-sitter
     :class:`~ash.repo.parser.SymbolExtractor`, and records every
     class/function/method/import symbol.
  2. Builds a file-level dependency graph: an edge ``A -> B`` means some
     symbol in file ``A`` imports or references a symbol in file ``B``.
  3. Runs :func:`calculate_personalized_pagerank` from the user's
     currently-active files so the highest-ranked files appear first
     in the rendered map.
  4. Renders a Markdown snippet containing the top-N files' top-K
     symbols each, suitable for injection into the system prompt.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

from ash.repo.parser import Symbol, SymbolExtractor


# Top-level file extensions we treat as parseable Python source.
PYTHON_SUFFIXES = {".py", ".pyi"}

# Folders that should never be descended into when building a repo map.
DEFAULT_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "env",
        ".tox",
        "node_modules",
        "dist",
        "build",
        ".ash",
    }
)


@dataclass(frozen=True)
class FileNode:
    """A workspace file plus its extracted symbols."""

    path: Path
    symbols: tuple[Symbol, ...]
    referenced_modules: tuple[str, ...]


def _normalize_module_name(path: Path, project_root: Path) -> str | None:
    """Turn a workspace path into a dotted module name when possible."""

    try:
        relative = path.relative_to(project_root)
    except ValueError:
        return None
    parts = list(relative.with_suffix("").parts)
    if not parts or parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    return ".".join(parts)


def _discover_python_files(
    project_root: Path,
    *,
    ignored_dirs: frozenset[str] = DEFAULT_IGNORED_DIRS,
) -> list[Path]:
    files: list[Path] = []
    for entry in sorted(project_root.iterdir(), key=lambda p: p.name):
        if entry.is_dir():
            if entry.name in ignored_dirs or entry.name.startswith("."):
                continue
            files.extend(_discover_python_files(entry, ignored_dirs=ignored_dirs))
        elif entry.is_file() and entry.suffix in PYTHON_SUFFIXES:
            files.append(entry)
    return files


def _module_to_path(module: str, project_root: Path) -> Path | None:
    """Resolve a dotted module reference back to a workspace file path."""

    parts = module.split(".")
    for suffix in (".py", Path(".py") / "__init__.py"):
        candidate = (
            project_root.joinpath(*parts).with_suffix(suffix)
            if isinstance(suffix, str)
            else project_root.joinpath(*parts) / "__init__.py"
        )
        if candidate.exists():
            return candidate
    return None


def _extract_references(symbols: Iterable[Symbol]) -> set[str]:
    """Pull out dotted module references from import statements."""

    refs: set[str] = set()
    for symbol in symbols:
        if symbol.kind not in {"import", "import_from"}:
            continue
        text = symbol.name
        if text.startswith("import "):
            tail = text[len("import ") :]
            for piece in tail.split(","):
                head = piece.strip().split(" as ")[0].strip()
                if head:
                    refs.add(head)
        elif text.startswith("from "):
            # ``from a.b.c import x, y as z`` -> ``a.b.c``
            try:
                _, rhs = text.split(" import ", 1)
                module = text[5:].rsplit(" import ", 1)[0].strip()
            except ValueError:
                continue
            if module:
                refs.add(module)
            # Also record the imported names so callers can match on
            # ``Foo`` (the class name) for files that re-export symbols.
            for piece in rhs.split(","):
                head = piece.strip().split(" as ")[0].strip()
                if head and "." in head:
                    refs.add(head.rsplit(".", 1)[0])
    return refs


class RepoMap:
    """In-memory repo map and PPR ranker."""

    def __init__(
        self,
        project_root: Path,
        *,
        extractor: SymbolExtractor | None = None,
        max_files: int = 500,
        exclude_patterns: list[str] | None = None,
    ) -> None:
        self.project_root = project_root
        self._extractor = extractor or SymbolExtractor()
        self._max_files = max_files
        self._exclude_patterns = exclude_patterns or []
        self._files: list[FileNode] = []
        self._module_index: dict[str, Path] = {}
        self._index: dict[Path, int] = {}
        self._adjacency: np.ndarray | None = None
        self._refresh()

    # --- public API -----------------------------------------------------

    @property
    def files(self) -> list[FileNode]:
        return list(self._files)

    def refresh(self) -> None:
        """Re-walk the workspace and rebuild the graph."""

        self._refresh()

    def rank(
        self,
        active_files: Iterable[Path],
        *,
        alpha: float = 0.85,
        max_iter: int = 100,
        tol: float = 1e-6,
    ) -> list[tuple[Path, float]]:
        """Return ``[(file_path, score), ...]`` sorted by descending PPR."""

        if not self._files or self._adjacency is None:
            return []

        teleport_indices: list[int] = []
        for path in active_files:
            resolved = path.resolve()
            if resolved in self._index:
                teleport_indices.append(self._index[resolved])
        if not teleport_indices:
            teleport_indices = list(range(len(self._files)))

        scores = calculate_personalized_pagerank(
            self._adjacency,
            teleport_indices=teleport_indices,
            alpha=alpha,
            max_iter=max_iter,
            tol=tol,
        )
        ordered = sorted(
            ((self._files[i].path, float(scores[i])) for i in range(len(self._files))),
            key=lambda pair: pair[1],
            reverse=True,
        )
        return [p for p in ordered if not self._is_excluded(p[0])]

    def _is_excluded(self, path: Path) -> bool:
        path_str = str(path)
        for pattern in self._exclude_patterns:
            if fnmatch.fnmatch(path_str, pattern) or fnmatch.fnmatch(
                path_str, f"**/{pattern}"
            ):
                return True
        return False

    def render(
        self,
        ranked: list[tuple[Path, float]],
        *,
        top_files: int = 10,
        symbols_per_file: int = 5,
    ) -> str:
        """Render a markdown repository map snippet from ranked files.

        Produces a ``## Repository Map`` section listing the top-N files
        and their top-K symbols, suitable for injection into the system prompt.
        """

        lines = ["## Repository Map", ""]
        selected = ranked[:top_files]
        if not selected:
            lines.append("*No files found.*")
            return "\n".join(lines)

        for path, score in selected:
            node = self._node_for_path(path)
            rel = str(path.relative_to(self.project_root))
            lines.append(f"### {rel} _(score: {score:.4f})_")
            if node is None or not node.symbols:
                lines.append("*No symbols extracted.*")
            else:
                # Show functions/methods/classes, skip plain imports.
                interesting = [
                    s for s in node.symbols
                    if s.kind in {"function", "method", "class"}
                ]
                for sym in interesting[:symbols_per_file]:
                    kind = sym.kind
                    parent = f" (in {sym.parent})" if sym.parent else ""
                    lines.append(f"- **{sym.name}** `{kind}`{parent}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def to_dot_graph(self, ranked: list[Path]) -> str:
        """Render the dependency graph as a Graphviz DOT string.

        Users can pipe the result to ``dot -Tpng`` to visualize the graph.
        """
        lines = ["digraph repo {", "  rankdir=LR;"]
        for src_path in ranked:
            src_idx = self._index.get(src_path.resolve())
            if src_idx is None:
                continue
            src_label = str(src_path.relative_to(self.project_root))
            if self._adjacency is None:
                continue
            for dep_idx in range(self._adjacency.shape[0]):
                if self._adjacency[src_idx, dep_idx] > 0:
                    dep_path = self._files[dep_idx].path
                    dep_label = str(dep_path.relative_to(self.project_root))
                    lines.append(f'  "{src_label}" -> "{dep_label}";')
        lines.append("}")
        return "\n".join(lines)

    # --- internal -------------------------------------------------------

    def _node_for_path(self, path: Path) -> FileNode | None:
        try:
            resolved = path.resolve()
        except OSError:
            return None
        if resolved not in self._index:
            return None
        return self._files[self._index[resolved]]

    def _refresh(self) -> None:
        paths = _discover_python_files(self.project_root)[: self._max_files]
        files: list[FileNode] = []
        module_index: dict[str, Path] = {}

        for path in paths:
            symbols = tuple(self._extractor.extract(path))
            refs = _extract_references(symbols)
            files.append(
                FileNode(
                    path=path.resolve(), symbols=symbols, referenced_modules=tuple(refs)
                )
            )
            module_name = _normalize_module_name(path, self.project_root)
            if module_name is not None:
                module_index[module_name] = path.resolve()

        self._files = files
        self._module_index = module_index
        self._index = {node.path: i for i, node in enumerate(files)}
        self._adjacency = self._build_adjacency()

    def _build_adjacency(self) -> np.ndarray:
        n = len(self._files)
        matrix = np.zeros((n, n), dtype=float)
        if n == 0:
            return matrix

        for i, node in enumerate(self._files):
            for ref in node.referenced_modules:
                target = self._module_index.get(ref)
                if target is None:
                    target = _module_to_path(ref, self.project_root)
                if target is None:
                    continue
                j = self._index.get(target.resolve())
                if j is not None and j != i:
                    matrix[i, j] += 1.0
        return matrix


def calculate_personalized_pagerank(
    adjacency_matrix: np.ndarray,
    teleport_indices: list[int],
    alpha: float = 0.85,
    max_iter: int = 100,
    tol: float = 1e-6,
) -> np.ndarray:
    """
    Compute Personalized PageRank for a directed dependency graph.

    Implements the spec formula::

        v = (1 - alpha) * M @ v + alpha * p

    where ``M`` is the column-normalized adjacency (transitions out of
    each node), ``p`` is the teleport vector concentrated on the
    ``teleport_indices``, and ``alpha`` is the damping factor.
    """

    n = adjacency_matrix.shape[0] if adjacency_matrix.ndim == 2 else 0
    if n == 0:
        return np.array([])

    # Column-normalize so each column is a transition distribution.
    column_sums = adjacency_matrix.sum(axis=0)
    normalized = np.zeros_like(adjacency_matrix, dtype=float)
    for col in range(n):
        if column_sums[col] > 0:
            normalized[:, col] = adjacency_matrix[:, col] / column_sums[col]
        else:
            # Dangling node: distribute uniformly.
            normalized[:, col] = np.ones(n) / n

    # Teleport vector.
    p = np.zeros(n)
    if teleport_indices:
        # Filter to in-bounds indices to be defensive.
        valid = [i for i in teleport_indices if 0 <= i < n]
        if valid:
            p[valid] = 1.0 / len(valid)
        else:
            p[:] = 1.0 / n
    else:
        p[:] = 1.0 / n

    v = np.copy(p)
    for _ in range(max_iter):
        v_next = (1.0 - alpha) * np.dot(normalized, v) + alpha * p
        if np.linalg.norm(v_next - v, 1) < tol:
            return v_next
        v = v_next
    return v
