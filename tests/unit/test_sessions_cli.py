from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ash.cli import main
from cli.sessions import list_session_summaries, render_session_summaries
from core.session import Message, SessionStore


def test_session_summary_renderer_emits_json(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path), model="openai/gpt-5.2")
    store.rename_session(session.session_id, "Feature Work")
    store.save_message(
        session.session_id,
        Message(role="user", content="hello", timestamp=datetime.now(timezone.utc)),
    )

    summaries = list_session_summaries(store, project_path=str(tmp_path))
    payload = json.loads(render_session_summaries(summaries, json_output=True))

    assert payload["sessions"][0]["session_id"] == session.session_id
    assert payload["sessions"][0]["title"] == "Feature Work"
    assert payload["sessions"][0]["message_count"] == 1
    assert payload["sessions"][0]["model"] == "openai/gpt-5.2"


def test_sessions_cli_lists_current_project_sessions(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    store = SessionStore(db_dir / "sessions.db")
    current = store.create_session(str(tmp_path), model="anthropic/claude-sonnet-4-6")
    store.rename_session(current.session_id, "Current Project")
    other = store.create_session(str(tmp_path / "other"))
    monkeypatch.chdir(tmp_path)

    status = main(
        [
            "--db-directory",
            str(db_dir),
            "sessions",
            "--json",
        ]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    ids = {session["session_id"] for session in payload["sessions"]}
    assert ids == {current.session_id}
    assert other.session_id not in ids


def test_sessions_cli_filters_query_and_all_projects(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    store = SessionStore(db_dir / "sessions.db")
    first = store.create_session(str(tmp_path))
    second = store.create_session(str(tmp_path / "other"))
    store.rename_session(first.session_id, "frontend fix")
    store.rename_session(second.session_id, "backend fix")
    monkeypatch.chdir(tmp_path)

    status = main(
        [
            "--db-directory",
            str(db_dir),
            "sessions",
            "list",
            "--all-projects",
            "--query",
            "backend",
            "--json",
        ]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert [session["session_id"] for session in payload["sessions"]] == [
        second.session_id
    ]


def test_sessions_cli_rejects_invalid_limit(tmp_path: Path, capsys) -> None:
    db_dir = tmp_path / "db"
    SessionStore(db_dir / "sessions.db")

    status = main(
        [
            "--db-directory",
            str(db_dir),
            "sessions",
            "--limit",
            "0",
        ]
    )

    assert status == 2
    assert "limit must be positive" in capsys.readouterr().err
