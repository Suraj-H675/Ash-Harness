"""Conflict-aware checkpoints for direct Ash file edits."""

from __future__ import annotations

import difflib
import hashlib
from itertools import islice
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ash.core.session import Session, SessionStore
from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolMiddleware, ToolResult
from ash.tools.patch import extract_patch_paths


EDIT_TOOLS = {
    "write_file",
    "whole_edit",
    "replace_file_content",
    "replace_file_edits",
    "apply_patch",
}
MAX_CHECKPOINT_BYTES = 20 * 1024 * 1024
MAX_CHECKPOINT_DIFF_LINES = 400
CHECKPOINT_READ_CHUNK_BYTES = 1024 * 1024
OVERSIZED_DIGEST = "<checkpoint-file-too-large>"


@dataclass(frozen=True)
class RecoveredToolCall:
    """One durable terminal decision for an interrupted tool intent."""

    call_id: str
    tool_name: str
    turn_id: str
    error: str
    dispatched: bool
    ambiguous: bool


@dataclass(frozen=True)
class RecoverySummary:
    interrupted_turns: int = 0
    compensated_calls: int = 0
    compensated_files: tuple[Path, ...] = ()
    unknown_calls: tuple[str, ...] = ()
    unresolved_files: tuple[Path, ...] = ()
    recovered_calls: tuple[RecoveredToolCall, ...] = ()

    @property
    def needs_attention(self) -> bool:
        return bool(self.unknown_calls or self.unresolved_files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "interrupted_turns": self.interrupted_turns,
            "compensated_calls": self.compensated_calls,
            "compensated_files": [str(path) for path in self.compensated_files],
            "unknown_calls": list(self.unknown_calls),
            "unresolved_files": [str(path) for path in self.unresolved_files],
            "recovered_calls": [
                {
                    "call_id": call.call_id,
                    "tool": call.tool_name,
                    "turn_id": call.turn_id,
                    "error": call.error,
                    "dispatched": call.dispatched,
                    "ambiguous": call.ambiguous,
                }
                for call in self.recovered_calls
            ],
            "needs_attention": self.needs_attention,
        }


class FileCheckpointMiddleware(ToolMiddleware):
    def __init__(
        self,
        store: SessionStore,
        guard: SafetyGuard,
        context_provider: Callable[[], tuple[str, str] | tuple[str, str, str] | None],
    ) -> None:
        self.store = store
        self.guard = guard
        self.context_provider = context_provider

    async def before_tool(
        self, tool_name: str, arguments: dict[str, Any], tool: BaseTool
    ) -> None:
        del tool
        context = self.context_provider()
        if context is None or tool_name not in EDIT_TOOLS:
            return
        session_id, turn_id, call_id = _checkpoint_context(context)
        for path in self._paths(tool_name, arguments):
            existed = path.is_file()
            content = _read_checkpoint_bytes(path) if existed else None
            mode = path.stat().st_mode if existed else None
            self.store.save_file_checkpoint(
                session_id,
                turn_id,
                tool_name,
                str(path),
                existed=existed,
                before_content=content,
                before_mode=mode,
                call_id=call_id,
            )

    async def after_tool(
        self, tool_name: str, arguments: dict[str, Any], result: ToolResult
    ) -> None:
        context = self.context_provider()
        if context is None or tool_name not in EDIT_TOOLS or not result.success:
            return
        session_id, turn_id, call_id = _checkpoint_context(context)
        for path in self._paths(tool_name, arguments):
            digest = _digest(path)
            self.store.finish_file_checkpoint(
                session_id,
                turn_id,
                str(path),
                digest,
                call_id=call_id,
            )

    def _paths(self, tool_name: str, arguments: dict[str, Any]) -> list[Path]:
        if tool_name == "apply_patch":
            raw = extract_patch_paths(str(arguments.get("patch", "")), self.guard)
            return [self.guard.validate_path(path) for path in sorted(raw)]
        raw_path = arguments.get("file_path")
        return [self.guard.validate_path(str(raw_path))] if raw_path else []


def _checkpoint_context(
    context: tuple[str, str] | tuple[str, str, str],
) -> tuple[str, str, str]:
    if len(context) == 2:
        return context[0], context[1], ""
    return context


