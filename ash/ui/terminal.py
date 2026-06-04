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
from dataclasses import dataclass
from typing import Any, Callable, TextIO

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text


ApprovalCallback = Callable[[str, dict[str, Any]], bool]


@dataclass
class _LiveBuffers:
    thought: Text
    response: Text

    @classmethod
    def fresh(cls) -> "_LiveBuffers":
        return cls(thought=Text(), response=Text())


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

    def __init__(
        self,
        safety_tier: str = "interactive",
        *,
        approval_callback: ApprovalCallback | None = None,
        console: Console | None = None,
        input_stream: TextIO | None = None,
    ) -> None:
        if safety_tier not in {"interactive", "auto_approve", "dry_run"}:
            raise ValueError(f"Unknown safety tier: {safety_tier!r}")
        self.safety_tier = safety_tier
        self._approval_callback = approval_callback
        self.console = console or Console()
        self._input_stream = input_stream or sys.stdin

    # --- streaming surface ------------------------------------------------

    def begin_turn(self) -> Live:
        """Return a :class:`rich.live.Live` context the loop can update."""

        buffers = _LiveBuffers.fresh()
        self._active_buffers = buffers

        def _render() -> Panel:
            return Panel(
                buffers.response,
                title="ash",
                border_style="cyan",
                padding=(0, 1),
            )

        live = Live(_render(), console=self.console, refresh_per_second=12, transient=False)
        self._active_live = live
        return live

    def _active_buffers_required(self) -> _LiveBuffers:
        if not hasattr(self, "_active_buffers") or self._active_buffers is None:
            raise RuntimeError("begin_turn() must be called before streaming output")
        return self._active_buffers

    def print_token(self, text: str) -> None:
        if not text:
            return
        buffers = self._active_buffers_required()
        buffers.response.append(text)
        self._refresh_live()

    def print_thought(self, text: str) -> None:
        if not text:
            return
        buffers = self._active_buffers_required()
        # Thoughts are rendered as an inline dim annotation in the response
        # panel — this keeps the layout single-region so rich.live does not
        # need a multi-panel grid for V1 minimal.
        if len(buffers.response) > 0:
            buffers.response.append("\n")
        thought = Text("💭 " + text, style="dim italic")
        buffers.response.append(thought)
        buffers.response.append("\n")
        self._refresh_live()

    def finalize_turn(self) -> None:
        """Flush any pending live rendering."""

        live = getattr(self, "_active_live", None)
        if live is not None:
            live.refresh()
        self._active_buffers = None
        self._active_live = None

    def _refresh_live(self) -> None:
        live = getattr(self, "_active_live", None)
        if live is not None:
            live.refresh()

    # --- approval surface -------------------------------------------------

    def request_tool_approval(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Decide whether the loop may execute a tool call."""

        if self._approval_callback is not None:
            return bool(self._approval_callback(tool_name, arguments))

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
        body.append(f"Tool: ", style="bold")
        body.append(tool_name, style="cyan")
        body.append("\nArgs:\n")
        for key, value in arguments.items():
            body.append(f"  {key} = ", style="dim")
            body.append(repr(value))
            body.append("\n")
        if auto:
            body.append("\n[auto-approved]", style="green")
        else:
            body.append("\nApprove? [y/N] ", style="bold yellow")
        self.console.print(Panel(body, border_style="yellow", title="approval"))

        if live is not None:
            live.start()
