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
from prompt_toolkit.history import FileHistory
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

from ash.ui.transcript import Transcript, TranscriptEntry, TranscriptEvent
from ash.ui.theme import Theme, get_theme, viewport_styles


_ENTRY_STYLE = {
    "user": ("class:user-prefix", "you"),
    "assistant": ("class:assistant-prefix", "ash"),
    "reasoning": ("class:reasoning-prefix", "reasoning"),
    "tool": ("class:tool-prefix", "tool"),
    "approval": ("class:approval-prefix", "approval"),
    "status": ("class:status-prefix", "status"),
    "error": ("class:error-prefix", "error"),
}


def format_transcript(entries: tuple[TranscriptEntry, ...]) -> AnyFormattedText:
    """Render semantic entries without baking in terminal dimensions."""

    fragments: list[tuple[str, str]] = []
    for index, entry in enumerate(entries):
        if index:
            fragments.append(("", "\n\n"))
        style, default_title = _ENTRY_STYLE[entry.kind]
        title = entry.title or default_title
        fragments.append((style, f"{title} > "))
        body_style = "class:reasoning" if entry.kind == "reasoning" else ""
        fragments.append((body_style, entry.content or " "))
        if not entry.finalized:
            fragments.append(("class:streaming", "  ..."))
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
            style, default_title = _ENTRY_STYLE[entry.kind]
            fragments.append((style, f"{entry.title or default_title} > "))
            if entry.kind == "assistant" and entry.content:
                key = (entry.entry_id, entry.content, width)
                live_keys.add(key)
                rendered = self._cache.get(key)
                if rendered is None:
                    rendered = self._render_markdown(entry.content, width=width)
                    self._cache[key] = rendered
                fragments.append(("", "\n"))
                fragments.extend(to_formatted_text(rendered))
            else:
                body_style = "class:reasoning" if entry.kind == "reasoning" else ""
                fragments.append((body_style, entry.content or " "))
            if not entry.finalized:
                fragments.append(("class:streaming", "  ..."))
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
        completer: Any = None,
        status_provider: Callable[[], str] | None = None,
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
            history=FileHistory(str(history_path)),
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
        self.prompt_control = FormattedTextControl(
            lambda: [("class:prompt", self._prompt)]
        )
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
        return self._formatter.format(self.transcript.snapshot(), width=width)

    def _status_text(self) -> AnyFormattedText:
        return FormattedText([("", f" {self.status_provider()} ")])

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
