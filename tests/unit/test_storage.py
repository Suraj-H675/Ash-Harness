from __future__ import annotations

import sqlite3
import json
from pathlib import Path

import pytest

from ash.commands.storage import (
    backup_database,
    check_database,
    render_storage_check,
    restore_database,
)
from ash.core.session import SessionStorageError, SessionStore
from ash.cli import main


def test_storage_check_does_not_create_missing_database(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    check = check_database(path)
    assert check.exists is False
    assert check.ok is False
    assert not path.exists()
    assert '"ok": false' in render_storage_check(check, json_output=True)


def test_storage_cli_honors_database_directory_override(tmp_path: Path, capsys) -> None:
    assert (
        main(
            [
                "--db-directory",
                str(tmp_path),
                "storage",
                "check",
                "--json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == str(tmp_path / "sessions.db")
    assert not (tmp_path / "sessions.db").exists()


def test_storage_check_detects_corrupt_database(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    path.write_bytes(b"not sqlite")
    check = check_database(path)
    assert check.exists is True
    assert check.ok is False
    assert check.messages


def test_backup_and_restore_preserve_current_database(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    store = SessionStore(path)
    original = store.create_session("/original")
    backup = backup_database(path, tmp_path / "known-good.db")
    replacement = store.create_session("/replacement")

    restored, preserved = restore_database(path, backup, confirmed=True)

    assert restored == path
    sessions = SessionStore(path).list_sessions(limit=10)
    assert {item.session_id for item in sessions} == {original.session_id}
    assert replacement.session_id not in {item.session_id for item in sessions}
    assert len(preserved) >= 1
    assert all(item.exists() for item in preserved)


def test_restore_refuses_unconfirmed_or_invalid_backup(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    SessionStore(path)
    backup = tmp_path / "broken.db"
    backup.write_bytes(b"broken")
    with pytest.raises(SessionStorageError, match="confirmation"):
        restore_database(path, backup, confirmed=False)
    with pytest.raises(SessionStorageError, match="unhealthy backup"):
        restore_database(path, backup, confirmed=True)


def test_storage_check_reports_newer_schema_as_unsupported(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL
            );
            INSERT INTO schema_migrations VALUES (999, CURRENT_TIMESTAMP);
            """
        )
    check = check_database(path)
    assert check.ok is False
    assert check.schema_version == 999
    assert "newer" in " ".join(check.messages)
