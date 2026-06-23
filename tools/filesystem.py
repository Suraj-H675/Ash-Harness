"""Workspace filesystem tools."""

from __future__ import annotations

import difflib
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from safety.guard import SafetyGuard
from tools.base import BaseTool, ToolResult, count_output_tokens


BINARY_DETECTION_BYTES = 8192
DEFAULT_MAX_READ_LINES = 800
BINARY_FILE_ERROR = (
    "Error: Binary file detected. Use specialized image/binary viewer tools instead."
)
EXISTS_ERROR = (
    "Error: Target file already exists. Set overwrite to true to replace it, "
    "or use replace_file_content to patch specific regions."
)


class ReadFileArgs(BaseModel):
    file_path: str = Field(
        ..., description="Absolute or relative path to the file to read."
    )
    start_line: int = Field(
        1, ge=1, description="1-indexed starting line number (inclusive)."
    )
    end_line: int | None = Field(
        None, ge=1, description="1-indexed ending line number (inclusive)."
    )


class WriteFileArgs(BaseModel):
    file_path: str = Field(..., description="Target path for writing content.")
    content: str = Field(..., description="Complete textual content to write.")
    overwrite: bool = Field(
        False,
        description="Set to true if target file already exists and overwrite is intended.",
    )


class ReplaceFileContentArgs(BaseModel):
    file_path: str = Field(..., description="Path of target file to edit.")
    start_line: int = Field(
        ...,
        ge=1,
        description="Start of block range containing target content (1-indexed, inclusive).",
    )
    end_line: int = Field(
        ...,
        ge=1,
        description="End of block range containing target content (1-indexed, inclusive).",
    )
    target_content: str = Field(
        ...,
        description="Exact string of characters to search for within the specified line range.",
    )
    replacement_content: str = Field(
        ...,
        description="The content to replace the target_content with.",
    )


class WholeEditArgs(BaseModel):
    file_path: str = Field(..., description="Absolute or relative path to the file.")
    content: str = Field(..., description="Complete new file content.")
    reason: str = Field("", description="Why the entire file is being replaced.")


class ReadFileTool(BaseTool):
    name = "read_file"
    description = "Read line-delimited text content from a workspace file."
    args_schema = ReadFileArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ReadFileArgs(**kwargs)
        resolved_path = self.safety_guard.validate_path(args.file_path)

        if not resolved_path.exists():
            return ToolResult(
                success=False,
                output="",
                error=f"Error: File not found: {args.file_path}",
            )
        if not resolved_path.is_file():
            return ToolResult(
                success=False, output="", error=f"Error: Not a file: {args.file_path}"
            )
        if _is_binary_file(resolved_path):
            return ToolResult(success=False, output="", error=BINARY_FILE_ERROR)

        lines = resolved_path.read_text(encoding="utf-8").splitlines()
        if args.end_line is not None and args.end_line < args.start_line:
            return ToolResult(
                success=False, output="", error="Error: end_line must be >= start_line."
            )

        start_index = args.start_line - 1
        end_index = args.end_line if args.end_line is not None else len(lines)
        selected = lines[start_index:end_index]
        selected_count = len(selected)

        truncated = False
        if len(selected) > DEFAULT_MAX_READ_LINES:
            selected = selected[:DEFAULT_MAX_READ_LINES]
            truncated = True

        output = "\n".join(
            f"{line_number}: {line}"
            for line_number, line in enumerate(selected, start=args.start_line)
        )
        if truncated:
            output = (
                f"{output}\n"
                f"{_format_read_truncation_metadata(args.start_line, end_index, len(lines), selected_count)}"
            )

        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
            truncated=truncated,
        )


class WriteFileTool(BaseTool):
    name = "write_file"
    description = "Create or overwrite a workspace text file."
    args_schema = WriteFileArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = WriteFileArgs(**kwargs)
        resolved_path = self.safety_guard.validate_path(args.file_path)

        if resolved_path.exists() and not args.overwrite:
            return ToolResult(success=False, output="", error=EXISTS_ERROR)

        error = _prepare_atomic_text_write(
            resolved_path,
            safety_guard=self.safety_guard,
            display_path=args.file_path,
        )
        if error is not None:
            return ToolResult(success=False, output="", error=error)

        _atomic_write_text(resolved_path, args.content)
        return ToolResult(
            success=True,
            output=f"Wrote {len(args.content)} characters to {resolved_path}.",
        )


