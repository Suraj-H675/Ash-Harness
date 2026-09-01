"""Rich-based terminal UI for streaming thoughts and tool output.

The UI is a thin facade over ``rich`` that the loop drives imperatively.
Two streams are surfaced: ``thought`` events render in a dim italic
panel, ``token`` events render in the primary response panel. Tool
approvals use an in-band key prompt in interactive mode; in
``auto_approve`` / ``dry_run`` the decision is made without a prompt so
automated tests and CI can drive the loop.
"""

from __future__ import annotations

import difflib
import os
import shlex
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskID, TextColumn
from rich.text import Text

from ash.ui.transcript import Transcript
from ash.ui.theme import get_theme


ApprovalCallback = Callable[[str, dict[str, Any]], bool | str]
MAX_EDIT_PREVIEW_FILE_BYTES = 1_000_000
MAX_EDIT_PREVIEW_TEXT_CHARS = 128_000
MAX_EDIT_PREVIEW_LINES = 400
DIFF_PREVIEW_TRUNCATED = "[diff preview truncated]"


@dataclass
class _LiveBuffers:
    thought: Text
    response: str
    tool_output: Text

    @classmethod
    def fresh(cls) -> "_LiveBuffers":
        return cls(thought=Text(), response="", tool_output=Text())


def _bounded_preview_lines(value: str) -> tuple[list[str], bool]:
    snippet = value[:MAX_EDIT_PREVIEW_TEXT_CHARS]
    truncated = len(snippet) < len(value)
    lines = snippet.splitlines()
    if len(lines) > MAX_EDIT_PREVIEW_LINES:
        lines = lines[:MAX_EDIT_PREVIEW_LINES]
        truncated = True
    return lines, truncated


def _append_preview_truncation(preview: str, truncated: bool) -> str:
    if not preview:
        return preview
    lines = preview.splitlines()
    if len(lines) > 200:
        return "\n".join(lines[:200] + [DIFF_PREVIEW_TRUNCATED])
    if truncated and not preview.endswith(DIFF_PREVIEW_TRUNCATED):
        return f"{preview}\n{DIFF_PREVIEW_TRUNCATED}"
    return preview


