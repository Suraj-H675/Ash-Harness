"""Workspace filesystem tools."""

from __future__ import annotations

import codecs
import difflib
import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.safety.scoped_io import (
    ScopedFileChanged,
    ScopedIOError,
    atomic_write_scoped_bytes,
    read_scoped_bytes,
)
from ash.tools.base import (
    BaseTool,
    ToolExecutionContract,
    ToolResult,
    count_output_tokens,
    scoped_path_refusal,
)


BINARY_DETECTION_BYTES = 8192
DEFAULT_MAX_READ_LINES = 800
DEFAULT_MAX_READ_CHARS = 32_768
HARD_MAX_READ_CHARS = 131_072
MAX_BATCH_READ_FILES = 4
MAX_BATCH_READ_SOURCE_CHARS = 16_384
MAX_BATCH_READ_ENTRY_CHARS = 8_192
MAX_BATCH_READ_RENDER_CHARS = 24_576
MAX_TEXT_FILE_BYTES = 8 * 1024 * 1024
MAX_TEXT_ARGUMENT_CHARS = 8 * 1024 * 1024
BINARY_FILE_ERROR = (
    "Error: Binary file detected. Use specialized image/binary viewer tools instead."
)
EXISTS_ERROR = (
    "Error: Target file already exists. Set overwrite to true to replace it, "
    "or use replace_file_content to patch specific regions."
)
NO_OP_EDIT_ERROR = (
    "Error: replacement would not change the file; no edit was made. "
    "Supply a replacement with different content."
)
IDENTICAL_EDIT_ERROR = (
    "Error: target_content and replacement_content are identical; "
    "no edit was made. Use the actual old file lines as target_content "
    "and the desired new lines as replacement_content."
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
        ...,
        description="Prefer a project-relative path, e.g. 'pkg/app.py' from list_dir.",
    )
    start_line: int = Field(1, ge=1, description="First line (1-based).")
    end_line: int | None = Field(None, ge=1, description="Last line.")
    start_column: int = Field(
        1,
        ge=1,
        description="First-line column (1-based).",
    )
    max_chars: int = Field(
        DEFAULT_MAX_READ_CHARS,
        ge=1,
        le=HARD_MAX_READ_CHARS,
        description="Source character limit.",
    )
    expected_sha256: str | None = Field(
        None,
        pattern=r"^[a-fA-F0-9]{64}$",
        description="Reject changed SHA-256.",
    )


class ReadFilesEntry(BaseModel):
    file_path: str = Field(..., description="Workspace-relative source path.")
    start_line: int = Field(1, ge=1, description="First line, 1-based.")
    end_line: int | None = Field(None, ge=1, description="Last line, inclusive.")
    start_column: int = Field(1, ge=1, description="First-line column, 1-based.")
    max_chars: int = Field(
        4_096,
        ge=1,
        le=MAX_BATCH_READ_ENTRY_CHARS,
        description="Maximum source characters for this range.",
    )
    expected_sha256: str | None = Field(
        None,
        pattern=r"^[a-fA-F0-9]{64}$",
        description="Optional source SHA-256; reject a stale read.",
    )


class ReadFilesArgs(BaseModel):
    files: list[ReadFilesEntry] = Field(
        ...,
        min_length=1,
        max_length=MAX_BATCH_READ_FILES,
        description="Up to four exact file ranges; total max_chars at most 16384.",
    )


class WriteFileArgs(BaseModel):
    file_path: str = Field(..., description="Target path for writing content.")
    content: str = Field(
        ...,
        max_length=MAX_TEXT_ARGUMENT_CHARS,
        description="Complete textual content to write.",
    )
    overwrite: bool = Field(
        False,
        description=(
            "Existing files are refused by default. Set true for an intentional "
            "full-file rewrite; prefer replace_file_content for small edits."
        ),
    )


