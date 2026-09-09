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


def test_backup_rejects_symlinked_destination(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    SessionStore(path).create_session("/workspace")
    victim = tmp_path / "victim.db"
    linked = tmp_path / "backup.db"
    try:
        linked.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(SessionStorageError, match="symlink or junction"):
        backup_database(path, linked)

    assert not victim.exists()


def test_restore_refuses_unconfirmed_or_invalid_backup(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    SessionStore(path)
    backup = tmp_path / "broken.db"
    backup.write_bytes(b"broken")
    with pytest.raises(SessionStorageError, match="confirmation"):
        restore_database(path, backup, confirmed=False)
    with pytest.raises(SessionStorageError, match="unhealthy backup"):
        restore_database(path, backup, confirmed=True)


def test_restore_rejects_linked_database_destination(tmp_path: Path) -> None:
    target = tmp_path / "target.db"
    target_store = SessionStore(target)
    current = target_store.create_session("/current")
    backup = tmp_path / "backup.db"
    backup_store = SessionStore(backup)
    replacement = backup_store.create_session("/backup")
    linked = tmp_path / "sessions.db"
    try:
        linked.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(SessionStorageError, match="symlink or junction"):
        restore_database(linked, backup, confirmed=True)

    session_ids = {
        item.session_id for item in SessionStore(target).list_sessions(limit=10)
    }
    assert current.session_id in session_ids
    assert replacement.session_id not in session_ids


def test_debug_bundle_is_bounded_json_and_restricted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands.storage import create_debug_bundle
    from ash.config import AshConfig

    workspace = tmp_path / "repo"
    workspace.mkdir()
    db_dir = tmp_path / "db"
    config = AshConfig(
        model="anthropic/claude-sonnet-4-6",
        workspace_root=workspace,
        db_directory=db_dir,
        memory_backend="off",
    )
    monkeypatch.chdir(workspace)
    destination = tmp_path / "bundle.json"

    created = create_debug_bundle(config, destination)

    payload = json.loads(created.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["ash"]["model"] == "anthropic/claude-sonnet-4-6"
    assert payload["storage"]["path"] == str(db_dir / "sessions.db")
    assert payload["runtime"]["workspace"] == str(workspace.resolve())
    assert oct(created.stat().st_mode & 0o777) in {"0o600", "0o644"}


def test_debug_bundle_rejects_symlinked_destination(tmp_path: Path) -> None:
    from ash.commands.storage import create_debug_bundle
    from ash.config import AshConfig

    workspace = tmp_path / "repo"
    workspace.mkdir()
    config = AshConfig(
        model="anthropic/claude-sonnet-4-6",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    victim = tmp_path / "victim.json"
    victim.write_text("keep", encoding="utf-8")
    linked = tmp_path / "bundle.json"
    try:
        linked.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(SessionStorageError, match="symlink or junction"):
        create_debug_bundle(config, linked)

    assert victim.read_text(encoding="utf-8") == "keep"


def test_metrics_cli_reports_local_only_aggregate(
    tmp_path: Path,
    capsys,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.save_session_token_stats(
        session.session_id,
        10,
        5,
        0.01,
        cache_read_tokens=2,
        cache_write_tokens=1,
        estimated_prompt_tokens=3,
        estimated_completion_tokens=2,
        estimated_cost_usd=0.002,
    )

    assert main(["--db-directory", str(tmp_path), "metrics"]) == 0
    output = capsys.readouterr().out
    assert "15 tokens" in output
    assert "$0.010000" in output

    assert main(["--db-directory", str(tmp_path), "metrics", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["telemetry"] == "local_only"
    assert payload["metrics"]["session_count"] == 1
    assert payload["metrics"]["total_tokens"] == 15
    assert payload["metrics"]["cost_usd"] == pytest.approx(0.01)


@pytest.mark.parametrize(
    "command",
    [
        ("metrics",),
        ("sessions",),
        ("plans", "list"),
        ("audit", "list", "--session", "missing"),
    ],
    ids=["metrics", "sessions", "plans", "audit"],
)
def test_session_cli_reports_corrupt_database_without_traceback(
    tmp_path: Path,
    capsys,
    command: tuple[str, ...],
) -> None:
    (tmp_path / "sessions.db").write_bytes(b"not sqlite")

    assert main(["--db-directory", str(tmp_path), *command]) == 1
    human = capsys.readouterr()
    assert human.out == ""
    assert "Error [storage]:" in human.err
    assert "Run `ash storage check`" in human.err
    assert "Traceback" not in human.err

    assert main(["--db-directory", str(tmp_path), *command, "--json"]) == 1
    structured = capsys.readouterr()
    assert structured.err == ""
    payload = json.loads(structured.out)
    assert payload["error"]["category"] == "storage"
    assert payload["error"]["exit_code"] == 1
    assert "file is not a database" in payload["error"]["message"]


def test_metrics_cli_classifies_store_operation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    database = tmp_path / "sessions.db"
    SessionStore(database)

    def fail_metrics(self: SessionStore) -> dict:
        raise SessionStorageError("database read failed")

    monkeypatch.setattr(SessionStore, "local_metrics_summary", fail_metrics)

    assert main(["--db-directory", str(tmp_path), "metrics", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "error": {
            "category": "storage",
            "exit_code": 1,
            "message": "database read failed",
            "remedy": (
                "Run `ash storage check`; if needed, create a backup and restore "
                "a known-good sessions database."
            ),
            "retriable": False,
        }
    }


def test_sessions_cli_classifies_corrupt_persisted_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    database = tmp_path / "sessions.db"
    session = SessionStore(database).create_session(str(tmp_path))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE sessions SET created_at = 'not-a-timestamp' "
            "WHERE session_id = ?",
            (session.session_id,),
        )

    assert main(["--db-directory", str(tmp_path), "sessions", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["error"]["category"] == "storage"
    assert "stored data is invalid" in payload["error"]["message"]


def test_plans_cli_classifies_corrupt_persisted_contract(
    tmp_path: Path,
    capsys,
) -> None:
    from ash.core.sprint import ChecklistItem, SprintContract, SprintExecution

    database = tmp_path / "sessions.db"
    store = SessionStore(database)
    session = store.create_session(str(tmp_path))
    execution = SprintExecution(contract=SprintContract(goal="corruptible"))
    execution.set_items([ChecklistItem(idx=1, section="test", description="item")])
    store.save_sprint(session.session_id, execution)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE sprints SET contract_json = 'not-json' WHERE sprint_id = ?",
            (execution.contract.contract_id,),
        )

    assert (
        main(
            [
                "--db-directory",
                str(tmp_path),
                "plans",
                "show",
                execution.contract.contract_id,
                "--json",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["error"]["category"] == "storage"
    assert "stored data is invalid" in payload["error"]["message"]


def test_session_cli_classifies_structurally_incomplete_sqlite(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES (11, 'now');
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                project_path TEXT NOT NULL,
                title TEXT,
                created_at TEXT,
                updated_at TEXT,
                model TEXT
            );
            """
        )

    assert main(["--db-directory", str(tmp_path), "sessions", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["error"]["category"] == "storage"


def test_session_cli_classifies_malformed_schema_metadata(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES ('bad', 'now');
            """
        )

    assert main(["--db-directory", str(tmp_path), "metrics", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["error"]["category"] == "storage"


def test_startup_storage_error_uses_headless_event_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    database = tmp_path / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES ('bad', 'now');
            """
        )
    monkeypatch.setenv("ASH_MODEL", "ollama/test-model")
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))

    assert (
        main(
            [
                "--db-directory",
                str(tmp_path),
                "--prompt",
                "hello",
                "--output-format",
                "stream-json",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["type"] == "error"
    assert payload["schema_version"] == 1
    assert payload["event_id"]
    assert payload["timestamp"]
    assert payload["source"] == {"type": "runtime", "id": "ash"}
    assert payload["error"]["category"] == "storage"


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