def recover_interrupted_turns(
    store: SessionStore,
    guard: SafetyGuard,
    session_id: str,
) -> RecoverySummary:
    """Compensate provably interrupted direct edits and flag unknown effects."""

    turns = store.started_turns(session_id)
    compensated_calls: list[str] = []
    compensated_files: list[Path] = []
    unknown_calls: list[str] = []
    unresolved_files: list[Path] = []
    recovered_calls: list[RecoveredToolCall] = []

    for turn in turns:
        turn_id = str(turn["turn_id"])
        pending = store.pending_tool_calls(session_id, turn_id)
        pending_ids = [str(row["call_id"]) for row in pending]
        call_errors: dict[str, str] = {}
        restored_checkpoint_ids: list[int] = []
        turn_compensated: list[str] = []
        turn_unknown: list[dict[str, str]] = []
        turn_unresolved: list[str] = []
        turn_recovered: list[RecoveredToolCall] = []

        for call in pending:
            call_id = str(call["call_id"])
            tool_name = str(call["tool_name"])
            dispatched = bool(call["dispatched"])
            if not dispatched:
                error = "Ash stopped before this tool was dispatched; it was not run."
                call_errors[call_id] = error
                turn_recovered.append(
                    RecoveredToolCall(
                        call_id=call_id,
                        tool_name=tool_name,
                        turn_id=turn_id,
                        error=error,
                        dispatched=False,
                        ambiguous=False,
                    )
                )
                continue
            if tool_name not in EDIT_TOOLS:
                error = (
                    "Ash stopped while this tool was running; its outcome is unknown. "
                    "Inspect the workspace before continuing."
                )
                call_errors[call_id] = error
                unknown_calls.append(f"{tool_name} ({call_id})")
                turn_unknown.append({"call_id": call_id, "tool": tool_name})
                turn_recovered.append(
                    RecoveredToolCall(
                        call_id=call_id,
                        tool_name=tool_name,
                        turn_id=turn_id,
                        error=error,
                        dispatched=True,
                        ambiguous=True,
                    )
                )
                continue

            rows = store.file_checkpoints_for_call(session_id, turn_id, call_id)
            if not rows:
                error = (
                    "Ash stopped before a file checkpoint was durable; the tool outcome "
                    "is unknown. Inspect the workspace before continuing."
                )
                call_errors[call_id] = error
                unknown_calls.append(f"{tool_name} ({call_id})")
                turn_unknown.append({"call_id": call_id, "tool": tool_name})
                turn_recovered.append(
                    RecoveredToolCall(
                        call_id=call_id,
                        tool_name=tool_name,
                        turn_id=turn_id,
                        error=error,
                        dispatched=True,
                        ambiguous=True,
                    )
                )
                continue
            safe_rows: list[tuple[Any, Path]] = []
            conflicts: list[Path] = []
            for row in rows:
                path = guard.validate_path(row["path"])
                before_digest = _checkpoint_before_digest(row)
                current_digest = _digest(path)
                after_digest = row["after_sha256"]
                if _digests_match(current_digest, before_digest):
                    safe_rows.append((row, path))
                elif after_digest is not None and _digests_match(
                    current_digest, after_digest
                ):
                    safe_rows.append((row, path))
                else:
                    conflicts.append(path)

            if conflicts:
                error = (
                    "Ash stopped during this file edit, but the affected file changed "
                    "again; automatic rollback was refused."
                )
                call_errors[call_id] = error
                for path in conflicts:
                    unresolved_files.append(path)
                    turn_unresolved.append(_relative_display(path, guard.project_root))
                turn_recovered.append(
                    RecoveredToolCall(
                        call_id=call_id,
                        tool_name=tool_name,
                        turn_id=turn_id,
                        error=error,
                        dispatched=True,
                        ambiguous=True,
                    )
                )
                continue

            rows_to_restore = [
                (row, path)
                for row, path in safe_rows
                if not _digests_match(_digest(path), _checkpoint_before_digest(row))
            ]
            _restore_checkpoint_rows(rows_to_restore)
            restored_checkpoint_ids.extend(
                int(row["checkpoint_id"]) for row, _ in safe_rows
            )
            compensated_calls.append(f"{tool_name} ({call_id})")
            turn_compensated.append(call_id)
            compensated_files.extend(path for _, path in rows_to_restore)
            call_errors[call_id] = (
                "Interrupted file edit was rolled back during startup recovery."
            )
            turn_recovered.append(
                RecoveredToolCall(
                    call_id=call_id,
                    tool_name=tool_name,
                    turn_id=turn_id,
                    error=call_errors[call_id],
                    dispatched=True,
                    ambiguous=False,
                )
            )

        unmatched = store.unmatched_incomplete_checkpoints(
            session_id, turn_id, pending_ids
        )
        for row in unmatched:
            path = guard.validate_path(row["path"])
            if _digest(path) == _checkpoint_before_digest(row):
                restored_checkpoint_ids.append(int(row["checkpoint_id"]))
            else:
                unresolved_files.append(path)
                turn_unresolved.append(_relative_display(path, guard.project_root))

        status = (
            "needs_attention"
            if turn_unknown or turn_unresolved
            else "compensated"
            if turn_compensated or restored_checkpoint_ids
            else "interrupted"
        )
        report: dict[str, Any] = {
            "turn_id": turn_id,
            "status": status,
            "compensated_calls": turn_compensated,
            "unknown_calls": turn_unknown,
            "unresolved_files": list(dict.fromkeys(turn_unresolved)),
            "recovered_calls": [
                {
                    "call_id": call.call_id,
                    "tool": call.tool_name,
                    "turn_id": call.turn_id,
                    "error": call.error,
                    "dispatched": call.dispatched,
                    "ambiguous": call.ambiguous,
                }
                for call in turn_recovered
            ],
        }
        store.finalize_interrupted_recovery(
            session_id,
            turn_id,
            call_errors=call_errors,
            restored_checkpoint_ids=list(dict.fromkeys(restored_checkpoint_ids)),
            recovery=report,
            recovered_calls=turn_recovered,
        )
        recovered_calls.extend(turn_recovered)

    return RecoverySummary(
        interrupted_turns=len(turns),
        compensated_calls=len(compensated_calls),
        compensated_files=tuple(dict.fromkeys(compensated_files)),
        unknown_calls=tuple(unknown_calls),
        unresolved_files=tuple(dict.fromkeys(unresolved_files)),
        recovered_calls=tuple(recovered_calls),
    )


