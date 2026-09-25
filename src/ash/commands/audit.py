"""Audit-log inspection and export helpers."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from ash.core.session import AuditLogRecord, SessionStore
from ash.plugins.anchored_fs import AnchoredDirectory, AnchoredFilesystemError
from ash.safe_io import validate_unlinked_file_path


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

    output_path = Path(os.path.abspath(Path(output).expanduser()))
    try:
        validate_unlinked_file_path(output_path, label="audit export")
    except ValueError as exc:
        raise OSError(str(exc)) from exc
    records = store.list_audit_logs(session_id)
    errors = store.verify_audit_log(session_id)
    payload = {
        "schema_version": 1,
        "session_id": session_id,
        "verified": not errors,
        "verification_errors": errors,
        "records": audit_records_payload(records),
    }
    encoded_payload = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    try:
        with AnchoredDirectory.open(
            output_path.parent,
            create=True,
            private=False,
            pin_path=True,
        ) as directory:
            existing = directory.stat(output_path.name)
            if existing is not None:
                if stat.S_ISLNK(existing.st_mode):
                    raise OSError(
                        "refusing to use audit export through a symlink or junction: "
                        f"{output_path}"
                    )
                if not stat.S_ISREG(existing.st_mode):
                    raise OSError(
                        f"refusing to replace non-regular audit export: {output_path}"
                    )
            temporary_name = directory.unique_name(f".{output_path.name}.", ".tmp")
            descriptor = directory.create_file(temporary_name, mode=0o600)
            renamed = False
            completed = False
            try:
                view = memoryview(encoded_payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short write while writing audit export")
                    view = view[written:]
                os.fsync(descriptor)
                directory.validation_path()
                directory.rename(
                    temporary_name,
                    output_path.name,
                    expected_source_descriptor=descriptor,
                )
                renamed = True
                directory.validation_path()
                completed = True
            finally:
                if not completed:
                    cleanup_name = output_path.name if renamed else temporary_name
                    try:
                        directory.unlink(
                            cleanup_name,
                            missing_ok=True,
                            expected_descriptor=descriptor,
                        )
                    except BaseException:
                        pass
                os.close(descriptor)
    except (AnchoredFilesystemError, ValueError) as exc:
        raise OSError(str(exc)) from exc
    return output_path
