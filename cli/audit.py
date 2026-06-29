"""Audit-log inspection and export helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from core.session import AuditLogRecord, SessionStore


def audit_records_payload(records: list[AuditLogRecord]) -> list[dict]:
    return [record.model_dump(mode="json") for record in records]


def render_audit_records(
    session_id: str,
    records: list[AuditLogRecord],
    *,
    json_output: bool = False,
) -> str:
    if json_output:
        return json.dumps(
            {"session_id": session_id, "records": audit_records_payload(records)},
            sort_keys=True,
        )
    if not records:
        return f"No audit records for session {session_id}."
    lines = [f"Audit records for session {session_id}:"]
    for record in records:
        log_id = "?" if record.log_id is None else str(record.log_id)
        lines.append(
            f"{log_id} {record.timestamp.isoformat()} "
            f"{record.action_type} {record.result} {record.target_resource}"
        )
    return "\n".join(lines)


def render_audit_verification(
    session_id: str,
    errors: list[str],
    *,
    json_output: bool = False,
) -> str:
    ok = not errors
    if json_output:
        return json.dumps(
            {"session_id": session_id, "ok": ok, "errors": errors},
            sort_keys=True,
        )
    if ok:
        return f"Audit log verified for session {session_id}."
    return f"Audit log verification failed for session {session_id}:\n" + "\n".join(
        errors
    )


def export_audit_log(
    store: SessionStore,
    session_id: str,
    output: str | Path,
) -> Path:
    """Write a versioned JSON audit bundle with verification status."""

    output_path = Path(output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    records = store.list_audit_logs(session_id)
    errors = store.verify_audit_log(session_id)
    payload = {
        "schema_version": 1,
        "session_id": session_id,
        "verified": not errors,
        "verification_errors": errors,
        "records": audit_records_payload(records),
    }
    temporary = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    if os.name != "nt":
        output_path.chmod(0o600)
    return output_path
