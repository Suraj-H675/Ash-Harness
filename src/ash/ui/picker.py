"""Small keyboard-first filtered picker used by interactive Ash surfaces."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.application import Application, get_app_or_none
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.input.base import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import BufferControl, FormattedTextControl, HSplit, Layout, VSplit, Window
from prompt_toolkit.output.base import Output
from rich.cells import cell_len, set_cell_size
from ash.ui.safe_text import terminal_safe_text
from ash.ui.theme import get_theme, overlay_styles, prompt_style


@dataclass(frozen=True)
class PickerOption:
    """One selectable row in a :class:`FilterPicker`."""

    value: str
    label: str
    description: str = ""
    keywords: tuple[str, ...] = ()
    state: str = ""


class FilterPicker:
    """Full-screen incremental picker with one predictable keyboard grammar."""

    def __init__(
        self,
        title: str,
        options: Sequence[PickerOption],
        *,
        current_value: str | None = None,
        filter_label: str = "Filter",
        hint: str = "type to filter",
        theme: str = "dark",
        no_color: bool = False,
        input: Input | None = None,
        output: Output | None = None,
    ) -> None:
        self.title = terminal_safe_text(title, single_line=True)
        self._hint = terminal_safe_text(hint, single_line=True)
        self._options = tuple(options)
        self._current_value = current_value
        self._filtered = list(self._options)
        self._selected = self._initial_selection(self._filtered)
        self.filter_buffer = Buffer(multiline=False)
        self.filter_buffer.on_text_changed += self._on_filter_changed

        self._title_control = FormattedTextControl(self._title_text)
        title_row = Window(
            self._title_control,
            height=1,
        )
        filter_row = VSplit(
            [
                Window(
                    FormattedTextControl(
                        FormattedText([("class:label", f"{filter_label} ")])
                    ),
                    width=max(4, len(filter_label) + 1),
                    height=1,
                ),
                Window(BufferControl(buffer=self.filter_buffer), height=1),
            ]
        )
        self._list_control = FormattedTextControl(self._render_list)
        self._detail_control = FormattedTextControl(self._render_detail)
        root = HSplit(
            [
                title_row,
                filter_row,
                Window(height=1, char="─", style="class:separator"),
                Window(self._list_control, wrap_lines=False),
                Window(self._detail_control, height=2, wrap_lines=True, style="class:detail"),
                Window(
                    FormattedTextControl(
                        " ↑/↓ move  Enter select  Esc clear/back  Ctrl-C cancel "
                    ),
                    height=1,
                    style="class:footer",
                ),
            ]
        )
        self.application: Application[str | None] = Application(
            layout=Layout(root, focused_element=self.filter_buffer),
            key_bindings=self._key_bindings(),
            full_screen=True,
            erase_when_done=False,
            style=prompt_style(
                overlay_styles(get_theme(theme)),
                no_color=no_color,
            ),
            input=input,
            output=output,
        )

    def run(self) -> str | None:
        return self.application.run()

    async def run_async(self) -> str | None:
        return await self.application.run_async()

    def set_options(self, options: Sequence[PickerOption]) -> None:
        """Replace rows while preserving the active filter and selection when possible."""

        selected = self._selected_option_value()
        self._options = tuple(options)
        self._refilter(preferred_value=selected)

    def set_hint(self, hint: str) -> None:
        self._hint = terminal_safe_text(hint, single_line=True)
        self.application.invalidate()

    def _title_text(self) -> FormattedText:
        fragments: list[tuple[str, str]] = [
            ("class:title", "ASH"),
            ("class:muted", "  ·  "),
            ("class:title", self.title),
        ]
        if self._hint:
            fragments.extend([("", "  "), ("class:muted", self._hint)])
        return FormattedText(fragments)

    def _search_text(self, option: PickerOption) -> str:
        return " ".join(
            (option.value, option.label, option.description, option.state, *option.keywords)
        ).casefold()

    def _initial_selection(self, options: Sequence[PickerOption]) -> int:
        if self._current_value is not None:
            for index, option in enumerate(options):
                if option.value == self._current_value:
                    return index
        return 0

    def _on_filter_changed(self, _: Buffer) -> None:
        self._refilter(preferred_value=self._selected_option_value())

    def _refilter(self, *, preferred_value: str | None = None) -> None:
        terms = self.filter_buffer.text.casefold().split()
        self._filtered = [
            option
            for option in self._options
            if all(term in self._search_text(option) for term in terms)
        ]
        self._selected = 0
        if preferred_value is not None:
            for index, option in enumerate(self._filtered):
                if option.value == preferred_value:
                    self._selected = index
                    break
        elif not terms:
            self._selected = self._initial_selection(self._filtered)
        self.application.invalidate()

    def _selected_option_value(self) -> str | None:
        if not self._filtered:
            return None
        return self._filtered[min(self._selected, len(self._filtered) - 1)].value

    def _page(self) -> tuple[int, int]:
        app = get_app_or_none()
        rows = app.output.get_size().rows if app is self.application else 24
        page_size = max(1, rows - 8)
        start = min(
            max(0, self._selected - page_size + 1),
            max(0, len(self._filtered) - page_size),
        )
        return start, start + page_size

    def _render_list(self) -> FormattedText:
        if not self._filtered:
            return FormattedText([("class:empty", " No matches")])
        app = get_app_or_none()
        columns = app.output.get_size().columns if app is self.application else 80
        fragments: list[tuple[str, str]] = []
        start, end = self._page()
        for index, option in enumerate(self._filtered[start:end], start=start):
            selected = index == self._selected
            style = "class:selected" if selected else ""
            marker = "> " if selected else "  "
            current = option.value == self._current_value
            state = option.state or ("current" if current else "")
            label = terminal_safe_text(option.label, single_line=True)
            state = terminal_safe_text(state, single_line=True)
            row_budget = max(1, columns - cell_len(marker))
            suffix = ""
            if state and row_budget > 4:
                state_budget = min(
                    cell_len(state),
                    max(3, row_budget // 3),
                    max(1, row_budget - 4),
                )
                suffix = "  " + _fit_cell_text(state, state_budget)
            label_budget = max(1, row_budget - cell_len(suffix))
            label = _fit_cell_text(label, label_budget)
            fragments.append((style, marker))
            fragments.append(
                (f"{style} class:option".strip(), label)
            )
            if suffix:
                suffix_style = "class:current" if current and not selected else style
                fragments.append((suffix_style, suffix))
            fragments.append((style, "\n"))
        return FormattedText(fragments)

    def _render_detail(self) -> FormattedText:
        if not self._filtered:
            query = terminal_safe_text(self.filter_buffer.text, single_line=True)
            return FormattedText([("class:empty", f" No matches for {query!r}")])
        option = self._filtered[self._selected]
        description = terminal_safe_text(option.description)
        state = terminal_safe_text(option.state, single_line=True)
        fragments: list[tuple[str, str]] = []
        if description:
            fragments.append(("", " " + description))
        if state:
            if fragments:
                fragments.append(("", "\n"))
            fragments.append(("class:meta", " " + state))
        return FormattedText(fragments or [("class:muted", " ")])

    def _move(self, offset: int) -> None:
        if not self._filtered:
            return
        self._selected = (self._selected + offset) % len(self._filtered)
        self.application.invalidate()

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("up", eager=True)
        @bindings.add("c-p", eager=True)
        def previous(_: Any) -> None:
            self._move(-1)

        @bindings.add("down", eager=True)
        @bindings.add("c-n", eager=True)
        def next_(_: Any) -> None:
            self._move(1)

        @bindings.add("enter", eager=True)
        def accept(event: Any) -> None:
            event.app.exit(result=self._selected_option_value())

        @bindings.add("escape", eager=True)
        def escape(event: Any) -> None:
            if self.filter_buffer.text:
                self.filter_buffer.text = ""
                return
            event.app.exit(result=None)

        @bindings.add("c-c", eager=True)
        def cancel(event: Any) -> None:
            event.app.exit(result=None)
        return bindings


def _fit_cell_text(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if cell_len(value) <= width:
        return value
    if width == 1:
        return "…"
    return set_cell_size(value, width - 1).rstrip() + "…"