class ReplaceFileContentTool(BaseTool):
    name = "replace_file_content"
    description = (
        "Replace exact content within a bounded line range of a workspace file."
    )
    args_schema = ReplaceFileContentArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ReplaceFileContentArgs(**kwargs)
        if args.end_line < args.start_line:
            return ToolResult(
                success=False, output="", error="Error: end_line must be >= start_line."
            )

        resolved_path = self.safety_guard.validate_path(args.file_path)
        if not resolved_path.exists():
            return ToolResult(
                success=False,
                output="",
                error=f"Error: File not found: {args.file_path}",
            )
        if not resolved_path.is_file():
            return ToolResult(
                success=False, output="", error=f"Error: Not a file: {args.file_path}"
            )
        if _is_binary_file(resolved_path):
            return ToolResult(success=False, output="", error=BINARY_FILE_ERROR)

        original_text = resolved_path.read_text(encoding="utf-8")
        normalized_text = _normalize_line_endings(original_text)
        lines = normalized_text.splitlines(keepends=True)

        start_index = args.start_line - 1
        end_index = args.end_line
        if start_index >= len(lines) or end_index > len(lines):
            return ToolResult(
                success=False,
                output="",
                error="Error: Line range is outside file bounds.",
            )

        segment = "".join(lines[start_index:end_index])
        normalized_segment = _strip_trailing_newlines(_normalize_line_endings(segment))
        normalized_target = _strip_trailing_newlines(
            _normalize_line_endings(args.target_content)
        )

        if normalized_segment != normalized_target:
            return ToolResult(
                success=False,
                output="",
                error=_build_mismatch_error(normalized_target, normalized_segment),
            )

        replacement = _normalize_line_endings(args.replacement_content)
        if segment.endswith("\n") and replacement and not replacement.endswith("\n"):
            replacement = f"{replacement}\n"
        replacement_lines = replacement.splitlines(keepends=True)
        new_text = "".join(lines[:start_index] + replacement_lines + lines[end_index:])

        _atomic_write_text(resolved_path, new_text)
        return ToolResult(
            success=True,
            output=(
                f"Replaced lines {args.start_line}-{args.end_line} in {resolved_path}."
            ),
        )


class WholeEditTool(BaseTool):
    name = "whole_edit"
    description = (
        "Replace the complete content of a file with new content. "
        "Use when the change is too large or complex for replace_file_content. "
        "The entire file content is replaced."
    )
    args_schema = WholeEditArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = WholeEditArgs(**kwargs)
        resolved_path = self.safety_guard.validate_path(args.file_path)

        error = _prepare_atomic_text_write(
            resolved_path,
            safety_guard=self.safety_guard,
            display_path=args.file_path,
        )
        if error is not None:
            return ToolResult(success=False, output="", error=error)

        _atomic_write_text(resolved_path, args.content)
        return ToolResult(
            success=True,
            output=f"Whole-edit applied to {resolved_path} ({len(args.content)} chars).",
        )


async def read_file(safety_guard: SafetyGuard, **kwargs: Any) -> ToolResult:
    return await ReadFileTool(safety_guard).run(**kwargs)


async def write_file(safety_guard: SafetyGuard, **kwargs: Any) -> ToolResult:
    return await WriteFileTool(safety_guard).run(**kwargs)


async def replace_file_content(safety_guard: SafetyGuard, **kwargs: Any) -> ToolResult:
    return await ReplaceFileContentTool(safety_guard).run(**kwargs)


def _is_binary_file(path: Path) -> bool:
    with path.open("rb") as file:
        return b"\x00" in file.read(BINARY_DETECTION_BYTES)


def _normalize_line_endings(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _strip_trailing_newlines(content: str) -> str:
    return content.rstrip("\n")


def _format_read_truncation_metadata(
    requested_start: int,
    requested_end: int,
    total_file_lines: int,
    selected_count: int,
) -> str:
    returned_end = requested_start + DEFAULT_MAX_READ_LINES - 1
    omitted_lines = max(0, selected_count - DEFAULT_MAX_READ_LINES)
    return (
        "[read_file truncated: "
        f"requested_lines={requested_start}-{requested_end}; "
        f"returned_lines={requested_start}-{returned_end}; "
        f"total_file_lines={total_file_lines}; "
        f"omitted_lines={omitted_lines}; "
        f"next_start_line={returned_end + 1}]"
    )


def _build_mismatch_error(expected: str, actual: str) -> str:
    diff = "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            fromfile="target_content",
            tofile="file_segment",
            lineterm="",
        )
    )
    return f"Error: target_content does not match the specified line range.\n{diff}"


def _prepare_atomic_text_write(
    path: Path,
    *,
    safety_guard: SafetyGuard,
    display_path: str,
) -> str | None:
    parent = path.parent
    safety_guard.validate_path(parent)
    parent.mkdir(parents=True, exist_ok=True)

    if path.exists() and not os.access(path, os.W_OK):
        return f"Error: File is not writable: {display_path}"
    if not os.access(parent, os.W_OK):
        return f"Error: Directory is not writable: {parent}"
    return None


def _atomic_write_text(path: Path, content: str) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_file.write(content)
            temp_path = Path(temp_file.name)

        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
