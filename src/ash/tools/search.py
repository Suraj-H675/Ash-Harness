"""Bounded workspace discovery and text-search tools."""

from __future__ import annotations

import asyncio
import io
import stat
from collections import deque
from dataclasses import dataclass
from time import monotonic
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, Literal, TextIO

import regex as regex_engine
from pydantic import BaseModel, Field

from ash.repo.ignore import DEFAULT_IGNORED_DIRS
from ash.repo.path_glob import matches_relative_glob
from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.safety.scoped_io import (
    list_scoped_directory,
    read_scoped_bytes,
    stat_scoped_path,
)
from ash.tools.base import (
    BaseTool,
    ToolExecutionContract,
    ToolResult,
    count_output_tokens,
    scoped_path_refusal,
)


DEFAULT_MAX_RESULTS = 200
HARD_MAX_RESULTS = 2_000
MAX_SEARCH_CAPTURE_BYTES = 2_000_000
DEFAULT_SEARCH_CAPTURE_BYTES = 16_384
MAX_SEARCH_LINE_CHARS = 64 * 1024
MAX_SEARCH_CONTEXT_PREVIEW_CHARS = 512
MAX_SEARCH_CONTEXT_CAPTURE_BYTES = 24_576
MAX_SEARCH_CONTEXT_LINES = 10
MAX_LIST_DIRECTORY_DEPTH = 4
MAX_GLOB_SCAN_ENTRIES = 100_000
MAX_GLOB_DEPTH = 32
MAX_SEARCH_SCAN_ENTRIES = 100_000
MAX_SEARCH_DEPTH = 32
MAX_SEARCH_FILE_BYTES = 8 * 1024 * 1024
WORKSPACE_SCAN_YIELD_INTERVAL = 64
MAX_SEARCH_REGEX_MATCH_SECONDS = 0.05
MAX_SEARCH_REGEX_TOTAL_SECONDS = 1.0
MAX_SEARCH_GROUPED_CONTEXT_ROWS = HARD_MAX_RESULTS * MAX_SEARCH_CONTEXT_LINES * 2
_SEARCH_GROUPED_ROW_OVERHEAD_BYTES = 128
NO_MATCH_SEARCH_FEEDBACK = (
    "No matches found; adjust the pattern or directory_path and retry."
)


@dataclass(frozen=True, slots=True)
class _SearchRow:
    path: str
    line_number: int
    preview: str
    kind: Literal["match", "context"]
    preview_start: int = 0
    preview_truncated: bool = False

    @property
    def is_match(self) -> bool:
        return self.kind == "match"


@dataclass(frozen=True, slots=True)
class _SearchGroup:
    path: str
    rows: tuple[_SearchRow, ...]

    @property
    def start_line(self) -> int:
        return self.rows[0].line_number

    @property
    def end_line(self) -> int:
        return self.rows[-1].line_number


def _group_search_rows(rows: Iterable[_SearchRow]) -> list[_SearchGroup]:
    """Coalesce ordered rows into contiguous, hit-bearing path ranges."""
    path_order: list[str] = []
    rows_by_path: dict[str, dict[int, _SearchRow]] = {}
    for row in rows:
        if row.path not in rows_by_path:
            path_order.append(row.path)
            rows_by_path[row.path] = {}
        by_line = rows_by_path[row.path]
        current = by_line.get(row.line_number)
        if current is None or (row.is_match and not current.is_match):
            by_line[row.line_number] = row

    groups: list[_SearchGroup] = []
    for path in path_order:
        current_rows: list[_SearchRow] = []
        for row in (rows_by_path[path][line] for line in sorted(rows_by_path[path])):
            if current_rows and row.line_number != current_rows[-1].line_number + 1:
                if any(item.is_match for item in current_rows):
                    groups.append(_SearchGroup(path, tuple(current_rows)))
                current_rows = []
            current_rows.append(row)
        if current_rows and any(item.is_match for item in current_rows):
            groups.append(_SearchGroup(path, tuple(current_rows)))
    return groups


def _render_search_group(group: _SearchGroup) -> str:
    rendered = [f"{group.path} (lines {group.start_line}-{group.end_line})"]
    for row in group.rows:
        marker = ">" if row.is_match else " "
        column = f"[column {row.preview_start + 1}] " if row.preview_start else ""
        rendered.append(f"{marker} {row.line_number}: {column}{row.preview}")
    return "\n".join(rendered)


def _render_search_groups(groups: Iterable[_SearchGroup]) -> str:
    rendered: list[str] = []
    for group in groups:
        if rendered:
            rendered.append("...")
        rendered.append(_render_search_group(group))
    return "\n".join(rendered)


