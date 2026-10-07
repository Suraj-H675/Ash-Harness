"""Fullscreen prompt surface with an independently scrollable transcript."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Callable

from prompt_toolkit.application import Application, get_app_or_none, run_in_terminal
from prompt_toolkit.application.current import get_app_session
from prompt_toolkit.data_structures import Point
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import (
    AnyFormattedText,
    FormattedText,
    StyleAndTextTuples,
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
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.keys import Keys
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType, MouseModifier
from prompt_toolkit.renderer import HeightIsUnknownError
from prompt_toolkit.utils import get_cwidth

from ash.ui.input_signals import PromptInterrupted
from ash.ui.safe_text import terminal_safe_text
from ash.ui.theme import Theme, prompt_style, terminal_styles
from ash.ui.transcript import Transcript
from ash.ui.transcript_view import TranscriptView


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


def format_context_bar(
    used: int,
    maximum: int,
    *,
    cells: int = 8,
    ascii_only: bool = False,
) -> FormattedText:
    maximum = max(1, maximum)
    ratio = min(1.0, max(0.0, used / maximum))
    filled = min(cells, max(0, round(ratio * cells)))
    percent = min(100, max(0, round(ratio * 100)))
    used_line, remaining_line = ("-", ".") if ascii_only else ("─", "╌")
    return FormattedText(
        [
            ("class:context-used", used_line * filled),
            ("class:context-empty", remaining_line * (cells - filled)),
            ("class:status", f" {percent:>3}% "),
        ]
    )


class WheelOnlyVt100Output(Vt100_Output):
    """VT output that enables only basic buttons and SGR wheel reports."""

    def enable_mouse_support(self) -> None:
        self.write_raw("\x1b[?1000h\x1b[?1006h")

    def disable_mouse_support(self) -> None:
        self.write_raw("\x1b[?1000l\x1b[?1006l")


def _wheel_output(output: Output) -> Output:
    if not isinstance(output, Vt100_Output) or isinstance(
        output, WheelOnlyVt100Output
    ):
        return output
    return WheelOnlyVt100Output(
        stdout=output.stdout,
        get_size=output.get_size,
        term=output.term,
        default_color_depth=output.get_default_color_depth(),
        enable_bell=output.enable_bell,
        enable_cpr=output.enable_cpr,
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
    """Persistent fullscreen owner for transcript navigation and composition."""

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
        transcript: Transcript | None = None,
        reduced_motion: bool = False,
        screen_reader_mode: bool = False,
    ) -> None:
        self.status_provider = status_provider
        self.context_provider = context_provider
        self.thinking_provider = thinking_provider or (lambda _width: (0, ""))
        self.dock_provider = dock_provider
        self.reduced_motion = reduced_motion or screen_reader_mode
        self._prompt = "> "
        self._read_context = ""
        self._choice_context = ""
        self._read_future: asyncio.Future[str] | None = None
        self._choice_future: asyncio.Future[str | None] | None = None
        self._app_task: asyncio.Task | None = None
        self._app_ready: asyncio.Future[None] | None = None
        self._app_loop: asyncio.AbstractEventLoop | None = None
        self._closing = False
        self._app_error: BaseException | None = None
        self._input_enabled = False
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
        self.transcript_view = TranscriptView(transcript or Transcript())

        self.input_buffer = Buffer(
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=completer,
            complete_while_typing=True,
            multiline=True,
            read_only=Condition(lambda: not self._input_enabled),
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

        self.composer_input_window = Window(
            BufferControl(buffer=self.input_buffer),
            height=self._composer_height,
            wrap_lines=True,
            style="class:composer",
        )
        composer = VSplit(
            [
                Window(
                    FormattedTextControl(self._composer_label),
                    width=self._composer_label_width,
                    height=self._composer_height,
                    dont_extend_width=True,
                    style="class:composer",
                ),
                self.composer_input_window,
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
        context_window = ConditionalContainer(
            Window(
                FormattedTextControl(self._decision_context_text),
                height=self._decision_context_height,
                wrap_lines=True,
                style="class:decision-context",
                dont_extend_height=True,
            ),
            filter=Condition(self._has_decision_context),
        )
        transcript_window = Window(
            content=self.transcript_view,
            height=Dimension(weight=1, min=1),
            wrap_lines=False,
            get_vertical_scroll=lambda _window: self.transcript_view.top_row(
                self.transcript_view._height
            ),
            always_hide_cursor=True,
            style="class:transcript",
        )
        latest_hint = ConditionalContainer(
            Window(
                FormattedTextControl(self._latest_hint),
                height=1,
                dont_extend_height=True,
                style="class:muted",
            ),
            filter=Condition(lambda: self.transcript_view.detached),
        )
        self.status_window = Window(
            FormattedTextControl(self._status_text),
            height=1,
            dont_extend_height=True,
            style="class:status",
        )

        body = HSplit(
            [
                transcript_window,
                latest_hint,
                thinking_window,
                context_window,
                ConditionalContainer(composer, filter=~choice_active),
                ConditionalContainer(choice_panel, filter=choice_active),
                self.status_window,
            ]
        )
        root = FloatContainer(
            content=body,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=6, scroll_offset=1),
                )
            ],
        )
        resolved_output = _wheel_output(output or get_app_session().output)
        self.application: Application[str | None] = Application(
            layout=Layout(root, focused_element=self.input_buffer),
            key_bindings=self._key_bindings(),
            full_screen=True,
            erase_when_done=False,
            editing_mode=EditingMode.VI if input_mode == "vi" else EditingMode.EMACS,
            style=prompt_style(terminal_styles(theme), no_color=no_color),
            mouse_support=True,
            input=input,
            output=resolved_output,
            min_redraw_interval=0.03,
            terminal_size_polling_interval=0.5,
        )
        self.application.pre_run_callables.append(self._start_terminal_pump)
        self.transcript_view.set_invalidator(self.invalidate)

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
        ready = self._app_ready
        if ready is not None and not ready.done():
            ready.set_result(None)

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

    async def start(self) -> None:
        if self._app_task is not None and not self._app_task.done():
            return
        loop = asyncio.get_running_loop()
        self._app_loop = loop
        self._app_ready = loop.create_future()
        self._app_error = None
        self._closing = False
        self._app_task = loop.create_task(
            self.application.run_async(handle_sigint=False)
        )
        self._app_task.add_done_callback(self._application_finished)
        await self._app_ready

    def _application_finished(self, task: asyncio.Task) -> None:
        try:
            task.result()
        except BaseException as exc:
            error = exc
        else:
            error = EOFError()
        if self._closing:
            return
        self._app_error = error
        ready = self._app_ready
        if ready is not None and not ready.done():
            ready.set_exception(error)
        for future in (self._read_future, self._choice_future):
            if future is not None and not future.done():
                future.set_exception(error)

    async def read(self, prompt: str = "> ", *, context: str = "") -> str:
        if self._running:
            raise RuntimeError("inline prompt surface already owns terminal input")
        await self.start()
        self._running = True
        self._input_enabled = True
        self._choice_mode = False
        self._prompt = terminal_safe_text(prompt, single_line=True)
        self._read_context = terminal_safe_text(context)[:6000]
        self.input_buffer.cancel_completion()
        self.input_buffer.set_document(Document("", 0), bypass_readonly=True)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._read_future = future
        self._thinking_cache_key = None
        self.application.invalidate()
        try:
            return await future
        finally:
            if self._read_future is future:
                self._read_future = None
            self._read_context = ""
            self._input_enabled = False
            self._running = False
            self._drain_terminal()

    async def choose(
        self,
        title: str,
        options: tuple[PromptChoice, ...],
        *,
        default_value: str | None = None,
        context: str = "",
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
        await self.start()
        self._running = True
        self._input_enabled = False
        self._choice_mode = True
        self._choice_title = terminal_safe_text(title, single_line=True)
        self._choice_context = terminal_safe_text(context)[:6000]
        self._choice_options = options
        self._choice_selected = selected
        self.input_buffer.cancel_completion()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str | None] = loop.create_future()
        self._choice_future = future
        self.application.invalidate()
        try:
            return await future
        finally:
            if self._choice_future is future:
                self._choice_future = None
            self._choice_mode = False
            self._choice_context = ""
            self._choice_options = ()
            self._choice_selected = 0
            self._running = False
            self.application.invalidate()
            self._drain_terminal()

    async def suspend_for_overlay(self, callback):
        """Release terminal ownership while a nested screen or editor runs."""

        await self._stop_application()
        result = callback()
        if hasattr(result, "__await__"):
            return await result
        return result

    async def _stop_application(self) -> None:
        task = self._app_task
        if task is None or task.done():
            self._app_task = None
            return
        self._closing = True
        if self.application.is_running:
            self.application.exit(result=None)
        else:
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, EOFError):
            pass
        self._app_task = None

    async def aclose(self) -> None:
        await self._stop_application()
        self.transcript_view.close()

    def close(self) -> None:
        """Request shutdown for synchronous compatibility callers."""

        task = self._app_task
        if task is not None and not task.done() and self.application.is_running:
            self._closing = True
            self.application.exit(result=None)

    def invalidate(self) -> None:
        self._thinking_cache_key = None
        self._dock_view_cache = None
        if self.application.is_running:
            self.application.invalidate()
        loop = self._terminal_loop
        wakeup = self._dock_wakeup
        if loop is not None and wakeup is not None:
            loop.call_soon_threadsafe(wakeup.set)

    def clear_visible_screen(self) -> None:
        """Redraw the app-owned view without touching terminal scrollback."""

        self.invalidate()

    def _output_width(self) -> int:
        app = get_app_or_none()
        if app is self.application:
            return max(1, app.output.get_size().columns)
        if hasattr(self, "application"):
            return max(1, self.application.output.get_size().columns)
        return 80

    def _composer_height(self) -> int:
        available = max(1, self._output_width() - self._composer_label_width())
        rows = 0
        for line in self.input_buffer.document.lines:
            width = get_cwidth(line)
            rows += max(1, (width + available - 1) // available)
        return min(8, max(1, rows))

    def _composer_label_width(self) -> int:
        return max(
            3,
            min(16, get_cwidth(self._composer_label_text()) + 1),
        )

    def _composer_label_text(self) -> str:
        if not self._input_enabled:
            return "… "
        prompt = self._prompt.rstrip()
        if prompt in {">", "native>"}:
            label = "› "
        elif prompt.casefold().startswith("steer"):
            label = "STEER › "
        elif prompt.casefold().startswith("plan"):
            label = "PLAN › "
        elif prompt.casefold().startswith("denial"):
            label = "DENY › "
        else:
            words = prompt.split()
            purpose = " ".join(words[:2]).strip("[]?()")
            label = (purpose[:10].upper() + " › ") if purpose else "› "
        return label

    def _composer_label(self) -> FormattedText:
        width = min(self._composer_label_width(), self._output_width())
        label = _take_cells(self._composer_label_text(), width)
        return FormattedText([("class:composer-label", f" {label}")])

    def _has_decision_context(self) -> bool:
        return bool(self._choice_context or self._read_context)

    def _decision_context_text(self) -> FormattedText:
        value = self._choice_context or self._read_context
        if not value:
            return FormattedText([])
        lines = value.splitlines()[:8]
        clipped = "\n".join(lines)
        if len(lines) < len(value.splitlines()):
            clipped += "\n… more details in transcript"
        return FormattedText([("class:decision-context", " " + clipped)])

    def _decision_context_height(self) -> int:
        if not self._has_decision_context():
            return 0
        rows = max(1, self.application.output.get_size().rows)
        dock = int(self._dock_visible())
        choice = 7 if self._choice_mode else 0
        composer = min(8, self._composer_height())
        reserve = 2  # one transcript row and the status row
        available = max(1, rows - dock - choice - composer - reserve)
        text = fragment_list_to_text(self._decision_context_text())
        return min(5, available, max(1, len(text.splitlines())))

    def _latest_hint(self) -> FormattedText:
        return FormattedText([("class:muted", " ↓ latest · Ctrl+End")])

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

        fragments: StyleAndTextTuples = []
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
        ascii_only = not self._output_supports_unicode()
        cells = 8 if width >= 48 else 4 if width >= 32 else 2 if width >= 18 else 0
        percent = min(100, max(0, round(100 * max(0, used) / max(1, maximum))))
        context = format_context_bar(
            used,
            maximum,
            cells=cells,
            ascii_only=ascii_only,
        )
        context_width = get_cwidth(fragment_list_to_text(context))
        prefix_width = 1 if width >= 12 else 0
        separator_width = 2 if value and width >= 20 else 1 if value else 0
        if context_width + prefix_width + separator_width > width:
            context = FormattedText(
                [("class:status", f"{percent}%" if width >= 4 else str(percent)[:width])]
            )
            context_width = get_cwidth(fragment_list_to_text(context))
            prefix_width = 0
            separator_width = 0
        available = max(0, width - context_width - prefix_width - separator_width)
        value = _truncate_status(value, available)
        fragments: StyleAndTextTuples = []
        if prefix_width:
            fragments.append(("class:status", " "))
        if value:
            fragments.append(("class:status", value))
            if separator_width:
                fragments.append(("class:status", "  " if separator_width == 2 else " "))
        fragments.extend(context)
        return FormattedText(fragments)

    def _output_supports_unicode(self) -> bool:
        stdout = getattr(self.application.output, "stdout", None)
        encoding = getattr(stdout, "encoding", None)
        if not encoding:
            return True
        try:
            "─╌…".encode(encoding)
        except (LookupError, UnicodeEncodeError):
            return False
        return True

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
        wheel_step = 2

        @bindings.add(Keys.Vt100MouseEvent, eager=True)
        def sgr_mouse(event):
            data = event.data
            if not data.startswith("\x1b[<") or data[-1:] not in {"M", "m"}:
                return NotImplemented
            try:
                button_text, x_text, y_text = data[3:-1].split(";")
                button = int(button_text)
                x = int(x_text) - 1
                y = int(y_text) - 1
            except (TypeError, ValueError):
                return NotImplemented

            if (button & 64) != 0:
                direction = button & 3
                if direction == 1:
                    self.transcript_view.scroll(wheel_step)
                elif direction == 0:
                    self.transcript_view.scroll(-wheel_step)
                return None

            button_kind = {
                0: MouseButton.LEFT,
                1: MouseButton.MIDDLE,
                2: MouseButton.RIGHT,
            }.get(button & 3)
            if button_kind is None:
                return NotImplemented
            modifiers: set[MouseModifier] = set()
            if button & 4:
                modifiers.add(MouseModifier.SHIFT)
            if button & 8:
                modifiers.add(MouseModifier.ALT)
            if button & 16:
                modifiers.add(MouseModifier.CONTROL)
            mouse_type = (
                MouseEventType.MOUSE_DOWN
                if data.endswith("M")
                else MouseEventType.MOUSE_UP
            )

            renderer = event.app.renderer
            if not renderer.height_is_known:
                return NotImplemented
            try:
                y -= renderer.rows_above_layout
            except HeightIsUnknownError:
                return NotImplemented
            if y < 0 or x < 0:
                return NotImplemented
            handlers = renderer.mouse_handlers.mouse_handlers
            if y >= len(handlers) or x >= len(handlers[y]):
                return NotImplemented
            return handlers[y][x](
                MouseEvent(
                    position=Point(x=x, y=y),
                    event_type=mouse_type,
                    button=button_kind,
                    modifiers=frozenset(modifiers),
                )
            )

        @bindings.add(Keys.ScrollUp, eager=True)
        def scroll_up(event) -> None:
            del event
            self.transcript_view.scroll(-wheel_step)

        @bindings.add(Keys.ScrollDown, eager=True)
        def scroll_down(event) -> None:
            del event
            self.transcript_view.scroll(wheel_step)

        @bindings.add(Keys.PageUp, eager=True)
        def page_up(event) -> None:
            del event
            self.transcript_view.scroll(-max(1, self.transcript_view._height - 2))

        @bindings.add(Keys.PageDown, eager=True)
        def page_down(event) -> None:
            del event
            self.transcript_view.scroll(max(1, self.transcript_view._height - 2))

        @bindings.add("c-end", eager=True)
        def latest(event) -> None:
            del event
            self.transcript_view.scroll_to_latest()

        @bindings.add("enter")
        def submit(event) -> None:
            if self._choice_mode:
                choice_future = self._choice_future
                if choice_future is not None and not choice_future.done():
                    choice_future.set_result(
                        self._choice_options[self._choice_selected].value
                    )
                return
            read_future = self._read_future
            if not self._input_enabled or read_future is None:
                return
            self.input_buffer.cancel_completion()
            self.input_buffer.append_to_history()
            if not read_future.done():
                read_future.set_result(self.input_buffer.text)
            self.input_buffer.set_document(Document("", 0), bypass_readonly=True)
            self._input_enabled = False
            self.application.invalidate()

        @bindings.add("tab")
        def complete(event) -> None:
            if not self._input_enabled:
                return
            state = self.input_buffer.complete_state
            if state is not None and state.completions:
                completion = state.current_completion or state.completions[0]
                self.input_buffer.apply_completion(completion)
                return
            self.input_buffer.start_completion(select_first=True)

        def newline(event) -> None:
            if self._input_enabled:
                event.current_buffer.insert_text("\n")

        def open_editor(event) -> None:
            if self._input_enabled:
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
            future = self._read_future
            if future is not None and not future.done():
                future.set_exception(PromptInterrupted())
                self.input_buffer.set_document(Document("", 0), bypass_readonly=True)
                self._input_enabled = False
                self.application.invalidate()

        @bindings.add("c-d")
        def eof(event) -> None:
            if self._choice_mode:
                choice_future = self._choice_future
                if choice_future is not None and not choice_future.done():
                    choice_future.set_result(None)
            elif self._input_enabled and not self.input_buffer.text:
                read_future = self._read_future
                if read_future is not None and not read_future.done():
                    read_future.set_exception(EOFError())
                    self._input_enabled = False
                    self.application.invalidate()
            else:
                if self._input_enabled:
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
            del event
            future = self._choice_future
            if future is not None and not future.done():
                future.set_result(None)

        return bindings
