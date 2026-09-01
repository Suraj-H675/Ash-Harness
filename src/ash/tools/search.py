"""Bounded workspace discovery and text-search tools."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

from pydantic import BaseModel, Field

from ash.sandbox.process_utils import (
    ProcessOutputLimitExceeded,
    communicate_process,
    process_group_options,
    terminate_process_tree,
)
from ash.tools.base import BaseTool, ToolResult, count_output_tokens


DEFAULT_MAX_RESULTS = 200
HARD_MAX_RESULTS = 2_000
MAX_SEARCH_CAPTURE_BYTES = 2_000_000
SEARCH_TIMEOUT_SECONDS = 30
MAX_SEARCH_LINE_CHARS = 64 * 1024
MAX_SEARCH_MATCH_CARRY_CHARS = 4 * 1024
MAX_LIST_DIRECTORY_DEPTH = 4


def _iter_workspace_paths(
    root: Path,
    *,
    recursive: bool,
    max_depth: int | None = None,
) -> Iterator[Path]:
    """Yield workspace entries lazily without descending through links."""

    def walk(directory: Path, depth: int) -> Iterator[Path]:
        if max_depth is not None and depth >= max_depth:
            return
        try:
            children = directory.iterdir()
            for path in children:
                yield path
                if not recursive or (
                    max_depth is not None and depth + 1 >= max_depth
                ):
                    continue
                try:
                    is_link = path.is_symlink()
                    is_directory = path.is_dir()
                except OSError:
                    continue
                if is_directory and not is_link:
                    yield from walk(path, depth + 1)
        except OSError:
            return

    yield from walk(root, 0)


class ListDirectoryArgs(BaseModel):
    directory_path: str = "."
    recursive: bool = False
    max_results: int = Field(DEFAULT_MAX_RESULTS, ge=1, le=HARD_MAX_RESULTS)


class ListDirectoryTool(BaseTool):
    name = "list_dir"
    description = "List workspace files and directories with bounded output."
    args_schema = ListDirectoryArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ListDirectoryArgs(**kwargs)
        root = self.safety_guard.validate_path(args.directory_path)
        if not root.is_dir():
            return ToolResult(
                success=False, output="", error=f"Not a directory: {root}"
            )
        entries: list[str] = []
        truncated = False
        for path in _iter_workspace_paths(
            root,
            recursive=args.recursive,
            max_depth=MAX_LIST_DIRECTORY_DEPTH,
        ):
            if len(entries) >= args.max_results:
                truncated = True
                break
            relative = path.relative_to(root).as_posix()
            try:
                is_directory = path.is_dir() and not path.is_symlink()
            except OSError:
                is_directory = False
            entries.append(relative + ("/" if is_directory else ""))
        entries.sort()
        output = "\n".join(entries)
        if truncated:
            output += f"\n[truncated after {args.max_results} entries]"
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
            truncated=truncated,
        )


class GlobFilesArgs(BaseModel):
    pattern: str = Field(..., min_length=1)
    directory_path: str = "."
    max_results: int = Field(DEFAULT_MAX_RESULTS, ge=1, le=HARD_MAX_RESULTS)


class GlobFilesTool(BaseTool):
    name = "glob_files"
    description = "Find workspace files by a glob pattern such as '**/*.py'."
    args_schema = GlobFilesArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = GlobFilesArgs(**kwargs)
        root = self.safety_guard.validate_path(args.directory_path)
        if not root.is_dir():
            return ToolResult(
                success=False, output="", error=f"Not a directory: {root}"
            )
        matches: list[str] = []
        truncated = False
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if not fnmatch.fnmatch(relative, args.pattern):
                continue
            if len(matches) >= args.max_results:
                truncated = True
                break
            matches.append(relative)
        matches.sort()
        output = "\n".join(matches)
        if truncated:
            output += f"\n[truncated after {args.max_results} matches]"
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
            truncated=truncated,
        )


class SearchTextArgs(BaseModel):
    pattern: str = Field(..., min_length=1)
    directory_path: str = "."
    glob: str | None = None
    fixed_strings: bool = False
    case_sensitive: bool = True
    max_results: int = Field(DEFAULT_MAX_RESULTS, ge=1, le=HARD_MAX_RESULTS)


class SearchTextTool(BaseTool):
    name = "search_text"
    description = "Search workspace text using ripgrep with file and line locations."
    args_schema = SearchTextArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = SearchTextArgs(**kwargs)
        root = self.safety_guard.validate_path(args.directory_path)
        if not root.is_dir():
            return ToolResult(
                success=False, output="", error=f"Not a directory: {root}"
            )
        if shutil.which("rg") is None:
            return await self._python_fallback(root, args)

        command = ["rg", "--json", "--line-number", "--color", "never"]
        if args.fixed_strings:
            command.append("--fixed-strings")
        if not args.case_sensitive:
            command.append("--ignore-case")
        if args.glob:
            command.extend(("--glob", args.glob))
        command.extend(("--", args.pattern, "."))
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_group_options(),
        )
        output_limited = False
        try:
            stdout, stderr = await asyncio.wait_for(
                communicate_process(
                    process,
                    max_output_bytes=MAX_SEARCH_CAPTURE_BYTES,
                ),
                timeout=SEARCH_TIMEOUT_SECONDS,
            )
        except ProcessOutputLimitExceeded as exc:
            stdout, stderr = exc.stdout, exc.stderr
            output_limited = True
        except asyncio.TimeoutError:
            await terminate_process_tree(process)
            return ToolResult(
                success=False,
                output="",
                error=f"search timed out after {SEARCH_TIMEOUT_SECONDS} seconds",
            )
        except asyncio.CancelledError:
            await terminate_process_tree(process)
            raise
        if process.returncode not in (0, 1):
            return ToolResult(
                success=False,
                output="",
                error=stderr.decode("utf-8", errors="replace").strip(),
            )
        matches: list[str] = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # A bounded capture may end in the middle of one JSON event.
                continue
            if event.get("type") != "match":
                continue
            data = event["data"]
            path = data["path"]["text"]
            line_number = data["line_number"]
            text = data["lines"]["text"].rstrip("\r\n")
            matches.append(f"{path}:{line_number}:{text}")
            if len(matches) >= args.max_results:
                break
        truncated = len(matches) >= args.max_results
        output = "\n".join(matches)
        if truncated or output_limited:
            suffix = (
                f"\n[search output capture truncated after "
                f"{MAX_SEARCH_CAPTURE_BYTES} bytes]"
                if output_limited
                else f"\n[truncated after {args.max_results} matches]"
            )
            output += suffix
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
            truncated=truncated or output_limited,
        )

    async def _python_fallback(
        self,
        root: Path,
        args: SearchTextArgs,
    ) -> ToolResult:
        import re

        flags = 0 if args.case_sensitive else re.IGNORECASE
        expression = re.escape(args.pattern) if args.fixed_strings else args.pattern
        try:
            regex = re.compile(expression, flags)
        except re.error as exc:
            return ToolResult(success=False, output="", error=f"Invalid regex: {exc}")

        def bounded_lines(
            handle: TextIO,
        ) -> Iterator[tuple[int, str, bool, bool]]:
            line_number = 0
            while True:
                chunks: list[str] = []
                preview_chars = 0
                carry = ""
                matched = False
                saw_data = False
                complete = False
                while True:
                    chunk = handle.readline(MAX_SEARCH_LINE_CHARS)
                    if not chunk:
                        break
                    saw_data = True
                    candidate = carry + chunk
                    if not matched and regex.search(candidate):
                        matched = True
                    carry = candidate[-MAX_SEARCH_MATCH_CARRY_CHARS:]
                    if preview_chars < MAX_SEARCH_LINE_CHARS:
                        remaining = MAX_SEARCH_LINE_CHARS - preview_chars
                        chunks.append(chunk[:remaining])
                        preview_chars += min(len(chunk), remaining)
                    if chunk.endswith(("\n", "\r")):
                        complete = True
                        break
                if not saw_data:
                    return
                line_number += 1
                preview = "".join(chunks).rstrip("\r\n")
                if not complete:
                    preview += "…"
                yield line_number, preview, matched, not complete
                if not complete:
                    return

        matches: list[str] = []
        output_bytes = 0
        output_limited = False
        match_limited = False
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if args.glob and not fnmatch.fnmatch(relative, args.glob):
                continue
            try:
                with path.open(encoding="utf-8") as handle:
                    for line_number, text, matched, line_truncated in bounded_lines(
                        handle
                    ):
                        if not matched:
                            continue
                        if line_truncated:
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
                        matches.append(rendered)
                        output_bytes += separator_bytes + rendered_bytes
                        if len(matches) >= args.max_results:
                            match_limited = True
                            break
            except (OSError, UnicodeError):
                continue
            if output_limited or match_limited:
                break
        output = "\n".join(matches)
        if output_limited:
            output += (
                f"\n[search output capture truncated after "
                f"{MAX_SEARCH_CAPTURE_BYTES} bytes]"
            )
        elif match_limited:
            output += f"\n[truncated after {args.max_results} matches]"
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
            truncated=output_limited or match_limited,
        )
