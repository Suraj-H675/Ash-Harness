import asyncio
from datetime import datetime, timedelta, timezone
import threading

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.output import DummyOutput
from rich.cells import cell_len

from ash.core.session import SessionPreviewMessage, SessionSummary
from ash.ui.session_picker import SessionPicker


class SizedDummyOutput(DummyOutput):
    def __init__(self, columns: int, rows: int = 24) -> None:
        self.columns = columns
        self.rows = rows

    def get_size(self) -> Size:
        return Size(rows=self.rows, columns=self.columns)


def _summary(session_id: str, title: str) -> SessionSummary:
    now = datetime.now(timezone.utc)
    return SessionSummary(
        session_id=session_id,
        project_path="/workspace",
        title=title,
        created_at=now - timedelta(hours=1),
        updated_at=now,
        message_count=4,
        model="openai/gpt-5",
    )


def _plain(value) -> str:
    return "".join(fragment[1] for fragment in to_formatted_text(value))


@pytest.mark.asyncio
async def test_session_picker_navigates_and_selects() -> None:
    with create_pipe_input() as pipe:
        picker = SessionPicker(
            [_summary("first-id", "Frontend"), _summary("second-id", "Backend")],
            input=pipe,
            output=DummyOutput(),
        )
        pending = picker.run()
        pipe.send_bytes(b"\x1b[B")
        pipe.send_text("\r")

        assert await pending == "second-id"


@pytest.mark.asyncio
async def test_session_picker_filters_without_loading_transcripts() -> None:
    loaded: list[str] = []

    def load_preview(session_id: str) -> tuple[SessionPreviewMessage, ...]:
        loaded.append(session_id)
        raise AssertionError("search must not load transcripts")

    with create_pipe_input() as pipe:
        picker = SessionPicker(
            [_summary("first-id", "Frontend"), _summary("second-id", "Backend")],
            load_preview=load_preview,
            input=pipe,
            output=DummyOutput(),
        )
        pending = asyncio.create_task(picker.run())
        await asyncio.sleep(0)
        pipe.send_text("backend\r")

        assert await pending == "second-id"
        assert loaded == []


@pytest.mark.asyncio
async def test_session_picker_can_match_transcript_search_without_loading_transcripts() -> None:
    loaded: list[str] = []
    queries: list[str] = []

    def search_sessions(query: str) -> tuple[SessionSummary, ...]:
        queries.append(query)
        return (_summary("first-id", "Frontend"),) if query == "websocket reconnect" else ()

    def load_preview(session_id: str) -> tuple[SessionPreviewMessage, ...]:
        loaded.append(session_id)
        raise AssertionError("search must not load transcripts")

    with create_pipe_input() as pipe:
        picker = SessionPicker(
            [_summary("first-id", "Frontend"), _summary("second-id", "Backend")],
            load_preview=load_preview,
            search_sessions=search_sessions,
            input=pipe,
            output=DummyOutput(),
        )
        pending = picker.run()
        pipe.send_text("websocket reconnect\r")

        assert await pending == "first-id"
        assert queries[-1] == "websocket reconnect"
        assert loaded == []


def test_session_picker_resets_selection_when_query_changes() -> None:
    picker = SessionPicker(
        [_summary("first-id", "Frontend"), _summary("second-id", "Backend")],
        output=DummyOutput(),
    )
    picker._move(1)

    picker.search_buffer.text = "e"

    assert picker._selected_session_id() == "first-id"


@pytest.mark.asyncio
async def test_session_picker_transcript_search_failure_keeps_metadata_search_available() -> None:
    def search_sessions(query: str) -> tuple[SessionSummary, ...]:
        raise RuntimeError(f"search unavailable for {query}")

    picker = SessionPicker(
        [_summary("first-id", "Frontend"), _summary("second-id", "Backend")],
        search_sessions=search_sessions,
        output=DummyOutput(),
    )
    picker.search_buffer.text = "backend"

    await picker._search_latest_query()

    assert picker._selected_session_id() == "second-id"


@pytest.mark.asyncio
async def test_session_picker_previews_on_demand_and_cancels() -> None:
    loaded: list[str] = []

    def load_preview(session_id: str) -> tuple[SessionPreviewMessage, ...]:
        loaded.append(session_id)
        return (SessionPreviewMessage(role="assistant", content="preview"),)

    with create_pipe_input() as pipe:
        picker = SessionPicker(
            [_summary("first-id", "Frontend")],
            load_preview=load_preview,
            input=pipe,
            output=DummyOutput(),
        )
        pending = asyncio.create_task(picker.run())
        await asyncio.sleep(0)
        pipe.send_bytes(b"\x00")
        for _ in range(100):
            if loaded:
                break
            await asyncio.sleep(0.01)
        pipe.send_bytes(b"\x03")

        assert await pending is None
        assert loaded == ["first-id"]


