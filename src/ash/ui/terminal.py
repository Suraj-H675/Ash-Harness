"""Rich-based terminal UI for streaming thoughts and tool output.

The UI is a thin facade over ``rich`` that the loop drives imperatively.
Two streams are surfaced: ``thought`` events render in a dim italic
panel, ``token`` events render in the primary response panel. Tool
approvals use an in-band key prompt in interactive mode; in
``auto_approve`` / ``dry_run`` the decision is made without a prompt so
automated tests and CI can drive the loop.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import math
import os
import shlex
import subprocess
import sys
import tempfile
import time
from uuid import uuid4
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from rich.cells import cell_len
from pathlib import Path
from typing import Any, Callable, TextIO

from prompt_toolkit.formatted_text import FormattedText
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskID, TextColumn
from rich.text import Text

from ash.core.redaction import redact_value
from ash.safe_io import read_bounded_bytes
from ash.safety.environment import build_scrubbed_environment, resolve_host_executable
from ash.ui.inline_surface import ActivityDockView
from ash.ui.safe_text import terminal_safe_text
from ash.ui.transcript import Transcript, TranscriptEntry
from ash.ui.theme import get_theme


ApprovalCallback = Callable[[str, dict[str, Any]], bool | str]
MAX_EDIT_PREVIEW_FILE_BYTES = 1_000_000
MAX_EDIT_PREVIEW_TEXT_CHARS = 128_000
MAX_EDIT_PREVIEW_LINES = 400
DIFF_PREVIEW_TRUNCATED = "[diff preview truncated]"
MAX_LINEAR_HISTORY_ENTRIES = 12
LIVE_AUX_PREVIEW_CHARS = 3_000
PROMPT_NOTICE_SECONDS = 2.0
ASSISTANT_MESSAGE_PREFIX = "· "
_MODEL_REQUEST_TERMINAL_EVENTS = {
    "model.request.completed",
    "model.request.error",
    "model.request.cancelled",
}
_TURN_TERMINAL_EVENTS = {"turn.completed", "turn.cancelled", "turn.error"}
_TOOL_TERMINAL_EVENTS = {
    "tool.completed",
    "tool.error",
    "tool.denied",
    "tool.skipped",
}
_TOOL_ACTIVITY: dict[str, str] = {
    **dict.fromkeys(
        {
            "read_file",
            "list_dir",
            "glob_files",
            "search_text",
            "git_status",
            "git_diff",
            "git_log",
            "find_symbol",
            "find_references",
            "lsp",
            "search_sessions",
            "list_skills",
            "read_skill_resource",
            "list_automations",
            "list_remote_agents",
            "list_remote_agent_tasks",
        },
        "Inspecting",
    ),
    **dict.fromkeys(
        {
            "web_search",
            "web_fetch",
            "brave",
            "tavily",
            "search_tools",
        },
        "Researching",
    ),
    **dict.fromkeys(
        {
            "browser_navigate",
            "browser_snapshot",
            "browser_tabs",
            "browser_open_tab",
            "browser_focus_tab",
            "browser_close_tab",
            "browser_click",
            "browser_click_at",
            "browser_type",
            "browser_press",
            "browser_hover",
            "browser_select",
            "browser_drag",
            "browser_wait",
            "browser_dialog",
            "browser_scroll",
            "browser_back",
            "browser_screenshot",
            "browser_upload",
            "browser_download",
        },
        "Browsing",
    ),
    **dict.fromkeys(
        {
            "write_file",
            "replace_file_content",
            "replace_file_edits",
            "whole_edit",
            "apply_patch",
        },
        "Coding",
    ),
    **dict.fromkeys({"run_command", "background_process"}, "Running"),
    **dict.fromkeys({"auto_commit"}, "Committing"),
    **dict.fromkeys({"activate_skill"}, "Loading"),
    **dict.fromkeys({"manage_automation"}, "Scheduling"),
    **dict.fromkeys({"update_goal"}, "Planning"),
    **dict.fromkeys({"ask_user"}, "Waiting"),
    **dict.fromkeys(
        {
            "spawn_agent",
            "delegate_agents",
            "delegate_remote_agent",
            "remote_agent_task_status",
            "remote_agent_task_cancel",
            "recover_remote_agent_task",
        },
        "Delegating",
    ),
}
_EDITOR_ENV_ALLOWLIST = (
    "COLORTERM",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "KITTY_WINDOW_ID",
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
    "TMUX",
    "VSCODE_IPC_HOOK_CLI",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
)


@dataclass
class _LiveBuffers:
    thought: Text
    response_chunks: list[str]
    tool_output: Text

    @classmethod
    def fresh(cls) -> "_LiveBuffers":
        return cls(thought=Text(), response_chunks=[], tool_output=Text())

    @property
    def response(self) -> str:
        return "".join(self.response_chunks)


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


def _user_message_text(
    content: str,
    *,
    theme_name: str,
    console: Console,
    width: int,
) -> Text:
    """Build cell-wrapped gray user bands for the linear terminal renderer."""

    width = max(1, width)
    style = get_theme(theme_name).user_message
    fill_background = console.color_system is not None and not console.no_color
    rendered = Text()
    physical_lines: list[Text] = []
    for logical_line in content.split("\n"):
        first_prefix = "> " if width >= 3 else ">" if width == 2 else ""
        continuation_prefix = "  " if width >= 3 else ""
        available = max(1, width - cell_len(first_prefix))
        wrapped = list(
            Text(logical_line).wrap(
                console,
                width=available,
                overflow="fold",
            )
        )
        if not wrapped:
            wrapped = [Text("")]
        for index, line in enumerate(wrapped):
            prefix = first_prefix if index == 0 else continuation_prefix
            row = Text(prefix, style=style)
            row.append_text(line)
            row.stylize(style, 0, len(row))
            padding = max(0, width - row.cell_len) if fill_background else 0
            if padding:
                row.append(" " * padding, style=style)
            physical_lines.append(row)
    for index, line in enumerate(physical_lines):
        if index:
            rendered.append("\n")
        rendered.append_text(line)
    return rendered


def _rich_diff_style(theme_name: str, line: str) -> str:
    theme = get_theme(theme_name)
    if line.startswith(("+++", "---", "@@")):
        return theme.diff_hunk
    if line.startswith("+"):
        return theme.diff_added.replace(" bg:", " on ")
    if line.startswith("-"):
        return theme.diff_removed.replace(" bg:", " on ")
    return theme.diff_context


def _append_styled_diff(
    body: Text,
    preview: str,
    *,
    theme_name: str,
    side_by_side: bool = False,
) -> None:
    for index, line in enumerate(preview.splitlines()):
        if index:
            body.append("\n")
        if side_by_side and " | " in line and not line.startswith(("---", "+++")):
            left, right = line.split(" | ", 1)
            body.append(left, style=_rich_diff_style(theme_name, left))
            body.append(" | ", style=_rich_diff_style(theme_name, " "))
            body.append(right, style=_rich_diff_style(theme_name, right))
            continue
        body.append(line, style=_rich_diff_style(theme_name, line))


def _read_preview_file(path: Path) -> str | None:
    try:
        raw = read_bounded_bytes(
            path,
            MAX_EDIT_PREVIEW_FILE_BYTES,
            label="edit preview file",
        )
    except ValueError as exc:
        if "exceeds" in str(exc):
            return None
        raise OSError(str(exc)) from exc
    return raw.decode("utf-8")


def _editor_launch(
    editor: str,
    *,
    workspace: Path,
) -> tuple[list[str], dict[str, str]]:
    """Resolve a user-selected editor without ambient-secret inheritance."""

    command = shlex.split(editor, posix=os.name != "nt")
    if not command:
        raise ValueError("VISUAL/EDITOR is empty")
    environment = build_scrubbed_environment(_EDITOR_ENV_ALLOWLIST)
    executable = command[0]
    if "/" in executable or "\\" in executable:
        candidate = Path(executable).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        try:
            candidate = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"configured editor is unavailable: {executable}") from exc
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ValueError(f"configured editor is unavailable: {executable}")
        command[0] = str(candidate)
    else:
        resolved = resolve_host_executable(
            executable,
            workspace_root=workspace,
            cwd=workspace,
            search_path=environment.get("PATH"),
        )
        if resolved is None:
            raise ValueError(
                f"configured editor is unavailable outside the workspace: {executable}"
            )
        command[0] = resolved
    return command, environment


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
        self._prompt_invalidator: Callable[[], None] | None = None
        self._prompt_closer: Callable[[], Any] | None = None
        self._prompt_notice: str | None = None
        self._prompt_notice_handle: asyncio.TimerHandle | None = None
        self._terminal_writer: Callable[[Callable[[], None]], None] = (
            lambda callback: callback()
        )
        self._conversation_line_open = False
        self._assistant_prefix_pending = True
        self._conversation_needs_user_gap = False
        self._reasoning_tail = ""
        self._prompt_revision = 0
        self._thinking_render_cache: tuple[int, int, FormattedText] | None = None
        self._session_approvals: set[str] = set()
        self.show_token_meter = show_token_meter
        self.reduced_motion = reduced_motion
        self.workspace_root = workspace_root.resolve() if workspace_root else None
        self.transcript = transcript or Transcript()
        self._assistant_entry_id: str | None = None
        self._reasoning_entry_id: str | None = None
        self._activity_status = ""
        self._activity_turn_closed = False
        self._model_activity: tuple[str | None, str] | None = None
        self._active_tool_activity: dict[str, str] = {}
        self._tool_output_entries: dict[str, str] = {}
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

    def bind_prompt_surface(
        self,
        invalidator: Callable[[], None],
        terminal_writer: Callable[[Callable[[], None]], None] | None = None,
        closer: Callable[[], Any] | None = None,
    ) -> None:
        """Route interactive live updates through the bounded prompt surface."""

        self._prompt_invalidator = invalidator
        self._terminal_writer = terminal_writer or (lambda callback: callback())
        self._prompt_closer = closer
        self._conversation_line_open = False
        self._assistant_prefix_pending = True

    async def aclose_prompt_surface(self) -> None:
        """Restore terminal modes before the runtime closes its event loop."""

        if self._prompt_closer is not None:
            result = self._prompt_closer()
            if hasattr(result, "__await__"):
                await result

    def write_output(self, text: str, *, error: bool = False) -> None:
        """Keep interactive command output inside the selected transcript view."""

        safe = terminal_safe_text(text)
        if not safe:
            return
        if error:
            self.transcript.append("error", safe, title="error")
        else:
            self.transcript.append("status", safe, title="output")
        if self._prompt_invalidator is not None and not self.screen_reader_mode:
            self._prompt_invalidator()
        else:
            self.console.print(
                safe,
                style=self.theme.error if error else None,
                markup=False,
                highlight=False,
                end="" if safe.endswith("\n") else "\n",
            )

    def prompt_status_notice(self) -> str | None:
        """Return transient feedback for the pinned status row, if present."""

        return self._prompt_notice

    def set_prompt_notice(self, text: str) -> None:
        """Show brief command feedback without adding it to conversation history."""

        self._clear_prompt_notice()
        notice = terminal_safe_text(text, single_line=True)[:160] or None
        self._prompt_notice = notice
        if notice is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                self._prompt_notice_handle = loop.call_later(
                    PROMPT_NOTICE_SECONDS,
                    self._expire_prompt_notice,
                    notice,
                )
        if self._prompt_invalidator is not None and not self.screen_reader_mode:
            self._prompt_invalidator()

    def _expire_prompt_notice(self, notice: str) -> None:
        if self._prompt_notice != notice:
            return
        self._prompt_notice = None
        self._prompt_notice_handle = None
        if self._prompt_invalidator is not None and not self.screen_reader_mode:
            self._prompt_invalidator()

    def _clear_prompt_notice(self) -> None:
        handle = self._prompt_notice_handle
        if handle is not None:
            handle.cancel()
            self._prompt_notice_handle = None
        self._prompt_notice = None

    def prompt_thinking_view(self, width: int) -> tuple[int, FormattedText]:
        """Return a bounded reasoning preview for legacy prompt hosts."""

        cached = self._thinking_render_cache
        if cached is not None and cached[:2] == (self._prompt_revision, width):
            return self._prompt_revision, cached[2]
        lines = self._reasoning_tail.splitlines()[-2:]
        if self._reasoning_tail and not lines:
            lines = [self._reasoning_tail]
        rendered = FormattedText([])
        if lines:
            prefix = "THINK  "
            first_width = max(1, width - len(prefix))
            fragments: list[tuple[str, str]] = [
                ("class:reasoning-prefix", prefix),
                ("class:reasoning", lines[0][-first_width:]),
            ]
            if len(lines) > 1:
                fragments.extend(
                    [
                        ("", "\n"),
                        ("class:reasoning", lines[1][-max(1, width):]),
                    ]
                )
            rendered = FormattedText(fragments)
        self._thinking_render_cache = (self._prompt_revision, width, rendered)
        return self._prompt_revision, rendered

    def prompt_dock_view(self) -> ActivityDockView:
        """Return current activity and a bounded provider-reasoning excerpt."""

        reasoning_lines = self._reasoning_tail.splitlines()
        reasoning = terminal_safe_text(
            reasoning_lines[-1] if reasoning_lines else self._reasoning_tail,
            single_line=True,
        )[-LIVE_AUX_PREVIEW_CHARS:]
        return ActivityDockView(
            revision=self._prompt_revision,
            activity=self._current_activity_label(),
            reasoning=reasoning,
        )

    def _current_activity_label(self) -> str | None:
        if self._active_tool_activity:
            categories = set(self._active_tool_activity.values())
            return categories.pop() if len(categories) == 1 else "Working"
        if self._model_activity is not None:
            return self._model_activity[1]
        return None

    def _apply_activity_event(self, payload: dict[str, Any]) -> bool:
        """Reduce runtime lifecycle events to bounded, prompt-facing state."""

        event_type = payload.get("type")
        before = (
            self._model_activity,
            dict(self._active_tool_activity),
            self._reasoning_tail,
            self._activity_turn_closed,
        )
        if event_type == "turn.started":
            self._activity_turn_closed = False
            self._model_activity = None
            self._active_tool_activity.clear()
            self._reasoning_tail = ""
        elif event_type in _TURN_TERMINAL_EVENTS:
            self._activity_turn_closed = True
            self._model_activity = None
            self._active_tool_activity.clear()
            self._reasoning_tail = ""
        elif self._activity_turn_closed:
            return False
        elif event_type == "model.request.started":
            operation_id = payload.get("operation_id")
            self._model_activity = (
                str(operation_id) if operation_id is not None else None,
                "Thinking",
            )
            self._reasoning_tail = ""
        elif event_type == "provider.retrying":
            self._model_activity = (None, "Retrying")
        elif event_type in _MODEL_REQUEST_TERMINAL_EVENTS:
            operation_id = payload.get("operation_id")
            current_id = self._model_activity[0] if self._model_activity else None
            if (
                self._model_activity is not None
                and (
                    operation_id is None
                    or current_id is None
                    or str(operation_id) == current_id
                )
            ):
                self._model_activity = None
        elif event_type == "assistant.delta":
            if (
                self._model_activity is not None
                and self._model_activity[1] == "Thinking"
            ):
                self._model_activity = (self._model_activity[0], "Responding")
        elif event_type == "tool.started":
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and call_id:
                tool_name = payload.get("tool")
                category = _TOOL_ACTIVITY.get(str(tool_name), "Working")
                self._active_tool_activity[call_id] = category
        elif event_type in _TOOL_TERMINAL_EVENTS:
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and call_id:
                self._active_tool_activity.pop(call_id, None)

        after = (
            self._model_activity,
            dict(self._active_tool_activity),
            self._reasoning_tail,
            self._activity_turn_closed,
        )
        return before != after

    def _invalidate_prompt_state(self) -> None:
        self._prompt_revision += 1
        self._thinking_render_cache = None
        if self._prompt_invalidator is not None:
            self._prompt_invalidator()

    @property
    def has_approval_callback(self) -> bool:
        """Whether an embedding host supplied an explicit decision callback."""

        return self._approval_callback is not None

    @property
    def supports_mcp_interactions(self) -> bool:
        """Interactive terminals can explicitly review MCP server requests."""

        return True

    def review_mcp_sampling(
        self, server: str, stage: str, payload: dict[str, Any]
    ) -> bool:
        """Require an explicit one-shot decision for MCP sampling."""

        live = getattr(self, "_active_live", None)
        if live is not None:
            live.stop()
        try:
            safe_server = terminal_safe_text(server, single_line=True)
            label = "request" if stage == "request" else "response"
            body = Text()
            body.append(f"Server: {safe_server}\n", style="bold")
            body.append(
                terminal_safe_text(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        indent=2,
                        allow_nan=False,
                    )
                )
            )
            body.append(
                f"\n\nApprove this MCP sampling {label}? [y/N] ",
                style=self.theme.approval_prompt,
            )
            self.transcript.append(
                "approval",
                f"MCP sampling {label} requested by {safe_server}",
                title="MCP sampling",
                metadata={"server": safe_server, "stage": label},
            )
            if self.screen_reader_mode:
                self.console.print(
                    f"MCP sampling {label} from {safe_server}:",
                    markup=False,
                    highlight=False,
                )
                self.console.print(body.plain, markup=False, highlight=False)
            else:
                self.console.print(
                    Panel(
                        body,
                        title=f"MCP sampling {label}",
                        border_style=self.theme.border_approval,
                    )
                )
            raw = self._input_stream.readline()
            if raw == "":
                return False
            return raw.strip().casefold() in {"y", "yes"}
        except (EOFError, KeyboardInterrupt, OSError, ValueError, TypeError):
            return False
        finally:
            if live is not None:
                live.start()

    @staticmethod
    def _parse_mcp_form_value(raw: str, schema: dict[str, Any]) -> Any:
        field_type = schema.get("type")
        if field_type == "boolean":
            normalized = raw.strip().casefold()
            if normalized in {"y", "yes", "true", "1"}:
                return True
            if normalized in {"n", "no", "false", "0"}:
                return False
            raise ValueError("enter yes/no or true/false")
        if field_type == "integer":
            integer_value = int(raw.strip())
            if "minimum" in schema and integer_value < schema["minimum"]:
                raise ValueError(f"minimum is {schema['minimum']}")
            if "maximum" in schema and integer_value > schema["maximum"]:
                raise ValueError(f"maximum is {schema['maximum']}")
            return integer_value
        if field_type == "number":
            number_value = float(raw.strip())
            if not math.isfinite(number_value):
                raise ValueError("number must be finite")
            if "minimum" in schema and number_value < schema["minimum"]:
                raise ValueError(f"minimum is {schema['minimum']}")
            if "maximum" in schema and number_value > schema["maximum"]:
                raise ValueError(f"maximum is {schema['maximum']}")
            return number_value
        if field_type == "array":
            values = [item.strip() for item in raw.split(",") if item.strip()]
            allowed = schema.get("items", {}).get("enum", [])
            if allowed and any(item not in allowed for item in values):
                raise ValueError("choose only the listed values")
            if "minItems" in schema and len(values) < schema["minItems"]:
                raise ValueError(f"select at least {schema['minItems']} value(s)")
            if "maxItems" in schema and len(values) > schema["maxItems"]:
                raise ValueError(f"select at most {schema['maxItems']} value(s)")
            return list(dict.fromkeys(values))
        if field_type != "string":
            raise ValueError("unsupported field type")
        text_value = raw.strip()
        allowed = schema.get("enum")
        if isinstance(allowed, list) and text_value not in allowed:
            raise ValueError("choose one of the listed values")
        if "minLength" in schema and len(text_value) < schema["minLength"]:
            raise ValueError(f"minimum length is {schema['minLength']}")
        if "maxLength" in schema and len(text_value) > schema["maxLength"]:
            raise ValueError(f"maximum length is {schema['maxLength']}")
        return text_value

    def _collect_mcp_form(
        self, schema: dict[str, Any], previous: dict[str, Any]
    ) -> dict[str, Any] | None:
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        output: dict[str, Any] = {}
        for name, field_schema in properties.items():
            if not isinstance(field_schema, dict):
                return None
            label = str(field_schema.get("title") or name)
            description = str(field_schema.get("description") or "")
            allowed = field_schema.get("enum")
            if allowed is None and field_schema.get("type") == "array":
                allowed = field_schema.get("items", {}).get("enum")
            while True:
                if description:
                    self.console.print(
                        terminal_safe_text(description),
                        style="dim",
                        markup=False,
                        highlight=False,
                    )
                if isinstance(allowed, list) and allowed:
                    options = ", ".join(terminal_safe_text(str(item)) for item in allowed)
                    self.console.print(
                        f"Options: {options}", markup=False, highlight=False
                    )
                default = previous.get(name, field_schema.get("default"))
                suffix = f" [{terminal_safe_text(str(default))}]" if default is not None else ""
                prompt = f"{terminal_safe_text(label, single_line=True)}{suffix}: "
                self.console.print(prompt, end="", markup=False, highlight=False)
                raw_line = self._input_stream.readline()
                if raw_line == "":
                    return None
                raw = raw_line.rstrip("\r\n")
                if not raw:
                    if default is not None:
                        output[name] = default
                        break
                    if name not in required:
                        break
                    self.console.print("A value is required.", style=self.theme.error)
                    continue
                try:
                    output[name] = self._parse_mcp_form_value(raw, field_schema)
                    break
                except (TypeError, ValueError) as exc:
                    self.console.print(
                        terminal_safe_text(f"Invalid value: {exc}"),
                        style=self.theme.error,
                        markup=False,
                        highlight=False,
                    )
        return output

    def request_mcp_elicitation(
        self, server: str, message: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Collect a reviewed non-sensitive MCP form response."""

        live = getattr(self, "_active_live", None)
        if live is not None:
            live.stop()
        try:
            safe_server = terminal_safe_text(server, single_line=True)
            self.console.print(
                Panel(
                    terminal_safe_text(message),
                    title=f"MCP form — {safe_server}",
                    border_style=self.theme.border_approval,
                )
            )
            previous: dict[str, Any] = {}
            while True:
                values = self._collect_mcp_form(schema, previous)
                if values is None:
                    return {"action": "cancel"}
                previous = values
                self.console.print("Review MCP form response:", style="bold")
                for key, value in values.items():
                    self.console.print(
                        f"  {terminal_safe_text(str(key), single_line=True)} = "
                        f"{terminal_safe_text(repr(value))}",
                        markup=False,
                        highlight=False,
                    )
                self.console.print(
                    "Submit [y], edit [e], decline [n], or cancel [c]? ",
                    end="",
                    markup=False,
                    highlight=False,
                )
                raw = self._input_stream.readline()
                if raw == "":
                    return {"action": "cancel"}
                action = raw.strip().casefold()
                if action in {"y", "yes"}:
                    self.transcript.append(
                        "approval",
                        f"Submitted MCP form for {safe_server} ({len(values)} field(s))",
                        title="MCP elicitation",
                        metadata={"server": safe_server, "action": "accept"},
                    )
                    return {"action": "accept", "content": values}
                if action in {"e", "edit"}:
                    continue
                if action in {"n", "no", "decline"}:
                    return {"action": "decline"}
                if action in {"c", "cancel", ""}:
                    return {"action": "cancel"}
                self.console.print("Choose y, e, n, or c.", style=self.theme.error)
        except (EOFError, KeyboardInterrupt, OSError, ValueError):
            return {"action": "cancel"}
        finally:
            if live is not None:
                live.start()

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
        self._assistant_prefix_pending = True
        if self._token_progress is not None:
            self._token_task = self._token_progress.add_task(
                "[dim]Tokens", total=100000, completed=0
            )
        else:
            self._token_task = None

        if self.screen_reader_mode or self._prompt_invalidator is not None:
            self._active_live = None
            self._last_refresh = 0.0
            self._refresh_live(force=True)
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

    @contextmanager
    def suspend_live_render(self):
        """Pause Rich Live while another interactive prompt owns the cursor."""

        live = self._active_live
        if live is not None:
            live.stop()
        try:
            yield
        finally:
            if live is not None and self._active_live is live:
                live.start(refresh=True)

    def _render_conversation(self, buffers: _LiveBuffers) -> Group:
        parts: list[Any] = [Text("ASH", style=self.theme.assistant_prefix)]
        tool_output = buffers.tool_output
        response = buffers.response
        if tool_output:
            parts.append(tool_output)
        if response:
            parts.append(Markdown(response, hyperlinks=False))
        if self.show_token_meter and self._token_task is not None:
            parts.append(
                Text(
                    self._render_token_meter(
                        self._current_tokens, self._maximum_tokens
                    ),
                    style="dim italic" if self.theme.name == "light" else "dim",
                )
            )
        return Group(*parts)

    def _render_thinking(self, buffers: _LiveBuffers) -> Group | None:
        thought = buffers.thought
        if len(thought) > LIVE_AUX_PREVIEW_CHARS:
            thought = thought[-LIVE_AUX_PREVIEW_CHARS:]
        if thought:
            rendered = Text("THINK  ", style="dim bold")
            rendered.append_text(thought)
            return Group(rendered)
        return None

    def _render_active_turn(self) -> Group:
        buffers = self._active_buffers_required()
        thinking = self._render_thinking(buffers)
        parts: list[Any] = []
        if thinking is not None:
            parts.append(thinking)
        if self._activity_status:
            parts.append(Text(self._activity_status, style="dim italic"))
        parts.append(self._render_conversation(buffers))
        return Group(*parts)

    def _active_buffers_required(self) -> _LiveBuffers:
        if not hasattr(self, "_active_buffers") or self._active_buffers is None:
            raise RuntimeError("begin_turn() must be called before streaming output")
        return self._active_buffers

    def _write_conversation(
        self,
        content: str | Text,
        *,
        style: str = "",
        end: str = "",
    ) -> None:
        renderable = Text(content, style=style) if isinstance(content, str) else content
        plain = renderable.plain + end

        if self._prompt_invalidator is not None and not self.screen_reader_mode:
            self._conversation_line_open = not plain.endswith("\n")
            return

        def write() -> None:
            self.console.print(
                renderable,
                end=end,
                soft_wrap=True,
                highlight=False,
            )

        self._terminal_writer(write)
        self._conversation_line_open = not plain.endswith("\n")

    def _seal_conversation(self) -> None:
        if self._conversation_line_open:
            self._write_conversation("\n")

    def print_token(self, text: str) -> None:
        if not text:
            return
        text = terminal_safe_text(text)
        buffers = self._active_buffers_required()
        buffers.response_chunks.append(text)
        if self._assistant_entry_id is None:
            self._assistant_entry_id = self.transcript.begin("assistant", title="ash")
        self.transcript.append_delta(self._assistant_entry_id, text)
        if self._prompt_invalidator is not None:
            self._assistant_prefix_pending = False
            self._conversation_needs_user_gap = True
        self._refresh_live()

    def print_thought(self, text: str) -> None:
        if not text:
            return
        text = terminal_safe_text(text)
        buffers = self._active_buffers_required()
        if self._prompt_invalidator is not None:
            self._reasoning_tail = (self._reasoning_tail + text)[
                -LIVE_AUX_PREVIEW_CHARS:
            ]
            self._invalidate_prompt_state()
        if self._reasoning_entry_id is None:
            self._reasoning_entry_id = self.transcript.begin(
                "reasoning", title="reasoning"
            )
        self.transcript.append_delta(self._reasoning_entry_id, text)
        buffers.thought.append(text, style="dim italic")
        if self.screen_reader_mode:
            self.console.print(
                f"Reasoning: {text}",
                markup=False,
                highlight=False,
            )
        if self._prompt_invalidator is None:
            self._refresh_live()

    def finalize_turn(self) -> None:
        """Flush any pending live rendering."""

        live = getattr(self, "_active_live", None)
        if live is not None:
            live.update(self._render_active_turn(), refresh=True)
        if self.screen_reader_mode and self._active_buffers is not None:
            response = self._active_buffers.response
            if response:
                self.console.print(Text("ASH", style=self.theme.assistant_prefix))
                self.console.print(
                    Markdown(ASSISTANT_MESSAGE_PREFIX + response, hyperlinks=False)
                )
        if self._reasoning_entry_id is not None:
            self.transcript.finalize(self._reasoning_entry_id)
        if self._assistant_entry_id is not None:
            self.transcript.finalize(self._assistant_entry_id)
        if self._prompt_invalidator is not None:
            self._seal_conversation()
        self._active_buffers = None
        self._active_live = None
        self._reasoning_entry_id = None
        self._assistant_entry_id = None
        if self._token_progress is not None:
            self._token_progress.stop()
        self._token_task = None
        if self._prompt_invalidator is not None:
            self._refresh_live(force=True)

    def commit_completed_turn(self) -> None:
        """Retained for callers; prompt-bound output is already in its transcript."""

        return

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

    def _refresh_live(self, *, force: bool = False) -> None:
        if self._prompt_invalidator is not None:
            now = time.monotonic()
            repaint = force or now - self._last_refresh >= 0.05
            if not repaint:
                return
            self._last_refresh = now
            self._prompt_revision += 1
            self._thinking_render_cache = None
            self._prompt_invalidator()
            return
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
        if event_type == "tool.output" and self._activity_turn_closed:
            return
        if event_type in _TURN_TERMINAL_EVENTS or event_type == "turn.started":
            self._finalize_pending_tool_output()
        previous_activity = self._current_activity_label()
        changed = self._apply_activity_event(payload)
        current_activity = self._current_activity_label()
        if current_activity != previous_activity:
            self._set_activity_status(
                f"{current_activity}…" if current_activity else ""
            )
        elif changed:
            self._invalidate_prompt_state()

        if event_type not in {
            "tool.started",
            "tool.output",
            "tool.completed",
            "tool.denied",
            "tool.error",
        }:
            return
        tool = terminal_safe_text(str(payload.get("tool", "unknown")), single_line=True)
        # Keep the provider/runtime call identity exact for internal lookup and
        # metadata.  Only the human-facing title/content is sanitized.
        call_id = str(payload.get("call_id", ""))
        if event_type == "tool.started":
            return
        if event_type == "tool.output":
            delta = terminal_safe_text(str(payload.get("delta", "")))
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
            if self._prompt_invalidator is not None:
                self._conversation_needs_user_gap = True
            elif self._active_buffers is not None:
                self._active_buffers.tool_output.append(delta, style=style)
                self._refresh_live()
            else:
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
            "TOOL  ", style="dim italic" if self.theme.name == "light" else "dim"
        )
        line.append(tool, style="bold")
        line.append(f" [{label}]", style=style)
        if self._prompt_invalidator is not None:
            self._conversation_needs_user_gap = True
        else:
            self.console.print(line)
        self._conversation_needs_user_gap = True

    def _finalize_pending_tool_output(self) -> None:
        for entry_id in self._tool_output_entries.values():
            try:
                self.transcript.finalize(entry_id)
            except KeyError:
                pass  # A session reset may have replaced the presentation history.
        self._tool_output_entries.clear()

    def _set_activity_status(self, text: str) -> None:
        """Update one ephemeral turn-status surface without polluting history."""

        text = terminal_safe_text(text, single_line=True)
        if text == self._activity_status:
            return
        self._activity_status = text
        if self.screen_reader_mode and text:
            self.console.print(
                f"Status: {text.removesuffix('…')}",
                markup=False,
                highlight=False,
            )
        if self._prompt_invalidator is not None:
            self._invalidate_prompt_state()
        elif self._active_buffers is not None:
            self._refresh_live()

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
        self.console.print(
            "Approve once [y], for this session [a], or deny [N]? ",
            end="",
            style=self.theme.approval_prompt,
        )
        try:
            answer = self._input_stream.readline().strip().lower()
        except (EOFError, KeyboardInterrupt, OSError, ValueError):
            return False
        if answer in {"a", "always", "session"}:
            self._session_approvals.add(tool_name)
            return True
        return answer in {"y", "yes"}

    def is_tool_approved_for_session(self, tool_name: str) -> bool:
        return tool_name in self._session_approvals

    def approve_tool_for_session(self, tool_name: str) -> None:
        self._session_approvals.add(tool_name)

    def clear_session_approvals(self) -> None:
        """Drop ephemeral tool approvals when the active chat changes."""

        self._session_approvals.clear()

    def show_tool_approval(
        self,
        tool_name: str,
        arguments: dict[str, Any] | dict[str, object],
        *,
        auto: bool,
        diff_mode: str = "unified",
    ) -> str:
        if diff_mode not in {"unified", "side-by-side"}:
            raise ValueError("diff_mode must be unified or side-by-side")
        return self._render_approval_notice(
            tool_name,
            dict(arguments),
            auto=auto,
            side_by_side=diff_mode == "side-by-side",
        )

    def _render_approval_notice(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        auto: bool,
        side_by_side: bool = False,
    ) -> str:
        body = Text()
        body.append("Tool: ", style="bold")
        display_tool_name = terminal_safe_text(tool_name, single_line=True)
        body.append(display_tool_name, style=self.theme.prompt)
        body.append("\nArgs:\n")
        display_arguments = redact_value(arguments)
        assert isinstance(display_arguments, dict)
        for key, value in display_arguments.items():
            body.append(
                f"  {terminal_safe_text(str(key), single_line=True)} = ",
                style="dim",
            )
            body.append(terminal_safe_text(repr(value)))
            body.append("\n")
        preview = self._edit_preview(
            tool_name,
            arguments,
            side_by_side=side_by_side,
        )
        if preview:
            preview = terminal_safe_text(preview)
            body.append(
                "\nDiff preview (side-by-side):\n" if side_by_side else "\nDiff preview:\n",
                style="bold",
            )
            _append_styled_diff(
                body,
                preview,
                theme_name=self.theme.name,
                side_by_side=side_by_side,
            )
        if auto:
            body.append("\n[auto-approved]", style=self.theme.success)
        self.transcript.append(
            "approval",
            body.plain,
            title=display_tool_name,
            metadata={"auto": auto},
        )
        if self.screen_reader_mode:
            self.console.print("Approval:", markup=False, highlight=False)
            self.console.print(body.plain, markup=False, highlight=False)
        elif self._prompt_invalidator is None:
            self.console.print(
                Panel(
                    body,
                    border_style=self.theme.border_approval,
                    title="approval",
                )
            )

        return body.plain

    def record_user_input(self, text: str) -> None:
        """Commit submitted user input to the interactive transcript."""

        safe = terminal_safe_text(text)
        self._clear_prompt_notice()
        self.transcript.append("user", safe, title="you")
        if self._prompt_invalidator is not None and not self.screen_reader_mode:
            self._conversation_line_open = False
            self._conversation_needs_user_gap = False
            if self._reasoning_tail:
                self._reasoning_tail = ""
            self._refresh_live(force=True)

    def load_session_transcript(self, session: Any | None) -> None:
        """Replace interactive history from a durable session snapshot."""

        self._activity_status = ""
        self._tool_output_entries.clear()
        self._clear_prompt_notice()
        self._activity_turn_closed = False
        self._model_activity = None
        self._active_tool_activity.clear()
        self._reasoning_tail = ""
        self._conversation_needs_user_gap = False
        self._invalidate_prompt_state()
        entries: list[TranscriptEntry] = []
        if session is None:
            self.transcript.replace(entries)
            return
        omitted_messages = int(getattr(session, "resident_message_offset", 0) or 0)
        if omitted_messages:
            entries.append(
                TranscriptEntry(
                    entry_id=str(uuid4()),
                    kind="status",
                    content=(
                        "Earlier session history is outside the resident snapshot: "
                        f"{omitted_messages} message(s)."
                    ),
                    title="history",
                )
            )
        for message in session.messages:
            content = terminal_safe_text(str(message.content))
            if message.role == "user":
                entries.append(
                    TranscriptEntry(str(uuid4()), "user", content, title="you")
                )
            elif message.role == "assistant" and content:
                entries.append(
                    TranscriptEntry(str(uuid4()), "assistant", content, title="ash")
                )
            elif message.role == "tool":
                bounded = content[:4000]
                if len(content) > len(bounded):
                    bounded += "\n[tool result truncated in transcript]"
                entries.append(
                    TranscriptEntry(
                        str(uuid4()),
                        "tool",
                        bounded,
                        title="tool result",
                        metadata=dict(message.metadata),
                    )
                )
        self.transcript.replace(entries)
        if self._prompt_invalidator is None or self.screen_reader_mode:
            if entries:
                self._render_linear_history()
        else:
            self._prompt_invalidator()

    def _render_linear_history(self) -> None:
        """Render the selected session as bounded line-oriented history."""

        entries = self.transcript.snapshot()
        visible = entries[-MAX_LINEAR_HISTORY_ENTRIES:]
        omitted = self.transcript.omitted_entries + len(entries) - len(visible)
        if omitted:
            self.console.print(Text(f"… {omitted} earlier entries omitted", style="dim"))
            self.console.print()
        previous_kind: str | None = None
        for entry in visible:
            if entry.kind == "user":
                if previous_kind in {"assistant", "tool"}:
                    self.console.print()
                self.console.print(
                    _user_message_text(
                        entry.content,
                        theme_name=self.theme.name,
                        console=self.console,
                        width=self.console.size.width,
                    )
                )
                self.console.print()
            elif entry.kind == "assistant":
                self.console.print(Text("ASH", style=self.theme.assistant_prefix))
                self.console.print(
                    Markdown(
                        ASSISTANT_MESSAGE_PREFIX + entry.content,
                        hyperlinks=False,
                    )
                )
            else:
                label = entry.title or entry.kind.upper()
                line = Text(f"{label.upper()}  ", style="dim bold")
                line.append(entry.content, style="dim")
                self.console.print(line)
            previous_kind = entry.kind

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
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        try:
            path = candidate.resolve()
        except OSError:
            return "[preview unavailable: path cannot be resolved]"
        try:
            relative = path.relative_to(self.workspace_root)
        except ValueError:
            return "[preview unavailable: path is outside workspace]"
        try:
            if candidate.is_symlink():
                return "[preview unavailable: existing file is not readable text]"
            before = _read_preview_file(candidate) if candidate.is_file() else ""
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
        rows: list[tuple[str, str, str]] = []
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            old_slice = old_lines[old_start:old_end]
            new_slice = new_lines[new_start:new_end]
            if tag == "equal":
                rows.extend(("equal", line, line) for line in old_slice)
            elif tag == "delete":
                rows.extend(("delete", line, "") for line in old_slice)
            elif tag == "insert":
                rows.extend(("insert", "", line) for line in new_slice)
            else:
                for index in range(max(len(old_slice), len(new_slice))):
                    rows.append(
                        (
                            "replace",
                            old_slice[index] if index < len(old_slice) else "",
                            new_slice[index] if index < len(new_slice) else "",
                        )
                    )
        output: list[str] = [
            "--- " + old_name.ljust(left_width)[:left_width],
            "+++ " + new_name.ljust(right_width)[:right_width],
        ]
        content_width = 36
        for row_tag, old_line, new_line in rows[:198]:
            left_marker = (
                "-" if row_tag in {"delete", "replace"} and old_line else " "
            )
            right_marker = (
                "+" if row_tag in {"insert", "replace"} and new_line else " "
            )
            output.append(
                left_marker
                + " "
                + old_line.ljust(content_width)[:content_width]
                + " | "
                + right_marker
                + " "
                + new_line[:content_width]
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
                except (EOFError, KeyboardInterrupt, OSError, ValueError):
                    answer = ""
                if answer in {"y", "yes"}:
                    return True
                if answer not in {"e", "edit"}:
                    return False
                try:
                    self._edit_plan(execution)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    self.console.print(
                        terminal_safe_text(f"Plan edit failed: {exc}"),
                        style=self.theme.error,
                        markup=False,
                        highlight=False,
                    )
                    return False
        finally:
            if live is not None:
                live.start()

    def show_plan_review(self, execution: Any) -> str:
        """Render a plan without reading input from the terminal."""

        return self._render_plan(execution)

    def edit_plan(self, execution: Any) -> None:
        """Open and validate a plan in the configured external editor."""

        self._edit_plan(execution)

    def _render_plan(self, execution: Any) -> str:
        body = Text()
        body.append("Goal: ", style="bold")
        body.append(terminal_safe_text(execution.contract.goal))
        body.append("\n\n")
        body.append("Definition of Done:\n", style="bold")
        for item in execution.contract.definition_of_done:
            body.append(f"  - {terminal_safe_text(item)}\n")
        if not execution.contract.definition_of_done:
            body.append("  (none)\n")
        body.append("\nFiles in Scope:\n", style="bold")
        for path in execution.contract.files_in_scope:
            body.append(f"  - {terminal_safe_text(path)}\n")
        if not execution.contract.files_in_scope:
            body.append("  - (none)\n")
        body.append("\nChecklist:\n", style="bold")
        if execution.items:
            for item in execution.items:
                if self.screen_reader_mode:
                    mark = "[x]" if item.status.value in {"done", "skipped"} else "[ ]"
                else:
                    mark = "☑" if item.status.value in {"done", "skipped"} else "☐"
                body.append(
                    f"  {mark} [{terminal_safe_text(item.section)}] "
                    f"{terminal_safe_text(item.description)}\n"
                )
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
        if self.screen_reader_mode:
            self.console.print(
                f"Sprint {execution.contract.contract_id[:8]}:",
                markup=False,
                highlight=False,
            )
            self.console.print(body.plain, markup=False, highlight=False)
        elif self._prompt_invalidator is None:
            self.console.print(
                Panel(
                    body,
                    border_style=self.theme.border_primary,
                    title=f"sprint {execution.contract.contract_id[:8]}",
                )
            )
        elif self._prompt_invalidator is not None:
            self._prompt_invalidator()
        return body.plain

    def write_status(self, text: str, *, error: bool = False) -> None:
        text = terminal_safe_text(text)
        if error:
            self.transcript.append("error", text, title="error")
        else:
            self.transcript.append("status", text, title="status")
        if self._prompt_invalidator is not None and not self.screen_reader_mode:
            self._refresh_live(force=True)
            return
        self.console.print(
            text,
            style=self.theme.error if error else None,
            markup=False,
            highlight=False,
        )

    def _edit_plan(self, execution: Any) -> None:
        from ash.core.planner import apply_sprint_markdown_edit, render_sprint_markdown

        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if not editor:
            raise ValueError("Set VISUAL or EDITOR to edit sprint plans")
        workspace = self.workspace_root or Path.cwd().resolve()
        command, editor_environment = _editor_launch(
            editor,
            workspace=workspace,
        )
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
            result = subprocess.run(
                [*command, str(file_path)],
                check=False,
                env=editor_environment,
            )
            if result.returncode != 0:
                raise subprocess.SubprocessError(
                    f"editor exited with status {result.returncode}"
                )
            edited_plan = _read_preview_file(file_path)
            if edited_plan is None:
                raise ValueError("edited plan exceeds 1 MB")
            apply_sprint_markdown_edit(
                execution,
                edited_plan,
            )
        finally:
            if file_path is not None:
                file_path.unlink(missing_ok=True)
