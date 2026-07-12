"""Workspace filesystem tools."""

from __future__ import annotations

import codecs
import difflib
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ash.safety.guard import SafetyGuard
from ash.safety.scoped_io import (
    ScopedFileChanged,
    ScopedIOError,
    atomic_write_scoped_bytes,
    read_scoped_bytes,
)
from ash.tools.base import BaseTool, ToolResult, count_output_tokens


BINARY_DETECTION_BYTES = 8192
DEFAULT_MAX_READ_LINES = 800
BINARY_FILE_ERROR = (
    "Error: Binary file detected. Use specialized image/binary viewer tools instead."
)
EXISTS_ERROR = (
    "Error: Target file already exists. Set overwrite to true to replace it, "
    "or use replace_file_content to patch specific regions."
)


@dataclass(frozen=True)
class TextEncoding:
    label: str
    codec: str
    bom: bytes = b""


UTF8_ENCODING = TextEncoding("utf-8", "utf-8")
BOM_ENCODINGS = (
    TextEncoding("utf-32-le", "utf-32-le", codecs.BOM_UTF32_LE),
    TextEncoding("utf-32-be", "utf-32-be", codecs.BOM_UTF32_BE),
    TextEncoding("utf-8-sig", "utf-8", codecs.BOM_UTF8),
    TextEncoding("utf-16-le", "utf-16-le", codecs.BOM_UTF16_LE),
    TextEncoding("utf-16-be", "utf-16-be", codecs.BOM_UTF16_BE),
)


class BinaryContentError(ValueError):
    pass


class TextEncodingError(ValueError):
    pass


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
    expected_sha256: str | None = Field(
        None,
        description=(
            "Optional lowercase SHA-256 digest from the last read_file result. "
            "If supplied, the edit is refused when the file changed since that read."
        ),
    )


class ReplacementEdit(BaseModel):
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
        description="Exact string to replace within this line range.",
    )
    replacement_content: str = Field(
        ...,
        description="Replacement content for this line range.",
    )


class ReplaceFileEditsArgs(BaseModel):
    file_path: str = Field(..., description="Path of target file to edit.")
    edits: list[ReplacementEdit] = Field(
        ...,
        min_length=1,
        max_length=50,
        description=(
            "Ordered exact replacements. Ranges must be non-overlapping and are "
            "all validated before any write occurs."
        ),
    )
    expected_sha256: str | None = Field(
        None,
        description=(
            "Optional lowercase SHA-256 digest from the last read_file result. "
            "If supplied, all edits are refused when the file changed since that read."
        ),
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
        resolved_path = self.safety_guard.validate_mutation_path(args.file_path)

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
        try:
            _, raw_content = read_scoped_bytes(resolved_path, self.safety_guard)
        except (OSError, ScopedIOError) as exc:
            return ToolResult(success=False, output="", error=f"Error: {exc}")
        try:
            original_text, encoding = _decode_text_bytes(raw_content)
        except BinaryContentError:
            return ToolResult(success=False, output="", error=BINARY_FILE_ERROR)
        except TextEncodingError as exc:
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
            )
        lines = original_text.splitlines()
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

        metadata = _format_read_metadata(
            resolved_path,
            lines,
            raw_content,
            encoding,
        )
        numbered_lines = "\n".join(
            f"{line_number}: {line}"
            for line_number, line in enumerate(selected, start=args.start_line)
        )
        output = metadata if not numbered_lines else f"{metadata}\n{numbered_lines}"
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
        resolved_path = self.safety_guard.validate_mutation_path(args.file_path)

        if resolved_path.exists() and not args.overwrite:
            return ToolResult(success=False, output="", error=EXISTS_ERROR)

        try:
            payload, expected_sha256 = _encode_for_write(
                resolved_path,
                args.content,
                self.safety_guard,
                preserve_existing=args.overwrite,
            )
            atomic_write_scoped_bytes(
                resolved_path,
                payload,
                self.safety_guard,
                overwrite=args.overwrite,
                expected_sha256=expected_sha256,
            )
        except FileExistsError:
            return ToolResult(success=False, output="", error=EXISTS_ERROR)
        except BinaryContentError:
            return ToolResult(success=False, output="", error=BINARY_FILE_ERROR)
        except TextEncodingError as exc:
            return ToolResult(success=False, output="", error=str(exc))
        except ScopedFileChanged:
            return ToolResult(
                success=False,
                output="",
                error="Error: File changed during overwrite. Read it again before editing.",
            )
        except (OSError, ScopedIOError) as exc:
            return ToolResult(success=False, output="", error=f"Error: {exc}")
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

        resolved_path = self.safety_guard.validate_mutation_path(args.file_path)
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
        try:
            _, raw_content = read_scoped_bytes(resolved_path, self.safety_guard)
        except (OSError, ScopedIOError) as exc:
            return ToolResult(success=False, output="", error=f"Error: {exc}")
        try:
            original_text, encoding = _decode_text_bytes(raw_content)
        except BinaryContentError:
            return ToolResult(success=False, output="", error=BINARY_FILE_ERROR)
        except TextEncodingError as exc:
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
            )
        actual_sha256 = _sha256_bytes(raw_content)
        stale_error = _validate_expected_sha256(args.expected_sha256, actual_sha256)
        if stale_error is not None:
            return ToolResult(success=False, output="", error=stale_error)
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

        try:
            atomic_write_scoped_bytes(
                resolved_path,
                _encode_text_bytes(new_text, encoding),
                self.safety_guard,
                overwrite=True,
                expected_sha256=actual_sha256,
            )
        except ScopedFileChanged:
            return ToolResult(
                success=False,
                output="",
                error="Error: File changed during edit. Read it again before editing.",
            )
        except (OSError, ScopedIOError) as exc:
            return ToolResult(success=False, output="", error=f"Error: {exc}")
        return ToolResult(
            success=True,
            output=(
                f"Replaced lines {args.start_line}-{args.end_line} in {resolved_path}."
            ),
        )


