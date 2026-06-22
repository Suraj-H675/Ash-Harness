"""Rich-based terminal UI for streaming thoughts and tool output.

The UI is a thin facade over ``rich`` that the loop drives imperatively.
Two streams are surfaced: ``thought`` events render in a dim italic
panel, ``token`` events render in the primary response panel. Tool
approvals use an in-band key prompt in interactive mode; in
``auto_approve`` / ``dry_run`` the decision is made without a prompt so
automated tests and CI can drive the loop.
"""

from __future__ import annotations

import sys
import difflib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskID, TextColumn
from rich.text import Text


ApprovalCallback = Callable[[str, dict[str, Any]], bool]


@dataclass
class _LiveBuffers:
    thought: Text
    response: str

    @classmethod
    def fresh(cls) -> "_LiveBuffers":
        return cls(thought=Text(), response="")


class TerminalUI:
    """
    Render streamed LLM output and gate tool approvals.

    Parameters
    ----------
    safety_tier
        ``"interactive"`` prompts on stdin for every tool call.
        ``"auto_approve"`` silently approves everything.
        ``"dry_run"`` silently denies every tool call (useful for replay).
    approval_callback
        Optional override that takes ``(tool_name, arguments)`` and returns
        ``True`` to approve. When set, the safety tier and stdin are
        bypassed. Primarily used by integration tests.
    console
        Optional :class:`rich.console.Console` to write through. Defaults
        to one bound to stdout.
    """

    def __init__(  # noqa: D107
        self,
        safety_tier: str = "interactive",
        *,
        approval_callback: ApprovalCallback | None = None,
        console: Console | None = None,
        input_stream: TextIO | None = None,
        show_token_meter: bool = False,
        no_color: bool = False,
        reduced_motion: bool = False,
        workspace_root: Path | None = None,
    ) -> None:
        if safety_tier not in {
            "interactive",
            "auto_edit",
            "plan",
            "auto_approve",
            "dry_run",
        }:
            raise ValueError(f"Unknown safety tier: {safety_tier!r}")
        self.safety_tier = safety_tier
        self._approval_callback = approval_callback
        self.console = console or Console(no_color=no_color)
        self._input_stream = input_stream or sys.stdin
        self._active_buffers: _LiveBuffers | None = None
        self._active_live: Live | None = None
        self._session_approvals: set[str] = set()
        self.show_token_meter = show_token_meter
        self.reduced_motion = reduced_motion
        self.workspace_root = workspace_root.resolve() if workspace_root else None
        self._token_progress = (
            Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
            )
            if show_token_meter
            else None
        )
        self._token_task: TaskID | None = None
        self._current_tokens = 0
        self._maximum_tokens = 100000
        self._last_refresh = 0.0

    @property
    def has_approval_callback(self) -> bool:
        """Whether an embedding host supplied an explicit decision callback."""

        return self._approval_callback is not None

    # --- streaming surface ------------------------------------------------

    def _render_token_meter(self, current_tokens: int, max_tokens: int) -> str:
        """Render a single-line ASCII token meter.

        Example output when current=3000, max=100000:
        [Token ████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 3000/100000 (3.0%)]
        """
        bar_width = 30
        pct = min(current_tokens / max_tokens, 1.0) if max_tokens > 0 else 0.0
        filled = int(bar_width * pct)
        bar = "█" * filled + "░" * (bar_width - filled)
        label = f"[Token {bar} {current_tokens}/{max_tokens} ({pct * 100:.1f}%)]"
        return label

    def begin_turn(self) -> Live:
        """Return a :class:`rich.live.Live` context the loop can update."""

        buffers: _LiveBuffers = _LiveBuffers.fresh()
        self._active_buffers = buffers
        if self._token_progress is not None:
            self._token_task = self._token_progress.add_task(
                "[dim]Tokens", total=100000, completed=0
            )
        else:
            self._token_task = None

        live = Live(
            self._render_active_turn(),
            console=self.console,
            refresh_per_second=12,
            transient=False,
        )
        self._active_live = live
        self._last_refresh = 0.0
        return live

    def _render_active_turn(self) -> Panel:
        buffers = self._active_buffers_required()
        parts: list[Any] = []
        if buffers.thought:
            parts.append(buffers.thought)
        parts.append(Markdown(buffers.response, hyperlinks=False))
        if self.show_token_meter and self._token_task is not None:
            parts.append(
                Text(
                    self._render_token_meter(
                        self._current_tokens, self._maximum_tokens
                    ),
                    style="dim",
                )
            )
        return Panel(
            Group(*parts),
            title="ash",
            border_style="cyan",
            padding=(0, 1),
        )

    def _active_buffers_required(self) -> _LiveBuffers:
        if not hasattr(self, "_active_buffers") or self._active_buffers is None:
            raise RuntimeError("begin_turn() must be called before streaming output")
        return self._active_buffers

    def print_token(self, text: str) -> None:
        if not text:
            return
        buffers = self._active_buffers_required()
        buffers.response += text
        self._refresh_live()

    def print_thought(self, text: str) -> None:
        if not text:
            return
        buffers = self._active_buffers_required()
        if buffers.thought:
            buffers.thought.append("\n")
        buffers.thought.append("reasoning: " + text, style="dim italic")
        self._refresh_live()

    def finalize_turn(self) -> None:
        """Flush any pending live rendering."""

        live = getattr(self, "_active_live", None)
        if live is not None:
            live.update(self._render_active_turn(), refresh=True)
        self._active_buffers = None
        self._active_live = None
        if self._token_progress is not None:
            self._token_progress.stop()
        self._token_task = None

    def update_token_count(self, current: int, maximum: int | None = None) -> None:
        """Update the token progress bar with current / maximum counts."""
        if self._token_task is None or self._token_progress is None:
            return
        self._current_tokens = current
        if maximum is not None:
            self._maximum_tokens = maximum
        self._token_progress.update(
            self._token_task,
            completed=current,
            total=self._maximum_tokens,
        )

    def _refresh_live(self) -> None:
        live = getattr(self, "_active_live", None)
        if live is None or self.reduced_motion:
            return
        now = time.monotonic()
        repaint = now - self._last_refresh >= 0.05
        live.update(self._render_active_turn(), refresh=repaint)
        if repaint:
            self._last_refresh = now

    # --- approval surface -------------------------------------------------

    def request_tool_approval(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Decide whether the loop may execute a tool call."""

        if self._approval_callback is not None:
            return bool(self._approval_callback(tool_name, arguments))
        if tool_name in self._session_approvals:
            self._render_approval_notice(tool_name, arguments, auto=True)
            return True

        if self.safety_tier == "auto_approve":
            self._render_approval_notice(tool_name, arguments, auto=True)
            return True
        if self.safety_tier == "dry_run":
            self._render_approval_notice(tool_name, arguments, auto=False)
            return False

        self._render_approval_notice(tool_name, arguments, auto=False)
        try:
            answer = self._input_stream.readline().strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer in {"a", "always", "session"}:
            self._session_approvals.add(tool_name)
            return True
        return answer in {"y", "yes"}

    def _render_approval_notice(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        auto: bool,
    ) -> None:
        # Suspend the live render while we print the approval panel.
        live = getattr(self, "_active_live", None)
        if live is not None:
            live.stop()

        body = Text()
        body.append("Tool: ", style="bold")
        body.append(tool_name, style="cyan")
        body.append("\nArgs:\n")
        for key, value in arguments.items():
            body.append(f"  {key} = ", style="dim")
            body.append(repr(value))
            body.append("\n")
        preview = self._edit_preview(tool_name, arguments)
        if preview:
            body.append("\nDiff preview:\n", style="bold")
            body.append(preview, style="dim")
        if auto:
            body.append("\n[auto-approved]", style="green")
        else:
            body.append(
                "\nApprove once [y], for this session [a], or deny [N]? ",
                style="bold yellow",
            )
        self.console.print(Panel(body, border_style="yellow", title="approval"))

        if live is not None:
            live.start()

    def _edit_preview(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "apply_patch":
            patch = arguments.get("patch")
            if isinstance(patch, str):
                lines = patch.splitlines()
                return "\n".join(
                    lines[:200]
                    + (["[diff preview truncated]"] if len(lines) > 200 else [])
                )
        if tool_name == "replace_file_content":
            before = arguments.get("target_content")
            after = arguments.get("replacement_content")
            if isinstance(before, str) and isinstance(after, str):
                return "\n".join(
                    difflib.unified_diff(
                        before.splitlines(),
                        after.splitlines(),
                        fromfile="target",
                        tofile="replacement",
                        lineterm="",
                    )
                )
        if self.workspace_root is None or tool_name not in {
            "write_file",
            "whole_edit",
        }:
            return ""
        raw_path = arguments.get("file_path")
        content = arguments.get("content")
        if not isinstance(raw_path, str) or not isinstance(content, str):
            return ""
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.workspace_root / path
        path = path.resolve()
        try:
            relative = path.relative_to(self.workspace_root)
        except ValueError:
            return "[preview unavailable: path is outside workspace]"
        try:
            before = path.read_text(encoding="utf-8") if path.is_file() else ""
        except (OSError, UnicodeError):
            return "[preview unavailable: existing file is not readable text]"
        lines = list(
            difflib.unified_diff(
                before.splitlines(),
                content.splitlines(),
                fromfile=f"a/{relative.as_posix()}",
                tofile=f"b/{relative.as_posix()}",
                lineterm="",
            )
        )
        if len(lines) > 200:
            lines = lines[:200] + ["[diff preview truncated]"]
        return "\n".join(lines)

    # --- sprint planning surface (Sprint 12 / V5) -----------------------

    def show_plan(self, execution: Any) -> bool:
        """Render a sprint plan and ask the user to approve / edit / reject.

        Returns ``True`` when the user approves, ``False`` otherwise.
        Edits are out of scope for V5 — we treat ``e`` as a synonym
        of ``n`` (reject) and log it; the planner can be re-invoked.
        """

        live = getattr(self, "_active_live", None)
        if live is not None:
            live.stop()

        body = Text()
        body.append("Goal: ", style="bold")
        body.append(execution.contract.goal)
        body.append("\n\n")
        body.append("Definition of Done:\n", style="bold")
        for item in execution.contract.definition_of_done:
            body.append(f"  - {item}\n")
        if not execution.contract.definition_of_done:
            body.append("  (none)\n")
        body.append("\nFiles in Scope:\n", style="bold")
        for path in execution.contract.files_in_scope:
            body.append(f"  - {path}\n")
        if not execution.contract.files_in_scope:
            body.append("  - (none)\n")
        body.append("\nChecklist:\n", style="bold")
        if execution.items:
            for item in execution.items:
                mark = "☑" if item.status.value in {"done", "skipped"} else "☐"
                body.append(f"  {mark} [{item.section}] {item.description}\n")
        else:
            body.append("  (empty)\n")
        self.console.print(
            Panel(
                body,
                border_style="cyan",
                title=f"sprint {execution.contract.contract_id[:8]}",
            )
        )

        try:
            answer = self._input_stream.readline().strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if live is not None:
            live.start()

        if answer in {"y", "yes"}:
            return True
        if answer in {"e", "edit"}:
            # Edits are out of scope for V5; surface that to the user
            # but still treat as a non-approval so they can re-plan.
            self.console.print(
                "[yellow]Edit mode is not implemented yet — rejecting the plan.[/yellow]"
            )
        return False
