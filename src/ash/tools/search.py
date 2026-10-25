"""Bounded workspace discovery and text-search tools."""

from __future__ import annotations

import asyncio
import fnmatch
import io
import stat
from time import monotonic
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TextIO

import regex as regex_engine
from pydantic import BaseModel, Field

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
)


DEFAULT_MAX_RESULTS = 200
HARD_MAX_RESULTS = 2_000
MAX_SEARCH_CAPTURE_BYTES = 2_000_000
MAX_SEARCH_LINE_CHARS = 64 * 1024
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
) -> Iterator[tuple[Path, bool]]:
    """Yield workspace entries lazily without descending through links."""

    def walk(directory: Path, depth: int) -> Iterator[tuple[Path, bool]]:
        if max_depth is not None and depth >= max_depth:
            return
        try:
            _, children = list_scoped_directory(directory, guard)
            for name, is_directory in children:
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


class ListDirectoryArgs(BaseModel):
    directory_path: str = "."
    recursive: bool = False
    max_results: int = Field(DEFAULT_MAX_RESULTS, ge=1, le=HARD_MAX_RESULTS)


class ListDirectoryTool(BaseTool):
    name = "list_dir"
    execution_contract = ToolExecutionContract(parallel_safe=True)
    description = "List workspace files and directories with bounded output."
    args_schema = ListDirectoryArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ListDirectoryArgs(**kwargs)
        root = self.safety_guard.validate_path(args.directory_path)
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
            relative = path.relative_to(root).as_posix()
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
    pattern: str = Field(..., min_length=1)
    directory_path: str = "."
    max_results: int = Field(DEFAULT_MAX_RESULTS, ge=1, le=HARD_MAX_RESULTS)


class GlobFilesTool(BaseTool):
    name = "glob_files"
    execution_contract = ToolExecutionContract(parallel_safe=True)
    description = "Find workspace files by a glob pattern such as '**/*.py'."
    args_schema = GlobFilesArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = GlobFilesArgs(**kwargs)
        root = self.safety_guard.validate_path(args.directory_path)
        if not _is_scoped_directory(root, self.safety_guard):
            return ToolResult(
                success=False, output="", error=f"Not a directory: {root}"
            )
        matches: list[str] = []
        match_truncated = False
        scan_truncated = False
        scan_incomplete = False

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
            relative = path.relative_to(root).as_posix()
            if not _matches_workspace_glob(relative, args.pattern):
                continue
            if len(matches) >= args.max_results:
                match_truncated = True
                break
            matches.append(relative)
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
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
            truncated=match_truncated or scan_truncated or scan_incomplete,
        )


class SearchTextArgs(BaseModel):
    pattern: str = Field(..., min_length=1, max_length=4_096)
    directory_path: str = Field(
        ".", description="Workspace directory or single regular file to search."
    )
    glob: str | None = None
    fixed_strings: bool = False
    case_sensitive: bool = True
    max_results: int = Field(DEFAULT_MAX_RESULTS, ge=1, le=HARD_MAX_RESULTS)


class SearchTextTool(BaseTool):
    name = "search_text"
    execution_contract = ToolExecutionContract(parallel_safe=True)
    description = (
        "Search bounded workspace text in a directory or file with line locations."
    )
    args_schema = SearchTextArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = SearchTextArgs(**kwargs)
        root = self.safety_guard.validate_path(args.directory_path)
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

        regex_time_remaining = MAX_SEARCH_REGEX_TOTAL_SECONDS
        regex_incomplete = False

        def bounded_lines(
            handle: TextIO,
        ) -> Iterator[tuple[int, str, bool, bool]]:
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
                clipped = len(line) > MAX_SEARCH_LINE_CHARS
                preview = line[:MAX_SEARCH_LINE_CHARS]
                if regex_time_remaining <= 0:
                    regex_incomplete = True
                    return
                started_at = monotonic()
                try:
                    matched = regex.search(
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
                yield (
                    line_number,
                    preview,
                    bool(matched),
                    clipped,
                )
                if not complete:
                    return

        matches: list[str] = []
        output_bytes = 0
        output_limited = False
        match_limited = False
        scan_truncated = False
        scan_incomplete = False
        skipped_file = False
        preview_truncated = False

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
            relative = path.relative_to(
                self.safety_guard.project_root if single_file else root
            ).as_posix()
            if args.glob and not _matches_workspace_glob(relative, args.glob):
                continue
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
                    for line_number, text, matched, line_truncated in bounded_lines(
                        handle
                    ):
                        if not matched:
                            continue
                        if line_truncated:
                            preview_truncated = True
                            text += " [line preview truncated]"
                        rendered = f"{relative}:{line_number}:{text}"
                        rendered_bytes = len(rendered.encode("utf-8"))
                        separator_bytes = 1 if matches else 0
                        if (
                            output_bytes + separator_bytes + rendered_bytes
                            > MAX_SEARCH_CAPTURE_BYTES
                        ):
                            output_limited = True
                            break
                        if len(matches) >= args.max_results:
                            match_limited = True
                            break
                        matches.append(rendered)
                        output_bytes += separator_bytes + rendered_bytes
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
                f"{MAX_SEARCH_CAPTURE_BYTES} bytes]"
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