def _checkpoint_before_digest(row: Any) -> str:
    if not bool(row["existed"]):
        return "missing"
    return hashlib.sha256(_checkpoint_content(row)).hexdigest()


def _restore_checkpoint_rows(rows: list[tuple[Any, Path]]) -> None:
    originals: dict[Path, tuple[bool, bytes, int | None]] = {}
    for _, path in rows:
        existed = path.is_file()
        originals[path] = (
            existed,
            _read_checkpoint_bytes(path) if existed else b"",
            path.stat().st_mode if existed else None,
        )
    try:
        for row, path in rows:
            if bool(row["existed"]):
                _atomic_restore(path, _checkpoint_content(row))
                if row["before_mode"] is not None:
                    os.chmod(path, int(row["before_mode"]))
            else:
                path.unlink(missing_ok=True)
    except OSError:
        for path, (existed, content, mode) in originals.items():
            if existed:
                _atomic_restore(path, content)
                if mode is not None:
                    os.chmod(path, mode)
            else:
                path.unlink(missing_ok=True)
        raise


def undo_latest_checkpoint(
    store: SessionStore, guard: SafetyGuard, session_id: str
) -> list[Path]:
    rows = store.latest_file_checkpoints(session_id)
    if not rows:
        return []
    paths = [guard.validate_path(row["path"]) for row in rows]
    conflicts = _checkpoint_chain_conflicts(rows, paths)
    if conflicts:
        raise RuntimeError(
            "Undo refused because files changed after Ash's edit: "
            + ", ".join(conflicts)
        )
    for row, path in zip(rows, paths, strict=True):
        if bool(row["existed"]):
            _atomic_restore(path, _checkpoint_content(row))
            if row["before_mode"] is not None:
                os.chmod(path, int(row["before_mode"]))
        else:
            path.unlink(missing_ok=True)
    store.mark_file_checkpoints_restored(session_id, rows[0]["turn_id"])
    return list(dict.fromkeys(paths))


def rewind_session_with_files(
    store: SessionStore,
    guard: SafetyGuard,
    session_id: str,
    message_count: int,
) -> tuple[Session, list[Path]]:
    """Rewind complete turns and restore all of their direct file edits."""

    turn_ids = store.rewind_turn_ids(
        session_id,
        message_count,
        require_complete_mapping=True,
    )
    rows = store.file_checkpoints_for_turns(session_id, turn_ids)
    paths = [guard.validate_path(row["path"]) for row in rows]
    simulated: dict[Path, str] = {}
    conflicts: list[str] = []
    for row, path in zip(rows, paths, strict=True):
        after_sha256 = row["after_sha256"]
        if after_sha256 is None:
            raise RuntimeError(
                f"Combined rewind refused because a checkpoint is incomplete: {path}"
            )
        current = simulated.setdefault(path, _digest(path))
        if current != after_sha256:
            conflicts.append(str(path))
        simulated[path] = (
            hashlib.sha256(_checkpoint_content(row)).hexdigest()
            if bool(row["existed"])
            else "missing"
        )
    if conflicts:
        raise RuntimeError(
            "Combined rewind refused because files changed after Ash's edit: "
            + ", ".join(dict.fromkeys(conflicts))
        )

    originals: dict[Path, tuple[bool, bytes, int | None]] = {}
    for path in paths:
        if path in originals:
            continue
        existed = path.is_file()
        originals[path] = (
            existed,
            _read_checkpoint_bytes(path) if existed else b"",
            path.stat().st_mode if existed else None,
        )

    try:
        for row, path in zip(rows, paths, strict=True):
            if bool(row["existed"]):
                _atomic_restore(path, _checkpoint_content(row))
                if row["before_mode"] is not None:
                    os.chmod(path, int(row["before_mode"]))
            else:
                path.unlink(missing_ok=True)
        session = store.rewind_session(
            session_id,
            message_count,
            restored_checkpoint_turn_ids=turn_ids,
        )
    except Exception:
        rollback_errors: list[str] = []
        for path, (existed, content, mode) in originals.items():
            try:
                if existed:
                    _atomic_restore(path, content)
                    if mode is not None:
                        os.chmod(path, mode)
                else:
                    path.unlink(missing_ok=True)
            except OSError as exc:
                rollback_errors.append(f"{path}: {exc}")
        if rollback_errors:
            raise RuntimeError(
                "Combined rewind failed and file rollback was incomplete: "
                + "; ".join(rollback_errors)
            )
        raise
    return session, list(dict.fromkeys(paths))