def _iter_workspace_paths(
    root: Path,
    guard: SafetyGuard,
    *,
    recursive: bool,
    max_depth: int | None = None,
    on_incomplete: Callable[[], None] | None = None,
    ignored_dirs: frozenset[str] = frozenset(),
    on_skipped: Callable[[str], None] | None = None,
) -> Iterator[tuple[Path, bool]]:
    """Yield workspace entries lazily without descending through links."""

    def walk(directory: Path, depth: int) -> Iterator[tuple[Path, bool]]:
        if max_depth is not None and depth >= max_depth:
            return
        try:
            _, children = list_scoped_directory(directory, guard)
            for name, is_directory in sorted(children):
                if is_directory and name in ignored_dirs:
                    if on_skipped is not None:
                        on_skipped(name)
                    continue
                path = directory / name
                yield path, is_directory
                if not recursive or (max_depth is not None and depth + 1 >= max_depth):
                    if (
                        recursive
                        and is_directory
                        and max_depth is not None
                        and depth + 1 >= max_depth
                        and on_incomplete is not None
                    ):
                        on_incomplete()
                    continue
                if is_directory:
                    yield from walk(path, depth + 1)
        except (OSError, SafetyViolation):
            if on_incomplete is not None:
                on_incomplete()
            return

    yield from walk(root, 0)


def _is_scoped_directory(path: Path, guard: SafetyGuard) -> bool:
    try:
        _, metadata = stat_scoped_path(path, guard)
    except (OSError, SafetyViolation):
        return False
    return stat.S_ISDIR(metadata.st_mode)


def _matches_workspace_glob(relative_path: str, pattern: str) -> bool:
    return matches_relative_glob(relative_path, pattern)


def _skipped_generated_note(skipped: set[str]) -> str:
    if not skipped:
        return ""
    return (
        "[skipped generated directories: "
        + ", ".join(sorted(skipped))
        + "; use directory_path to search one explicitly]"
    )


def _generated_descendants_to_skip(root: Path, guard: SafetyGuard) -> frozenset[str]:
    """Skip generated descendants unless the selected root is already inside one.

    Broad code discovery should behave the same in `src/` as at the project
    root. An explicit `directory_path` within a generated tree opts into that
    tree, including its nested caches and dependencies.
    """
    relative_parts = root.relative_to(guard.project_root).parts
    return (
        frozenset()
        if any(part in DEFAULT_IGNORED_DIRS for part in relative_parts)
        else DEFAULT_IGNORED_DIRS
    )


class ListDirectoryArgs(BaseModel):
    directory_path: str = Field(
        ".", description="Project-relative directory; '.' lists the project root."
    )
    recursive: bool = False
    max_results: int = Field(DEFAULT_MAX_RESULTS, ge=1, le=HARD_MAX_RESULTS)


class ListDirectoryTool(BaseTool):
    name = "list_dir"
    execution_contract = ToolExecutionContract(parallel_safe=True)
    description = (
        "List bounded workspace files and directories with project-relative paths."
    )
    args_schema = ListDirectoryArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ListDirectoryArgs(**kwargs)
        try:
            root = self.safety_guard.validate_path(args.directory_path)
        except SafetyViolation as exc:
            return scoped_path_refusal(exc)
        if not _is_scoped_directory(root, self.safety_guard):
            return ToolResult(
                success=False, output="", error=f"Not a directory: {root}"
            )
        entries: list[str] = []
        truncated = False
        scan_incomplete = False
        skipped_generated: set[str] = set()

        def mark_incomplete() -> None:
            nonlocal scan_incomplete
            scan_incomplete = True

        scanned = 0
        for path, is_directory in _iter_workspace_paths(
            root,
            self.safety_guard,
            recursive=args.recursive,
            max_depth=MAX_LIST_DIRECTORY_DEPTH,
            on_incomplete=mark_incomplete if args.recursive else None,
            ignored_dirs=(
                _generated_descendants_to_skip(root, self.safety_guard)
                if args.recursive
                else frozenset()
            ),
            on_skipped=skipped_generated.add if args.recursive else None,
        ):
            scanned += 1
            if len(entries) >= args.max_results:
                truncated = True
                break
            if scanned % WORKSPACE_SCAN_YIELD_INTERVAL == 0:
                await asyncio.sleep(0)
            relative = path.relative_to(self.safety_guard.project_root).as_posix()
            entries.append(relative + ("/" if is_directory else ""))
        entries.sort()
        def build_result(selected: list[str], context_limited: bool) -> ToolResult:
            output = "\n".join(selected)
            if truncated and not context_limited:
                output += f"\n[truncated after {args.max_results} entries]"
            if scan_incomplete:
                output += (
                    ("\n" if output else "")
                    + "[workspace directory listing incomplete]"
                )
            if context_limited:
                other_limit = (
                    f"; max_results={args.max_results} also reached"
                    if truncated else ""
                )
                output += (
                    ("\n" if output else "")
                    + "[context budget truncated directory results"
                    + other_limit
                    + "; narrow the path or request fewer entries.]"
                )
            note = _skipped_generated_note(skipped_generated)
            if note:
                output += ("\n" if output else "") + note
            return ToolResult(
                success=True,
                output=output,
                token_count=count_output_tokens(output),
                truncated=truncated or scan_incomplete or context_limited,
            )

        budget = self.output_budget
        if budget is not None:
            return budget.fit_lines(entries, build_result)
        return build_result(entries, False)


