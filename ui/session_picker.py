"""Searchable prompt-toolkit session picker."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
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

from core.session import Session, SessionSummary


def _relative_time(value: datetime) -> str:
    now = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    seconds = max(0, int((now - value.astimezone(timezone.utc)).total_seconds()))
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    if seconds < 86400 * 30:
        return f"{seconds // 86400}d"
    return value.astimezone().strftime("%Y-%m-%d")


class SessionPicker:
    """Full-screen session selector with incremental metadata filtering."""

    def __init__(
        self,
        sessions: Sequence[SessionSummary],
        *,
        load_session: Callable[[str], Session] | None = None,
        initial_query: str = "",
        input: Input | None = None,
        output: Output | None = None,
    ) -> None:
        self._sessions = tuple(sessions)
        self._load_session = load_session
        self._filtered = list(self._sessions)
        self._selected = 0
        self._previewed_id: str | None = None
        self._preview_text = ""
        self.search_buffer = Buffer(multiline=False)
        self.search_buffer.on_text_changed += self._on_query_changed

        title = Window(
            FormattedTextControl(
                FormattedText(
                    [
                        ("class:title", "Resume session"),
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
                Window(
                    BufferControl(buffer=self.search_buffer),
                    height=1,
                ),
            ]
        )
        self._list_control = FormattedTextControl(self._render_list)
        self._preview_control = FormattedTextControl(self._render_preview)
        root = HSplit(
            [
                title,
                search,
                Window(height=1, char="─", style="class:separator"),
                Window(self._list_control, wrap_lines=False),
                Window(
                    self._preview_control,
                    height=Dimension(min=2, max=6),
                    wrap_lines=True,
                    style="class:preview",
                ),
                Window(
                    FormattedTextControl(
                        " ↑/↓ navigate  Enter resume  Space preview  Esc cancel "
                    ),
                    height=1,
                    style="class:footer",
                ),
            ]
        )
        self.application: Application[str | None] = Application(
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
                    "session-title": "bold",
                    "meta": "#808080",
                    "separator": "#444444",
                    "preview": "bg:#1c1c1c #d0d0d0",
                    "footer": "bg:#262626 #bcbcbc",
                    "empty": "italic #808080",
                }
            ),
            input=input,
            output=output,
        )
        if initial_query:
            self.search_buffer.text = initial_query

    async def run(self) -> str | None:
        return await self.application.run_async()

    def _on_query_changed(self, _: Buffer) -> None:
        terms = self.search_buffer.text.casefold().split()
        self._filtered = [
            session
            for session in self._sessions
            if all(term in self._search_text(session) for term in terms)
        ]
        self._selected = 0
        self._clear_preview()
        self.application.invalidate()

    @staticmethod
    def _search_text(session: SessionSummary) -> str:
        return " ".join(
            (
                session.session_id,
                session.title,
                session.model,
                session.project_path,
            )
        ).casefold()

    def _page(self) -> tuple[int, int]:
        app = get_app_or_none()
        rows = app.output.get_size().rows if app is self.application else 24
        page_size = max(1, rows - 9)
        start = min(
            max(0, self._selected - page_size + 1),
            max(0, len(self._filtered) - page_size),
        )
        return start, start + page_size

    def _render_list(self) -> FormattedText:
        if not self._filtered:
            return FormattedText([("class:empty", " No matching sessions")])
        app = get_app_or_none()
        columns = app.output.get_size().columns if app is self.application else 80
        fragments: list[tuple[str, str]] = []
        start, end = self._page()
        for index, session in enumerate(self._filtered[start:end], start=start):
            style = "class:selected" if index == self._selected else ""
            title = session.title or "(untitled)"
            model = session.model or "unknown model"
            suffix = (
                f"  {session.message_count} msg  {_relative_time(session.updated_at)}  "
                f"{model}  {session.session_id[:8]}"
            )
            available = max(8, columns - len(suffix) - 4)
            if len(title) > available:
                title = title[: max(1, available - 1)] + "…"
            fragments.extend(
                [
                    (style, "> " if index == self._selected else "  "),
                    (f"{style} class:session-title".strip(), title),
                    (f"{style} class:meta".strip(), suffix),
                    (style, "\n"),
                ]
            )
        return FormattedText(fragments)

    def _render_preview(self) -> FormattedText:
        if not self._previewed_id:
            return FormattedText(
                [("class:muted", " Space previews the selected transcript")]
            )
        return FormattedText([("", self._preview_text or "No transcript messages")])

    def _clear_preview(self) -> None:
        self._previewed_id = None
        self._preview_text = ""

    def _toggle_preview(self) -> None:
        if not self._filtered:
            return
        session_id = self._filtered[self._selected].session_id
        if self._previewed_id == session_id:
            self._clear_preview()
            return
        self._previewed_id = session_id
        if self._load_session is None:
            self._preview_text = "Transcript preview unavailable"
            return
        try:
            session = self._load_session(session_id)
        except Exception as exc:  # noqa: BLE001
            self._preview_text = f"Could not load preview: {exc}"
            return
        messages = [
            f"{message.role}: {message.content}"
            for message in session.messages[-3:]
            if message.content
        ]
        text = "\n".join(messages)
        self._preview_text = text[:1200] + ("…" if len(text) > 1200 else "")

    def _move(self, offset: int) -> None:
        if not self._filtered:
            return
        self._selected = (self._selected + offset) % len(self._filtered)
        self._clear_preview()
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
            selected = (
                self._filtered[self._selected].session_id if self._filtered else None
            )
            event.app.exit(result=selected)

        @bindings.add(" ", eager=True)
        def preview(event: Any) -> None:
            self._toggle_preview()
            event.app.invalidate()

        @bindings.add("escape", eager=True)
        @bindings.add("c-c", eager=True)
        def cancel(event: Any) -> None:
            event.app.exit(result=None)

        return bindings
