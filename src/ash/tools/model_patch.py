"""Translate model-oriented file patches into checked unified diffs.

The envelope and hunk syntax follow the public Codex apply_patch convention.
This implementation deliberately rejects ambiguous matches and unsupported
moves instead of guessing which workspace bytes the model intended to edit.
Actual mutation and authorization remain owned by ApplyPatchTool.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path

from ash.safety.guard import SafetyGuard
from ash.safety.scoped_io import read_scoped_bytes


MAX_MODEL_PATCH_FILES = 128
MAX_MODEL_PATCH_CHUNKS = 256
MAX_MODEL_PATCH_SOURCE_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class _Chunk:
    anchor: str | None
    old: tuple[str, ...]
    new: tuple[str, ...]
    end_of_file: bool


@dataclass(frozen=True)
class _Operation:
    action: str
    path: str
    additions: tuple[str, ...] = ()
    chunks: tuple[_Chunk, ...] = ()


def parse_model_patch(patch: str, guard: SafetyGuard) -> tuple[_Operation, ...]:
    """Parse a strict, bounded model patch without opening or editing files."""

    lines = patch.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("expected *** Begin Patch and *** End Patch markers")

    result: list[_Operation] = []
    seen: set[str] = set()
    total_chunks = 0
    index = 1
    while index < len(lines) - 1:
        header = lines[index]
        action = next(
            (
                kind
                for kind in ("Update", "Add", "Delete")
                if header.startswith(f"*** {kind} File: ")
            ),
            None,
        )
        if action is None:
            raise ValueError("expected a supported *** Update/Add/Delete File header")
        relative = header.split(" File: ", 1)[1]
        if (
            not relative.strip()
            or relative != relative.strip()
            or not all(char.isprintable() for char in relative)
        ):
            raise ValueError("invalid patch file path")
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("patch path must stay within the workspace")
        relative = path.as_posix()
        guard.validate_mutation_path(relative)
        if relative in seen:
            raise ValueError("a model patch may only address each file once")
        seen.add(relative)
        index += 1

        if action == "Delete":
            if index < len(lines) - 1 and not lines[index].startswith("*** "):
                raise ValueError("a delete operation cannot include content")
            result.append(_Operation(action="delete", path=relative))
            continue

        if action == "Add":
            additions: list[str] = []
            while index < len(lines) - 1 and not lines[index].startswith("*** "):
                if not lines[index].startswith("+"):
                    raise ValueError("added file lines must start with +")
                additions.append(lines[index][1:])
                index += 1
            if not additions:
                raise ValueError("added file needs at least one content line")
            result.append(
                _Operation(action="add", path=relative, additions=tuple(additions))
            )
            continue

        chunks: list[_Chunk] = []
        anchor: str | None = None
        old: list[str] = []
        new: list[str] = []
        end_of_file = False

        def flush() -> None:
            nonlocal old, new, end_of_file
            if old or new:
                chunks.append(_Chunk(anchor, tuple(old), tuple(new), end_of_file))
            old, new, end_of_file = [], [], False

        while index < len(lines) - 1:
            line = lines[index]
            if line.startswith("*** "):
                if line == "*** Move to: " or line.startswith("*** Move to: "):
                    raise ValueError(
                        "model patch moves are not supported; use a unified diff"
                    )
                if line == "*** End of File":
                    if end_of_file or not (old or new):
                        raise ValueError("invalid *** End of File placement")
                    end_of_file = True
                    index += 1
                    if index < len(lines) - 1 and not lines[index].startswith("*** "):
                        raise ValueError("*** End of File must terminate its update")
                    continue
                break
            if line == "@@" or line.startswith("@@ "):
                flush()
                anchor = line[3:].strip() or None
            elif line.startswith((" ", "-", "+")):
                if end_of_file:
                    raise ValueError("unexpected content after *** End of File")
                if line[0] in {" ", "-"}:
                    old.append(line[1:])
                if line[0] in {" ", "+"}:
                    new.append(line[1:])
            else:
                raise ValueError("expected an @@ context or prefixed change line")
            index += 1
        flush()
        if not chunks or all(chunk.old == chunk.new for chunk in chunks):
            raise ValueError("update has no changes")
        result.append(_Operation(action="update", path=relative, chunks=tuple(chunks)))
        total_chunks += len(chunks)
        if total_chunks > MAX_MODEL_PATCH_CHUNKS:
            raise ValueError("model patch contains too many update hunks")

        if len(result) > MAX_MODEL_PATCH_FILES:
            raise ValueError("model patch contains too many files")

    if not result:
        raise ValueError("model patch contains no operations")
    if len(result) > MAX_MODEL_PATCH_FILES:
        raise ValueError("model patch contains too many files")
    return tuple(result)


def _source_lines(content: bytes) -> tuple[list[str], str, bool]:
    if b"\x00" in content:
        raise ValueError("model patch cannot update binary files")
    try:
        source = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("model patch requires UTF-8 text; use a unified diff") from exc
    newline = "\r\n" if "\r\n" in source else "\n"
    if "\r" in source.replace("\r\n", "") or (
        newline == "\r\n" and "\n" in source.replace("\r\n", "")
    ):
        raise ValueError("model patch refuses mixed or bare-CR line endings")
    if not source:
        return [], newline, False
    ended = source.endswith(newline)
    lines = source.split(newline)
    if ended:
        lines.pop()
    return lines, newline, ended


def _find_unique(lines: list[str], block: tuple[str, ...], start: int) -> int:
    matches = [
        index
        for index in range(start, len(lines) - len(block) + 1)
        if tuple(lines[index : index + len(block)]) == block
    ]
    if not matches:
        raise ValueError("patch context was not found in the current file")
    if len(matches) != 1:
        raise ValueError("ambiguous patch context; provide a larger unique hunk")
    return matches[0]


def _updated_content(content: bytes, chunks: tuple[_Chunk, ...], *, path: str) -> str:
    source_lines, newline, ended = _source_lines(content)
    result: list[str] = []
    cursor = 0
    for hunk_index, chunk in enumerate(chunks, start=1):
        try:
            minimum = cursor
            if chunk.anchor is not None:
                anchors = [
                    index
                    for index in range(cursor, len(source_lines))
                    if source_lines[index].strip() == chunk.anchor
                ]
                if len(anchors) != 1:
                    raise ValueError("patch @@ anchor was missing or ambiguous")
                minimum = anchors[0]
                if not chunk.old or chunk.old[0] != source_lines[minimum]:
                    minimum += 1

            if chunk.old:
                position = _find_unique(source_lines, chunk.old, minimum)
                if chunk.end_of_file and position + len(chunk.old) != len(source_lines):
                    raise ValueError("patch *** End of File does not match the file tail")
            elif chunk.end_of_file:
                position = len(source_lines)
            elif chunk.anchor is not None:
                position = minimum
            else:
                raise ValueError("unanchored insertion; include an @@ anchor or context")
        except ValueError as exc:
            raise ValueError(f"{path}: hunk {hunk_index}: {exc}") from exc
        result.extend(source_lines[cursor:position])
        result.extend(chunk.new)
        cursor = position + len(chunk.old)
    result.extend(source_lines[cursor:])
    return newline.join(result) + (newline if ended else "")


def _unified_file_diff(path: str, before: str | None, after: str | None) -> str:
    from_name = f"a/{path}" if before is not None else "/dev/null"
    to_name = f"b/{path}" if after is not None else "/dev/null"
    fragments = unified_diff(
        before.splitlines(keepends=True) if before is not None else [],
        after.splitlines(keepends=True) if after is not None else [],
        fromfile=from_name,
        tofile=to_name,
        lineterm="\n",
    )
    # difflib does not emit Git's no-final-newline marker on its own.
    return "".join(
        part if part.endswith("\n") else part + "\n\\ No newline at end of file\n"
        for part in fragments
    )


def model_patch_as_unified_diff(
    operations: tuple[_Operation, ...],
    guard: SafetyGuard,
    *,
    max_source_bytes: int,
) -> str:
    """Read no-follow source snapshots and build one Git-checkable diff."""

    patches: list[str] = []
    cumulative_source_bytes = 0
    for operation in operations:
        path = guard.validate_mutation_path(operation.path)
        before: str | None
        after: str | None
        if operation.action == "add":
            if path.exists() or path.is_symlink():
                raise ValueError(f"added patch file already exists: {operation.path}")
            before, after = None, "\n".join(operation.additions) + "\n"
        else:
            _, raw = read_scoped_bytes(path, guard, max_bytes=max_source_bytes)
            cumulative_source_bytes += len(raw)
            if cumulative_source_bytes > MAX_MODEL_PATCH_SOURCE_BYTES:
                raise ValueError("model patch source budget exceeded")
            # _source_lines checks unsupported content without losing it.
            _source_lines(raw)
            original = raw.decode("utf-8")
            before = original
            after = (
                None
                if operation.action == "delete"
                else _updated_content(raw, operation.chunks, path=operation.path)
            )
        if before == after:
            raise ValueError(f"patch made no changes to {operation.path}")
        patch = _unified_file_diff(operation.path, before, after)
        if not patch:
            raise ValueError(
                f"patch cannot represent {operation.path} as a unified diff"
            )
        patches.append(patch)
    return "".join(patches)