class GlobFilesArgs(BaseModel):
    pattern: str = Field(
        ...,
        min_length=1,
        description="Glob relative to directory_path; returned paths are project-relative.",
    )
    directory_path: str = Field(
        ".", description="Project-relative search directory; '.' is the project root."
    )
    max_results: int = Field(DEFAULT_MAX_RESULTS, ge=1, le=HARD_MAX_RESULTS)


class GlobFilesTool(BaseTool):
    name = "glob_files"
    execution_contract = ToolExecutionContract(parallel_safe=True)
    description = (
        "Find workspace files by glob (e.g. '**/*.py'). Broad searches skip "
        "known generated directories; set directory_path to include one. "
        "Returned paths are project-relative."
    )
    args_schema = GlobFilesArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = GlobFilesArgs(**kwargs)
        try:
            root = self.safety_guard.validate_path(args.directory_path)
        except SafetyViolation as exc:
            return scoped_path_refusal(exc)
        if not _is_scoped_directory(root, self.safety_guard):
            return ToolResult(
                success=False, output="", error=f"Not a directory: {root}"
            )
        matches: list[str] = []
        match_truncated = False
        scan_truncated = False
        scan_incomplete = False
        skipped_generated: set[str] = set()

        def mark_incomplete() -> None:
            nonlocal scan_incomplete
            scan_incomplete = True

        scanned = 0
        for path, is_directory in _iter_workspace_paths(
            root,
            self.safety_guard,
            recursive=True,
            max_depth=MAX_GLOB_DEPTH,
            on_incomplete=mark_incomplete,
            ignored_dirs=_generated_descendants_to_skip(root, self.safety_guard),
            on_skipped=skipped_generated.add,
        ):
            scanned += 1
            if scanned > MAX_GLOB_SCAN_ENTRIES:
                scan_truncated = True
                break
            if scanned % WORKSPACE_SCAN_YIELD_INTERVAL == 0:
                await asyncio.sleep(0)
            if is_directory:
                continue
            try:
                _, metadata = stat_scoped_path(path, self.safety_guard)
                if not stat.S_ISREG(metadata.st_mode):
                    continue
            except (OSError, SafetyViolation):
                scan_incomplete = True
                continue
            relative_to_search = path.relative_to(root).as_posix()
            if not _matches_workspace_glob(relative_to_search, args.pattern):
                continue
            if len(matches) >= args.max_results:
                match_truncated = True
                break
            matches.append(path.relative_to(self.safety_guard.project_root).as_posix())
        matches.sort()
        def build_result(selected: list[str], context_limited: bool) -> ToolResult:
            output = "\n".join(selected)
            if match_truncated and not context_limited:
                output += f"\n[truncated after {args.max_results} matches]"
            if scan_truncated:
                output += (
                    ("\n" if output else "")
                    + f"[workspace scan truncated after {MAX_GLOB_SCAN_ENTRIES} entries]"
                )
            elif scan_incomplete:
                output += ("\n" if output else "") + "[workspace glob incomplete]"
            if context_limited:
                other_limit = (
                    f"; max_results={args.max_results} also reached"
                    if match_truncated else ""
                )
                output += (
                    ("\n" if output else "")
                    + "[context budget truncated glob results"
                    + other_limit
                    + "; narrow the pattern or request fewer matches.]"
                )
            note = _skipped_generated_note(skipped_generated)
            if note:
                output += ("\n" if output else "") + note
            return ToolResult(
                success=True,
                output=output,
                token_count=count_output_tokens(output),
                truncated=(
                    match_truncated
                    or scan_truncated
                    or scan_incomplete
                    or context_limited
                ),
            )

        budget = self.output_budget
        if budget is not None:
            return budget.fit_lines(matches, build_result)
        return build_result(matches, False)