def test_session_picker_sanitizes_persisted_list_metadata() -> None:
    summary = _summary("first-id", "s\x1b[2J\u202ex\u202c\nf")
    summary.model = "openai/model\x1b]0;owned\x07\nforged-model"
    picker = SessionPicker([summary], output=DummyOutput())

    rendered = _plain(picker._render_list())

    assert "\x1b[2J" not in rendered
    assert "\x1b]0;" not in rendered
    assert "\u202e" not in rendered
    assert rendered.count("\n") == 1
    assert "s\\x1b" in rendered
    assert "openai/model\\x1b]0;owned" in rendered


@pytest.mark.asyncio
async def test_session_picker_sanitizes_and_redacts_transcript_preview() -> None:
    secret = "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz"

    def load_preview(_session_id: str) -> tuple[SessionPreviewMessage, ...]:
        return (
            SessionPreviewMessage(
                role="assistant",
                content=f"answer\x1b[2J\u202ehidden\u202c {secret}",
            ),
        )

    with create_pipe_input() as pipe:
        picker = SessionPicker(
            [_summary("first-id", "Frontend")],
            load_preview=load_preview,
            input=pipe,
            output=DummyOutput(),
        )
        pending = asyncio.create_task(picker.run())
        await asyncio.sleep(0)
        pipe.send_bytes(b"\x00")
        for _ in range(100):
            rendered = _plain(picker._render_preview())
            if "answer" in rendered:
                break
            await asyncio.sleep(0.01)
        pipe.send_bytes(b"\x03")
        assert await pending is None

    assert "\x1b[2J" not in rendered
    assert "\u202e" not in rendered
    assert "answer\\x1b[2J\\u202ehidden\\u202c" in rendered
    assert "sk-proj-" not in rendered


def test_session_picker_uses_ash_identity_and_cell_safe_narrow_rows(
    monkeypatch,
) -> None:
    summary = _summary("abcdefgh-1234", "会議👨‍💻-extremely-long-session-title")
    summary.model = "custom/非常に長いモデル名-with-extra-suffix"
    output = SizedDummyOutput(columns=34)
    picker = SessionPicker([summary], output=output)
    monkeypatch.setattr(
        "ash.ui.session_picker.get_app_or_none",
        lambda: picker.application,
    )

    title = _plain(picker.application.layout.container.children[0].content.text)
    row = _plain(picker._render_list()).rstrip("\n")

    assert title.startswith("ASH  ·  Resume session")
    assert cell_len(row) <= 34
    assert "会議" in row or "👨‍💻" in row


@pytest.mark.parametrize("columns", [1, 2, 3, 4, 6, 8, 12, 18, 28, 34])
def test_session_picker_prioritizes_titles_and_fits_tiny_terminals(
    monkeypatch, columns
) -> None:
    picker = SessionPicker(
        [_summary("first-id", "Frontend work")],
        output=SizedDummyOutput(columns=columns),
    )
    monkeypatch.setattr(
        "ash.ui.session_picker.get_app_or_none",
        lambda: picker.application,
    )

    row = _plain(picker._render_list()).rstrip("\n")
    assert cell_len(row) <= columns
    assert row.startswith(">")
    if 4 <= columns <= 12:
        assert "F" in row
        assert "msg" not in row


@pytest.mark.asyncio
async def test_session_picker_redacts_and_bounds_preview_load_errors() -> None:
    secret = "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz"

    def fail_preview(_session_id: str) -> tuple[SessionPreviewMessage, ...]:
        raise RuntimeError("backend failed \x1b[2J " + secret + " X" * 500)

    picker = SessionPicker(
        [_summary("first-id", "Frontend work")],
        load_preview=fail_preview,
        output=DummyOutput(),
    )
    picker._previewed_id = "first-id"
    picker._preview_generation = 1

    await picker._load_preview_for_selection()
    rendered = _plain(picker._render_preview())

    assert rendered.startswith("Could not load preview: backend failed")
    assert "sk-proj-" not in rendered
    assert "\x1b[2J" not in rendered
    assert "\\x1b[2J" in rendered
    assert len(rendered) <= 350