class ReplaceFileContentArgs(BaseModel):
    file_path: str = Field(..., description="Path of target file to edit.")
    start_line: int | None = Field(
        None,
        ge=1,
        description=(
            "Optional 1-indexed inclusive start line. Prefer omitting both bounds "
            "when OLD complete lines identify exactly one block (especially after "
            "previous edits shift line numbers). Supply both bounds only when "
            "the exact current range is known; bounded edits never relocate."
        ),
    )
    end_line: int | None = Field(
        None,
        ge=1,
        description=(
            "Optional 1-indexed inclusive end line. Must be supplied with start_line."
        ),
    )
    target_content: str = Field(
        ...,
        max_length=MAX_TEXT_ARGUMENT_CHARS,
        description=(
            "OLD existing text to match, not the proposed fix: copy the exact "
            "complete lines without read_file's line numbers, including indentation. "
            "With line bounds, it must equal the entire selected range; without "
            "bounds, it must match exactly one full-line block in the file."
        ),
    )
    replacement_content: str = Field(
        ...,
        max_length=MAX_TEXT_ARGUMENT_CHARS,
        description=(
            "NEW desired text to write. Normally differs from target_content; "
            "identical no-op edits are refused. For line endings, LF-only "
            "separators follow the replaced range's local style; replacements "
            "containing CR keep their supplied separators."
        ),
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
        max_length=MAX_TEXT_ARGUMENT_CHARS,
        description="Exact string to replace within this line range.",
    )
    replacement_content: str = Field(
        ...,
        max_length=MAX_TEXT_ARGUMENT_CHARS,
        description=(
            "Replacement content. LF-only separators follow the replaced range's "
            "local style; replacements containing CR keep their supplied separators."
        ),
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
    content: str = Field(
        ...,
        max_length=MAX_TEXT_ARGUMENT_CHARS,
        description="Complete new file content.",
    )
    reason: str = Field("", description="Why the entire file is being replaced.")


class ReadFileTool(BaseTool):
    name = "read_file"
    execution_contract = ToolExecutionContract(parallel_safe=True)
    description = (
        "Read numbered file lines. For large files, locate relevant symbols "
        "with search_text, then read focused start_line/end_line ranges; avoid "
        "large full-file reads that bloat later requests. Truncated results "
        "include next line/column and SHA-256."
    )
    args_schema = ReadFileArgs

    def json_schema(self) -> dict[str, Any]:
        # Optional defaults are enforced by ReadFileArgs; omitting their schema
        # annotations keeps this always-visible tool compact on small models.
        schema = super().json_schema()
        for prop in schema.get("properties", {}).values():
            if isinstance(prop, dict):
                prop.pop("default", None)
        return schema

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ReadFileArgs(**kwargs)
        try:
            resolved_path = self.safety_guard.validate_mutation_path(args.file_path)
        except SafetyViolation as exc:
            return scoped_path_refusal(exc)

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
            _, raw_content = read_scoped_bytes(
                resolved_path,
                self.safety_guard,
                max_bytes=MAX_TEXT_FILE_BYTES,
            )
        except (OSError, ScopedIOError) as exc:
            return ToolResult(success=False, output="", error=f"Error: {exc}")
        digest = _sha256_bytes(raw_content)
        stale_error = _validate_expected_sha256(args.expected_sha256, digest)
        if stale_error is not None:
            return ToolResult(success=False, output="", error=stale_error)
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
        visible = selected[:DEFAULT_MAX_READ_LINES]
        remaining = args.max_chars
        numbered_lines: list[str] = []
        continuation: tuple[int, int] | None = None
        for index, line in enumerate(visible):
            line_number = args.start_line + index
            column = args.start_column if index == 0 else 1
            if column > len(line) + 1:
                return ToolResult(
                    success=False,
                    output="",
                    error=(
                        f"Error: start_column {column} exceeds line {line_number} "
                        f"length ({len(line)} characters)."
                    ),
                )
            offset = column - 1
            shown = line[offset : offset + remaining]
            prefix = (
                f"{line_number}: "
                if column == 1
                else f"{line_number} [column {column}]: "
            )
            numbered_lines.append(prefix + shown)
            remaining -= len(shown)
            if offset + len(shown) < len(line):
                continuation = (line_number, column + len(shown))
                break
            if remaining == 0 and index + 1 < len(visible):
                continuation = (line_number + 1, 1)
                break

        if continuation is None and remaining == 0 and len(visible) < selected_count:
            continuation = (args.start_line + len(visible), 1)
        truncated = continuation is not None or selected_count > DEFAULT_MAX_READ_LINES

        metadata = _format_read_metadata(
            resolved_path,
            lines,
            digest,
            encoding,
        )
        rendered_lines = "\n".join(numbered_lines)
        output = metadata if not rendered_lines else f"{metadata}\n{rendered_lines}"
        if continuation is not None:
            next_line, next_column = continuation
            output += (
                "\n[read_file truncated: "
                f"source_chars_limit={args.max_chars}; "
                f"next_start_line={next_line}; next_start_column={next_column}]"
            )
        elif truncated:
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


class ReadFilesTool(BaseTool):
    """Read a handful of independent source ranges in one model round trip.

    All access is delegated to ReadFileTool so the normal anchored-file,
    stale-hash, encoding, binary and truncation protections remain authoritative.
    """

    name = "read_files"
    execution_contract = ToolExecutionContract(parallel_safe=True)
    description = (
        "Read up to 4 focused source ranges together, with numbered lines, "
        "SHA-256 and truncation markers per file. Search for symbols first; "
        "keep the combined source budget at or below 16384 characters. "
        "Each read retains normal workspace and stale-file checks."
    )
    args_schema = ReadFilesArgs

    def __init__(self, safety_guard: SafetyGuard) -> None:
        super().__init__(safety_guard)
        self._reader = ReadFileTool(safety_guard)

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ReadFilesArgs(**kwargs)
        if sum(item.max_chars for item in args.files) > MAX_BATCH_READ_SOURCE_CHARS:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "Combined source-character limits exceed 16384; "
                    "request shorter ranges or split the batch."
                ),
            )
        outputs: list[str] = []
        failures = 0
        truncated = False
        output_chars = 0
        omitted = False
        for index, item in enumerate(args.files, 1):
            result = await self._reader.run(**item.model_dump())
            label = f"[{index}/{len(args.files)}] {item.file_path}"
            content = result.output if result.success else (result.error or "Read failed")
            rendered = f"{label}\n{content}"
            if output_chars + len(rendered) + 2 > MAX_BATCH_READ_RENDER_CHARS:
                outputs.append(
                    f"[{index}/{len(args.files)}] Remaining batch output omitted; "
                    "request smaller ranges."
                )
                omitted = True
                break
            outputs.append(rendered)
            output_chars += len(rendered) + 2
            truncated = truncated or result.truncated
            failures += not result.success
        output = "\n\n".join(outputs)
        error = None
        if failures:
            error = f"{failures} of {len(args.files)} reads failed."
        elif omitted:
            error = "Batch render limit reached; request smaller ranges."
        return ToolResult(
            success=failures == 0 and not omitted,
            output=output,
            error=error,
            truncated=truncated or omitted,
            token_count=count_output_tokens(output),
        )


