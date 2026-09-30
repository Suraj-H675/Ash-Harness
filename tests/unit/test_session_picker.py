from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.output import DummyOutput

from ash.core.session import Message, Session, SessionSummary
from ash.ui.session_picker import SessionPicker


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
        pipe.send_text(" ")
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
    assert "openai/model\\x1b]0;owned\\x07\\x0aforged-model" in rendered


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
