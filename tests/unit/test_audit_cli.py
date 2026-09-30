from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ash.cli import main
from ash.commands.audit import (
    export_audit_log,
    iter_render_audit_records,
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


def test_audit_renderer_consumes_records_lazily(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("/workspace")
    for target in ("one", "two", "three"):
        store.append_audit_log(
            session.session_id,
            action_type="tool_call",
            target_resource=target,
            details={},
            result="SUCCESS",
        )
    consumed: list[int] = []

    def tracked_records():
        for record in store.iter_audit_logs(session.session_id):
            consumed.append(int(record.log_id or 0))
            yield record

    chunks = iter_render_audit_records(
        session.session_id,
        tracked_records(),
        json_output=True,
    )

    assert next(chunks) == '{"records":['
    assert len(consumed) == 1
    payload = json.loads('{"records":[' + "".join(chunks))
    assert [record["target_resource"] for record in payload["records"]] == [
        "one",
        "two",
        "three",
    ]


def test_audit_verification_error_collection_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.core.session as session_module

    monkeypatch.setattr(session_module, "MAX_AUDIT_VERIFICATION_ERRORS", 2)
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("/workspace")
    for target in ("one", "two", "three"):
        store.append_audit_log(
            session.session_id,
            action_type="tool_call",
            target_resource=target,
            details={"value": target},
            result="SUCCESS",
        )
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        connection.execute(
            "UPDATE audit_logs SET details_json = ?",
            (json.dumps({"tampered": True}),),
        )

    errors = store.verify_audit_log(session.session_id)

    assert len(errors) == 3
    assert all("sha256_hash mismatch" in error for error in errors[:2])
    assert errors[2] == "1 additional audit verification error(s) omitted"


def test_audit_persistence_redacts_secrets_before_hashing(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("/workspace")
    secret = "sk-proj-" + "A" * 32

    record = store.append_audit_log(
        session.session_id,
        action_type="command_run",
        target_resource=f"command token={secret}",
        details={"api_key": secret, "nested": {"message": f"token={secret}"}},
        result="SUCCESS",
    )

    assert secret not in record.target_resource
    assert secret not in json.dumps(record.details)
    loaded = store.list_audit_logs(session.session_id)[0]
    assert secret not in loaded.target_resource
    assert secret not in json.dumps(loaded.details)
    assert store.verify_audit_log(session.session_id) == []


def test_audit_human_renderers_escape_terminal_controls(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("/workspace")
    record = store.append_audit_log(
        session.session_id,
        action_type="tool_call",
        target_resource="read_file\x1b[2J\u202ehidden\u202c",
        details={},
        result="SUCCESS",
    )

    rendered = render_audit_records(session.session_id, [record])
    verification = render_audit_verification(
        session.session_id,
        ["bad\x1b[2J\u202eerror\u202c"],
    )

    assert "\x1b[2J" not in rendered
    assert "\u202e" not in rendered
    assert "\\x1b[2J" in rendered
    assert "\x1b[2J" not in verification
    assert "\u202e" not in verification
    assert "\\x1b[2J" in verification


def test_audit_export_writes_verifiable_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("/workspace")
    store.append_audit_log(
        session.session_id,
        action_type="command_run",
        target_resource="pytest",
        details={"argv": ["pytest"]},
        result="APPROVED",
    )

    def fail_materialized_list(_session_id: str):
        raise AssertionError("audit export must stream instead of materializing")

    monkeypatch.setattr(store, "list_audit_logs", fail_materialized_list)

    output = export_audit_log(store, session.session_id, tmp_path / "audit.json")

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["session_id"] == session.session_id
    assert payload["verified"] is True
    assert payload["verification_errors"] == []
    assert payload["records"][0]["target_resource"] == "pytest"


@pytest.mark.parametrize("link_parent", [False, True])
def test_audit_export_rejects_symlinked_destination(
    tmp_path: Path,
    link_parent: bool,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("/workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "audit.json"
    outside_file.write_text("ORIGINAL\n", encoding="utf-8")

    if link_parent:
        destination_parent = tmp_path / "exports"
        try:
            destination_parent.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlinks are unavailable: {exc}")
        destination = destination_parent / "audit.json"
    else:
        destination = tmp_path / "audit.json"
        try:
            destination.symlink_to(outside_file)
        except OSError as exc:
            pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(OSError, match="symlink or junction"):
        export_audit_log(store, session.session_id, destination)

    assert outside_file.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_audit_export_rejects_plain_parent_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.commands.audit as audit_module

    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("/workspace")
    store.append_audit_log(
        session.session_id,
        action_type="tool_call",
        target_resource="read_file",
        details={"path": "README.md"},
        result="SUCCESS",
    )
    target_directory = tmp_path / "exports"
    saved_directory = tmp_path / "exports-original"
    replacement_directory = tmp_path / "replacement"
    target_directory.mkdir()
    replacement_directory.mkdir()
    victim = replacement_directory / "audit.json"
    victim.write_text("DO NOT REPLACE\n", encoding="utf-8")

    real_open = audit_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == target_directory and not swapped:
            swapped = True
            target_directory.rename(saved_directory)
            replacement_directory.rename(target_directory)
        return directory

    monkeypatch.setattr(
        audit_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises(OSError):
        export_audit_log(store, session.session_id, target_directory / "audit.json")

    assert swapped is True
    assert (target_directory / "audit.json").read_text(encoding="utf-8") == (
        "DO NOT REPLACE\n"
    )
    assert not (saved_directory / "audit.json").exists()


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


def test_audit_cli_list_does_not_load_session_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("/workspace")
    store.append_audit_log(
        session.session_id,
        action_type="tool_call",
        target_resource="read_file",
        details={},
        result="SUCCESS",
    )

    def fail_full_load(*_args, **_kwargs):
        raise AssertionError("audit CLI must not load the session transcript")

    monkeypatch.setattr(SessionStore, "load_session", fail_full_load)

    status = main(
        [
            "--db-directory",
            str(tmp_path),
            "audit",
            "list",
            "--session",
            session.session_id,
            "--json",
        ]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == session.session_id
    assert payload["records"][0]["target_resource"] == "read_file"
