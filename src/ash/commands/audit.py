"""Audit-log inspection and export helpers."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterable, Iterator
from pathlib import Path

from ash.core.redaction import redact_text
from ash.core.session import AuditLogRecord, SessionStore
from ash.safety.anchored_fs import AnchoredDirectory, AnchoredFilesystemError
from ash.safe_io import validate_unlinked_file_path
from ash.ui.safe_text import terminal_safe_text


def audit_records_payload(records: list[AuditLogRecord]) -> list[dict]:
    return [record.model_dump(mode="json") for record in records]


def render_audit_records(
    session_id: str,
    records: list[AuditLogRecord],
    *,
    json_output: bool = False,
) -> str:
    return "".join(
        iter_render_audit_records(
            session_id,
            records,
            json_output=json_output,
        )
    )


def iter_render_audit_records(
    session_id: str,
    records: Iterable[AuditLogRecord],
    *,
    json_output: bool = False,
) -> Iterator[str]:
    """Render audit records incrementally without materializing the chain."""

    iterator = iter(records)
    first = next(iterator, None)
    if json_output:
        yield '{"records":['
        if first is not None:
            yield json.dumps(first.model_dump(mode="json"), sort_keys=True)
            for record in iterator:
                yield ","
                yield json.dumps(record.model_dump(mode="json"), sort_keys=True)
        yield '],"session_id":'
        yield json.dumps(session_id)
        yield "}"
        return
    if first is None:
        yield f"No audit records for session {_audit_human_text(session_id)}."
        return
    yield f"Audit records for session {_audit_human_text(session_id)}:"

    def render_record(record: AuditLogRecord) -> str:
        log_id = "?" if record.log_id is None else str(record.log_id)
        return (
            "\n"
            f"{log_id} {record.timestamp.isoformat()} "
            f"{record.action_type} {record.result} "
            f"{_audit_human_text(record.target_resource)}"
        )

    yield render_record(first)
    for record in iterator:
        yield render_record(record)


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
        return f"Audit log verified for session {_audit_human_text(session_id)}."
    return (
        f"Audit log verification failed for session {_audit_human_text(session_id)}:\n"
        + "\n".join(_audit_human_text(error) for error in errors)
    )


def _audit_human_text(value: str) -> str:
    """Redact secrets and make persisted audit text safe for a human terminal."""

    return terminal_safe_text(redact_text(value))


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
    errors = store.verify_audit_log(session_id)
    header = {
        "schema_version": 1,
        "session_id": session_id,
        "verified": not errors,
        "verification_errors": errors,
    }

    def write_all(descriptor: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while writing audit export")
            view = view[written:]

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
                header_json = json.dumps(
                    header,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                write_all(
                    descriptor,
                    (header_json[:-1] + ',"records":[').encode("utf-8"),
                )
                first = True
                for record in store.iter_audit_logs(session_id):
                    if not first:
                        write_all(descriptor, b",")
                    encoded_record = json.dumps(
                        record.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                    write_all(descriptor, encoded_record)
                    first = False
                write_all(descriptor, b"]}\n")
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