class SearchTextArgs(BaseModel):
    pattern: str = Field(..., min_length=1, max_length=4_096)
    directory_path: str = Field(
        ".",
        description="Project-relative directory/file; output paths are project-relative.",
    )
    glob: str | None = None
    fixed_strings: bool = False
    case_sensitive: bool = True
    files_only: bool = Field(
        False,
        description="List one file:line per matching file (no source excerpts).",
    )
    max_results: int = Field(DEFAULT_MAX_RESULTS, ge=1, le=HARD_MAX_RESULTS)
    context_lines: int = Field(
        0,
        ge=0,
        le=MAX_SEARCH_CONTEXT_LINES,
    )
    grouped_context: bool = Field(
        False,
        description=(
            "Group matching lines and optional context into numbered, inclusive "
            "path ranges."
        ),
    )
    max_output_bytes: int = Field(
        DEFAULT_SEARCH_CAPTURE_BYTES, ge=1_024, le=MAX_SEARCH_CAPTURE_BYTES
    )


class SearchTextTool(BaseTool):
    name = "search_text"
    execution_contract = ToolExecutionContract(parallel_safe=True)
    description = (
        "Search source text to find function definitions, call sites, and "
        "symbol usages by textual matches (not semantic reference analysis). "
        "Return file:line matches; scope with directory_path, and use "
        "context_lines for nearby lines or grouped_context for numbered ranges."
    )
    args_schema = SearchTextArgs

    def json_schema(self) -> dict[str, Any]:
        schema = super().json_schema()
        for name in (
            "context_lines",
            "grouped_context",
            "max_output_bytes",
            "files_only",
        ):
            schema["properties"][name].pop("default", None)
        return schema

    async def run(self, **kwargs: Any) -> ToolResult:
        args = SearchTextArgs(**kwargs)
        if args.files_only and args.grouped_context:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "files_only=true cannot be combined with grouped_context; "
                    "use normal search results to inspect numbered source ranges."
                ),
            )
        if args.files_only and args.context_lines:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "files_only=true cannot be combined with context_lines; "
                    "use normal search results to inspect nearby source."
                ),
            )
        try:
            root = self.safety_guard.validate_path(args.directory_path)
        except SafetyViolation as exc:
            return scoped_path_refusal(exc)
        try:
            _, metadata = stat_scoped_path(root, self.safety_guard)
        except (OSError, SafetyViolation):
            return ToolResult(
                success=False, output="", error=f"Not a directory or file: {root}"
            )
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            return ToolResult(
                success=False, output="", error=f"Not a directory or file: {root}"
            )
        return await self._python_fallback(
            root, args, single_file=stat.S_ISREG(metadata.st_mode)
        )

    @staticmethod
    def _compile_pattern(args: SearchTextArgs) -> regex_engine.Pattern | ToolResult:
        flags = regex_engine.VERSION0
        if not args.case_sensitive:
            flags |= regex_engine.IGNORECASE
        expression = (
            regex_engine.escape(args.pattern) if args.fixed_strings else args.pattern
        )
        try:
            return regex_engine.compile(expression, flags)
        except regex_engine.error as exc:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Invalid regex: {exc}. Escape regex special characters or "
                    "use fixed_strings=true for a literal search."
                ),
            )

    def preflight_pattern(self, **kwargs: Any) -> ToolResult | None:
        """Validate a pattern without reading files or dispatching a tool."""
        result = self._compile_pattern(SearchTextArgs(**kwargs))
        return result if isinstance(result, ToolResult) else None

    async def _python_fallback(
        self,
        root: Path,
        args: SearchTextArgs,
        *,
        single_file: bool,
    ) -> ToolResult:
        regex = self._compile_pattern(args)
        if isinstance(regex, ToolResult):
            return regex

        capture_limit = (
            min(args.max_output_bytes, MAX_SEARCH_CONTEXT_CAPTURE_BYTES)
            if args.context_lines
            else min(args.max_output_bytes, MAX_SEARCH_CAPTURE_BYTES)
        )
        match_preview_limit = min(MAX_SEARCH_LINE_CHARS, max(256, capture_limit // 2))

        regex_time_remaining = MAX_SEARCH_REGEX_TOTAL_SECONDS
        regex_incomplete = False

        def bounded_lines(
            handle: TextIO,
        ) -> Iterator[tuple[int, str, bool, bool, int]]:
            nonlocal regex_time_remaining, regex_incomplete
            line_number = 0
            file_bytes = 0
            while True:
                line_chunks: list[str] = []
                saw_data = False
                complete = False
                while True:
                    chunk = handle.readline(MAX_SEARCH_LINE_CHARS)
                    if not chunk:
                        break
                    file_bytes += len(chunk.encode("utf-8"))
                    if file_bytes > MAX_SEARCH_FILE_BYTES:
                        return
                    saw_data = True
                    line_chunks.append(chunk)
                    if chunk.endswith(("\n", "\r")):
                        complete = True
                        break
                if not saw_data:
                    return
                line_number += 1
                scanned_line = "".join(line_chunks)
                line = scanned_line.rstrip("\r\n")
                if regex_time_remaining <= 0:
                    regex_incomplete = True
                    return
                started_at = monotonic()
                try:
                    match = regex.search(
                        scanned_line,
                        timeout=min(
                            MAX_SEARCH_REGEX_MATCH_SECONDS,
                            regex_time_remaining,
                        ),
                    )
                except TimeoutError:
                    regex_incomplete = True
                    return
                finally:
                    regex_time_remaining = max(
                        0.0,
                        regex_time_remaining - (monotonic() - started_at),
                    )
                if match is not None and args.files_only:
                    # The caller only wants a path and first location. Avoid
                    # even constructing a potentially huge source excerpt.
                    yield (line_number, "", True, False, 0)
                    if not complete:
                        return
                    continue
                preview_start = 0
                if match is not None and match.start() > 0:
                    # A match late in a long line should be visible even when
                    # the search-result budget only permits an excerpt.
                    prefix_bytes = len(line[: match.start()].encode("utf-8"))
                    if prefix_bytes >= match_preview_limit - 80:
                        preview_start = max(0, match.start() - 80)
                preview = line[preview_start : preview_start + MAX_SEARCH_LINE_CHARS]
                clipped = preview_start > 0 or (
                    preview_start + len(preview) < len(line)
                )
                yield (
                    line_number,
                    preview,
                    match is not None,
                    clipped,
                    preview_start,
                )
                if not complete:
                    return

        matches: list[str] = []
        match_count = 0
        output_bytes = 0
        output_limited = False
        match_limited = False
        scan_truncated = False
        scan_incomplete = False
        skipped_file = False
        preview_truncated = False
        skipped_generated: set[str] = set()
        grouped_hits: list[_SearchRow] = []
        grouped_hit_keys: set[tuple[str, int]] = set()
        grouped_hit_paths: set[str] = set()
        grouped_context_rows: dict[tuple[str, int], _SearchRow] = {}
        grouped_hit_bytes = 0
        grouped_context_bytes = 0
        grouped_context_limited = False

        def append_result(rendered: str) -> bool:
            nonlocal output_bytes, output_limited
            rendered_bytes = len(rendered.encode("utf-8"))
            separator_bytes = 1 if matches else 0
            if output_bytes + separator_bytes + rendered_bytes > capture_limit:
                output_limited = True
                return False
            matches.append(rendered)
            output_bytes += separator_bytes + rendered_bytes
            return True

        def context_preview(text: str, line_truncated: bool) -> str:
            nonlocal preview_truncated
            clipped = text[:MAX_SEARCH_CONTEXT_PREVIEW_CHARS]
            if len(text) > len(clipped) or line_truncated:
                preview_truncated = True
                clipped += " [context preview truncated]"
            return clipped

        def match_preview(text: str, line_truncated: bool) -> str:
            nonlocal preview_truncated
            # One pathological source line must not consume the entire search
            # budget and hide even the first match location.
            encoded = text.encode("utf-8")
            if len(encoded) > match_preview_limit:
                text = encoded[:match_preview_limit].decode("utf-8", errors="ignore")
                line_truncated = True
            if line_truncated:
                preview_truncated = True
                text += " [line preview truncated]"
            return text

        def add_grouped_context(
            path: str,
            line_number: int,
            text: str,
            line_truncated: bool,
        ) -> None:
            nonlocal grouped_context_bytes, grouped_context_limited
            key = (path, line_number)
            if key in grouped_hit_keys or key in grouped_context_rows:
                return
            preview = context_preview(text, line_truncated)
            row = _SearchRow(
                path=path,
                line_number=line_number,
                preview=preview,
                kind="context",
                preview_truncated=(
                    line_truncated or len(text) > MAX_SEARCH_CONTEXT_PREVIEW_CHARS
                ),
            )
            row_bytes = len(path.encode("utf-8")) + len(preview.encode("utf-8")) + 64
            if (
                len(grouped_context_rows) >= MAX_SEARCH_GROUPED_CONTEXT_ROWS
                or grouped_context_bytes + row_bytes > MAX_SEARCH_CONTEXT_CAPTURE_BYTES
            ):
                grouped_context_limited = True
                return
            grouped_context_rows[key] = row
            grouped_context_bytes += row_bytes

        def discard_grouped_context(path: str, line_number: int) -> None:
            nonlocal grouped_context_bytes
            row = grouped_context_rows.pop((path, line_number), None)
            if row is not None:
                grouped_context_bytes -= (
                    len(row.path.encode("utf-8"))
                    + len(row.preview.encode("utf-8"))
                    + 64
                )

        def mark_scan_incomplete() -> None:
            nonlocal scan_incomplete
            scan_incomplete = True

        scanned = 0
        candidates: Iterator[tuple[Path, bool]] = (
            iter(((root, False),))
            if single_file
            else _iter_workspace_paths(
                root,
                self.safety_guard,
                recursive=True,
                max_depth=MAX_SEARCH_DEPTH,
                on_incomplete=mark_scan_incomplete,
                ignored_dirs=_generated_descendants_to_skip(root, self.safety_guard),
                on_skipped=skipped_generated.add,
            )
        )
        for path, is_directory in candidates:
            scanned += 1
            if scanned > MAX_SEARCH_SCAN_ENTRIES:
                scan_truncated = True
                break
            if scanned % WORKSPACE_SCAN_YIELD_INTERVAL == 0:
                await asyncio.sleep(0)
            if is_directory:
                continue
            relative_to_search = path.relative_to(
                self.safety_guard.project_root if single_file else root
            ).as_posix()
            if args.glob and not (
                _matches_workspace_glob(relative_to_search, args.glob)
                or (single_file and _matches_workspace_glob(path.name, args.glob))
            ):
                continue
            relative = path.relative_to(self.safety_guard.project_root).as_posix()
            try:
                _, content = read_scoped_bytes(
                    path,
                    self.safety_guard,
                    max_bytes=MAX_SEARCH_FILE_BYTES,
                )
                with io.TextIOWrapper(
                    io.BytesIO(content),
                    encoding="utf-8",
                ) as handle:
                    preceding: deque[tuple[int, str, bool]] = deque(
                        maxlen=args.context_lines
                    )
                    trailing_until = 0
                    last_rendered_line = 0
                    for (
                        line_number, text, matched, line_truncated, preview_start
                    ) in bounded_lines(handle):
                        if matched:
                            if args.files_only:
                                if match_count >= args.max_results:
                                    match_limited = True
                                    break
                                if not append_result(f"{relative}:{line_number}"):
                                    break
                                match_count += 1
                                # Move to the next file without consuming
                                # more model context on repeated matches here.
                                break
                            if match_count >= args.max_results:
                                if args.grouped_context:
                                    discard_grouped_context(relative, line_number)
                                match_limited = True
                                break
                            if args.grouped_context:
                                for (
                                    previous_line,
                                    previous_text,
                                    previous_clipped,
                                ) in preceding:
                                    add_grouped_context(
                                        relative,
                                        previous_line,
                                        previous_text,
                                        previous_clipped,
                                    )
                                preview = match_preview(text, line_truncated)
                                row = _SearchRow(
                                    path=relative,
                                    line_number=line_number,
                                    preview=preview,
                                    kind="match",
                                    preview_start=preview_start,
                                    preview_truncated=(
                                        line_truncated
                                        or len(text.encode("utf-8"))
                                        > match_preview_limit
                                    ),
                                )
                                row_bytes = (
                                    (
                                        0
                                        if relative in grouped_hit_paths
                                        else len(relative.encode("utf-8"))
                                    )
                                    + len(preview.encode("utf-8"))
                                    + _SEARCH_GROUPED_ROW_OVERHEAD_BYTES
                                )
                                discard_grouped_context(relative, line_number)
                                if (
                                    grouped_hit_bytes + row_bytes
                                    > MAX_SEARCH_CAPTURE_BYTES
                                ):
                                    output_limited = True
                                    break
                                key = (relative, line_number)
                                grouped_hits.append(row)
                                grouped_hit_keys.add(key)
                                grouped_hit_paths.add(relative)
                                grouped_hit_bytes += row_bytes
                                match_count += 1
                                trailing_until = line_number + args.context_lines
                            else:
                                for (
                                    previous_line,
                                    previous_text,
                                    previous_clipped,
                                ) in preceding:
                                    if previous_line <= last_rendered_line:
                                        continue
                                    preview = context_preview(
                                        previous_text, previous_clipped
                                    )
                                    if not append_result(
                                        f"{relative}-{previous_line}-{preview}"
                                    ):
                                        break
                                    last_rendered_line = previous_line
                                if output_limited:
                                    break
                                preview = match_preview(text, line_truncated)
                                column_label = (
                                    f"[column {preview_start + 1}] "
                                    if preview_start
                                    else ""
                                )
                                if not append_result(
                                    f"{relative}:{line_number}:{column_label}{preview}"
                                ):
                                    break
                                match_count += 1
                                last_rendered_line = line_number
                                trailing_until = line_number + args.context_lines
                        elif args.grouped_context:
                            if args.context_lines and line_number <= trailing_until:
                                add_grouped_context(
                                    relative, line_number, text, line_truncated
                                )
                        elif args.context_lines and line_number <= trailing_until:
                            preview = context_preview(text, line_truncated)
                            if not append_result(f"{relative}-{line_number}-{preview}"):
                                break
                            last_rendered_line = line_number
                        if args.context_lines:
                            preceding.append((line_number, text, line_truncated))
            except (OSError, UnicodeError, SafetyViolation):
                skipped_file = True
                continue
            if regex_incomplete:
                break
            if output_limited or match_limited:
                break

        if args.grouped_context:

            def available_context_rows(
                selected_hits: list[_SearchRow],
            ) -> list[_SearchRow]:
                if not args.context_lines:
                    return []
                allowed_context: set[tuple[str, int]] = set()
                for hit in selected_hits:
                    for distance in range(1, args.context_lines + 1):
                        if hit.line_number > distance:
                            allowed_context.add(
                                (hit.path, hit.line_number - distance)
                            )
                        allowed_context.add((hit.path, hit.line_number + distance))
                return [
                    row
                    for key, row in grouped_context_rows.items()
                    if key in allowed_context and key not in grouped_hit_keys
                ]

            def build_grouped_result(
                selected_hits: list[_SearchRow],
                *,
                include_context: bool,
                context_omitted: bool,
                capture_prefix_limited: bool,
                provider_limited: bool,
            ) -> ToolResult:
                rows: list[_SearchRow] = list(selected_hits)
                if include_context:
                    rows.extend(available_context_rows(selected_hits))
                output = _render_search_groups(_group_search_rows(rows))
                notes: list[str] = []
                capture_truncated = output_limited or capture_prefix_limited
                if (
                    not grouped_hits
                    and not regex_incomplete
                    and not capture_truncated
                    and not match_limited
                    and not scan_truncated
                    and not scan_incomplete
                    and not skipped_file
                ):
                    output = NO_MATCH_SEARCH_FEEDBACK
                elif regex_incomplete:
                    notes.append(
                        "[workspace search incomplete: regex time budget exhausted]"
                    )
                elif capture_truncated:
                    notes.append(
                        f"[search output capture truncated after {capture_limit} bytes]"
                    )
                elif match_limited:
                    notes.append(f"[truncated after {args.max_results} matches]")
                elif scan_truncated:
                    notes.append(
                        f"[workspace scan truncated after {MAX_SEARCH_SCAN_ENTRIES} entries]"
                    )
                elif scan_incomplete:
                    notes.append("[workspace search incomplete]")
                elif skipped_file:
                    notes.append(
                        "[workspace search incomplete: skipped unreadable or invalid text files]"
                    )
                if context_omitted:
                    notes.append("[search context omitted to preserve matching lines]")
                if provider_limited:
                    other_limit = (
                        f"; max_results={args.max_results} also reached"
                        if match_limited
                        else ""
                    )
                    notes.append(
                        "[context budget truncated search results"
                        + other_limit
                        + "; narrow the pattern or directory_path to continue.]"
                    )
                note = _skipped_generated_note(skipped_generated)
                if note:
                    notes.append(note)
                if notes:
                    output += ("\n" if output else "") + "\n".join(notes)
                return ToolResult(
                    success=True,
                    output=output,
                    token_count=count_output_tokens(output),
                    truncated=(
                        regex_incomplete
                        or capture_truncated
                        or match_limited
                        or scan_truncated
                        or scan_incomplete
                        or skipped_file
                        or preview_truncated
                        or context_omitted
                        or provider_limited
                    ),
                )

            all_hits = list(grouped_hits)
            result = build_grouped_result(
                all_hits,
                include_context=True,
                context_omitted=grouped_context_limited,
                capture_prefix_limited=False,
                provider_limited=False,
            )
            selected_hits = all_hits
            context_omitted = grouped_context_limited
            capture_prefix_limited = False
            if len(result.output.encode("utf-8")) > capture_limit:
                context_omitted = (
                    grouped_context_limited or bool(available_context_rows(all_hits))
                )
                result = build_grouped_result(
                    all_hits,
                    include_context=False,
                    context_omitted=context_omitted,
                    capture_prefix_limited=False,
                    provider_limited=False,
                )
                if len(result.output.encode("utf-8")) > capture_limit:
                    low = 0
                    high = len(all_hits)
                    while low < high:
                        middle = (low + high + 1) // 2
                        hit_prefix = all_hits[:middle]
                        candidate = build_grouped_result(
                            hit_prefix,
                            include_context=False,
                            context_omitted=(
                                grouped_context_limited
                                or bool(available_context_rows(hit_prefix))
                            ),
                            capture_prefix_limited=True,
                            provider_limited=False,
                        )
                        if len(candidate.output.encode("utf-8")) <= capture_limit:
                            low = middle
                        else:
                            high = middle - 1
                    selected_hits = all_hits[:low]
                    context_omitted = (
                        grouped_context_limited
                        or bool(available_context_rows(selected_hits))
                    )
                    capture_prefix_limited = True
                    result = build_grouped_result(
                        selected_hits,
                        include_context=False,
                        context_omitted=context_omitted,
                        capture_prefix_limited=True,
                        provider_limited=False,
                    )

            budget = self.output_budget
            if budget is None or budget.accepts(result):
                return result

            hit_entries = [f"{hit.path}:{hit.line_number}" for hit in selected_hits]

            def build_provider_prefix(
                selected: list[str], provider_limited: bool
            ) -> ToolResult:
                return build_grouped_result(
                    selected_hits[: len(selected)],
                    include_context=False,
                    context_omitted=(
                        grouped_context_limited
                        or bool(
                            available_context_rows(
                                selected_hits[: len(selected)]
                            )
                        )
                    ),
                    capture_prefix_limited=capture_prefix_limited,
                    provider_limited=provider_limited,
                )

            return budget.fit_lines(hit_entries, build_provider_prefix)

        def build_result(selected: list[str], context_limited: bool) -> ToolResult:
            output = "\n".join(selected)
            separator = "\n" if output else ""
            if (
                not matches
                and not regex_incomplete
                and not output_limited
                and not match_limited
                and not scan_truncated
                and not scan_incomplete
                and not skipped_file
            ):
                output = NO_MATCH_SEARCH_FEEDBACK
            elif regex_incomplete:
                output += (
                    f"{separator}[workspace search incomplete: "
                    "regex time budget exhausted]"
                )
            elif output_limited and not context_limited:
                output += (
                    f"{separator}[search output capture truncated after "
                    f"{capture_limit} bytes]"
                )
            elif match_limited and not context_limited:
                unit = "matching files" if args.files_only else "matches"
                output += f"{separator}[truncated after {args.max_results} {unit}]"
            elif scan_truncated:
                output += (
                    f"{separator}[workspace scan truncated after "
                    f"{MAX_SEARCH_SCAN_ENTRIES} entries]"
                )
            elif scan_incomplete:
                output += f"{separator}[workspace search incomplete]"
            elif skipped_file:
                output += (
                    f"{separator}[workspace search incomplete: "
                    "skipped unreadable or invalid text files]"
                )
            if context_limited:
                other_limit = (
                    "; output capture limit also reached"
                    if output_limited
                    else (
                        f"; max_results={args.max_results} also reached"
                        if match_limited else ""
                    )
                )
                output += (
                    ("\n" if output else "")
                    + "[context budget truncated search results"
                    + other_limit
                    + "; narrow the pattern or directory_path to continue.]"
                )
            note = _skipped_generated_note(skipped_generated)
            if note:
                output += ("\n" if output else "") + note
            return ToolResult(
                success=True,
                output=output,
                token_count=count_output_tokens(output),
                truncated=(
                    regex_incomplete
                    or output_limited
                    or match_limited
                    or scan_truncated
                    or scan_incomplete
                    or skipped_file
                    or preview_truncated
                    or context_limited
                ),
            )

        budget = self.output_budget
        if budget is not None:
            return budget.fit_lines(matches, build_result)
        return build_result(matches, False)