def _read_preview_file(path: Path) -> str | None:
    with path.open("rb") as handle:
        raw = handle.read(MAX_EDIT_PREVIEW_FILE_BYTES + 1)
    if len(raw) > MAX_EDIT_PREVIEW_FILE_BYTES:
        return None
    return raw.decode("utf-8")


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
        theme: str = "dark",
        screen_reader_mode: bool = False,
        workspace_root: Path | None = None,
        transcript: Transcript | None = None,
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
        self.screen_reader_mode = screen_reader_mode
        if screen_reader_mode:
            no_color = True
            reduced_motion = True
            show_token_meter = False
            theme = "dark"
        self.console = console or Console(no_color=no_color)
        self.theme = get_theme(theme)
        self._input_stream = input_stream or sys.stdin
        self._active_buffers: _LiveBuffers | None = None
        self._active_live: Live | None = None
        self._session_approvals: set[str] = set()
        self.show_token_meter = show_token_meter
        self.reduced_motion = reduced_motion
        self.workspace_root = workspace_root.resolve() if workspace_root else None
        self.transcript = transcript or Transcript()
        self._assistant_entry_id: str | None = None
        self._reasoning_entry_id: str | None = None
        self._tool_output_entries: dict[str, str] = {}
        self.viewport_mode = False
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

    def begin_turn(self) -> Any:
        """Return a :class:`rich.live.Live` context the loop can update."""

        buffers: _LiveBuffers = _LiveBuffers.fresh()
        self._active_buffers = buffers
        self._assistant_entry_id = None
        self._reasoning_entry_id = None
        if self._token_progress is not None:
            self._token_task = self._token_progress.add_task(
                "[dim]Tokens", total=100000, completed=0
            )
        else:
            self._token_task = None

        if self.viewport_mode or self.screen_reader_mode:
            self._active_live = None
            self._last_refresh = 0.0
            return nullcontext()

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
        if buffers.tool_output:
            parts.append(buffers.tool_output)
        parts.append(Markdown(buffers.response, hyperlinks=False))
        if self.show_token_meter and self._token_task is not None:
            parts.append(
                Text(
                    self._render_token_meter(
                        self._current_tokens, self._maximum_tokens
                    ),
                    style="dim italic" if self.theme.name == "light" else "dim",
                )
            )
        return Panel(
            Group(*parts),
            title="ash",
            border_style=self.theme.border_primary,
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
        if self._assistant_entry_id is None:
            self._assistant_entry_id = self.transcript.begin("assistant", title="ash")
        self.transcript.append_delta(self._assistant_entry_id, text)
        self._refresh_live()

    def print_thought(self, text: str) -> None:
        if not text:
            return
        buffers = self._active_buffers_required()
        if self._reasoning_entry_id is None:
            self._reasoning_entry_id = self.transcript.begin(
                "reasoning", title="reasoning"
            )
        elif buffers.thought:
            self.transcript.append_delta(self._reasoning_entry_id, "\n")
        self.transcript.append_delta(self._reasoning_entry_id, text)
        if buffers.thought:
            buffers.thought.append("\n")
        buffers.thought.append("reasoning: " + text, style="dim italic")
        if self.screen_reader_mode:
            self.console.print(
                f"Reasoning: {text}",
                markup=False,
                highlight=False,
            )
        self._refresh_live()

    def finalize_turn(self) -> None:
        """Flush any pending live rendering."""

        live = getattr(self, "_active_live", None)
        if live is not None:
            live.update(self._render_active_turn(), refresh=True)
        if self.screen_reader_mode and self._active_buffers is not None:
            response = self._active_buffers.response
            if response:
                self.console.print(Markdown(response, hyperlinks=False))
        if self._reasoning_entry_id is not None:
            self.transcript.finalize(self._reasoning_entry_id)
        if self._assistant_entry_id is not None:
            self.transcript.finalize(self._assistant_entry_id)
        self._active_buffers = None
        self._active_live = None
        self._reasoning_entry_id = None
        self._assistant_entry_id = None
        if self._token_progress is not None:
            self._token_progress.stop()
        self._token_task = None

    def update_token_count(self, current: int, maximum: int | None = None) -> None:
        """Update the token progress bar with current / maximum counts."""
        self._current_tokens = current
        if maximum is not None:
            self._maximum_tokens = maximum
        if self._token_task is None or self._token_progress is None:
            return
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

    def emit_event(self, payload: dict[str, Any]) -> None:
        """Render concise tool lifecycle state outside the assistant panel."""

        event_type = payload.get("type")
        if event_type not in {
            "tool.started",
            "tool.output",
            "tool.completed",
            "tool.denied",
            "tool.error",
        }:
            return
        tool = str(payload.get("tool", "unknown"))
        call_id = str(payload.get("call_id", ""))
        if event_type == "tool.output":
            delta = str(payload.get("delta", ""))
            if not delta:
                return
            entry_id = self._tool_output_entries.get(call_id)
            if entry_id is None:
                entry_id = self.transcript.begin(
                    "tool",
                    title=f"{tool} output",
                    metadata={"type": event_type, "call_id": call_id},
                )
                self._tool_output_entries[call_id] = entry_id
            self.transcript.append_delta(entry_id, delta)
            stream = str(payload.get("stream", "stdout"))
            style = "red" if stream == "stderr" else ""
            if self._active_buffers is not None:
                self._active_buffers.tool_output.append(delta, style=style)
                self._refresh_live()
            elif not self.viewport_mode:
                self.console.print(
                    delta,
                    style=style,
                    end="",
                    markup=False,
                    highlight=False,
                )
            return
        output_entry = self._tool_output_entries.pop(call_id, None)
        if output_entry is not None:
            self.transcript.finalize(output_entry)
        labels = {
            "tool.started": ("started", self.theme.prompt),
            "tool.completed": (
                "completed" if payload.get("success") else "failed",
                self.theme.success if payload.get("success") else self.theme.error,
            ),
            "tool.denied": ("denied", self.theme.border_approval),
            "tool.error": ("error", self.theme.error),
        }
        label, style = labels[event_type]
        self.transcript.append(
            "tool",
            f"{tool} [{label}]",
            title=tool,
            metadata={
                key: payload[key]
                for key in ("type", "call_id", "success")
                if key in payload
            },
        )
        line = Text(
            "tool ", style="dim italic" if self.theme.name == "light" else "dim"
        )
        line.append(tool, style="bold")
        line.append(f" [{label}]", style=style)
        if not self.viewport_mode:
            self.console.print(line)

    # --- approval surface -------------------------------------------------

    def request_tool_approval(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> bool | str:
        """Decide whether the loop may execute a tool call."""

        if self._approval_callback is not None:
            return self._approval_callback(tool_name, arguments)
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

    def is_tool_approved_for_session(self, tool_name: str) -> bool:
        return tool_name in self._session_approvals

    def approve_tool_for_session(self, tool_name: str) -> None:
        self._session_approvals.add(tool_name)

    def show_tool_approval(
        self,
        tool_name: str,
        arguments: dict[str, Any] | dict[str, object],
        *,
        auto: bool,
        diff_mode: str = "unified",
    ) -> None:
        if diff_mode not in {"unified", "side-by-side"}:
            raise ValueError("diff_mode must be unified or side-by-side")
        self._render_approval_notice(
            tool_name,
            dict(arguments),
            auto=auto,
            scope_options=not auto,
            side_by_side=diff_mode == "side-by-side",
        )

    def _render_approval_notice(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        auto: bool,
        scope_options: bool = False,
        side_by_side: bool = False,
    ) -> None:
        # Suspend the live render while we print the approval panel.
        live = getattr(self, "_active_live", None)
        if live is not None:
            live.stop()

        body = Text()
        body.append("Tool: ", style="bold")
        body.append(tool_name, style=self.theme.prompt)
        body.append("\nArgs:\n")
        for key, value in arguments.items():
            body.append(f"  {key} = ", style="dim")
            body.append(repr(value))
            body.append("\n")
        preview = self._edit_preview(
            tool_name,
            arguments,
            side_by_side=side_by_side,
        )
        if preview:
            body.append(
                "\nDiff preview (side-by-side):\n" if side_by_side else "\nDiff preview:\n",
                style="bold",
            )
            body.append(preview, style="dim")
        if auto:
            body.append("\n[auto-approved]", style=self.theme.success)
        elif scope_options:
            command_choice = (
                ", command prefix for project [c]" if tool_name == "run_command" else ""
            )
            body.append(
                "\nApprove once [y], scoped for session [s], tool for session [a], "
                "scoped for project [p], deny scope for project [x]"
                f"{command_choice}, or deny [N]? ",
                style=self.theme.approval_prompt,
            )
        else:
            body.append(
                "\nApprove once [y], for this session [a], or deny [N]? ",
                style=self.theme.approval_prompt,
            )
        self.transcript.append(
            "approval",
            body.plain,
            title=tool_name,
            metadata={"auto": auto},
        )
        if not self.viewport_mode:
            if self.screen_reader_mode:
                self.console.print("Approval:", markup=False, highlight=False)
                self.console.print(body.plain, markup=False, highlight=False)
            else:
                self.console.print(
                    Panel(
                        body,
                        border_style=self.theme.border_approval,
                        title="approval",
                    )
                )

        if live is not None:
            live.start()

    def record_user_input(self, text: str) -> None:
        """Commit submitted user input to the interactive transcript."""

        self.transcript.append("user", text, title="you")

    def load_session_transcript(self, session: Any | None) -> None:
        """Replace viewport history from a durable session snapshot."""

        self.transcript.clear()
        if session is None:
            return
        for message in session.messages:
            content = str(message.content)
            if message.role == "user":
                self.transcript.append("user", content, title="you")
            elif message.role == "assistant" and content:
                self.transcript.append("assistant", content, title="ash")
            elif message.role == "tool":
                bounded = content[:4000]
                if len(content) > len(bounded):
                    bounded += "\n[tool result truncated in transcript]"
                self.transcript.append(
                    "tool",
                    bounded,
                    title="tool result",
                    metadata=dict(message.metadata),
                )

    def _edit_preview(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        side_by_side: bool = False,
    ) -> str:
        if tool_name == "apply_patch":
            patch = arguments.get("patch")
            if isinstance(patch, str):
                lines, truncated = _bounded_preview_lines(patch)
                return "\n".join(
                    lines[:200]
                    + ([DIFF_PREVIEW_TRUNCATED] if truncated or len(lines) > 200 else [])
                )
        if tool_name == "replace_file_content":
            before = arguments.get("target_content")
            after = arguments.get("replacement_content")
            if isinstance(before, str) and isinstance(after, str):
                before_lines, before_truncated = _bounded_preview_lines(before)
                after_lines, after_truncated = _bounded_preview_lines(after)
                preview = self._render_diff(
                    "target",
                    "replacement",
                    before_lines,
                    after_lines,
                    side_by_side=side_by_side,
                )
                return _append_preview_truncation(
                    preview, before_truncated or after_truncated
                )
        if tool_name == "replace_file_edits":
            edits = arguments.get("edits")
            if isinstance(edits, list):
                previews: list[str] = []
                for index, edit in enumerate(edits[:20], start=1):
                    if not isinstance(edit, dict):
                        continue
                    before = edit.get("target_content")
                    after = edit.get("replacement_content")
                    if not isinstance(before, str) or not isinstance(after, str):
                        continue
                    before_lines, before_truncated = _bounded_preview_lines(before)
                    after_lines, after_truncated = _bounded_preview_lines(after)
                    diff = self._render_diff(
                        f"edit-{index}-target",
                        f"edit-{index}-replacement",
                        before_lines,
                        after_lines,
                        side_by_side=side_by_side,
                    )
                    diff = _append_preview_truncation(
                        diff, before_truncated or after_truncated
                    )
                    if diff:
                        previews.append(diff)
                if len(edits) > 20:
                    previews.append("[diff preview truncated]")
                return "\n\n".join(previews)
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
        try:
            path = path.resolve()
        except OSError:
            return "[preview unavailable: path cannot be resolved]"
        try:
            relative = path.relative_to(self.workspace_root)
        except ValueError:
            return "[preview unavailable: path is outside workspace]"
        try:
            before = _read_preview_file(path) if path.is_file() else ""
            if before is None:
                return (
                    "[preview unavailable: existing file exceeds "
                    f"{MAX_EDIT_PREVIEW_FILE_BYTES} bytes]"
                )
        except (OSError, UnicodeError):
            return "[preview unavailable: existing file is not readable text]"
        before_lines, before_truncated = _bounded_preview_lines(before)
        content_lines, content_truncated = _bounded_preview_lines(content)
        preview = self._render_diff(
            f"a/{relative.as_posix()}",
            f"b/{relative.as_posix()}",
            before_lines,
            content_lines,
            side_by_side=side_by_side,
        )
        preview = _append_preview_truncation(
            preview, before_truncated or content_truncated
        )
        if not preview:
            return preview
        lines = preview.splitlines()
        if len(lines) > 200:
            lines = lines[:200] + [DIFF_PREVIEW_TRUNCATED]
        return "\n".join(lines)

    @staticmethod
    def _render_diff(
        old_name: str,
        new_name: str,
        old_lines: list[str],
        new_lines: list[str],
        *,
        side_by_side: bool = False,
    ) -> str:
        if not side_by_side:
            return "\n".join(
                difflib.unified_diff(
                    old_lines,
                    new_lines,
                    fromfile=old_name,
                    tofile=new_name,
                    lineterm="",
                )
            )
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        left_width, right_width = 38, 38
        rows: list[tuple[str, str]] = []
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            old_slice = old_lines[old_start:old_end]
            new_slice = new_lines[new_start:new_end]
            if tag == "equal":
                rows.extend((line, line) for line in old_slice)
            elif tag == "delete":
                rows.extend((line, "") for line in old_slice)
            elif tag == "insert":
                rows.extend(("", line) for line in new_slice)
            else:
                for index in range(max(len(old_slice), len(new_slice))):
                    rows.append(
                        (
                            old_slice[index] if index < len(old_slice) else "",
                            new_slice[index] if index < len(new_slice) else "",
                        )
                    )
        output: list[str] = [
            "--- " + old_name.ljust(left_width)[:left_width],
            "+++ " + new_name.ljust(right_width)[:right_width],
        ]
        for old_line, new_line in rows[:198]:
            output.append(
                old_line.ljust(left_width)[:left_width]
                + " | "
                + new_line[:right_width]
            )
        if len(rows) > 198:
            output.append(DIFF_PREVIEW_TRUNCATED)
        return "\n".join(output)

    # --- sprint planning surface (Sprint 12 / V5) -----------------------

    def show_plan(self, execution: Any) -> bool:
        """Render a sprint plan and ask the user to approve / edit / reject.

        Returns ``True`` when the user approves, ``False`` otherwise.
        Typing ``e`` opens ``$VISUAL`` or ``$EDITOR`` with the plan markdown,
        validates the edited result, then asks for approval again.
        """

        live = getattr(self, "_active_live", None)
        if live is not None:
            live.stop()

        try:
            while True:
                self._render_plan(execution)
                try:
                    answer = self._input_stream.readline().strip().lower()
                except (EOFError, KeyboardInterrupt):
                    answer = ""
                if answer in {"y", "yes"}:
                    return True
                if answer not in {"e", "edit"}:
                    return False
                try:
                    self._edit_plan(execution)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    self.console.print(
                        f"Plan edit failed: {exc}",
                        style=self.theme.error,
                    )
                    return False
        finally:
            if live is not None:
                live.start()

    def show_plan_review(self, execution: Any) -> None:
        """Render a plan without reading input from the terminal."""

        self._render_plan(execution)

    def edit_plan(self, execution: Any) -> None:
        """Open and validate a plan in the configured external editor."""

        self._edit_plan(execution)

    def _render_plan(self, execution: Any) -> None:
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
                if self.screen_reader_mode:
                    mark = "[x]" if item.status.value in {"done", "skipped"} else "[ ]"
                else:
                    mark = "☑" if item.status.value in {"done", "skipped"} else "☐"
                body.append(f"  {mark} [{item.section}] {item.description}\n")
        else:
            body.append("  (empty)\n")
        body.append(
            "\nApprove [y], edit [e], or deny [N]?",
            style=self.theme.approval_prompt,
        )
        self.transcript.append(
            "approval",
            body.plain,
            title=f"sprint {execution.contract.contract_id[:8]}",
            metadata={"type": "plan.approval"},
        )
        if not self.viewport_mode:
            if self.screen_reader_mode:
                self.console.print(
                    f"Sprint {execution.contract.contract_id[:8]}:",
                    markup=False,
                    highlight=False,
                )
                self.console.print(body.plain, markup=False, highlight=False)
            else:
                self.console.print(
                    Panel(
                        body,
                        border_style=self.theme.border_primary,
                        title=f"sprint {execution.contract.contract_id[:8]}",
                    )
                )

    def write_status(self, text: str, *, error: bool = False) -> None:
        if error:
            self.transcript.append("error", text, title="error")
        else:
            self.transcript.append("status", text, title="status")
        if not self.viewport_mode:
            self.console.print(
                text,
                style=self.theme.error if error else None,
            )

    def _edit_plan(self, execution: Any) -> None:
        from ash.core.planner import apply_sprint_markdown_edit, render_sprint_markdown

        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if not editor:
            raise ValueError("Set VISUAL or EDITOR to edit sprint plans")
        command = shlex.split(editor, posix=os.name != "nt")
        if not command:
            raise ValueError("VISUAL/EDITOR is empty")
        file_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".md",
                prefix="ash-plan-",
                delete=False,
            ) as handle:
                handle.write(render_sprint_markdown(execution))
                file_path = Path(handle.name)
            result = subprocess.run([*command, str(file_path)], check=False)
            if result.returncode != 0:
                raise subprocess.SubprocessError(
                    f"editor exited with status {result.returncode}"
                )
            if file_path.stat().st_size > 1_000_000:
                raise ValueError("edited plan exceeds 1 MB")
            apply_sprint_markdown_edit(
                execution,
                file_path.read_text(encoding="utf-8"),
            )
        finally:
            if file_path is not None:
                file_path.unlink(missing_ok=True)
