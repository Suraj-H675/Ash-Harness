"""Conflict-aware checkpoints for direct Ash file edits."""

from __future__ import annotations

import difflib
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from core.session import SessionStore
from safety.guard import SafetyGuard
from tools.base import BaseTool, ToolMiddleware, ToolResult
from tools.patch import extract_patch_paths


EDIT_TOOLS = {
    "write_file",
    "whole_edit",
    "replace_file_content",
    "replace_file_edits",
    "apply_patch",
}
MAX_CHECKPOINT_BYTES = 20 * 1024 * 1024
MAX_CHECKPOINT_DIFF_LINES = 400


class FileCheckpointMiddleware(ToolMiddleware):
    def __init__(
        self,
        store: SessionStore,
        guard: SafetyGuard,
        context_provider: Callable[[], tuple[str, str] | None],
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
        session_id, turn_id = context
        for path in self._paths(tool_name, arguments):
            existed = path.is_file()
            content = path.read_bytes() if existed else None
            if content is not None and len(content) > MAX_CHECKPOINT_BYTES:
                raise ValueError(f"Refusing uncheckpointed edit over 20 MiB: {path}")
            mode = path.stat().st_mode if existed else None
            self.store.save_file_checkpoint(
                session_id,
                turn_id,
                tool_name,
                str(path),
                existed=existed,
                before_content=content,
                before_mode=mode,
            )

    async def after_tool(
        self, tool_name: str, arguments: dict[str, Any], result: ToolResult
    ) -> None:
        context = self.context_provider()
        if context is None or tool_name not in EDIT_TOOLS or not result.success:
            return
        session_id, turn_id = context
        for path in self._paths(tool_name, arguments):
            digest = _digest(path)
            self.store.finish_file_checkpoint(session_id, turn_id, str(path), digest)

    def _paths(self, tool_name: str, arguments: dict[str, Any]) -> list[Path]:
        if tool_name == "apply_patch":
            raw = extract_patch_paths(str(arguments.get("patch", "")), self.guard)
            return [self.guard.validate_path(path) for path in sorted(raw)]
        raw_path = arguments.get("file_path")
        return [self.guard.validate_path(str(raw_path))] if raw_path else []


def undo_latest_checkpoint(
    store: SessionStore, guard: SafetyGuard, session_id: str
) -> list[Path]:
    rows = store.latest_file_checkpoints(session_id)
    if not rows:
        return []
    paths = [guard.validate_path(row["path"]) for row in rows]
    conflicts = [
        str(path)
        for row, path in zip(rows, paths, strict=True)
        if _digest(path) != row["after_sha256"]
    ]
    if conflicts:
        raise RuntimeError(
            "Undo refused because files changed after Ash's edit: "
            + ", ".join(conflicts)
        )
    for row, path in zip(rows, paths, strict=True):
        if bool(row["existed"]):
            _atomic_restore(path, bytes(row["before_content"] or b""))
            if row["before_mode"] is not None:
                os.chmod(path, int(row["before_mode"]))
        else:
            path.unlink(missing_ok=True)
    store.mark_file_checkpoints_restored(session_id, rows[0]["turn_id"])
    return paths


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
    conflicts = [
        str(path)
        for row, path in zip(rows, paths, strict=True)
        if _digest(path) != row["after_sha256"]
    ]
    if conflicts:
        raise RuntimeError(
            "Checkpoint diff refused because files changed after Ash's edit: "
            + ", ".join(conflicts)
        )

    lines: list[str] = []
    truncated = False
    for row, path in zip(rows, paths, strict=True):
        before = bytes(row["before_content"] or b"") if bool(row["existed"]) else b""
        after = path.read_bytes() if path.is_file() else b""
        if _looks_binary(before) or _looks_binary(after):
            lines.append(f"Binary file changed: {path}")
            continue
        relative = _relative_display(path, guard.project_root)
        before_lines = before.decode("utf-8", errors="replace").splitlines()
        after_lines = after.decode("utf-8", errors="replace").splitlines()
        diff = list(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
                lineterm="",
            )
        )
        if not diff:
            continue
        remaining = max_lines - len(lines)
        if remaining <= 0:
            truncated = True
            break
        if len(diff) > remaining:
            lines.extend(diff[:remaining])
            truncated = True
            break
        lines.extend(diff)
    if truncated:
        lines.append("[checkpoint diff truncated]")
    return "\n".join(lines) if lines else "No checkpoint diff."


def _digest(path: Path) -> str:
    if not path.is_file():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
