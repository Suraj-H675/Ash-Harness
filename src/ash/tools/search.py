"""Bounded workspace discovery and text-search tools."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import shutil
from pathlib import Path
from typing import Any

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
        iterator = root.rglob("*") if args.recursive else root.iterdir()
        entries: list[str] = []
        truncated = False
        for path in sorted(iterator):
            if len(entries) >= args.max_results:
                truncated = True
                break
            relative = path.relative_to(root).as_posix()
            entries.append(relative + ("/" if path.is_dir() else ""))
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
        matches: list[str] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if args.glob and not fnmatch.fnmatch(relative, args.glob):
                continue
            try:
                with path.open(encoding="utf-8") as handle:
                    for line_number, text in enumerate(handle, 1):
                        text = text.rstrip("\r\n")
                        if regex.search(text):
                            matches.append(f"{relative}:{line_number}:{text}")
                            if len(matches) >= args.max_results:
                                output = "\n".join(matches)
                                output += f"\n[truncated after {args.max_results} matches]"
                                return ToolResult(
                                    success=True,
                                    output=output,
                                    token_count=count_output_tokens(output),
                                    truncated=True,
                                )
            except (OSError, UnicodeError):
                continue
        output = "\n".join(matches)
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
        )
