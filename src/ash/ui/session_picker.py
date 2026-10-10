"""Searchable prompt-toolkit session picker."""

from __future__ import annotations

import asyncio
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
from rich.cells import cell_len, set_cell_size
from ash.core.session import SessionPreviewMessage, SessionSummary
from ash.core.redaction import redact_text
from ash.ui.safe_text import terminal_safe_text
from ash.ui.theme import get_theme, overlay_styles, prompt_style


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
        load_preview: Callable[[str], Sequence[SessionPreviewMessage]] | None = None,
        search_sessions: Callable[[str], Sequence[SessionSummary]] | None = None,
        initial_query: str = "",
        theme: str = "dark",
        no_color: bool = False,
        input: Input | None = None,
        output: Output | None = None,
    ) -> None:
        self._sessions = tuple(sessions)
        self._load_preview = load_preview
        self._search_sessions = search_sessions
        self._content_matches: tuple[SessionSummary, ...] = ()
        self._filtered = list(self._sessions)
        self._selected = 0
        self._query = ""
        self._search_generation = 0
        self._query_changed_event = asyncio.Event()
        self._search_task: asyncio.Task[None] | None = None
        self._active = False
        self._accept_pending = False
        self._previewed_id: str | None = None
        self._preview_text = ""
        self._preview_generation = 0
        self._preview_task: asyncio.Task[None] | None = None
        self.search_buffer = Buffer(multiline=False)
        self.search_buffer.on_text_changed += self._on_query_changed

        title = Window(
            FormattedTextControl(
                FormattedText(
                    [
                        ("class:title", "ASH"),
                        ("class:muted", "  ·  "),
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
                    FormattedTextControl(self._render_footer),
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
            style=prompt_style(
                overlay_styles(get_theme(theme)),
                no_color=no_color,
            ),
            input=input,
            output=output,
        )
        if initial_query:
            self.search_buffer.text = initial_query

    async def run(self) -> str | None:
        self._active = True
        self._ensure_search_task()
        try:
            return await self.application.run_async()
        finally:
            self._active = False

    def _on_query_changed(self, _: Buffer) -> None:
        self._query = " ".join(self.search_buffer.text.casefold().split())
        self._search_generation += 1
        self._query_changed_event.set()
        self._content_matches = ()
        self._apply_filter()
        self._clear_preview()
        self._ensure_search_task()
        self.application.invalidate()

    def _ensure_search_task(self) -> None:
        if (
            not self._active
            or not self._query
            or self._search_sessions is None
            or (self._search_task is not None and not self._search_task.done())
        ):
            return
        self._search_task = self.application.create_background_task(
            self._search_latest_query()
        )

    async def _search_latest_query(self) -> None:
        current_task = asyncio.current_task()
        try:
            while True:
                generation = self._search_generation
                query = self._query
                if not query or self._search_sessions is None:
                    return
                await asyncio.sleep(0.1)
                if generation != self._search_generation:
                    continue
                try:
                    matches = await asyncio.to_thread(self._search_sessions, query)
                except Exception:  # noqa: BLE001 - transcript search is best-effort
                    matches = ()
                if generation != self._search_generation:
                    continue
                selected_id = self._selected_session_id()
                self._content_matches = tuple(matches)
                self._apply_filter(preferred_session_id=selected_id)
                self.application.invalidate()
                return
        finally:
            if self._search_task is current_task:
                self._search_task = None

    def _apply_filter(self, *, preferred_session_id: str | None = None) -> None:
        terms = self._query.split()
        metadata_matches = [
            session
            for session in self._sessions
            if all(term in self._search_text(session) for term in terms)
        ]
        seen = {session.session_id for session in metadata_matches}
        self._filtered = metadata_matches + [
            session
            for session in self._content_matches
            if session.session_id not in seen
        ]
        self._selected = 0
        if preferred_session_id is not None:
            for index, session in enumerate(self._filtered):
                if session.session_id == preferred_session_id:
                    self._selected = index
                    break

    def _selected_session_id(self) -> str | None:
        if not self._filtered:
            return None
        return self._filtered[min(self._selected, len(self._filtered) - 1)].session_id

    def _selected_session_matches_metadata(self) -> bool:
        session_id = self._selected_session_id()
        terms = self._query.split()
        return any(
            session.session_id == session_id
            and all(term in self._search_text(session) for term in terms)
            for session in self._sessions
        )

    @staticmethod
    def _search_text(session: SessionSummary) -> str:
        return " ".join(
            (
                session.session_id,
                session.title,
                session.model,
                session.project_path,
                session.context_summary,
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
            marker = ("> " if index == self._selected else "  ")[:columns]
            title = terminal_safe_text(session.title or "(untitled)", single_line=True)
            model = terminal_safe_text(
                session.model or "unknown model", single_line=True
            )
            session_id = terminal_safe_text(session.session_id[:8], single_line=True)
            metadata = (
                f"{session.message_count} msg  {_relative_time(session.updated_at)}  "
                f"{model}  {session_id}"
            )
            row_budget = max(0, columns - cell_len(marker))
            if row_budget >= 16:
                metadata_budget = min(
                    cell_len(metadata),
                    max(8, row_budget // 2),
                    row_budget - 6,
                )
                metadata = _fit_cell_text(metadata, metadata_budget)
            else:
                metadata = ""
            separator = "  " if metadata else ""
            title_budget = max(0, row_budget - cell_len(separator) - cell_len(metadata))
            title = _fit_cell_text(title, title_budget)
            fragments.extend(
                [
                    (style, marker),
                    (f"{style} class:session-title".strip(), title),
                    (style, separator),
                    (f"{style} class:meta".strip(), metadata),
                    (style, "\n"),
                ]
            )
        return FormattedText(fragments)

    def _render_preview(self) -> FormattedText:
        if not self._previewed_id:
            return FormattedText(
                [("class:muted", " Ctrl-Space previews the selected transcript")]
            )
        return FormattedText([("", self._preview_text or "No transcript messages")])

    def _clear_preview(self) -> None:
        self._preview_generation += 1
        self._previewed_id = None
        self._preview_text = ""

    def _toggle_preview(self) -> None:
        if not self._filtered:
            return
        session_id = self._filtered[self._selected].session_id
        if self._previewed_id == session_id:
            self._clear_preview()
            return
        self._clear_preview()
        self._previewed_id = session_id
        if self._load_preview is None:
            self._preview_text = "Transcript preview unavailable"
            return
        self._preview_text = "Loading preview…"
        self._ensure_preview_task()

    def _ensure_preview_task(self) -> None:
        if (
            not self._active
            or self._previewed_id is None
            or self._load_preview is None
            or (self._preview_task is not None and not self._preview_task.done())
        ):
            return
        self._preview_task = self.application.create_background_task(
            self._load_preview_for_selection()
        )

    async def _load_preview_for_selection(self) -> None:
        current_task = asyncio.current_task()
        try:
            while True:
                session_id = self._previewed_id
                generation = self._preview_generation
                load_preview = self._load_preview
                if session_id is None or load_preview is None:
                    return
                try:
                    messages = await asyncio.to_thread(load_preview, session_id)
                except Exception as exc:  # noqa: BLE001 - preview reads are best-effort
                    preview_text = "Could not load preview: " + terminal_safe_text(
                        redact_text(str(exc))[:300], single_line=True
                    )
                else:
                    messages_text = [
                        f"{terminal_safe_text(str(message.role), single_line=True)}: "
                        f"{terminal_safe_text(redact_text(message.content))}"
                        for message in messages
                        if message.content
                    ]
                    text = "\n".join(messages_text)
                    preview_text = text[:1200] + (
                        "…" if len(text) > 1200 else ""
                    )
                if (
                    generation != self._preview_generation
                    or self._previewed_id != session_id
                    or self._selected_session_id() != session_id
                ):
                    continue
                self._preview_text = preview_text
                self.application.invalidate()
                return
        finally:
            if self._preview_task is current_task:
                self._preview_task = None

    def _move(self, offset: int) -> None:
        if not self._filtered:
            return
        self._selected = (self._selected + offset) % len(self._filtered)
        self._clear_preview()
        self.application.invalidate()

    def _render_footer(self) -> FormattedText:
        app = get_app_or_none()
        columns = app.output.get_size().columns if app is self.application else 80
        if columns < cell_len("Esc"):
            return FormattedText([])
        hints = (
            "↑/↓ navigate  Enter resume  Ctrl-Space preview  Esc cancel",
            "↑/↓ move  Enter resume  Ctrl-Space preview  Esc",
            "↑/↓  Enter resume  Ctrl-Space",
            "↑/↓  Enter  Ctrl-Space",
            "Enter  Esc",
            "Esc",
        )
        hint = next((item for item in hints if cell_len(item) <= columns), hints[-1])
        return FormattedText([("", _fit_cell_text(hint, columns))])

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
            if self._accept_pending:
                return
            self._accept_pending = True

            async def accept_after_search() -> None:
                try:
                    while self._query and not self._selected_session_matches_metadata():
                        search_task = self._search_task
                        if search_task is None:
                            break
                        self._query_changed_event.clear()
                        query_change_task = asyncio.create_task(
                            self._query_changed_event.wait()
                        )
                        try:
                            await asyncio.wait(
                                (search_task, query_change_task),
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                        finally:
                            if not query_change_task.done():
                                query_change_task.cancel()
                            await asyncio.gather(
                                query_change_task,
                                return_exceptions=True,
                            )
                    event.app.exit(result=self._selected_session_id())
                finally:
                    self._accept_pending = False

            event.app.create_background_task(accept_after_search())

        @bindings.add("c-space", eager=True)
        def preview(event: Any) -> None:
            self._toggle_preview()
            event.app.invalidate()

        @bindings.add("escape", eager=True)
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