class WriteFileTool(BaseTool):
    name = "write_file"
    description = (
        "Create a workspace text file. Replacing an existing file requires "
        "overwrite=true; use replace_file_content for smaller edits."
    )
    args_schema = WriteFileArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = WriteFileArgs(**kwargs)
        try:
            resolved_path = self.safety_guard.validate_mutation_path(args.file_path)
        except SafetyViolation as exc:
            return ToolResult(success=False, output="", error=str(exc))

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
        "Replace OLD existing complete lines (target_content) with NEW desired "
        "lines (replacement_content). Prefer omitting both line bounds when OLD "
        "lines form a unique full-line block, especially after earlier edits; "
        "use both bounds only for a verified exact range. Bounded edits never "
        "relocate automatically. Ambiguous matches and stale hashes are rejected."
    )
    args_schema = ReplaceFileContentArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ReplaceFileContentArgs(**kwargs)
        if (args.start_line is None) != (args.end_line is None):
            return ToolResult(
                success=False,
                output="",
                error="Error: Supply both start_line and end_line, or omit both.",
            )
        if (
            args.start_line is not None
            and args.end_line is not None
            and args.end_line < args.start_line
        ):
            return ToolResult(
                success=False, output="", error="Error: end_line must be >= start_line."
            )

        try:
            resolved_path = self.safety_guard.validate_mutation_path(args.file_path)
        except SafetyViolation as exc:
            return ToolResult(success=False, output="", error=str(exc))
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
            _, raw_content = read_scoped_bytes(
                resolved_path,
                self.safety_guard,
                max_bytes=MAX_TEXT_FILE_BYTES,
            )
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
        lines = original_text.splitlines(keepends=True)

        normalized_target = _strip_trailing_newlines(
            _normalize_line_endings(args.target_content)
        )
        if args.start_line is None:
            span, match_error = _find_unique_line_span(original_text, normalized_target)
            if match_error is not None:
                if (
                    args.target_content == args.replacement_content
                    and "does not match a complete block of lines" in match_error
                ):
                    return ToolResult(
                        success=False,
                        output="",
                        error=IDENTICAL_EDIT_ERROR,
                    )
                return ToolResult(success=False, output="", error=match_error)
            assert span is not None
            start_index, end_index = span
            segment = "".join(lines[start_index:end_index])
            if (
                _strip_trailing_newlines(_normalize_line_endings(segment))
                != normalized_target
            ):
                return ToolResult(
                    success=False,
                    output="",
                    error="Error: target_content does not match complete file lines.",
                )
        else:
            assert args.end_line is not None
            start_index = args.start_line - 1
            end_index = args.end_line
            if start_index >= len(lines) or end_index > len(lines):
                return ToolResult(
                    success=False,
                    output="",
                    error="Error: Line range is outside file bounds.",
                )

            segment = "".join(lines[start_index:end_index])
            normalized_segment = _strip_trailing_newlines(
                _normalize_line_endings(segment)
            )
            if normalized_segment != normalized_target:
                recovery_hint = _unique_line_mismatch_hint(
                    original_text, normalized_target, start_index, end_index
                )
                return ToolResult(
                    success=False,
                    output="",
                    error=(
                        _build_mismatch_error(normalized_target, normalized_segment)
                        + recovery_hint
                    ),
                )

        replacement_lines = _replacement_lines_for_span(
            lines, start_index, end_index, args.replacement_content
        )
        new_text = "".join(lines[:start_index] + replacement_lines + lines[end_index:])
        if new_text == original_text:
            return ToolResult(
                success=False,
                output="",
                error=(
                    IDENTICAL_EDIT_ERROR
                    if args.target_content == args.replacement_content
                    else NO_OP_EDIT_ERROR
                ),
            )

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
                f"Replaced lines {start_index + 1}-{end_index} in {resolved_path}."
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
        try:
            resolved_path = self.safety_guard.validate_mutation_path(args.file_path)
        except SafetyViolation as exc:
            return ToolResult(success=False, output="", error=str(exc))
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
            _, raw_content = read_scoped_bytes(
                resolved_path,
                self.safety_guard,
                max_bytes=MAX_TEXT_FILE_BYTES,
            )
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

        lines = original_text.splitlines(keepends=True)
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
            start_index = edit.start_line - 1
            end_index = edit.end_line
            new_lines[start_index:end_index] = _replacement_lines_for_span(
                lines, start_index, end_index, edit.replacement_content
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
        try:
            resolved_path = self.safety_guard.validate_mutation_path(args.file_path)
        except SafetyViolation as exc:
            return ToolResult(success=False, output="", error=str(exc))
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
    payload = encoding.bom + content.encode(encoding.codec)
    if len(payload) > MAX_TEXT_FILE_BYTES:
        raise ScopedIOError(f"content exceeds {MAX_TEXT_FILE_BYTES} bytes")
    return payload


def _encode_for_write(
    path: Path,
    content: str,
    guard: SafetyGuard,
    *,
    preserve_existing: bool,
) -> tuple[bytes, str | None]:
    if not preserve_existing or not path.exists():
        return _encode_text_bytes(content, UTF8_ENCODING), None
    _, existing = read_scoped_bytes(path, guard, max_bytes=MAX_TEXT_FILE_BYTES)
    _, encoding = _decode_text_bytes(existing)
    return _encode_text_bytes(content, encoding), _sha256_bytes(existing)


def _normalize_line_endings(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _line_ending_suffix(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\r"):
        return "\r"
    if line.endswith("\n"):
        return "\n"
    return ""


def _preferred_line_ending(
    lines: list[str], start_index: int, end_index: int
) -> str:
    endings = [
        ending
        for index in range(start_index, end_index)
        if (ending := _line_ending_suffix(lines[index]))
    ]
    if endings:
        counts = Counter(endings)
        most_common_count = max(counts.values())
        return next(ending for ending in endings if counts[ending] == most_common_count)

    search_limit = max(start_index, len(lines) - end_index)
    for distance in range(1, search_limit + 1):
        previous_index = start_index - distance
        if previous_index >= 0:
            ending = _line_ending_suffix(lines[previous_index])
            if ending:
                return ending

        next_index = end_index + distance - 1
        if next_index < len(lines):
            ending = _line_ending_suffix(lines[next_index])
            if ending:
                return ending

    return "\n"


def _replacement_lines_for_span(
    lines: list[str],
    start_index: int,
    end_index: int,
    replacement_content: str,
) -> list[str]:
    local_ending = _preferred_line_ending(lines, start_index, end_index)
    if "\r" not in replacement_content:
        replacement_content = replacement_content.replace("\n", local_ending)

    source_boundary = _line_ending_suffix(lines[end_index - 1])
    if (
        source_boundary
        and replacement_content
        and not _line_ending_suffix(replacement_content)
    ):
        replacement_content += source_boundary

    return replacement_content.splitlines(keepends=True)


def _strip_trailing_newlines(content: str) -> str:
    return content.rstrip("\n")


def _find_unique_line_span(
    original_text: str, normalized_target: str
) -> tuple[tuple[int, int] | None, str | None]:
    if not normalized_target:
        return None, "Error: A nonempty full-line target_content is required."

    content = _normalize_line_endings(original_text)
    matches: list[int] = []
    if content.startswith(normalized_target) and (
        len(content) == len(normalized_target)
        or content[len(normalized_target)] == "\n"
    ):
        matches.append(0)

    cursor = 0
    needle = "\n" + normalized_target
    while (index := content.find(needle, cursor)) != -1:
        start = index + 1
        end = start + len(normalized_target)
        if end == len(content) or content[end] == "\n":
            matches.append(start)
            if len(matches) > 1:
                return (
                    None,
                    "Error: target_content matches multiple full-line blocks. "
                    "Specify both start_line and end_line to choose one.",
                )
        cursor = index + 1

    if not matches:
        return (
            None,
            "Error: target_content does not match a complete block of lines. "
            "Read the file again and copy the exact lines, including indentation.",
        )

    start_index = content.count("\n", 0, matches[0])
    return (start_index, start_index + normalized_target.count("\n") + 1), None


def _unique_line_mismatch_hint(
    original_text: str, normalized_target: str, start_index: int, end_index: int
) -> str:
    """Diagnose a shifted match; never turn a bounded edit into a search/edit."""
    span, error = _find_unique_line_span(original_text, normalized_target)
    if span is not None and span != (start_index, end_index):
        # Match the unique-block tool's second exact-line validation. Some
        # Unicode line separators affect splitlines() differently than LF.
        source_lines = original_text.splitlines(keepends=True)
        segment = "".join(source_lines[span[0] : span[1]])
        if _strip_trailing_newlines(_normalize_line_endings(segment)) == normalized_target:
            return (
                f" Exact OLD lines occur once at lines {span[0] + 1}-{span[1]} "
                "instead; no edit was made. Verify the target and omit both "
                "start_line and end_line to use the unique-block mode."
            )
    if error is not None and "multiple full-line blocks" in error:
        return (
            " OLD lines occur in multiple full-line blocks; no edit was made. "
            "Read the file again and use precise current line bounds."
        )
    return (
        " If target_content uniquely matches a full-line block, "
        "omit both start_line and end_line instead."
    )


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
    digest: str,
    encoding: TextEncoding,
) -> str:
    return (
        "[read_file metadata: "
        f"path={path}; sha256={digest}; encoding={encoding.label}; "
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
    return (
        "Error: target_content does not match the specified line range. "
        "Read the file again and copy the exact complete lines, including indentation."
        f"\n{diff}"
    )
