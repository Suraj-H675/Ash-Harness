from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ash.cli import main
from ash.commands.audit import (
    export_audit_log,
    render_audit_records,
    render_audit_verification,
)
from ash.core.session import SessionStore


def test_audit_renderers_emit_json_payloads(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("/workspace")
    record = store.append_audit_log(
        session.session_id,
        action_type="tool_call",
        target_resource="read_file",
        details={"path": "README.md"},
        result="SUCCESS",
    )

    listed = json.loads(
        render_audit_records(session.session_id, [record], json_output=True)
    )
    verified = json.loads(
        render_audit_verification(session.session_id, [], json_output=True)
    )

    assert listed["session_id"] == session.session_id
    assert listed["records"][0]["target_resource"] == "read_file"
    assert verified == {"errors": [], "ok": True, "session_id": session.session_id}


def test_audit_export_writes_verifiable_bundle(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("/workspace")
    store.append_audit_log(
        session.session_id,
        action_type="command_run",
        target_resource="pytest",
        details={"argv": ["pytest"]},
        result="APPROVED",
    )

    output = export_audit_log(store, session.session_id, tmp_path / "audit.json")

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["session_id"] == session.session_id
    assert payload["verified"] is True
    assert payload["verification_errors"] == []
    assert payload["records"][0]["target_resource"] == "pytest"


def test_audit_cli_honors_database_directory_override(
    tmp_path: Path,
    capsys,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("/workspace")
    store.append_audit_log(
        session.session_id,
        action_type="user_approval",
        target_resource="replace_file_content",
        details={"decision": "approved"},
        result="APPROVED",
    )

    status = main(
        [
            "--db-directory",
            str(tmp_path),
            "audit",
            "verify",
            "--session",
            session.session_id,
            "--json",
        ]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"errors": [], "ok": True, "session_id": session.session_id}


def test_audit_cli_returns_nonzero_for_tampered_chain(
    tmp_path: Path,
    capsys,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("/workspace")
    record = store.append_audit_log(
        session.session_id,
        action_type="file_write",
        target_resource="src/app.py",
        details={"path": "src/app.py"},
        result="SUCCESS",
    )
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        connection.execute(
            "UPDATE audit_logs SET details_json = ? WHERE log_id = ?",
            (json.dumps({"path": "src/changed.py"}), record.log_id),
        )

    status = main(
        [
            "--db-directory",
            str(tmp_path),
            "audit",
            "verify",
            "--session",
            session.session_id,
            "--json",
        ]
    )

    assert status == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "sha256_hash mismatch" in payload["errors"][0]


def test_audit_cli_reports_missing_session(tmp_path: Path, capsys) -> None:
    SessionStore(tmp_path / "sessions.db")

    status = main(
        [
            "--db-directory",
            str(tmp_path),
            "audit",
            "list",
            "--session",
            "missing",
        ]
    )

    assert status == 1
    assert "session not found" in capsys.readouterr().err