@pytest.mark.asyncio
async def test_session_picker_enter_does_not_wait_for_search_when_metadata_match_is_visible() -> None:
    search_started = threading.Event()
    release_search = threading.Event()

    def search_sessions(_query: str) -> tuple[SessionSummary, ...]:
        search_started.set()
        release_search.wait(timeout=2)
        return ()

    with create_pipe_input() as pipe:
        picker = SessionPicker(
            [_summary("frontend-id", "Frontend")],
            search_sessions=search_sessions,
            input=pipe,
            output=DummyOutput(),
        )
        pending = asyncio.create_task(picker.run())
        await asyncio.sleep(0)
        pipe.send_text("slow")
        try:
            assert await asyncio.wait_for(
                asyncio.to_thread(search_started.wait, 1), 1
            )
            picker.search_buffer.text = "frontend"
            pipe.send_text("\r")
            assert await asyncio.wait_for(pending, 0.2) == "frontend-id"
        finally:
            release_search.set()


@pytest.mark.asyncio
async def test_session_picker_enter_does_not_wait_for_search_after_clearing_query() -> None:
    search_started = threading.Event()
    release_search = threading.Event()

    def search_sessions(_query: str) -> tuple[SessionSummary, ...]:
        search_started.set()
        release_search.wait(timeout=2)
        return ()

    with create_pipe_input() as pipe:
        picker = SessionPicker(
            [_summary("recent-id", "Recent")],
            search_sessions=search_sessions,
            input=pipe,
            output=DummyOutput(),
        )
        pending = asyncio.create_task(picker.run())
        await asyncio.sleep(0)
        pipe.send_text("slow")
        try:
            assert await asyncio.wait_for(
                asyncio.to_thread(search_started.wait, 1), 1
            )
            picker.search_buffer.text = ""
            pipe.send_text("\r")
            assert await asyncio.wait_for(pending, 0.2) == "recent-id"
        finally:
            release_search.set()


@pytest.mark.asyncio
async def test_session_picker_enter_rechecks_metadata_after_query_changes() -> None:
    search_started = threading.Event()
    release_search = threading.Event()

    def search_sessions(_query: str) -> tuple[SessionSummary, ...]:
        search_started.set()
        release_search.wait(timeout=2)
        return ()

    with create_pipe_input() as pipe:
        picker = SessionPicker(
            [_summary("frontend-id", "Frontend")],
            search_sessions=search_sessions,
            input=pipe,
            output=DummyOutput(),
        )
        pending = asyncio.create_task(picker.run())
        await asyncio.sleep(0)
        pipe.send_text("slow\r")
        try:
            assert await asyncio.wait_for(
                asyncio.to_thread(search_started.wait, 1), 1
            )
            picker.search_buffer.text = "frontend"
            assert await asyncio.wait_for(pending, 0.2) == "frontend-id"
        finally:
            release_search.set()


@pytest.mark.asyncio
async def test_session_picker_repeated_enter_exits_only_once_during_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archived = _summary("archived-id", "Archived")
    with create_pipe_input() as pipe:
        picker = SessionPicker(
            [_summary("recent-id", "Recent")],
            search_sessions=lambda _query: (archived,),
            input=pipe,
            output=DummyOutput(),
        )
        exit_calls: list[str | None] = []
        original_exit = picker.application.exit

        def record_exit(*, result: str | None = None) -> None:
            exit_calls.append(result)
            original_exit(result=result)

        monkeypatch.setattr(picker.application, "exit", record_exit)
        pending = asyncio.create_task(picker.run())
        await asyncio.sleep(0)
        pipe.send_text("needle\r\r")

        assert await asyncio.wait_for(pending, 2) == archived.session_id
        assert exit_calls == [archived.session_id]


@pytest.mark.asyncio
async def test_session_picker_search_keeps_loop_live_and_escape_responsive() -> None:
    started = threading.Event()
    release = threading.Event()
    queries: list[str] = []

    def search_sessions(query: str) -> tuple[SessionSummary, ...]:
        queries.append(query)
        started.set()
        release.wait(timeout=2)
        return ()

    with create_pipe_input() as pipe:
        picker = SessionPicker(
            [_summary("recent-id", "Recent")],
            search_sessions=search_sessions,
            input=pipe,
            output=DummyOutput(),
        )
        pending = asyncio.create_task(picker.run())
        await asyncio.sleep(0)
        picker.search_buffer.text = "s"
        picker.search_buffer.text = "slow query"
        try:
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), 1)
            assert queries == ["slow query"]
            heartbeat = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_later(0.02, heartbeat.set_result, True)
            assert await asyncio.wait_for(heartbeat, 0.2)

            pipe.send_bytes(b"\x1b")
            assert await asyncio.wait_for(pending, 1.0) is None
        finally:
            release.set()


