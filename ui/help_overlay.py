"""Searchable prompt-toolkit slash-command help overlay."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from prompt_toolkit.application import Application, get_app_or_none
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.input.base import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    BufferControl,
    FormattedTextControl,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.output.base import Output
from prompt_toolkit.styles import Style

from cli.slash import COMMANDS, SlashCommand


class HelpOverlay:
    """Full-screen searchable slash-command reference."""

    def __init__(
        self,
        commands: Sequence[SlashCommand] = COMMANDS,
        *,
        initial_query: str = "",
        input: Input | None = None,
        output: Output | None = None,
    ) -> None:
        self._commands = tuple(commands)
        initial_terms = initial_query.casefold().split()
        self._filtered = [
            command
            for command in self._commands
            if all(term in self._search_text(command) for term in initial_terms)
        ]
        self._selected = 0
        self.search_buffer = Buffer(multiline=False)
        self.search_buffer.on_text_changed += self._on_query_changed

        title = Window(
            FormattedTextControl(
                FormattedText(
                    [
                        ("class:title", "Slash commands"),
                        ("", "  "),
                        ("class:muted", "type to search"),
                    ]
                )
            ),
            height=1,
        )
        search = VSplit(
            [
                Window(
                    FormattedTextControl(FormattedText([("class:label", "Search ")])),
                    height=1,
                    width=7,
                ),
                Window(BufferControl(buffer=self.search_buffer), height=1),
            ]
        )
        self._list_control = FormattedTextControl(self._render_list)
        self._detail_control = FormattedTextControl(self._render_detail)
        root = HSplit(
            [
                title,
                search,
                Window(height=1, char="-", style="class:separator"),
                Window(self._list_control, wrap_lines=False),
                Window(
                    self._detail_control,
                    height=Dimension(min=4, max=8),
                    wrap_lines=True,
                    style="class:detail",
                ),
                Window(
                    FormattedTextControl(
                        " Up/Down navigate  Enter/Esc close  Ctrl-C cancel "
                    ),
                    height=1,
                    style="class:footer",
                ),
            ]
        )
        self.application: Application[None] = Application(
            layout=Layout(root, focused_element=self.search_buffer),
            key_bindings=self._key_bindings(),
            full_screen=True,
            erase_when_done=False,
            style=Style.from_dict(
                {
                    "title": "bold #5fd7ff",
                    "muted": "#808080",
                    "label": "bold #bcbcbc",
                    "selected": "bold bg:#005f87 #ffffff",
                    "usage": "bold",
                    "meta": "#808080",
                    "separator": "#444444",
                    "detail": "bg:#1c1c1c #d0d0d0",
                    "footer": "bg:#262626 #bcbcbc",
                    "empty": "italic #808080",
                }
            ),
            input=input,
            output=output,
        )
        if initial_query:
            self.search_buffer.text = initial_query

    async def run(self) -> None:
        await self.application.run_async()

    def _on_query_changed(self, _: Buffer) -> None:
        terms = self.search_buffer.text.casefold().split()
        self._filtered = [
            command
            for command in self._commands
            if all(term in self._search_text(command) for term in terms)
        ]
        self._selected = 0
        self.application.invalidate()

    @staticmethod
    def _search_text(command: SlashCommand) -> str:
        return " ".join(
            (
                command.name,
                command.description,
                command.usage,
                *command.aliases,
                *(f"/{alias}" for alias in command.aliases),
            )
        ).casefold()

    def _page(self) -> tuple[int, int]:
        app = get_app_or_none()
        rows = app.output.get_size().rows if app is self.application else 24
        page_size = max(1, rows - 10)
        start = min(
            max(0, self._selected - page_size + 1),
            max(0, len(self._filtered) - page_size),
        )
        return start, start + page_size

    def _render_list(self) -> FormattedText:
        if not self._filtered:
            return FormattedText([("class:empty", " No matching slash commands")])
        app = get_app_or_none()
        columns = app.output.get_size().columns if app is self.application else 80
        usage_width = min(42, max(len(command.usage) for command in self._filtered))
        fragments: list[tuple[str, str]] = []
        start, end = self._page()
        for index, command in enumerate(self._filtered[start:end], start=start):
            style = "class:selected" if index == self._selected else ""
            usage = command.usage
            description = command.description
            available = max(8, columns - usage_width - 6)
            if len(description) > available:
                description = description[: max(1, available - 1)] + "..."
            fragments.extend(
                [
                    (style, "> " if index == self._selected else "  "),
                    (f"{style} class:usage".strip(), f"{usage:<{usage_width}}"),
                    (style, "  "),
                    (f"{style} class:meta".strip(), description),
                    (style, "\n"),
                ]
            )
        return FormattedText(fragments)

    def _render_detail(self) -> FormattedText:
        if not self._filtered:
            query = self.search_buffer.text
            return FormattedText([("class:empty", f"No matches for {query!r}")])
        command = self._filtered[self._selected]
        fragments: list[tuple[str, str]] = [
            ("class:usage", command.usage),
            ("", "\n"),
            ("", command.description),
        ]
        if command.aliases:
            aliases = ", ".join(f"/{alias}" for alias in command.aliases)
            fragments.extend([("", "\n"), ("class:meta", f"Aliases: {aliases}")])
        fragments.extend(
            [
                ("", "\n"),
                ("class:meta", f"Command: /{command.name}"),
            ]
        )
        return FormattedText(fragments)

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
        @bindings.add("escape", eager=True)
        @bindings.add("c-c", eager=True)
        def close(event: Any) -> None:
            event.app.exit(result=None)

        return bindings


async def show_help_overlay(
    *,
    initial_query: str = "",
    input: Input | None = None,
    output: Output | None = None,
) -> None:
    """Open the full-screen slash help overlay."""

    await HelpOverlay(initial_query=initial_query, input=input, output=output).run()
