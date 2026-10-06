"""Responsive prompt-toolkit viewport for Ash's interactive transcript."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from io import StringIO
import os
from pathlib import Path
import re
from typing import Any
import webbrowser

from prompt_toolkit.application import Application, get_app_or_none
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import (
    ANSI,
    AnyFormattedText,
    FormattedText,
    to_formatted_text,
)
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.history import History
from prompt_toolkit.input.base import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    BufferControl,
    Float,
    FloatContainer,
    FormattedTextControl,
    HSplit,
    Layout,
    ConditionalContainer,
    Window,
)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output.base import Output
from rich.cells import cell_len, set_cell_size
from rich.console import Console
from rich.markdown import Markdown

from ash.ui.clipboard import (
    ClipboardUnavailable,
    copy_to_clipboard,
    read_from_clipboard,
)
from ash.ui.history import PrivateFileHistory
from ash.ui.history import validate_history_path as _validate_history_path
from ash.ui.input_signals import PromptInterrupted
from ash.ui.safe_text import terminal_safe_text
from ash.ui.transcript import Transcript, TranscriptEntry, TranscriptEvent
from ash.ui.theme import Theme, get_theme, prompt_style, viewport_styles


validate_history_path = _validate_history_path

_ENTRY_STYLE = {
    "user": ("class:user-prefix", "YOU"),
    "assistant": ("class:assistant-prefix", "ASH"),
    "reasoning": ("class:reasoning-prefix", "THINK"),
    "tool": ("class:tool-prefix", "TOOL"),
    "approval": ("class:approval-prefix", "APPROVAL"),
    "status": ("class:status-prefix", "STATUS"),
    "error": ("class:error-prefix", "ERROR"),
}


_DEFAULT_ENTRY_TITLES = {
    "user": {"", "user", "you"},
    "assistant": {"", "assistant", "ash"},
    "reasoning": {"", "reasoning", "think"},
    "tool": {"", "tool"},
    "approval": {"", "approval"},
    "status": {"", "status"},
    "error": {"", "error"},
}


@dataclass(frozen=True)
class ViewportChoice:
    value: str
    label: str
    description: str = ""


_URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]{}]+")


def _is_remote_session() -> bool:
    return any(
        os.environ.get(name)
        for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
    )


def _url_at_point(text: str, point: Point) -> str | None:
    lines = text.split("\n")
    if point.y < 0 or point.y >= len(lines):
        return None
    line = lines[point.y]
    for match in _URL_PATTERN.finditer(line):
        if match.start() <= point.x < match.end():
            return match.group(0).rstrip(".,;:!?")
    return None


def _open_external_url(url: str) -> bool:
    if _is_remote_session():
        return False
    try:
        return bool(webbrowser.open(url))
    except (OSError, webbrowser.Error):
        return False


class _ComposerBufferControl(BufferControl):
    """Buffer control with Ash-owned copy/paste mouse behavior."""

    def __init__(
        self,
        *,
        buffer: Buffer,
        on_selection_release: Callable[[], None],
        on_context_click: Callable[[MouseButton], None],
    ) -> None:
        super().__init__(buffer=buffer)
        self._on_selection_release = on_selection_release
        self._on_context_click = on_context_click

    def mouse_handler(self, mouse_event: MouseEvent) -> object:
        if (
            mouse_event.event_type == MouseEventType.MOUSE_UP
            and mouse_event.button in {MouseButton.RIGHT, MouseButton.MIDDLE}
        ):
            self._on_context_click(mouse_event.button)
            return None
        result = super().mouse_handler(mouse_event)
        if (
            mouse_event.event_type == MouseEventType.MOUSE_UP
            and mouse_event.button == MouseButton.LEFT
            and self.buffer.selection_state is not None
        ):
            self._on_selection_release()
        return result


def _ordered_points(first: Point, second: Point) -> tuple[Point, Point]:
    if (first.y, first.x) <= (second.y, second.x):
        return first, second
    return second, first


def _selection_text(text: str, first: Point, second: Point) -> str:
    start, end = _ordered_points(first, second)
    if start == end:
        return ""
    lines = text.split("\n")
    if not lines:
        return ""
    start_y = min(max(start.y, 0), len(lines) - 1)
    end_y = min(max(end.y, 0), len(lines) - 1)
    start_x = min(max(start.x, 0), len(lines[start_y]))
    end_x = min(max(end.x, 0), len(lines[end_y]))
    if start_y == end_y:
        return lines[start_y][start_x:end_x]
    parts = [lines[start_y][start_x:].rstrip()]
    parts.extend(line.rstrip() for line in lines[start_y + 1 : end_y])
    parts.append(lines[end_y][:end_x].rstrip())
    return "\n".join(parts)


def _selection_style_fragments(
    rendered: AnyFormattedText,
    first: Point | None,
    second: Point | None,
) -> FormattedText:
    fragments = list(to_formatted_text(rendered))
    if first is None or second is None or first == second:
        return FormattedText(fragments)
    start, end = _ordered_points(first, second)
    output: list[tuple[str, str] | tuple[str, str, Any]] = []
    row = 0
    column = 0

    def append_piece(item: tuple[Any, ...], value: str, *, selected: bool) -> None:
        if not value:
            return
        style = str(item[0])
        if selected:
            style = f"{style} class:selection".strip()
        if len(item) >= 3:
            output.append((style, value, item[2]))
        else:
            output.append((style, value))

    for item in fragments:
        text = item[1]
        pieces = text.split("\n")
        for index, piece in enumerate(pieces):
            piece_start = column
            piece_end = column + len(piece)
            selected_start = piece_end
            selected_end = piece_end
            if start.y <= row <= end.y:
                selected_start = start.x if row == start.y else 0
                selected_end = end.x if row == end.y else piece_end
                selected_start = min(max(selected_start, piece_start), piece_end)
                selected_end = min(max(selected_end, piece_start), piece_end)
            local_start = selected_start - piece_start
            local_end = selected_end - piece_start
            append_piece(item, piece[:local_start], selected=False)
            append_piece(item, piece[local_start:local_end], selected=True)
            append_piece(item, piece[local_end:], selected=False)
            column = piece_end
            if index < len(pieces) - 1:
                append_piece(item, "\n", selected=False)
                row += 1
                column = 0
    return FormattedText(output)


def _entry_heading(entry: TranscriptEntry) -> tuple[str, str]:
    style, label = _ENTRY_STYLE[entry.kind]
    title = terminal_safe_text(entry.title, single_line=True)
    if title.casefold() not in _DEFAULT_ENTRY_TITLES[entry.kind]:
        return style, f"{label}  ·  {title}"
    return style, label


def _entry_body(entry: TranscriptEntry) -> str:
    safe = terminal_safe_text(entry.content) or " "
    if entry.kind == "tool" and entry.title and "\n" not in safe:
        title = terminal_safe_text(entry.title, single_line=True)
        prefix = f"{title} ["
        if safe.startswith(prefix) and safe.endswith("]"):
            safe = safe[len(prefix) : -1] or "completed"
    return "  " + safe.replace("\n", "\n  ")


def _entry_body_fragments(entry: TranscriptEntry) -> list[tuple[str, str]]:
    safe = terminal_safe_text(entry.content) or " "
    base_style = "class:reasoning" if entry.kind == "reasoning" else ""
    if entry.kind != "approval" or "Diff preview" not in safe:
        return [(base_style, _entry_body(entry))]

    fragments: list[tuple[str, str]] = []
    in_diff = False
    side_by_side = False
    for index, raw_line in enumerate(safe.splitlines() or [""]):
        if index:
            fragments.append(("", "\n"))
        style = base_style
        stripped = raw_line.strip()
        if stripped.startswith("Diff preview"):
            in_diff = True
            side_by_side = stripped.startswith("Diff preview (side-by-side)")
        elif in_diff:
            if raw_line.startswith(("+++", "---", "@@")):
                style = "class:diff-hunk"
            elif side_by_side and " | " in raw_line:
                left, right = raw_line.split(" | ", 1)
                left_style = (
                    "class:diff-removed"
                    if left.startswith("- ")
                    else "class:diff-context"
                )
                right_style = (
                    "class:diff-added"
                    if right.startswith("+ ")
                    else "class:diff-context"
                )
                fragments.append((left_style, "  " + left))
                fragments.append(("class:diff-context", " | "))
                fragments.append((right_style, right))
                continue
            elif raw_line.startswith("+"):
                style = "class:diff-added"
            elif raw_line.startswith("-"):
                style = "class:diff-removed"
            elif stripped:
                style = "class:diff-context"
        fragments.append((style, "  " + raw_line))
    return fragments


def _fit_segments(value: str, width: int) -> str:
    if width <= 0 or not value:
        return ""
    if cell_len(value) <= width:
        return value
    parts = [part.strip() for part in value.split("  ·  ") if part.strip()]
    kept: list[str] = []
    for part in parts:
        candidate = "  ·  ".join((*kept, part))
        if cell_len(candidate) > width:
            break
        kept.append(part)
    if kept:
        return "  ·  ".join(kept)
    if width == 1:
        return "…"
    return set_cell_size(value, width - 1).rstrip() + "…"


def format_transcript(entries: tuple[TranscriptEntry, ...]) -> AnyFormattedText:
    """Render semantic entries without baking in terminal dimensions."""

    fragments: list[tuple[str, str]] = []
    for index, entry in enumerate(entries):
        if index:
            fragments.append(("", "\n\n"))
        style, heading = _entry_heading(entry)
        fragments.append((style, heading))
        fragments.append(("", "\n"))
        fragments.extend(_entry_body_fragments(entry))
        if not entry.finalized and not (
            entry.kind == "status" and entry.title == "working"
        ):
            fragments.append(("class:streaming", "  …"))
    return FormattedText(fragments)


class RichTranscriptFormatter:
    """Render assistant Markdown once per content/width combination."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, int], AnyFormattedText] = {}

    def format(
        self,
        entries: tuple[TranscriptEntry, ...],
        *,
        width: int,
    ) -> AnyFormattedText:
        fragments: list[tuple[str, str] | tuple[str, str, Any]] = []
        live_keys: set[tuple[str, str, int]] = set()
        for index, entry in enumerate(entries):
            if index:
                fragments.append(("", "\n\n"))
            style, heading = _entry_heading(entry)
            fragments.append((style, heading))
            fragments.append(("", "\n"))
            if entry.kind == "assistant" and entry.content and entry.finalized:
                safe_content = terminal_safe_text(entry.content)
                key = (entry.entry_id, safe_content, width)
                live_keys.add(key)
                rendered = self._cache.get(key)
                if rendered is None:
                    rendered = self._render_markdown(safe_content, width=width)
                    self._cache[key] = rendered
                fragments.extend(to_formatted_text(rendered))
            else:
                fragments.extend(_entry_body_fragments(entry))
            if not entry.finalized and not (
                entry.kind == "status" and entry.title == "working"
            ):
                fragments.append(("class:streaming", "  …"))
        if len(self._cache) > max(32, len(live_keys) * 4):
            self._cache = {
                key: value for key, value in self._cache.items() if key in live_keys
            }
        return FormattedText(fragments)

    @staticmethod
    def _render_markdown(content: str, *, width: int) -> AnyFormattedText:
        stream = StringIO()
        console = Console(
            file=stream,
            force_terminal=True,
            color_system="truecolor",
            width=max(20, width - 2),
            soft_wrap=False,
        )
        console.print(Markdown(content, hyperlinks=False))
        return ANSI(stream.getvalue().rstrip("\n"))