@pytest.mark.asyncio
async def test_session_picker_discards_stale_preview_after_selection_moves() -> None:
    old_started = threading.Event()
    old_finished = threading.Event()
    release_old = threading.Event()
    new_started = threading.Event()
    active_lock = threading.Lock()
    active_loads = 0
    maximum_active_loads = 0

    def load_preview(session_id: str) -> tuple[SessionPreviewMessage, ...]:
        nonlocal active_loads, maximum_active_loads
        with active_lock:
            active_loads += 1
            maximum_active_loads = max(maximum_active_loads, active_loads)
        try:
            if session_id == "first-id":
                old_started.set()
                release_old.wait(timeout=2)
                old_finished.set()
                content = "stale preview"
            else:
                new_started.set()
                content = "fresh preview"
            return (SessionPreviewMessage(role="assistant", content=content),)
        finally:
            with active_lock:
                active_loads -= 1

    with create_pipe_input() as pipe:
        picker = SessionPicker(
            [_summary("first-id", "First"), _summary("second-id", "Second")],
            load_preview=load_preview,
            input=pipe,
            output=DummyOutput(),
        )
        pending = asyncio.create_task(picker.run())
        await asyncio.sleep(0)
        pipe.send_bytes(b"\x00")
        try:
            assert await asyncio.wait_for(asyncio.to_thread(old_started.wait, 1), 1)
            pipe.send_bytes(b"\x1b[B\x00")
            assert not await asyncio.to_thread(new_started.wait, 0.05)
            release_old.set()
            assert await asyncio.wait_for(asyncio.to_thread(new_started.wait, 1), 1)
            for _ in range(100):
                rendered = _plain(picker._render_preview())
                if "fresh preview" in rendered:
                    break
                await asyncio.sleep(0.01)
            assert "fresh preview" in rendered

            assert await asyncio.wait_for(
                asyncio.to_thread(old_finished.wait, 1), 1
            )
            await asyncio.sleep(0.05)
            assert maximum_active_loads == 1
            assert "fresh preview" in _plain(picker._render_preview())
            assert "stale preview" not in _plain(picker._render_preview())
            pipe.send_bytes(b"\x03")
            assert await pending is None
        finally:
            release_old.set()


@pytest.mark.asyncio
async def test_session_picker_discards_stale_search_results() -> None:
    old_started = threading.Event()
    old_finished = threading.Event()
    release_old = threading.Event()

    def search_sessions(query: str) -> tuple[SessionSummary, ...]:
        if query == "old":
            old_started.set()
            release_old.wait(timeout=2)
            old_finished.set()
            return (_summary("stale-id", "Stale"),)
        if query == "new":
            return (_summary("fresh-id", "Fresh"),)
        return ()

    with create_pipe_input() as pipe:
        picker = SessionPicker(
            [_summary("recent-id", "Recent")],
            search_sessions=search_sessions,
            input=pipe,
            output=DummyOutput(),
        )
        pending = asyncio.create_task(picker.run())
        await asyncio.sleep(0)
        pipe.send_text("old")
        try:
            assert await asyncio.wait_for(asyncio.to_thread(old_started.wait, 1), 1)
            picker.search_buffer.text = "new"
            release_old.set()
            assert await asyncio.wait_for(
                asyncio.to_thread(old_finished.wait, 1), 1
            )
            for _ in range(100):
                if any(item.session_id == "fresh-id" for item in picker._filtered):
                    break
                await asyncio.sleep(0.01)

            assert [item.session_id for item in picker._filtered] == ["fresh-id"]
            pipe.send_bytes(b"\x03")
            assert await pending is None
        finally:
            release_old.set()


@pytest.mark.parametrize("columns", [1, 4, 8, 12, 20, 34, 80])
def test_session_picker_footer_fits_terminal_width(monkeypatch, columns) -> None:
    picker = SessionPicker(
        [_summary("first-id", "Frontend")],
        output=SizedDummyOutput(columns=columns),
    )
    monkeypatch.setattr("ash.ui.session_picker.get_app_or_none", lambda: picker.application)

    footer = _plain(picker._render_footer())
    assert cell_len(footer) <= columns
    if columns < 3:
        assert footer == ""