def diff_latest_checkpoint(
    store: SessionStore,
    guard: SafetyGuard,
    session_id: str,
    *,
    max_lines: int = MAX_CHECKPOINT_DIFF_LINES,
) -> str:
    """Render a bounded unified diff for the latest unrestored checkpoint group."""

    rows = store.latest_file_checkpoints(session_id)
    if not rows:
        return "No checkpointed file changes for this session."
    paths = [guard.validate_path(row["path"]) for row in rows]
    conflicts = _checkpoint_chain_conflicts(rows, paths)
    if conflicts:
        raise RuntimeError(
            "Checkpoint diff refused because files changed after Ash's edit: "
            + ", ".join(conflicts)
        )

    lines: list[str] = []
    truncated = False
    earliest_by_path: dict[Path, Any] = {}
    for row, path in zip(rows, paths, strict=True):
        earliest_by_path[path] = row
    for path, row in earliest_by_path.items():
        before = _checkpoint_content(row) if bool(row["existed"]) else b""
        after = _read_checkpoint_bytes(path) if path.is_file() else b""
        if _looks_binary(before) or _looks_binary(after):
            lines.append(f"Binary file changed: {path}")
            continue
        relative = _relative_display(path, guard.project_root)
        before_lines = before.decode("utf-8", errors="replace").splitlines()
        after_lines = after.decode("utf-8", errors="replace").splitlines()
        remaining = max_lines - len(lines)
        if remaining <= 0:
            truncated = True
            break
        diff = difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
            lineterm="",
        )
        diff_lines = list(islice(diff, remaining + 1))
        if not diff_lines:
            continue
        if len(diff_lines) > remaining:
            lines.extend(diff_lines[:remaining])
            truncated = True
            break
        lines.extend(diff_lines)
    if truncated:
        lines.append("[checkpoint diff truncated]")
    return "\n".join(lines) if lines else "No checkpoint diff."


def _checkpoint_chain_conflicts(rows: list[Any], paths: list[Path]) -> list[str]:
    simulated: dict[Path, str] = {}
    conflicts: list[str] = []
    for row, path in zip(rows, paths, strict=True):
        current = simulated.setdefault(path, _digest(path))
        if row["after_sha256"] is None or not _digests_match(
            current, row["after_sha256"]
        ):
            conflicts.append(str(path))
        simulated[path] = _checkpoint_before_digest(row)
    return list(dict.fromkeys(conflicts))


def _digest(path: Path) -> str:
    if not path.is_file():
        return "missing"
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHECKPOINT_READ_CHUNK_BYTES):
            total += len(chunk)
            if total > MAX_CHECKPOINT_BYTES:
                return OVERSIZED_DIGEST
            digest.update(chunk)
    return digest.hexdigest()


def _digests_match(left: str, right: str) -> bool:
    """Compare only bounded, trustworthy file digests."""

    return left != OVERSIZED_DIGEST and right != OVERSIZED_DIGEST and left == right


def _checkpoint_content(row: Any) -> bytes:
    """Return a durable checkpoint payload without accepting oversized rows."""

    content = bytes(row["before_content"] or b"")
    if len(content) > MAX_CHECKPOINT_BYTES:
        raise ValueError(f"Checkpoint payload exceeds {MAX_CHECKPOINT_BYTES} bytes")
    return content


def _read_checkpoint_bytes(path: Path) -> bytes:
    """Read a checkpoint candidate in bounded chunks."""

    chunks: list[bytes] = []
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(
            min(CHECKPOINT_READ_CHUNK_BYTES, MAX_CHECKPOINT_BYTES + 1 - total)
        ):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_CHECKPOINT_BYTES:
                raise ValueError(
                    f"Refusing uncheckpointed edit over {MAX_CHECKPOINT_BYTES} bytes: "
                    f"{path}"
                )
    return b"".join(chunks)


def _looks_binary(content: bytes) -> bool:
    return b"\0" in content


def _relative_display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _atomic_restore(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