class ReplaceFileEditsTool(BaseTool):
    name = "replace_file_edits"
    description = (
        "Apply multiple exact replacements to one workspace file atomically after "
        "validating every bounded line range."
    )
    args_schema = ReplaceFileEditsArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ReplaceFileEditsArgs(**kwargs)
        resolved_path = self.safety_guard.validate_mutation_path(args.file_path)
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
        try:
            _, raw_content = read_scoped_bytes(resolved_path, self.safety_guard)
        except (OSError, ScopedIOError) as exc:
            return ToolResult(success=False, output="", error=f"Error: {exc}")
        try:
            original_text, encoding = _decode_text_bytes(raw_content)
        except BinaryContentError:
            return ToolResult(success=False, output="", error=BINARY_FILE_ERROR)
        except TextEncodingError as exc:
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
            )
        actual_sha256 = _sha256_bytes(raw_content)
        stale_error = _validate_expected_sha256(args.expected_sha256, actual_sha256)
        if stale_error is not None:
            return ToolResult(success=False, output="", error=stale_error)

        normalized_text = _normalize_line_endings(original_text)
        lines = normalized_text.splitlines(keepends=True)
        validation_error = _validate_non_overlapping_edits(args.edits, len(lines))
        if validation_error is not None:
            return ToolResult(success=False, output="", error=validation_error)

        for index, edit in enumerate(args.edits, start=1):
            segment = "".join(lines[edit.start_line - 1 : edit.end_line])
            normalized_segment = _strip_trailing_newlines(
                _normalize_line_endings(segment)
            )
            normalized_target = _strip_trailing_newlines(
                _normalize_line_endings(edit.target_content)
            )
            if normalized_segment != normalized_target:
                return ToolResult(
                    success=False,
                    output="",
                    error=(
                        f"Error: edit {index} target_content does not match "
                        "the specified line range.\n"
                        f"{_build_mismatch_error(normalized_target, normalized_segment)}"
                    ),
                )

        new_lines = list(lines)
        for edit in sorted(args.edits, key=lambda item: item.start_line, reverse=True):
            segment = "".join(lines[edit.start_line - 1 : edit.end_line])
            replacement = _normalize_line_endings(edit.replacement_content)
            if (
                segment.endswith("\n")
                and replacement
                and not replacement.endswith("\n")
            ):
                replacement = f"{replacement}\n"
            new_lines[edit.start_line - 1 : edit.end_line] = replacement.splitlines(
                keepends=True
            )
        try:
            atomic_write_scoped_bytes(
                resolved_path,
                _encode_text_bytes("".join(new_lines), encoding),
                self.safety_guard,
                overwrite=True,
                expected_sha256=actual_sha256,
            )
        except ScopedFileChanged:
            return ToolResult(
                success=False,
                output="",
                error="Error: File changed during edit. Read it again before editing.",
            )
        except (OSError, ScopedIOError) as exc:
            return ToolResult(success=False, output="", error=f"Error: {exc}")
        return ToolResult(
            success=True,
            output=f"Applied {len(args.edits)} edits to {resolved_path}.",
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
        resolved_path = self.safety_guard.validate_mutation_path(args.file_path)
        try:
            payload, expected_sha256 = _encode_for_write(
                resolved_path,
                args.content,
                self.safety_guard,
                preserve_existing=True,
            )
            atomic_write_scoped_bytes(
                resolved_path,
                payload,
                self.safety_guard,
                overwrite=True,
                expected_sha256=expected_sha256,
            )
        except BinaryContentError:
            return ToolResult(success=False, output="", error=BINARY_FILE_ERROR)
        except TextEncodingError as exc:
            return ToolResult(success=False, output="", error=str(exc))
        except ScopedFileChanged:
            return ToolResult(
                success=False,
                output="",
                error="Error: File changed during overwrite. Read it again before editing.",
            )
        except (OSError, ScopedIOError) as exc:
            return ToolResult(success=False, output="", error=f"Error: {exc}")
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


async def replace_file_edits(safety_guard: SafetyGuard, **kwargs: Any) -> ToolResult:
    return await ReplaceFileEditsTool(safety_guard).run(**kwargs)


def _is_binary_content(content: bytes) -> bool:
    return b"\x00" in content[:BINARY_DETECTION_BYTES]


def _decode_text_bytes(content: bytes) -> tuple[str, TextEncoding]:
    for encoding in BOM_ENCODINGS:
        if content.startswith(encoding.bom):
            try:
                return (
                    content[len(encoding.bom) :].decode(encoding.codec),
                    encoding,
                )
            except UnicodeDecodeError as exc:
                raise TextEncodingError(
                    f"Error: File is not valid {encoding.label} text."
                ) from exc
    if _is_binary_content(content):
        raise BinaryContentError
    try:
        return content.decode("utf-8"), UTF8_ENCODING
    except UnicodeDecodeError as exc:
        raise TextEncodingError("Error: File is not valid UTF-8 text.") from exc


def _encode_text_bytes(content: str, encoding: TextEncoding) -> bytes:
    return encoding.bom + content.encode(encoding.codec)


def _encode_for_write(
    path: Path,
    content: str,
    guard: SafetyGuard,
    *,
    preserve_existing: bool,
) -> tuple[bytes, str | None]:
    if not preserve_existing or not path.exists():
        return content.encode("utf-8"), None
    _, existing = read_scoped_bytes(path, guard)
    _, encoding = _decode_text_bytes(existing)
    return _encode_text_bytes(content, encoding), _sha256_bytes(existing)


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


def _format_read_metadata(
    path: Path,
    lines: list[str],
    content: bytes,
    encoding: TextEncoding,
) -> str:
    return (
        "[read_file metadata: "
        f"path={path}; sha256={_sha256_bytes(content)}; encoding={encoding.label}; "
        f"total_file_lines={len(lines)}]"
    )


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _validate_expected_sha256(expected: str | None, actual: str) -> str | None:
    if expected is None or expected.casefold() == actual:
        return None
    return (
        "Error: File changed since it was read. "
        f"expected_sha256={expected}; actual_sha256={actual}. "
        "Read the file again before editing."
    )


def _validate_non_overlapping_edits(
    edits: list[ReplacementEdit],
    line_count: int,
) -> str | None:
    previous_end = 0
    for index, edit in enumerate(sorted(edits, key=lambda item: item.start_line), 1):
        if edit.end_line < edit.start_line:
            return f"Error: edit {index} end_line must be >= start_line."
        if edit.start_line <= previous_end:
            return f"Error: edit {index} overlaps an earlier edit."
        if edit.start_line - 1 >= line_count or edit.end_line > line_count:
            return f"Error: edit {index} line range is outside file bounds."
        previous_end = edit.end_line
    return None


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
