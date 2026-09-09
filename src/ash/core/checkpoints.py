"""Conflict-aware checkpoints for direct Ash file edits."""

from __future__ import annotations

import difflib
import hashlib
from itertools import islice
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from ash.core.session import Session, SessionStore
from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.safety.scoped_io import (
    ScopedFileSnapshot,
    ScopedIOError,
    remove_scoped_file,
    restore_scoped_file,
    snapshot_scoped_file,
)
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
            _, snapshot = _checkpoint_snapshot(path, self.guard)
            self.store.save_file_checkpoint(
                session_id,
                turn_id,
                tool_name,
                str(path),
                existed=snapshot.exists,
                before_content=snapshot.content if snapshot.exists else None,
                before_mode=snapshot.mode,
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
            digest = _digest(path, self.guard)
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
            return [self.guard.validate_mutation_path(path) for path in sorted(raw)]
        raw_path = arguments.get("file_path")
        return [self.guard.validate_mutation_path(str(raw_path))] if raw_path else []


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
                raw_path = Path(str(row["path"]))
                try:
                    path = guard.validate_mutation_path(raw_path)
                    before_digest = _checkpoint_before_digest(row)
                    current_digest = _digest(path, guard)
                except (OSError, SafetyViolation):
                    conflicts.append(raw_path)
                    continue
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

            rows_to_restore: list[tuple[Any, Path]] = []
            for row, path in safe_rows:
                try:
                    current_digest = _digest(path, guard)
                except (OSError, SafetyViolation):
                    conflicts.append(path)
                    continue
                if not _digests_match(
                    current_digest, _checkpoint_before_digest(row)
                ):
                    rows_to_restore.append((row, path))
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
            try:
                _restore_checkpoint_rows(rows_to_restore, guard)
            except (OSError, SafetyViolation, RuntimeError) as exc:
                error = (
                    "Ash stopped during this file edit, but the affected file could "
                    "not be safely restored; automatic rollback was refused."
                )
                call_errors[call_id] = error
                paths_needing_attention = [path for _, path in rows_to_restore]
                for path in paths_needing_attention:
                    unresolved_files.append(path)
                    turn_unresolved.append(_relative_display(path, guard.project_root))
                turn_recovered.append(
                    RecoveredToolCall(
                        call_id=call_id,
                        tool_name=tool_name,
                        turn_id=turn_id,
                        error=f"{error} ({exc})",
                        dispatched=True,
                        ambiguous=True,
                    )
                )
                continue
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
            raw_path = Path(str(row["path"]))
            try:
                path = guard.validate_mutation_path(raw_path)
                current_digest = _digest(path, guard)
            except (OSError, SafetyViolation):
                unresolved_files.append(raw_path)
                turn_unresolved.append(_relative_display(raw_path, guard.project_root))
                continue
            if current_digest == _checkpoint_before_digest(row):
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


def _restore_checkpoint_rows(
    rows: list[tuple[Any, Path]], guard: SafetyGuard
) -> None:
    originals = _capture_file_states((path for _, path in rows), guard)
    try:
        _apply_checkpoint_rows(rows, guard, originals)
    except Exception:
        _rollback_file_states(originals, guard)
        raise


def _capture_file_states(
    paths: Iterable[Path], guard: SafetyGuard
) -> dict[Path, ScopedFileSnapshot]:
    snapshots: dict[Path, ScopedFileSnapshot] = {}
    for path in paths:
        if path in snapshots:
            continue
        _, snapshots[path] = _checkpoint_snapshot(path, guard)
    return snapshots


def _apply_checkpoint_rows(
    rows: list[tuple[Any, Path]],
    guard: SafetyGuard,
    originals: dict[Path, ScopedFileSnapshot],
) -> None:
    current_digests = {path: snapshot.sha256 for path, snapshot in originals.items()}
    for row, path in rows:
        expected = row["after_sha256"] or current_digests[path]
        if bool(row["existed"]):
            restore_scoped_file(
                path,
                _checkpoint_content(row),
                guard,
                expected_sha256=str(expected),
                mode=(
                    int(row["before_mode"])
                    if row["before_mode"] is not None
                    else None
                ),
                max_bytes=MAX_CHECKPOINT_BYTES,
            )
        else:
            remove_scoped_file(
                path,
                guard,
                expected_sha256=str(expected),
                max_bytes=MAX_CHECKPOINT_BYTES,
            )
        current_digests[path] = _checkpoint_before_digest(row)


def _rollback_file_states(
    originals: dict[Path, ScopedFileSnapshot], guard: SafetyGuard
) -> None:
    rollback_errors: list[str] = []
    for path, original in originals.items():
        try:
            _, current = _checkpoint_snapshot(path, guard)
            if original.exists:
                restore_scoped_file(
                    path,
                    original.content,
                    guard,
                    expected_sha256=current.sha256,
                    mode=original.mode,
                    max_bytes=MAX_CHECKPOINT_BYTES,
                )
            else:
                remove_scoped_file(
                    path,
                    guard,
                    expected_sha256=current.sha256,
                    max_bytes=MAX_CHECKPOINT_BYTES,
                )
        except (OSError, SafetyViolation) as exc:
            rollback_errors.append(f"{path}: {exc}")
    if rollback_errors:
        raise RuntimeError(
            "Checkpoint file rollback was incomplete: " + "; ".join(rollback_errors)
        )


def undo_latest_checkpoint(
    store: SessionStore, guard: SafetyGuard, session_id: str
) -> list[Path]:
    rows = store.latest_file_checkpoints(session_id)
    if not rows:
        return []
    paths = [guard.validate_mutation_path(row["path"]) for row in rows]
    conflicts = _checkpoint_chain_conflicts(rows, paths, guard)
    if conflicts:
        raise RuntimeError(
            "Undo refused because files changed after Ash's edit: "
            + ", ".join(conflicts)
        )
    _restore_checkpoint_rows(list(zip(rows, paths, strict=True)), guard)
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
    paths = [guard.validate_mutation_path(row["path"]) for row in rows]
    simulated: dict[Path, str] = {}
    conflicts: list[str] = []
    for row, path in zip(rows, paths, strict=True):
        after_sha256 = row["after_sha256"]
        if after_sha256 is None:
            raise RuntimeError(
                f"Combined rewind refused because a checkpoint is incomplete: {path}"
            )
        current = simulated.setdefault(path, _digest(path, guard))
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

    originals = _capture_file_states(paths, guard)

    try:
        _apply_checkpoint_rows(list(zip(rows, paths, strict=True)), guard, originals)
        session = store.rewind_session(
            session_id,
            message_count,
            restored_checkpoint_turn_ids=turn_ids,
        )
    except Exception:
        try:
            _rollback_file_states(originals, guard)
        except RuntimeError as rollback_error:
            raise RuntimeError(
                "Combined rewind failed and file rollback was incomplete: "
                + str(rollback_error)
            ) from rollback_error
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
    paths = [guard.validate_mutation_path(row["path"]) for row in rows]
    conflicts = _checkpoint_chain_conflicts(rows, paths, guard)
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
        _, snapshot = _checkpoint_snapshot(path, guard)
        after = snapshot.content if snapshot.exists else b""
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


def _checkpoint_chain_conflicts(
    rows: list[Any], paths: list[Path], guard: SafetyGuard
) -> list[str]:
    simulated: dict[Path, str] = {}
    conflicts: list[str] = []
    for row, path in zip(rows, paths, strict=True):
        current = simulated.setdefault(path, _digest(path, guard))
        if row["after_sha256"] is None or not _digests_match(
            current, row["after_sha256"]
        ):
            conflicts.append(str(path))
        simulated[path] = _checkpoint_before_digest(row)
    return list(dict.fromkeys(conflicts))


def _checkpoint_snapshot(
    path: Path, guard: SafetyGuard
) -> tuple[Path, ScopedFileSnapshot]:
    try:
        return snapshot_scoped_file(
            path,
            guard,
            max_bytes=MAX_CHECKPOINT_BYTES,
        )
    except ScopedIOError as exc:
        if str(exc).startswith("file exceeds "):
            raise ValueError(
                f"Refusing uncheckpointed edit over {MAX_CHECKPOINT_BYTES} bytes: "
                f"{path}"
            ) from exc
        raise


def _digest(path: Path, guard: SafetyGuard) -> str:
    try:
        _, snapshot = snapshot_scoped_file(
            path,
            guard,
            max_bytes=MAX_CHECKPOINT_BYTES,
        )
    except ScopedIOError as exc:
        if str(exc).startswith("file exceeds "):
            return OVERSIZED_DIGEST
        raise
    return snapshot.sha256


def _digests_match(left: str, right: str) -> bool:
    """Compare only bounded, trustworthy file digests."""

    return left != OVERSIZED_DIGEST and right != OVERSIZED_DIGEST and left == right


def _checkpoint_content(row: Any) -> bytes:
    """Return a durable checkpoint payload without accepting oversized rows."""

    content = bytes(row["before_content"] or b"")
    if len(content) > MAX_CHECKPOINT_BYTES:
        raise ValueError(f"Checkpoint payload exceeds {MAX_CHECKPOINT_BYTES} bytes")
    return content


def _looks_binary(content: bytes) -> bool:
    return b"\0" in content


def _relative_display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)
