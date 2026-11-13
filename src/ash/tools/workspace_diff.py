"""Read-only review of Ash-owned edits, including in projects without Git."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from ash.core.checkpoints import MAX_CHECKPOINT_DIFF_LINES, diff_turn_checkpoint
from ash.core.redaction import redact_text
from ash.core.session import SessionStore
from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.tools.base import (
    BaseTool,
    ToolResult,
    count_output_tokens,
)


MAX_WORKSPACE_DIFF_BYTES = 16_384
_TRUNCATED_NOTICE = "\n[workspace diff truncated; use focused file reads to inspect more]"


class WorkspaceDiffArgs(BaseModel):
    max_lines: int = Field(200, ge=1, le=MAX_CHECKPOINT_DIFF_LINES)
    max_output_bytes: int = Field(8192, ge=256, le=MAX_WORKSPACE_DIFF_BYTES)


class WorkspaceDiffTool(BaseTool):
    name = "workspace_diff"
    description = (
        "Review the current Ash turn's before/after file edits as a bounded "
        "unified diff without Git, commands, or write permissions. Refuses "
        "stale files changed outside Ash. This covers recorded Ash file edits "
        "only, not shell-made changes or the whole Git worktree."
    )
    args_schema = WorkspaceDiffArgs
    # Though read-only, a checkpoint diff depends on preceding edit effects.
    # Do not parallelize it alongside a model's simultaneous write request.

    def __init__(
        self,
        safety_guard: SafetyGuard,
        session_store: SessionStore,
        active_turn: Callable[[], tuple[str, str] | None],
    ) -> None:
        super().__init__(safety_guard)
        self.session_store = session_store
        self.active_turn = active_turn

    async def run(self, **kwargs: Any) -> ToolResult:
        args = WorkspaceDiffArgs(**kwargs)
        context = self.active_turn()
        if context is None:
            return ToolResult(
                success=False, output="",
                error="Workspace diff requires an active coding turn.",
            )
        try:
            text = diff_turn_checkpoint(
                self.session_store,
                self.safety_guard,
                context[0],
                context[1],
                max_lines=args.max_lines,
            )
        except (RuntimeError, ValueError, OSError, SafetyViolation) as exc:
            # A conflicting edit must never be presented as a current diff.
            return ToolResult(
                success=False, output="",
                error=redact_text(str(exc))[:1024],
            )
        redacted = redact_text(text)
        encoded = redacted.encode("utf-8")
        truncated = len(encoded) > args.max_output_bytes
        if truncated:
            budget = args.max_output_bytes - len(_TRUNCATED_NOTICE.encode("utf-8"))
            redacted = encoded[:budget].decode("utf-8", errors="ignore") + _TRUNCATED_NOTICE
        return ToolResult(
            success=True,
            output=redacted,
            truncated=truncated or "[checkpoint diff truncated]" in redacted,
            token_count=count_output_tokens(redacted),
        )
