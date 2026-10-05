from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.output import DummyOutput
from rich.cells import cell_len

from ash.core.session import Message, Session, SessionSummary
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

    def load_session(session_id: str) -> Session:
        loaded.append(session_id)
        raise AssertionError("search must not load transcripts")

    with create_pipe_input() as pipe:
        picker = SessionPicker(
            [_summary("first-id", "Frontend"), _summary("second-id", "Backend")],
            load_session=load_session,
            input=pipe,
            output=DummyOutput(),
        )
        pending = picker.run()
        pipe.send_text("backend\r")

        assert await pending == "second-id"
        assert loaded == []


@pytest.mark.asyncio
async def test_session_picker_can_match_transcript_search_without_loading_transcripts() -> None:
    loaded: list[str] = []
    queries: list[str] = []

    def load_session(session_id: str) -> Session:
        loaded.append(session_id)
        raise AssertionError("search must not load transcripts")

    def search_session_ids(query: str) -> tuple[str, ...]:
        queries.append(query)
        return ("first-id",) if query == "websocket reconnect" else ()

    with create_pipe_input() as pipe:
        picker = SessionPicker(
            [_summary("first-id", "Frontend"), _summary("second-id", "Backend")],
            load_session=load_session,
            search_session_ids=search_session_ids,
            input=pipe,
            output=DummyOutput(),
        )
        pending = picker.run()
        pipe.send_text("websocket reconnect\r")

        assert await pending == "first-id"
        assert queries[-1] == "websocket reconnect"
        assert loaded == []


@pytest.mark.asyncio
async def test_session_picker_transcript_search_failure_keeps_metadata_search_available() -> None:
    def search_session_ids(query: str) -> tuple[str, ...]:
        raise RuntimeError(f"search unavailable for {query}")

    with create_pipe_input() as pipe:
        picker = SessionPicker(
            [_summary("first-id", "Frontend"), _summary("second-id", "Backend")],
            search_session_ids=search_session_ids,
            input=pipe,
            output=DummyOutput(),
        )
        pending = picker.run()
        pipe.send_text("backend\r")

        assert await pending == "second-id"


@pytest.mark.asyncio
async def test_session_picker_previews_on_demand_and_cancels(tmp_path: Path) -> None:
    loaded: list[str] = []

    def load_session(session_id: str) -> Session:
        loaded.append(session_id)
        now = datetime.now(timezone.utc)
        return Session(
            session_id=session_id,
            project_path=str(tmp_path),
            created_at=now,
        )

    with create_pipe_input() as pipe:
        picker = SessionPicker(
            [_summary("first-id", "Frontend")],
            load_session=load_session,
            input=pipe,
            output=DummyOutput(),
        )
        pending = picker.run()
        pipe.send_bytes(b"\x00")
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


def test_session_picker_sanitizes_transcript_preview(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)

    def load_session(session_id: str) -> Session:
        return Session(
            session_id=session_id,
            project_path=str(tmp_path),
            created_at=now,
            messages=[
                Message(
                    role="assistant",
                    content="answer\x1b[2J\u202ehidden\u202c",
                    timestamp=now,
                )
            ],
        )

    picker = SessionPicker(
        [_summary("first-id", "Frontend")],
        load_session=load_session,
        output=DummyOutput(),
    )
    picker._toggle_preview()

    rendered = _plain(picker._render_preview())
    assert "\x1b[2J" not in rendered
    assert "\u202e" not in rendered
    assert "answer\\x1b[2J\\u202ehidden\\u202c" in rendered


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