class TranscriptViewport:
    """One full-screen transcript/composer application reusable across reads."""

    def __init__(
        self,
        transcript: Transcript,
        *,
        history_path: Path,
        history: History | None = None,
        completer: Any = None,
        status_provider: Callable[[], str] | None = None,
        header_provider: Callable[[], str] | None = None,
        input_mode: str = "emacs",
        keybindings: dict[str, list[str]] | None = None,
        theme: str = "dark",
        no_color: bool = False,
        workspace_root: Path | None = None,
        open_url: Callable[[str], bool] | None = None,
        input: Input | None = None,
        output: Output | None = None,
    ) -> None:
        if input_mode not in {"emacs", "vi"}:
            raise ValueError("input_mode must be emacs or vi")
        self.transcript = transcript
        self.status_provider = status_provider or (lambda: "")
        self.header_provider = header_provider or (lambda: "")
        self._prompt = "> "
        self._running = False
        self._follow_tail = True
        self._manual_cursor_line = 0
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self._open_url = open_url or _open_external_url
        self._selection_anchor: Point | None = None
        self._selection_focus: Point | None = None
        self._choice_mode = False
        self._choice_title = ""
        self._choice_options: tuple[ViewportChoice, ...] = ()
        self._choice_selected = 0
        self._formatter = RichTranscriptFormatter()
        self._last_transcript_text: AnyFormattedText | None = None
        self._configured_keybindings = keybindings or {
            "newline": ["escape enter", "c-j"],
            "open_editor": ["c-x c-e"],
        }
        selected_theme: Theme = get_theme(theme)

        self.input_buffer = Buffer(
            history=history or PrivateFileHistory(history_path),
            auto_suggest=AutoSuggestFromHistory(),
            completer=completer,
            complete_while_typing=True,
            multiline=True,
        )
        self.transcript_control = FormattedTextControl(
            self._transcript_text,
            get_cursor_position=self._transcript_cursor_position,
            show_cursor=False,
        )
        self.transcript_window = Window(
            self.transcript_control,
            wrap_lines=True,
            always_hide_cursor=True,
        )
        self.header_control = FormattedTextControl(self._header_text)
        self.prompt_control = FormattedTextControl(self._composer_label)
        self.composer_control = _ComposerBufferControl(
            buffer=self.input_buffer,
            on_selection_release=self._copy_composer_selection,
            on_context_click=self._handle_composer_context_click,
        )
        composer = HSplit(
            [
                Window(self.prompt_control, height=1, dont_extend_height=True),
                Window(
                    self.composer_control,
                    height=Dimension(min=1, max=8),
                    wrap_lines=True,
                ),
            ],
            style="class:composer",
        )
        self._choice_control = FormattedTextControl(self._choice_text)
        self._choice_detail_control = FormattedTextControl(self._choice_detail_text)
        choice_panel = HSplit(
            [
                Window(
                    FormattedTextControl(self._choice_heading),
                    height=1,
                    style="class:composer",
                ),
                Window(
                    self._choice_control,
                    height=Dimension(min=1, max=6),
                    dont_extend_height=True,
                    style="class:composer",
                ),
                Window(
                    self._choice_detail_control,
                    height=2,
                    wrap_lines=True,
                    style="class:composer",
                ),
            ]
        )
        choice_active = Condition(lambda: self._choice_mode)
        body = HSplit(
            [
                Window(
                    self.header_control,
                    height=1,
                    style="class:header",
                ),
                self.transcript_window,
                Window(height=1, char="─", style="class:separator"),
                ConditionalContainer(composer, filter=~choice_active),
                ConditionalContainer(choice_panel, filter=choice_active),
                Window(
                    FormattedTextControl(self._status_text),
                    height=1,
                    style="class:status",
                ),
            ]
        )
        root = FloatContainer(
            content=body,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=8, scroll_offset=1),
                )
            ],
        )
        self.application: Application[str] = Application(
            layout=Layout(root, focused_element=self.input_buffer),
            key_bindings=self._key_bindings(),
            full_screen=True,
            erase_when_done=False,
            editing_mode=EditingMode.VI if input_mode == "vi" else EditingMode.EMACS,
            style=prompt_style(viewport_styles(selected_theme), no_color=no_color),
            mouse_support=True,
            input=input,
            output=output,
            min_redraw_interval=0.03,
            terminal_size_polling_interval=0.25,
        )
        self._unsubscribe = transcript.subscribe(self._on_transcript_event)

    async def read(self, prompt: str = "> ") -> str:
        if self._running:
            raise RuntimeError("transcript viewport already owns terminal input")
        self._running = True
        self._choice_mode = False
        self._prompt = prompt
        self._follow_tail = True
        self._manual_cursor_line = 0
        self._clear_transcript_selection()
        self.transcript_window.vertical_scroll = 0
        self.transcript_window.vertical_scroll_2 = 0
        self.input_buffer.set_document(Document("", 0), bypass_readonly=True)
        try:
            return await self.application.run_async()
        finally:
            self._running = False

    async def choose(
        self,
        title: str,
        options: tuple[ViewportChoice, ...],
        *,
        default_value: str | None = None,
    ) -> str | None:
        if self._running:
            raise RuntimeError("transcript viewport already owns terminal input")
        if not options:
            return None
        selected = 0
        if default_value is not None:
            try:
                selected = next(
                    index
                    for index, option in enumerate(options)
                    if option.value == default_value
                )
            except StopIteration as exc:
                raise ValueError("default choice is not present in options") from exc
        self._running = True
        self._choice_mode = True
        self._choice_title = terminal_safe_text(title, single_line=True)
        self._choice_options = options
        self._choice_selected = selected
        self._follow_tail = True
        self._manual_cursor_line = 0
        self._clear_transcript_selection()
        try:
            return await self.application.run_async()
        finally:
            self._choice_mode = False
            self._choice_options = ()
            self._choice_selected = 0
            self._running = False

    def close(self) -> None:
        self._unsubscribe()

    def _transcript_text(self) -> AnyFormattedText:
        app = get_app_or_none()
        width = app.output.get_size().columns if app is self.application else 80
        entries = self.transcript.snapshot()
        if not entries:
            rendered: AnyFormattedText = FormattedText(
                [
                    ("class:empty-title", "\n  Ready"),
                    (
                        "class:muted",
                        "\n  Ask about this workspace, attach @files, or type /help.",
                    ),
                ]
            )
        else:
            rendered = self._formatter.format(entries, width=width)
        rendered = _selection_style_fragments(
            rendered,
            self._selection_anchor,
            self._selection_focus,
        )
        rendered = FormattedText(
            [
                (fragment[0], fragment[1], self._handle_transcript_mouse)
                for fragment in to_formatted_text(rendered)
            ]
        )
        self._last_transcript_text = rendered
        return rendered

    def _transcript_cursor_position(self) -> Point | None:
        rendered = self._last_transcript_text or self._transcript_text()
        text = fragment_list_to_text(to_formatted_text(rendered))
        lines = text.split("\n") or [""]
        if self._follow_tail:
            return Point(x=len(lines[-1]), y=len(lines) - 1)
        line = min(max(0, self._manual_cursor_line), len(lines) - 1)
        return Point(x=0, y=line)

    def _header_text(self) -> AnyFormattedText:
        identity = terminal_safe_text(self.header_provider(), single_line=True)
        app = get_app_or_none()
        width = app.output.get_size().columns if app is self.application else 80
        identity = _fit_segments(identity, max(0, width - len(" ASH   ") - 1))
        fragments: list[tuple[str, str]] = [
            ("class:header-brand", " ASH "),
        ]
        if identity:
            fragments.extend(
                [
                    ("class:header-meta", "  "),
                    ("class:header", identity),
                    ("class:header", " "),
                ]
            )
        return FormattedText(fragments)

    def _composer_label(self) -> AnyFormattedText:
        raw = terminal_safe_text(self._prompt, single_line=True).strip()
        if raw in {"", ">"}:
            label = "ASK ASH"
        elif raw.casefold() == "steer>":
            label = "STEER"
        else:
            label = raw.rstrip(" >:")
        return FormattedText([("class:composer-label", f" {label} ")])

    def _status_text(self) -> AnyFormattedText:
        value = terminal_safe_text(self.status_provider(), single_line=True)
        app = get_app_or_none()
        width = app.output.get_size().columns if app is self.application else 80
        fitted = _fit_segments(value, max(0, width - 2))
        return FormattedText([("", f" {fitted} ")])

    def _choice_heading(self) -> AnyFormattedText:
        return FormattedText(
            [
                ("class:approval-prefix", " APPROVAL "),
                ("", "  "),
                ("class:muted", self._choice_title),
            ]
        )

    def _choice_text(self) -> AnyFormattedText:
        fragments: list[tuple[str, str] | tuple[str, str, Any]] = []
        for index, option in enumerate(self._choice_options):
            selected = index == self._choice_selected
            style = "class:selected" if selected else ""
            marker = "> " if selected else "  "
            mouse_handler = partial(self._handle_choice_mouse, index)
            fragments.append((style, marker, mouse_handler))
            fragments.append(
                (f"{style} class:option".strip(), option.label, mouse_handler)
            )
            fragments.append((style, "\n"))
        return FormattedText(fragments)

    def _choice_detail_text(self) -> AnyFormattedText:
        if not self._choice_options:
            return FormattedText([("class:muted", "")])
        option = self._choice_options[self._choice_selected]
        return FormattedText(
            [
                ("class:meta", " " + terminal_safe_text(option.description)),
                ("class:muted", "\n ↑/↓ choose  Enter select  Esc deny"),
            ]
        )

    def _on_transcript_event(self, event: TranscriptEvent) -> None:
        del event
        self._last_transcript_text = None
        app = get_app_or_none()
        if app is self.application:
            app.invalidate()

    def _page_height(self) -> int:
        info = self.transcript_window.render_info
        return max(1, info.window_height - 1) if info is not None else 10

    def _scroll_transcript(self, offset: int) -> None:
        info = self.transcript_window.render_info
        current = (
            info.vertical_scroll
            if info is not None
            else self.transcript_window.vertical_scroll
        )
        self._follow_tail = False
        target = max(0, current + offset)
        self._manual_cursor_line = target
        self.transcript_window.vertical_scroll = target
        self.transcript_window.vertical_scroll_2 = 0
        self.application.invalidate()

    def _handle_transcript_mouse(self, event: MouseEvent) -> object:
        if event.event_type == MouseEventType.SCROLL_UP:
            self._scroll_transcript(-3)
            return None
        if event.event_type == MouseEventType.SCROLL_DOWN:
            self._scroll_transcript(3)
            return None
        if event.button == MouseButton.LEFT:
            if event.event_type == MouseEventType.MOUSE_DOWN:
                self._selection_anchor = event.position
                self._selection_focus = event.position
                self.application.invalidate()
                return None
            if (
                event.event_type == MouseEventType.MOUSE_MOVE
                and self._selection_anchor is not None
            ):
                self._selection_focus = event.position
                self.application.invalidate()
                return None
            if (
                event.event_type == MouseEventType.MOUSE_UP
                and self._selection_anchor is not None
            ):
                self._selection_focus = event.position
                selected = self._selected_transcript_text()
                if selected:
                    self._copy_text(selected)
                else:
                    url = _url_at_point(
                        self._plain_transcript_text(),
                        event.position,
                    )
                    self._clear_transcript_selection()
                    if url is not None:
                        self._open_url(url)
                self.application.invalidate()
                return None
        if (
            event.button in {MouseButton.RIGHT, MouseButton.MIDDLE}
            and event.event_type == MouseEventType.MOUSE_UP
        ):
            if event.button == MouseButton.MIDDLE:
                if not self._choice_mode:
                    self._paste_into_composer(selection="primary")
            else:
                selected = self._selected_transcript_text()
                if selected:
                    self._copy_text(selected)
                elif not self._choice_mode:
                    self._paste_into_composer(selection="clipboard")
            self.application.invalidate()
            return None
        return NotImplemented

    def _plain_transcript_text(self) -> str:
        rendered = self._last_transcript_text or self._transcript_text()
        return fragment_list_to_text(to_formatted_text(rendered))

    def _selected_transcript_text(self) -> str:
        if self._selection_anchor is None or self._selection_focus is None:
            return ""
        return _selection_text(
            self._plain_transcript_text(),
            self._selection_anchor,
            self._selection_focus,
        )

    def _clear_transcript_selection(self) -> None:
        self._selection_anchor = None
        self._selection_focus = None

    def _copy_text(self, text: str) -> bool:
        if not text:
            return False
        try:
            copy_to_clipboard(text, workspace_root=self.workspace_root)
        except (ClipboardUnavailable, OSError, ValueError):
            return False
        return True

    def _paste_into_composer(self, *, selection: str = "clipboard") -> bool:
        if _is_remote_session():
            return False
        try:
            text = read_from_clipboard(
                workspace_root=self.workspace_root,
                selection=selection,
            )
        except (ClipboardUnavailable, OSError, ValueError):
            return False
        if not text:
            return False
        self.application.layout.focus(self.input_buffer)
        self.input_buffer.insert_text(text, fire_event=self._running)
        return True

    def _copy_composer_selection(self) -> None:
        if self.input_buffer.selection_state is None:
            return
        start, end = self.input_buffer.document.selection_range()
        self._copy_text(self.input_buffer.text[start:end])

    def _handle_composer_context_click(self, button: MouseButton) -> None:
        if button == MouseButton.RIGHT and self.input_buffer.selection_state is not None:
            start, end = self.input_buffer.document.selection_range()
            selected = self.input_buffer.text[start:end]
            if selected:
                self._copy_text(selected)
                return
        self._paste_into_composer(
            selection="primary" if button == MouseButton.MIDDLE else "clipboard"
        )

    def _handle_choice_mouse(self, index: int, event: MouseEvent) -> object:
        if (
            not self._choice_mode
            or index < 0
            or index >= len(self._choice_options)
            or event.button != MouseButton.LEFT
        ):
            return NotImplemented
        if event.event_type == MouseEventType.MOUSE_DOWN:
            self._choice_selected = index
            self.application.invalidate()
            return None
        if event.event_type == MouseEventType.MOUSE_UP:
            self._choice_selected = index
            self.application.exit(result=self._choice_options[index].value)
            return None
        return NotImplemented

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("enter")
        def submit(event) -> None:
            if self._choice_mode:
                event.app.exit(result=self._choice_options[self._choice_selected].value)
                return
            state = self.input_buffer.complete_state
            if (
                self.input_buffer.text.strip() == "/"
                and (state is None or state.complete_index is None)
            ):
                event.app.exit(result="/")
                return
            if state is not None and state.completions:
                completion = state.current_completion or state.completions[0]
                if self.input_buffer.text.strip() != completion.text:
                    self.input_buffer.apply_completion(completion)
                    return
            event.app.exit(result=self.input_buffer.text)

        @bindings.add("tab")
        def complete(event) -> None:
            state = self.input_buffer.complete_state
            if state is not None and state.completions:
                completion = state.current_completion or state.completions[0]
                self.input_buffer.apply_completion(completion)
                return
            self.input_buffer.start_completion(select_first=True)

        def newline(event) -> None:
            event.current_buffer.insert_text("\n")

        def open_editor(event) -> None:
            event.current_buffer.open_in_editor()

        handlers = {
            "newline": newline,
            "open_editor": open_editor,
        }
        for action, sequences in self._configured_keybindings.items():
            handler = handlers[action]
            for sequence in sequences:
                bindings.add(*sequence.split())(handler)

        @bindings.add("c-c")
        def interrupt(event) -> None:
            event.app.exit(exception=PromptInterrupted())

        @bindings.add("c-d")
        def eof(event) -> None:
            if not self.input_buffer.text:
                event.app.exit(exception=EOFError())
            else:
                event.current_buffer.delete()

        choice_active = Condition(lambda: self._choice_mode)

        @bindings.add("up", filter=choice_active, eager=True)
        def choice_up(event) -> None:
            del event
            if self._choice_options:
                self._choice_selected = (
                    self._choice_selected - 1
                ) % len(self._choice_options)
                self.application.invalidate()

        @bindings.add("down", filter=choice_active, eager=True)
        def choice_down(event) -> None:
            del event
            if self._choice_options:
                self._choice_selected = (
                    self._choice_selected + 1
                ) % len(self._choice_options)
                self.application.invalidate()

        @bindings.add("escape", filter=choice_active, eager=True)
        def choice_deny(event) -> None:
            event.app.exit(result=None)

        @bindings.add("pageup")
        def page_up(event) -> None:
            del event
            self._scroll_transcript(-self._page_height())

        @bindings.add("pagedown")
        def page_down(event) -> None:
            del event
            self._scroll_transcript(self._page_height())

        @bindings.add("end")
        def follow_tail(event) -> None:
            self._follow_tail = True
            event.app.invalidate()

        return bindings
