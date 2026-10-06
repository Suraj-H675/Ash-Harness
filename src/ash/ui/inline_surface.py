"""Small retained prompt surface backed by native terminal scrollback.

Only the current turn and composer are redrawn. Completed conversation history
is emitted once by :mod:`ash.ui.terminal`, so render cost does not grow with
session length and the terminal keeps ownership of mouse selection/scrolling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from prompt_toolkit.application import Application, get_app_or_none
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import (
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
    ConditionalContainer,
    Float,
    FloatContainer,
    FormattedTextControl,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output.base import Output

from ash.ui.input_signals import PromptInterrupted
from ash.ui.safe_text import terminal_safe_text
from ash.ui.theme import Theme, prompt_style, terminal_styles


LiveViewProvider = Callable[[int], tuple[int, AnyFormattedText]]


@dataclass(frozen=True)
class PromptChoice:
    value: str
    label: str
    description: str = ""


class InlinePromptSurface:
    """One non-full-screen application for live turn state and composition."""

    def __init__(
        self,
        *,
        history: History,
        completer,
        status_provider: Callable[[], str],
        header_provider: Callable[[], str],
        live_provider: LiveViewProvider,
        input_mode: str,
        keybindings: dict[str, list[str]],
        theme: Theme,
        no_color: bool,
        input: Input | None = None,
        output: Output | None = None,
    ) -> None:
        self.status_provider = status_provider
        self.header_provider = header_provider
        self.live_provider = live_provider
        self._prompt = "> "
        self._running = False
        self._choice_mode = False
        self._choice_title = ""
        self._choice_options: tuple[PromptChoice, ...] = ()
        self._choice_selected = 0
        self._live_cache_key: tuple[int, int] | None = None
        self._live_cache: AnyFormattedText = FormattedText([])
        self._configured_keybindings = keybindings

        self.input_buffer = Buffer(
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=completer,
            complete_while_typing=True,
            multiline=True,
        )
        self.live_control = FormattedTextControl(
            self._live_text,
            get_cursor_position=self._live_cursor_position,
            show_cursor=False,
        )
        live_window = Window(
            self.live_control,
            height=Dimension(min=1, max=16),
            wrap_lines=False,
            always_hide_cursor=True,
            dont_extend_height=True,
        )
        live_visible = Condition(self._live_visible)

        self.prompt_control = FormattedTextControl(self._composer_label)
        composer = HSplit(
            [
                Window(
                    self.prompt_control,
                    height=1,
                    dont_extend_height=True,
                    style="class:composer",
                ),
                Window(
                    BufferControl(buffer=self.input_buffer),
                    height=Dimension(min=1, max=8),
                    wrap_lines=True,
                    style="class:composer",
                ),
            ]
        )
        self.choice_control = FormattedTextControl(self._choice_text)
        choice_panel = HSplit(
            [
                Window(
                    FormattedTextControl(self._choice_heading),
                    height=1,
                    dont_extend_height=True,
                    style="class:composer",
                ),
                Window(
                    self.choice_control,
                    height=Dimension(min=1, max=6),
                    dont_extend_height=True,
                    style="class:composer",
                ),
                Window(
                    FormattedTextControl(self._choice_detail_text),
                    height=2,
                    wrap_lines=True,
                    style="class:composer",
                ),
            ]
        )
        choice_active = Condition(lambda: self._choice_mode)

        body = HSplit(
            [
                ConditionalContainer(live_window, filter=live_visible),
                ConditionalContainer(
                    Window(height=1, char="─", style="class:separator"),
                    filter=live_visible,
                ),
                Window(
                    FormattedTextControl(self._header_text),
                    height=1,
                    dont_extend_height=True,
                    style="class:header",
                ),
                ConditionalContainer(composer, filter=~choice_active),
                ConditionalContainer(choice_panel, filter=choice_active),
                Window(
                    FormattedTextControl(self._status_text),
                    height=1,
                    dont_extend_height=True,
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
        self.application: Application[str | None] = Application(
            layout=Layout(root, focused_element=self.input_buffer),
            key_bindings=self._key_bindings(),
            full_screen=False,
            erase_when_done=True,
            editing_mode=EditingMode.VI if input_mode == "vi" else EditingMode.EMACS,
            style=prompt_style(terminal_styles(theme), no_color=no_color),
            mouse_support=False,
            input=input,
            output=output,
            min_redraw_interval=0.03,
            terminal_size_polling_interval=0.5,
        )

    async def read(self, prompt: str = "> ") -> str:
        if self._running:
            raise RuntimeError("inline prompt surface already owns terminal input")
        self._running = True
        self._choice_mode = False
        self._prompt = terminal_safe_text(prompt, single_line=True)
        self.input_buffer.set_document(Document("", 0), bypass_readonly=True)
        self._live_cache_key = None
        try:
            result = await self.application.run_async()
            if result is None:
                raise EOFError
            return result
        finally:
            self._running = False

    async def choose(
        self,
        title: str,
        options: tuple[PromptChoice, ...],
        *,
        default_value: str | None = None,
    ) -> str | None:
        if self._running:
            raise RuntimeError("inline prompt surface already owns terminal input")
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
        try:
            return await self.application.run_async()
        finally:
            self._choice_mode = False
            self._choice_options = ()
            self._choice_selected = 0
            self._running = False

    def invalidate(self) -> None:
        self._live_cache_key = None
        if self._running:
            self.application.invalidate()

    def _output_width(self) -> int:
        app = get_app_or_none()
        if app is self.application:
            return max(20, app.output.get_size().columns)
        return 80

    def _live_text(self) -> AnyFormattedText:
        width = self._output_width()
        revision, rendered = self.live_provider(width)
        key = (revision, width)
        if key != self._live_cache_key:
            self._live_cache_key = key
            self._live_cache = rendered
        return self._live_cache

    def _live_visible(self) -> bool:
        return bool(fragment_list_to_text(to_formatted_text(self._live_text())))

    def _live_cursor_position(self):
        text = fragment_list_to_text(to_formatted_text(self._live_text()))
        if not text:
            return None
        lines = text.split("\n")
        from prompt_toolkit.data_structures import Point

        return Point(x=len(lines[-1]), y=len(lines) - 1)

    def _header_text(self) -> FormattedText:
        value = terminal_safe_text(self.header_provider(), single_line=True)
        if not value:
            return FormattedText([("class:header-brand", " ASH ")])
        return FormattedText(
            [
                ("class:header-brand", " ASH "),
                ("class:header-meta", "  " + value + " "),
            ]
        )

    def _status_text(self) -> FormattedText:
        value = terminal_safe_text(self.status_provider(), single_line=True)
        return FormattedText([("class:status", f" {value} " if value else " ")])

    def _composer_label(self) -> FormattedText:
        label = "STEER" if self._prompt.casefold().startswith("steer") else "YOU"
        return FormattedText(
            [
                ("class:composer-label", f" {label} › "),
                ("class:muted", "Enter send  ·  Ctrl+J newline"),
            ]
        )

    def _choice_heading(self) -> FormattedText:
        return FormattedText(
            [
                ("class:approval-prefix", " APPROVAL "),
                ("class:muted", "  " + self._choice_title),
            ]
        )

    def _choice_text(self) -> FormattedText:
        fragments: list[tuple[str, str]] = []
        for index, option in enumerate(self._choice_options):
            selected = index == self._choice_selected
            style = "class:selected" if selected else ""
            fragments.append((style, "> " if selected else "  "))
            fragments.append(
                (f"{style} class:option".strip(), option.label)
            )
            if index + 1 < len(self._choice_options):
                fragments.append(("", "\n"))
        return FormattedText(fragments)

    def _choice_detail_text(self) -> FormattedText:
        if not self._choice_options:
            return FormattedText([])
        description = terminal_safe_text(
            self._choice_options[self._choice_selected].description
        )
        return FormattedText(
            [
                ("class:meta", " " + description if description else ""),
                ("class:muted", "\n ↑/↓ choose  Enter select  Esc deny"),
            ]
        )

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("enter")
        def submit(event) -> None:
            if self._choice_mode:
                event.app.exit(result=self._choice_options[self._choice_selected].value)
                return
            state = self.input_buffer.complete_state
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

        return bindings
