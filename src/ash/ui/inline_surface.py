"""Small retained prompt surface backed by native terminal scrollback.

Only the conditional activity dock, composer, and status row are redrawn.
Conversation output is appended to native scrollback, so render cost does not
grow with session length and the terminal keeps ownership of selection and
scrolling.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Callable

from prompt_toolkit.application import Application, get_app_or_none, run_in_terminal
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
    VSplit,
    Window,
)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output.base import Output
from prompt_toolkit.utils import get_cwidth

from ash.ui.input_signals import PromptInterrupted
from ash.ui.safe_text import terminal_safe_text
from ash.ui.theme import Theme, prompt_style, terminal_styles


ThinkingViewProvider = Callable[[int], tuple[int, AnyFormattedText]]
ContextUsageProvider = Callable[[], tuple[int, int]]
DockViewProvider = Callable[[], "ActivityDockView"]


@dataclass(frozen=True)
class ActivityDockView:
    """Bounded semantic state for the retained prompt's single-line dock."""

    revision: int
    activity: str | None
    reasoning: str


@dataclass(frozen=True)
class PromptChoice:
    value: str
    label: str
    description: str = ""


def format_context_bar(used: int, maximum: int, *, cells: int = 8) -> FormattedText:
    maximum = max(1, maximum)
    ratio = min(1.0, max(0.0, used / maximum))
    filled = min(cells, max(0, round(ratio * cells)))
    percent = min(100, max(0, round(ratio * 100)))
    return FormattedText(
        [
            ("class:context-used", "█" * filled),
            ("class:context-empty", "░" * (cells - filled)),
            ("class:status", f" {percent:>3}% "),
        ]
    )


def _truncate_status(value: str, width: int) -> str:
    if get_cwidth(value) <= width:
        return value
    if width <= 0:
        return ""
    remaining = width - 1
    visible: list[str] = []
    for character in value:
        character_width = get_cwidth(character)
        if character_width > remaining:
            break
        visible.append(character)
        remaining -= character_width
    return "".join(visible) + "…"


def _take_cells(value: str, width: int) -> str:
    if width <= 0:
        return ""
    visible: list[str] = []
    remaining = width
    for character in value:
        character_width = get_cwidth(character)
        if character_width > remaining:
            break
        visible.append(character)
        remaining -= character_width
    return "".join(visible)


