from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ash.cli import main
from cli.sessions import (
    list_session_summaries,
    render_session_summaries,
    render_session_tree,
    select_startup_session,
)
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


def test_sessions_cli_renders_branch_tree_by_title(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    store = SessionStore(db_dir / "sessions.db")
    root = store.create_session(str(tmp_path))
    store.rename_session(root.session_id, "feature work")
    child = store.fork_session(root.session_id, branch_name="alternative")
    monkeypatch.chdir(tmp_path)

    status = main(
        [
            "--db-directory",
            str(db_dir),
            "sessions",
            "tree",
            "--session",
            "FEATURE WORK",
            "--json",
        ]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert [node["session_id"] for node in payload["sessions"]] == [
        root.session_id,
        child.session_id,
    ]
    assert payload["sessions"][1]["branch_name"] == "alternative"
    assert "alternative" in render_session_tree(store.session_tree(root.session_id))


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


def test_sessions_cli_rejects_tree_only_session_option(tmp_path: Path, capsys) -> None:
    db_dir = tmp_path / "db"
    SessionStore(db_dir / "sessions.db")

    status = main(
        [
            "--db-directory",
            str(db_dir),
            "sessions",
            "list",
            "--session",
            "unused",
        ]
    )

    assert status == 2
    assert "requires 'sessions tree'" in capsys.readouterr().err


def test_startup_continue_selects_latest_project_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    first = store.create_session(str(tmp_path))
    store.create_session(str(tmp_path))
    store.rename_session(first.session_id, "most recently touched")

    selection = asyncio.run(
        select_startup_session(
            store,
            project_path=str(tmp_path),
            continue_session=True,
        )
    )

    assert selection.session_id == first.session_id
    assert selection.cancelled is False


def test_startup_resume_supports_name_and_fork(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    original = store.create_session(str(tmp_path))
    store.rename_session(original.session_id, "auth refactor")

    selection = asyncio.run(
        select_startup_session(
            store,
            project_path=str(tmp_path),
            resume="AUTH REFACTOR",
            fork_session=True,
        )
    )

    assert selection.session_id != original.session_id
    assert store.load_session(selection.session_id).title == "auth refactor (fork)"


def test_bare_resume_requires_tty_and_honors_picker_cancel(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.create_session(str(tmp_path))

    with pytest.raises(ValueError, match="interactive terminal"):
        asyncio.run(
            select_startup_session(
                store,
                project_path=str(tmp_path),
                resume="",
                interactive=False,
            )
        )

    async def cancel() -> None:
        return None

    selection = asyncio.run(
        select_startup_session(
            store,
            project_path=str(tmp_path),
            resume="",
            interactive=True,
            picker=cancel,
        )
    )
    assert selection.cancelled is True


def test_continue_reports_empty_project(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")

    with pytest.raises(ValueError, match="no session found"):
        asyncio.run(
            select_startup_session(
                store,
                project_path=str(tmp_path),
                continue_session=True,
            )
        )
