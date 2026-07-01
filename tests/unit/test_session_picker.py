from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from core.session import Session, SessionSummary
from ui.session_picker import SessionPicker


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
