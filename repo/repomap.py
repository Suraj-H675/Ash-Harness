"""Repository map and Personalized PageRank (PPR) for Ash context.

Given a workspace, the map:
  1. Walks supported source files, parses each with the tree-sitter
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
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

from repo.parser import SOURCE_SUFFIXES, Symbol, SymbolExtractor

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

    if path.suffix.casefold() not in {".py", ".pyi"}:
        return None
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


def _discover_source_files(
    project_root: Path,
    *,
    ignored_dirs: frozenset[str] = DEFAULT_IGNORED_DIRS,
    exclude_patterns: Iterable[str] = (),
) -> list[Path]:
    patterns = tuple(exclude_patterns)
    files: list[Path] = []
    for entry in sorted(project_root.iterdir(), key=lambda p: p.name):
        relative = entry.relative_to(project_root)
        if _matches_exclude_pattern(relative, patterns, is_dir=entry.is_dir()):
            continue
        if entry.is_dir():
            if entry.name in ignored_dirs or entry.name.startswith("."):
                continue
            files.extend(
                _discover_source_files(
                    entry,
                    ignored_dirs=ignored_dirs,
                    exclude_patterns=(
                        _relative_pattern_for_child(pattern, entry.name)
                        for pattern in patterns
                    ),
                )
            )
        elif entry.is_file() and entry.suffix.casefold() in SOURCE_SUFFIXES:
            files.append(entry)
    return files


def _relative_pattern_for_child(pattern: str, child_name: str) -> str:
    """Translate a root-relative glob for recursive discovery."""

    normalized = pattern.replace("\\", "/").lstrip("./")
    prefix = f"{child_name}/"
    if normalized.startswith(prefix):
        return normalized[len(prefix) :]
    return normalized


def _matches_exclude_pattern(
    relative_path: Path,
    patterns: Iterable[str],
    *,
    is_dir: bool,
) -> bool:
    path = relative_path.as_posix().lstrip("./")
    for raw_pattern in patterns:
        pattern = raw_pattern.replace("\\", "/").lstrip("./")
        if not pattern:
            continue
        directory_pattern = pattern.rstrip("/")
        if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(
            path, directory_pattern
        ):
            return True
        if is_dir and fnmatch.fnmatchcase(f"{path}/__ash_probe__", pattern):
            return True
    return False


def _git_ignored_files(project_root: Path, paths: Iterable[Path]) -> set[Path]:
    """Return untracked files ignored by Git, or an empty set outside a worktree."""

    relative_paths = [path.relative_to(project_root).as_posix() for path in paths]
    if not relative_paths:
        return set()
    payload = "\0".join(relative_paths).encode("utf-8") + b"\0"
    try:
        completed = subprocess.run(
            ["git", "check-ignore", "--stdin", "-z"],
            cwd=project_root,
            input=payload,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if completed.returncode not in {0, 1}:
        return set()
    return {
        (project_root / item.decode("utf-8", errors="surrogateescape")).resolve()
        for item in completed.stdout.split(b"\0")
        if item
    }


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


def _resolve_module_reference(
    module: str,
    *,
    source_path: Path,
    project_root: Path,
    module_index: dict[str, Path],
) -> Path | None:
    """Resolve absolute and package-relative imports to an indexed file."""

    if module.startswith("path:"):
        return _resolve_path_reference(
            module[5:], source_path=source_path, project_root=project_root
        )
    if module.startswith("include:"):
        return _resolve_include_reference(
            module[8:], source_path=source_path, project_root=project_root
        )
    if module.startswith("rust:"):
        return _resolve_rust_reference(
            module[5:], source_path=source_path, project_root=project_root
        )
    if module.startswith("java:"):
        return _resolve_dotted_source(module[5:], project_root, ".java")
    if module.startswith("csharp:"):
        return _resolve_dotted_source(module[7:], project_root, ".cs")
    if module.startswith("go:"):
        return _resolve_go_reference(module[3:], project_root)

    leading_dots = len(module) - len(module.lstrip("."))
    remainder = module[leading_dots:]
    if leading_dots:
        source_module = _normalize_module_name(source_path, project_root)
        if source_module is None:
            return None
        package_parts = source_module.split(".")
        if source_path.name != "__init__.py":
            package_parts = package_parts[:-1]
        parents_to_drop = leading_dots - 1
        if parents_to_drop > len(package_parts):
            return None
        base_parts = package_parts[: len(package_parts) - parents_to_drop]
        module_parts = [*base_parts, *(remainder.split(".") if remainder else [])]
        normalized = ".".join(part for part in module_parts if part)
    else:
        normalized = module

    candidate = normalized
    while candidate:
        target = module_index.get(candidate)
        if target is not None:
            return target
        target = _module_to_path(candidate, project_root)
        if target is not None:
            return target.resolve()
        candidate = candidate.rpartition(".")[0]
    return None


def _resolve_path_reference(
    reference: str,
    *,
    source_path: Path,
    project_root: Path,
) -> Path | None:
    if not reference.startswith("."):
        return None
    base = (source_path.parent / reference).resolve()
    candidates = [base]
    source_suffix = source_path.suffix.casefold()
    if source_suffix in {".ts", ".tsx", ".mts", ".cts"}:
        suffixes = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
    else:
        suffixes = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
    candidates.extend(Path(f"{base}{suffix}") for suffix in suffixes)
    candidates.extend(base / f"index{suffix}" for suffix in suffixes)
    return _first_workspace_file(candidates, project_root)


def _resolve_include_reference(
    reference: str,
    *,
    source_path: Path,
    project_root: Path,
) -> Path | None:
    return _first_workspace_file(
        [
            source_path.parent / reference,
            project_root / reference,
            project_root / "include" / reference,
        ],
        project_root,
    )


def _resolve_rust_reference(
    reference: str,
    *,
    source_path: Path,
    project_root: Path,
) -> Path | None:
    cleaned = reference.replace("{", "").replace("}", "").replace("*", "")
    parts = [part.strip() for part in cleaned.split("::") if part.strip()]
    if not parts:
        return None
    if parts[0] == "crate":
        parts = parts[1:]
        base = project_root / "src" if (project_root / "src").is_dir() else project_root
    elif parts[0] == "self":
        parts = parts[1:]
        base = source_path.parent
    else:
        base = source_path.parent
        while parts and parts[0] == "super":
            base = base.parent
            parts = parts[1:]
    while parts:
        path = base.joinpath(*parts)
        target = _first_workspace_file(
            [path.with_suffix(".rs"), path / "mod.rs"], project_root
        )
        if target is not None:
            return target
        parts.pop()
    return None


def _resolve_dotted_source(
    reference: str, project_root: Path, suffix: str
) -> Path | None:
    parts = [part for part in reference.split(".") if part]
    roots = [
        project_root,
        project_root / "src",
        project_root / "src" / "main" / "java",
        project_root / "src" / "test" / "java",
        project_root / "app" / "src" / "main" / "java",
    ]
    while parts:
        target = _first_workspace_file(
            [root.joinpath(*parts).with_suffix(suffix) for root in roots], project_root
        )
        if target is not None:
            return target
        parts.pop(0)
    return None


def _resolve_go_reference(reference: str, project_root: Path) -> Path | None:
    parts = [part for part in reference.split("/") if part]
    for offset in range(len(parts)):
        directory = project_root.joinpath(*parts[offset:])
        if not directory.is_dir():
            continue
        try:
            files = sorted(directory.glob("*.go"))
        except OSError:
            continue
        target = _first_workspace_file(files, project_root)
        if target is not None:
            return target
    return None


def _first_workspace_file(
    candidates: Iterable[Path], project_root: Path
) -> Path | None:
    root = project_root.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    return None


def _extract_references(symbols: Iterable[Symbol]) -> set[str]:
    """Pull out dotted module references from import statements."""

    refs: set[str] = set()
    for symbol in symbols:
        if symbol.reference:
            refs.add(symbol.reference)
            continue
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
            # Imported names may be submodules (``from pkg import module``).
            for piece in rhs.split(","):
                head = piece.strip().split(" as ")[0].strip()
                if head and head != "*":
                    separator = "" if module.endswith(".") else "."
                    refs.add(f"{module}{separator}{head}" if module else head)
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
        self.project_root = project_root.resolve()
        self._extractor = extractor or SymbolExtractor()
        self._max_files = max_files
        self._exclude_patterns = exclude_patterns or []
        self._files: list[FileNode] = []
        self._module_index: dict[str, Path] = {}
        self._index: dict[Path, int] = {}
        self._adjacency: np.ndarray | None = None
        self._file_cache: dict[Path, tuple[tuple[int, int], FileNode]] = {}
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
        try:
            relative = path.resolve().relative_to(self.project_root.resolve())
        except (OSError, ValueError):
            return True
        return _matches_exclude_pattern(relative, self._exclude_patterns, is_dir=False)

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
                    s
                    for s in node.symbols
                    if s.kind
                    in {
                        "function",
                        "method",
                        "class",
                        "interface",
                        "enum",
                        "struct",
                        "trait",
                        "record",
                        "type",
                    }
                ]
                if not interesting:
                    lines.append("*No definitions extracted.*")
                else:
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
                if self._adjacency[dep_idx, src_idx] > 0:
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
        discovered = _discover_source_files(
            self.project_root,
            exclude_patterns=self._exclude_patterns,
        )
        ignored = _git_ignored_files(self.project_root, discovered)
        paths = [path for path in discovered if path.resolve() not in ignored][
            : self._max_files
        ]
        files: list[FileNode] = []
        module_index: dict[str, Path] = {}
        next_cache: dict[Path, tuple[tuple[int, int], FileNode]] = {}

        for path in paths:
            resolved = path.resolve()
            try:
                stat = path.stat()
            except OSError:
                continue
            fingerprint = (stat.st_mtime_ns, stat.st_size)
            cached = self._file_cache.get(resolved)
            if cached is not None and cached[0] == fingerprint:
                node = cached[1]
            else:
                symbols = tuple(self._extractor.extract(path))
                refs = _extract_references(symbols)
                node = FileNode(
                    path=resolved,
                    symbols=symbols,
                    referenced_modules=tuple(sorted(refs)),
                )
            files.append(node)
            next_cache[resolved] = (fingerprint, node)
            module_name = _normalize_module_name(path, self.project_root)
            if module_name is not None:
                module_index[module_name] = resolved

        self._files = files
        self._file_cache = next_cache
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
                target = _resolve_module_reference(
                    ref,
                    source_path=node.path,
                    project_root=self.project_root,
                    module_index=self._module_index,
                )
                if target is None:
                    continue
                j = self._index.get(target.resolve())
                if j is not None and j != i:
                    # Columns are sources and rows are destinations so M @ v
                    # follows an import edge from the importer to its dependency.
                    matrix[j, i] += 1.0
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

        v = alpha * M @ v + (1 - alpha) * p

    where ``M`` is the column-normalized adjacency (transitions out of
    each node), ``p`` is the teleport vector concentrated on the
    ``teleport_indices``, and ``alpha`` is the damping factor.
    """

    n = adjacency_matrix.shape[0] if adjacency_matrix.ndim == 2 else 0
    if n == 0:
        return np.array([])
    if adjacency_matrix.shape != (n, n):
        raise ValueError("adjacency_matrix must be square")
    if not 0.0 <= alpha < 1.0:
        raise ValueError("alpha must be in the range [0, 1)")

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
        valid = sorted({i for i in teleport_indices if 0 <= i < n})
        if valid:
            p[valid] = 1.0 / len(valid)
        else:
            p[:] = 1.0 / n
    else:
        p[:] = 1.0 / n

    v = np.copy(p)
    for _ in range(max_iter):
        v_next = alpha * np.dot(normalized, v) + (1.0 - alpha) * p
        if np.linalg.norm(v_next - v, 1) < tol:
            return v_next
        v = v_next
    return v
