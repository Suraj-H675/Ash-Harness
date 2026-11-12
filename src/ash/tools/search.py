"""Bounded workspace discovery and text-search tools."""

from __future__ import annotations

import asyncio
import fnmatch
import io
import stat
from collections import deque
from time import monotonic
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TextIO

import regex as regex_engine
from pydantic import BaseModel, Field

from ash.repo.ignore import DEFAULT_IGNORED_DIRS
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
MAX_LIST_DIRECTORY_DEPTH = 4
MAX_GLOB_SCAN_ENTRIES = 100_000
MAX_GLOB_DEPTH = 32
MAX_SEARCH_SCAN_ENTRIES = 100_000
MAX_SEARCH_DEPTH = 32
MAX_SEARCH_FILE_BYTES = 8 * 1024 * 1024
WORKSPACE_SCAN_YIELD_INTERVAL = 64
MAX_SEARCH_REGEX_MATCH_SECONDS = 0.05
MAX_SEARCH_REGEX_TOTAL_SECONDS = 1.0


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
    return fnmatch.fnmatchcase(relative_path, pattern) or (
        pattern.startswith("**/")
        and fnmatch.fnmatchcase(relative_path, pattern[3:])
    )


def _skipped_generated_note(skipped: set[str]) -> str:
    if not skipped:
        return ""
    return (
        "[skipped generated directories: "
        + ", ".join(sorted(skipped))
        + "; use directory_path to search one explicitly]"
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
        output = "\n".join(entries)
        if truncated:
            output += f"\n[truncated after {args.max_results} entries]"
        elif scan_incomplete:
            output += ("\n" if output else "") + "[workspace directory listing incomplete]"
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
            truncated=truncated or scan_incomplete,
        )


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
            ignored_dirs=(
                DEFAULT_IGNORED_DIRS
                if root == self.safety_guard.project_root
                else frozenset()
            ),
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
        output = "\n".join(matches)
        if match_truncated:
            output += f"\n[truncated after {args.max_results} matches]"
        elif scan_truncated:
            output += (
                f"\n[workspace scan truncated after {MAX_GLOB_SCAN_ENTRIES} entries]"
            )
        elif scan_incomplete:
            output += ("\n" if output else "") + "[workspace glob incomplete]"
        note = _skipped_generated_note(skipped_generated)
        if note:
            output += ("\n" if output else "") + note
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
            truncated=match_truncated or scan_truncated or scan_incomplete,
        )


class SearchTextArgs(BaseModel):
    pattern: str = Field(..., min_length=1, max_length=4_096)
    directory_path: str = Field(
        ".",
        description="Project-relative directory/file; output paths are project-relative.",
    )
    glob: str | None = None
    fixed_strings: bool = False
    case_sensitive: bool = True
    max_results: int = Field(DEFAULT_MAX_RESULTS, ge=1, le=HARD_MAX_RESULTS)
    context_lines: int = Field(0, ge=0, le=3)
    max_output_bytes: int = Field(
        DEFAULT_SEARCH_CAPTURE_BYTES, ge=1_024, le=MAX_SEARCH_CAPTURE_BYTES
    )


class SearchTextTool(BaseTool):
    name = "search_text"
    execution_contract = ToolExecutionContract(parallel_safe=True)
    description = (
        "Search text with file:line matches. Scope with directory_path; "
        "context_lines adds nearby lines."
    )
    args_schema = SearchTextArgs

    def json_schema(self) -> dict[str, Any]:
        schema = super().json_schema()
        for name in ("context_lines", "max_output_bytes"):
            schema["properties"][name].pop("default", None)
        return schema

    async def run(self, **kwargs: Any) -> ToolResult:
        args = SearchTextArgs(**kwargs)
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

    async def _python_fallback(
        self,
        root: Path,
        args: SearchTextArgs,
        *,
        single_file: bool,
    ) -> ToolResult:
        flags = regex_engine.VERSION0
        if not args.case_sensitive:
            flags |= regex_engine.IGNORECASE
        expression = (
            regex_engine.escape(args.pattern) if args.fixed_strings else args.pattern
        )
        try:
            regex = regex_engine.compile(expression, flags)
        except regex_engine.error as exc:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Invalid regex: {exc}. Escape regex special characters or "
                    "use fixed_strings=true for a literal search."
                ),
            )

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
                ignored_dirs=(
                    DEFAULT_IGNORED_DIRS
                    if root == self.safety_guard.project_root
                    else frozenset()
                ),
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
            if args.glob and not _matches_workspace_glob(relative_to_search, args.glob):
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
                            if match_count >= args.max_results:
                                match_limited = True
                                break
                            for previous_line, previous_text, previous_clipped in preceding:
                                if previous_line <= last_rendered_line:
                                    continue
                                preview = context_preview(previous_text, previous_clipped)
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
        output = "\n".join(matches)
        separator = "\n" if output else ""
        if regex_incomplete:
            output += (
                f"{separator}[workspace search incomplete: "
                "regex time budget exhausted]"
            )
        elif output_limited:
            output += (
                f"{separator}[search output capture truncated after "
                f"{capture_limit} bytes]"
            )
        elif match_limited:
            output += f"{separator}[truncated after {args.max_results} matches]"
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
            ),
        )
