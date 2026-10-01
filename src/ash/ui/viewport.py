"""Responsive prompt-toolkit viewport for Ash's interactive transcript."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from io import StringIO
from typing import Any

from prompt_toolkit.application import Application, get_app_or_none
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.formatted_text import (
    ANSI,
    AnyFormattedText,
    FormattedText,
    to_formatted_text,
)
from prompt_toolkit.history import History
from prompt_toolkit.input.base import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    BufferControl,
    FormattedTextControl,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.output.base import Output
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown

from ash.ui.history import PrivateFileHistory
from ash.ui.history import validate_history_path as _validate_history_path
from ash.ui.safe_text import terminal_safe_text
from ash.ui.transcript import Transcript, TranscriptEntry, TranscriptEvent
from ash.ui.theme import Theme, get_theme, viewport_styles


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


def _fit_segments(value: str, width: int) -> str:
    if width <= 0 or not value:
        return ""
    if len(value) <= width:
        return value
    parts = [part.strip() for part in value.split("  ·  ") if part.strip()]
    kept: list[str] = []
    for part in parts:
        candidate = "  ·  ".join((*kept, part))
        if len(candidate) > width:
            break
        kept.append(part)
    if kept:
        return "  ·  ".join(kept)
    if width == 1:
        return "…"
    return value[: width - 1].rstrip() + "…"


def format_transcript(entries: tuple[TranscriptEntry, ...]) -> AnyFormattedText:
    """Render semantic entries without baking in terminal dimensions."""

    fragments: list[tuple[str, str]] = []
    for index, entry in enumerate(entries):
        if index:
            fragments.append(("", "\n\n"))
        style, heading = _entry_heading(entry)
        fragments.append((style, heading))
        fragments.append(("", "\n"))
        body_style = "class:reasoning" if entry.kind == "reasoning" else ""
        fragments.append((body_style, _entry_body(entry)))
        if not entry.finalized:
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
            if entry.kind == "assistant" and entry.content:
                safe_content = terminal_safe_text(entry.content)
                key = (entry.entry_id, safe_content, width)
                live_keys.add(key)
                rendered = self._cache.get(key)
                if rendered is None:
                    rendered = self._render_markdown(safe_content, width=width)
                    self._cache[key] = rendered
                fragments.extend(to_formatted_text(rendered))
            else:
                body_style = "class:reasoning" if entry.kind == "reasoning" else ""
                fragments.append((body_style, _entry_body(entry)))
            if not entry.finalized:
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
        self._vertical_scroll = 10**9
        self._formatter = RichTranscriptFormatter()
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
        self.transcript_control = FormattedTextControl(self._transcript_text)
        self.transcript_window = Window(
            self.transcript_control,
            wrap_lines=True,
            always_hide_cursor=True,
            get_vertical_scroll=lambda _: self._vertical_scroll,
        )
        self.header_control = FormattedTextControl(self._header_text)
        self.prompt_control = FormattedTextControl(self._composer_label)
        composer = HSplit(
            [
                Window(self.prompt_control, height=1, dont_extend_height=True),
                Window(
                    BufferControl(buffer=self.input_buffer),
                    height=Dimension(min=1, max=8),
                    wrap_lines=True,
                ),
            ],
            style="class:composer",
        )
        root = HSplit(
            [
                Window(
                    self.header_control,
                    height=1,
                    style="class:header",
                ),
                self.transcript_window,
                Window(height=1, char="─", style="class:separator"),
                composer,
                Window(
                    FormattedTextControl(self._status_text),
                    height=1,
                    style="class:status",
                ),
            ]
        )
        self.application: Application[str] = Application(
            layout=Layout(root, focused_element=self.input_buffer),
            key_bindings=self._key_bindings(),
            full_screen=True,
            erase_when_done=False,
            editing_mode=EditingMode.VI if input_mode == "vi" else EditingMode.EMACS,
            style=Style.from_dict(viewport_styles(selected_theme)),
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
        self._prompt = prompt
        self._follow_tail = True
        self._vertical_scroll = 10**9
        self.input_buffer.set_document(Document("", 0), bypass_readonly=True)
        try:
            return await self.application.run_async()
        finally:
            self._running = False

    def close(self) -> None:
        self._unsubscribe()

    def _transcript_text(self) -> AnyFormattedText:
        app = get_app_or_none()
        width = app.output.get_size().columns if app is self.application else 80
        entries = self.transcript.snapshot()
        if not entries:
            return FormattedText(
                [
                    ("class:empty-title", "\n  Ready"),
                    (
                        "class:muted",
                        "\n  Ask about this workspace, attach @files, or type /help.",
                    ),
                ]
            )
        return self._formatter.format(entries, width=width)

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

    def _on_transcript_event(self, event: TranscriptEvent) -> None:
        del event
        if self._follow_tail:
            self._vertical_scroll = 10**9
        app = get_app_or_none()
        if app is self.application:
            app.invalidate()

    def _page_height(self) -> int:
        info = self.transcript_window.render_info
        return max(1, info.window_height - 1) if info is not None else 10

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("enter")
        def submit(event) -> None:
            state = self.input_buffer.complete_state
            if state is not None and state.current_completion is not None:
                self.input_buffer.apply_completion(state.current_completion)
                return
            event.app.exit(result=self.input_buffer.text)

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
            event.app.exit(exception=KeyboardInterrupt())

        @bindings.add("c-d")
        def eof(event) -> None:
            if not self.input_buffer.text:
                event.app.exit(exception=EOFError())
            else:
                event.current_buffer.delete()

        @bindings.add("pageup")
        def page_up(event) -> None:
            info = self.transcript_window.render_info
            current = info.vertical_scroll if info is not None else 0
            self._follow_tail = False
            self._vertical_scroll = max(0, current - self._page_height())
            event.app.invalidate()

        @bindings.add("pagedown")
        def page_down(event) -> None:
            info = self.transcript_window.render_info
            current = info.vertical_scroll if info is not None else 0
            self._vertical_scroll = current + self._page_height()
            event.app.invalidate()

        @bindings.add("end")
        def follow_tail(event) -> None:
            self._follow_tail = True
            self._vertical_scroll = 10**9
            event.app.invalidate()

        return bindings