class InlinePromptSurface:
    """One non-full-screen application for live turn state and composition."""

    def __init__(
        self,
        *,
        history: History,
        completer,
        status_provider: Callable[[], str],
        context_provider: ContextUsageProvider,
        thinking_provider: ThinkingViewProvider | None = None,
        dock_provider: DockViewProvider | None = None,
        input_mode: str,
        keybindings: dict[str, list[str]],
        theme: Theme,
        no_color: bool,
        input: Input | None = None,
        output: Output | None = None,
        reduced_motion: bool = False,
        screen_reader_mode: bool = False,
    ) -> None:
        self.status_provider = status_provider
        self.context_provider = context_provider
        self.thinking_provider = thinking_provider or (lambda _width: (0, ""))
        self.dock_provider = dock_provider
        self.reduced_motion = reduced_motion or screen_reader_mode
        self._prompt = "> "
        self._running = False
        self._terminal_callbacks: deque[Callable[[], None]] = deque()
        self._terminal_wakeup: asyncio.Event | None = None
        self._dock_wakeup: asyncio.Event | None = None
        self._terminal_loop: asyncio.AbstractEventLoop | None = None
        self._choice_mode = False
        self._choice_title = ""
        self._choice_options: tuple[PromptChoice, ...] = ()
        self._choice_selected = 0
        self._thinking_cache_key: tuple[int, int, int] | None = None
        self._thinking_cache: AnyFormattedText = FormattedText([])
        self._dock_view_cache: ActivityDockView | None = None
        self._legacy_dock_visible = False
        self._dot_phase = 0
        self._configured_keybindings = keybindings

        self.input_buffer = Buffer(
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=completer,
            complete_while_typing=True,
            multiline=True,
        )
        self.thinking_control = FormattedTextControl(
            self._thinking_text,
            show_cursor=False,
        )
        thinking_window = ConditionalContainer(
            Window(
                self.thinking_control,
                height=1,
                wrap_lines=False,
                always_hide_cursor=True,
                dont_extend_height=True,
            ),
            filter=Condition(self._dock_visible),
        )

        composer = VSplit(
            [
                Window(
                    FormattedTextControl(
                        FormattedText([("class:prompt", " › ")])
                    ),
                    width=3,
                    height=self._composer_height,
                    dont_extend_width=True,
                    style="class:composer",
                ),
                Window(
                    BufferControl(buffer=self.input_buffer),
                    height=self._composer_height,
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
        status_row = Window(
            FormattedTextControl(self._status_text),
            height=1,
            dont_extend_height=True,
            style="class:status",
        )

        body = HSplit(
            [
                Window(height=Dimension(weight=1)),
                thinking_window,
                ConditionalContainer(composer, filter=~choice_active),
                ConditionalContainer(choice_panel, filter=choice_active),
                status_row,
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
        self.application.pre_run_callables.append(self._start_terminal_pump)

    def write_terminal(self, callback: Callable[[], None]) -> None:
        if not self.application.is_running:
            callback()
            return
        self._terminal_callbacks.append(callback)
        loop = self._terminal_loop
        wakeup = self._terminal_wakeup
        if loop is not None and wakeup is not None:
            loop.call_soon_threadsafe(wakeup.set)

    def _start_terminal_pump(self) -> None:
        self._terminal_loop = asyncio.get_running_loop()
        self._terminal_wakeup = asyncio.Event()
        self._dock_wakeup = asyncio.Event()
        self.application.create_background_task(self._pump_terminal())
        if not self.reduced_motion:
            self.application.create_background_task(self._animate_dock())

    async def _pump_terminal(self) -> None:
        wakeup = self._terminal_wakeup
        if wakeup is None:
            return
        while self.application.is_running:
            await wakeup.wait()
            wakeup.clear()
            if self._terminal_callbacks:
                await run_in_terminal(self._drain_terminal)

    def _drain_terminal(self) -> None:
        while self._terminal_callbacks:
            self._terminal_callbacks.popleft()()

    async def read(self, prompt: str = "> ") -> str:
        if self._running:
            raise RuntimeError("inline prompt surface already owns terminal input")
        self._running = True
        self._choice_mode = False
        self._prompt = terminal_safe_text(prompt, single_line=True)
        self.input_buffer.set_document(Document("", 0), bypass_readonly=True)
        self._thinking_cache_key = None
        try:
            result = await self.application.run_async()
            if result is None:
                raise EOFError
            return result
        finally:
            self._running = False
            self._drain_terminal()

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
            self._drain_terminal()

    def invalidate(self) -> None:
        self._thinking_cache_key = None
        self._dock_view_cache = None
        if self._running:
            self.application.invalidate()
        loop = self._terminal_loop
        wakeup = self._dock_wakeup
        if loop is not None and wakeup is not None:
            loop.call_soon_threadsafe(wakeup.set)

    def clear_visible_screen(self) -> None:
        """Clear only the visible terminal viewport, preserving scrollback."""

        output = self.application.output
        output.erase_screen()
        output.cursor_goto(0, 0)
        output.flush()

    def _output_width(self) -> int:
        app = get_app_or_none()
        if app is self.application:
            return max(1, app.output.get_size().columns)
        if hasattr(self, "application"):
            return max(1, self.application.output.get_size().columns)
        return 80

    def _composer_height(self) -> int:
        available = max(1, self._output_width() - 3)
        rows = 0
        for line in self.input_buffer.document.lines:
            width = get_cwidth(line)
            rows += max(1, (width + available - 1) // available)
        return min(8, max(1, rows))

    def _thinking_text(self) -> AnyFormattedText:
        width = self._output_width()
        view = self._dock_view()
        if self.dock_provider is None:
            revision, rendered = self.thinking_provider(width)
            self._legacy_dock_visible = bool(
                fragment_list_to_text(to_formatted_text(rendered)).strip()
            )
            key = (revision, width, -1)
        else:
            rendered = self._format_dock(view, width)
            key = (view.revision, width, self._dot_phase if view.activity else -1)
        if key != self._thinking_cache_key:
            self._thinking_cache_key = key
            self._thinking_cache = rendered
        return self._thinking_cache

    def _dock_view(self) -> ActivityDockView:
        if self._dock_view_cache is not None:
            return self._dock_view_cache
        if self.dock_provider is not None:
            view = self.dock_provider()
        else:
            width = self._output_width()
            revision, rendered = self.thinking_provider(width)
            reasoning = fragment_list_to_text(to_formatted_text(rendered))
            view = ActivityDockView(
                revision=revision,
                activity=None,
                reasoning=reasoning,
            )
            self._legacy_dock_visible = bool(reasoning.strip())
        self._dock_view_cache = view
        return view

    def _dock_visible(self) -> bool:
        view = self._dock_view()
        return bool(view.activity or view.reasoning or self._legacy_dock_visible)

    def _format_dock(self, view: ActivityDockView, width: int) -> FormattedText:
        if width <= 0 or not (view.activity or view.reasoning):
            return FormattedText([])

        fragments: list[tuple[str, str]] = []
        remaining = width
        if view.activity:
            dots = (".  ", ".. ", "...")[self._dot_phase % 3]
            dots_width = get_cwidth(dots) if not self.reduced_motion else 0
            if get_cwidth(view.activity) + dots_width <= remaining and dots_width:
                fragments.append(("class:activity", view.activity))
                fragments.append(("class:activity-dots", dots))
                remaining -= get_cwidth(view.activity) + dots_width
            else:
                activity = _take_cells(view.activity, remaining)
                fragments.append(("class:activity", activity))
                remaining -= get_cwidth(activity)
        elif view.reasoning:
            prefix = "Reasoning: "
            if get_cwidth(prefix) <= remaining:
                fragments.append(("class:reasoning-prefix", prefix))
                remaining -= get_cwidth(prefix)
            elif remaining > 0:
                label = (
                    "R"
                    if remaining == 1
                    else _take_cells("Reasoning", remaining - 1) + "…"
                )
                fragments.append(("class:reasoning-prefix", label))
                remaining = 0

        if view.reasoning and remaining > 0:
            separator = "  ·  " if view.activity else ""
            separator_width = get_cwidth(separator)
            if separator_width < remaining:
                fragments.append(("class:muted", separator))
                remaining -= separator_width
                reasoning = _take_cells(view.reasoning, remaining)
                if reasoning:
                    fragments.append(("class:reasoning", reasoning))
        return FormattedText(fragments)

    async def _animate_dock(self) -> None:
        """Advance only the dock row while semantic work remains active."""

        wakeup = self._dock_wakeup
        if wakeup is None:
            return
        loop = asyncio.get_running_loop()
        interval = 0.45
        next_tick = loop.time() + interval
        active_label: str | None = None
        while self.application.is_running:
            wakeup.clear()
            view = self._dock_view()
            if not view.activity:
                active_label = None
                self._dot_phase = 0
                await wakeup.wait()
                next_tick = loop.time() + interval
                continue

            if view.activity != active_label:
                active_label = view.activity
                self._dot_phase = 0
                next_tick = loop.time() + interval
            delay = max(0.0, next_tick - loop.time())
            try:
                await asyncio.wait_for(wakeup.wait(), timeout=delay)
            except asyncio.TimeoutError:
                self._dot_phase = (self._dot_phase + 1) % 3
                self.application.invalidate()
                next_tick += interval
                if next_tick <= loop.time():
                    next_tick = loop.time() + interval

    def _status_text(self) -> FormattedText:
        value = terminal_safe_text(self.status_provider(), single_line=True)
        used, maximum = self.context_provider()
        width = self._output_width()
        cells = 8 if width >= 48 else 4 if width >= 32 else 2
        context = format_context_bar(used, maximum, cells=cells)
        context_width = cells + 6
        separator = "  " if value else " "
        available_status_width = max(
            0,
            width - 1 - get_cwidth(separator) - context_width,
        )
        value = _truncate_status(value, available_status_width)
        fragments: list[tuple[str, str]] = [
            ("class:status", f" {value}" if value else " "),
            ("class:status", separator),
            *context,
        ]
        return FormattedText(fragments)

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
