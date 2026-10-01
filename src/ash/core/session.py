"""Session persistence and SQLite storage for Ash."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sqlite3
import stat
import sys
import threading
import weakref
from contextlib import asynccontextmanager, closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Literal, Sequence
from uuid import uuid4

from pydantic import BaseModel, Field, PrivateAttr

from ash.core.goals import (
    MAX_GOAL_EVIDENCE_BYTES,
    MAX_GOAL_OBJECTIVE_BYTES,
    GoalRecord,
    GoalState,
    bounded_goal_text,
    validate_goal_continuation_limit,
)
from ash.providers.identifiers import MAX_MODEL_IDENTIFIER_BYTES

from ash.safety.anchored_fs import AnchoredDirectory, AnchoredFilesystemError
from ash.safe_io import (
    strict_json_loads,
    validate_unlinked_file_path,
)
from ash.sqlite_utils import (
    PinnedSQLiteDatabase,
    SQLitePathError,
    configure_sqlite_journal_mode,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX CI/runtime exercises flock support.
    fcntl = None  # type: ignore[assignment]


Role = Literal["system", "user", "assistant", "tool"]
AuditAction = Literal[
    "tool_call",
    "command_run",
    "file_write",
    "safety_block",
    "user_approval",
    "permission_mode",
]
AuditResult = Literal["APPROVED", "DENIED", "BLOCKED_BY_GUARD", "SUCCESS", "FAILURE"]
CURRENT_SCHEMA_VERSION = 16
SQLITE_INTEGER_MAX = 2**63 - 1
SQLITE_REAL_MAX = sys.float_info.max
MAX_SESSION_IMPORT_BYTES = 64 * 1024 * 1024
MAX_RUNTIME_SESSION_TOOL_CALLS = 256
MAX_DURABLE_MCP_TASKS_PER_SESSION = 64
MAX_DURABLE_MCP_TASK_STATE_BYTES = 8 * 1024 * 1024
MAX_DURABLE_MCP_ANSWERED_INPUTS_BYTES = 8 * 1024 * 1024
MAX_DURABLE_MCP_ANSWERED_INPUTS = 256
MAX_RECENT_SESSION_CONTEXT_SESSIONS = 20
MAX_RECENT_SESSION_CONTEXT_MESSAGES = 32
MAX_RECENT_SESSION_CONTEXT_CHARS = 2000
MAX_SESSION_LIST_LIMIT = 1000
MAX_SESSION_TREE_NODES = 4096
MAX_AUDIT_VERIFICATION_ERRORS = 1000
MAX_RECOVERY_TOOL_OUTPUT_PREVIEW_BYTES = 2 * 1024 * 1024
MAX_RECOVERY_TOOL_ERROR_PREVIEW_BYTES = 256 * 1024
MAX_RECOVERY_AUDIT_OUTPUT_PREVIEW_BYTES = 64 * 1024
MAX_RECOVERY_ARGUMENT_PREVIEW_BYTES = 64 * 1024


class SessionStorageError(RuntimeError):
    """Session database cannot be opened or migrated safely."""


class _SessionRuntimeLease:
    """Held advisory lock proving one process owns active session work."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor

    def close(self) -> None:
        descriptor = self._descriptor
        if descriptor < 0:
            return
        self._descriptor = -1
        os.close(descriptor)


class SessionResolutionError(ValueError):
    """A human session reference cannot be resolved unambiguously."""


class _DatabaseCoordinationState:
    """Process-local reader/writer state paired with a cross-process flock."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.readers = 0
        self.reader_threads: dict[int, int] = {}
        self.writer = False
        self.writer_thread: int | None = None
        self.waiting_writers = 0


_db_coordination_states_guard = threading.Lock()
_db_coordination_states: dict[str, _DatabaseCoordinationState] = {}


def _database_coordination_state(db_path: str) -> _DatabaseCoordinationState:
    with _db_coordination_states_guard:
        return _db_coordination_states.setdefault(db_path, _DatabaseCoordinationState())


def _database_coordination_lock_path(db_path: str) -> Path:
    path = Path(db_path)
    return path.with_name(f".{path.name}.ash-lock")


def _open_database_coordination_file(
    db_path: str,
    *,
    parent_descriptor: int | None = None,
) -> int | None:
    if os.name != "posix" or fcntl is None:
        return None
    path = _database_coordination_lock_path(db_path)
    flags = os.O_RDWR | os.O_CREAT
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        if parent_descriptor is None:
            descriptor = os.open(path, flags, 0o600)
        else:
            if os.open not in getattr(os, "supports_dir_fd", ()):
                raise SessionStorageError(
                    "Secure session database coordination is unavailable on this platform"
                )
            descriptor = os.open(
                path.name,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
    except OSError as exc:
        raise SessionStorageError(
            f"Could not open session database coordination lock {path}: {exc}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SessionStorageError(
                f"Session database coordination lock is not a regular file: {path}"
            )
        os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _acquire_database_coordination(
    db_path: str,
    *,
    exclusive: bool,
    coordination_parent_descriptor: int | None = None,
) -> Any:
    state = _database_coordination_state(db_path)
    thread_id = threading.get_ident()
    with state.condition:
        if exclusive:
            if state.writer and state.writer_thread == thread_id:
                raise SessionStorageError(
                    "Cannot acquire exclusive session database access recursively"
                )
            if state.reader_threads.get(thread_id, 0):
                raise SessionStorageError(
                    "Cannot acquire exclusive session database access while the current "
                    "thread holds an open database connection"
                )
            state.waiting_writers += 1
            try:
                while state.writer or state.readers:
                    state.condition.wait()
                state.writer = True
                state.writer_thread = thread_id
            finally:
                state.waiting_writers -= 1
        else:
            if state.writer and state.writer_thread == thread_id:
                raise SessionStorageError(
                    "Cannot open a session database connection while holding exclusive "
                    "database access"
                )
            current_thread_readers = state.reader_threads.get(thread_id, 0)
            while state.writer or (state.waiting_writers and not current_thread_readers):
                state.condition.wait()
            state.readers += 1
            state.reader_threads[thread_id] = current_thread_readers + 1

    descriptor: int | None = None
    try:
        descriptor = _open_database_coordination_file(
            db_path,
            parent_descriptor=coordination_parent_descriptor,
        )
        if descriptor is not None:
            assert fcntl is not None
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(descriptor, operation)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        with state.condition:
            if exclusive:
                state.writer = False
                state.writer_thread = None
            else:
                state.readers -= 1
                remaining = state.reader_threads.get(thread_id, 0) - 1
                if remaining > 0:
                    state.reader_threads[thread_id] = remaining
                else:
                    state.reader_threads.pop(thread_id, None)
            state.condition.notify_all()
        raise

    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        release_error: BaseException | None = None
        if descriptor is not None:
            try:
                assert fcntl is not None
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except BaseException as exc:  # pragma: no cover - OS-level failure.
                release_error = exc
            finally:
                os.close(descriptor)
        with state.condition:
            if exclusive:
                state.writer = False
                state.writer_thread = None
            else:
                state.readers -= 1
                remaining = state.reader_threads.get(thread_id, 0) - 1
                if remaining > 0:
                    state.reader_threads[thread_id] = remaining
                else:
                    state.reader_threads.pop(thread_id, None)
            state.condition.notify_all()
        if release_error is not None:
            raise SessionStorageError(
                f"Could not release session database coordination lock: {release_error}"
            ) from release_error

    return release


@contextmanager
def exclusive_database_access(
    db_path: str | Path,
    *,
    coordination_parent_descriptor: int | None = None,
):
    """Quiesce Ash database connections while a storage-level mutation runs."""

    normalized_path = Path(_normalize_db_path(db_path))
    normalized = _normalize_db_path(normalized_path)
    if coordination_parent_descriptor is not None:
        release = _acquire_database_coordination(
            normalized,
            exclusive=True,
            coordination_parent_descriptor=coordination_parent_descriptor,
        )
        try:
            yield Path(normalized)
        finally:
            release()
        return

    try:
        with AnchoredDirectory.open(
            normalized_path.parent,
            create=True,
            private=False,
            pin_path=True,
        ) as directory:
            release = _acquire_database_coordination(
                normalized,
                exclusive=True,
                coordination_parent_descriptor=(
                    directory.descriptor if os.name == "posix" else None
                ),
            )
            try:
                directory.validation_path()
                yield Path(normalized)
                directory.validation_path()
            finally:
                release()
    except (AnchoredFilesystemError, OSError) as exc:
        raise SessionStorageError(
            f"Could not safely coordinate session database access: {exc}"
        ) from exc


class _CoordinatedConnection(sqlite3.Connection):
    """SQLite connection that releases its Ash coordination lease on close."""

    _ash_coordination_release: Any = None

    def close(self) -> None:
        release = self._ash_coordination_release
        self._ash_coordination_release = None
        try:
            super().close()
        finally:
            if release is not None:
                release()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


class Message(BaseModel):
    role: Role
    content: str
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolCallRecord(BaseModel):
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    approved: bool
    executed: bool
    dispatched: bool = False
    result: str | None = None
    error: str | None = None
    timestamp: datetime


class AuditLogRecord(BaseModel):
    log_id: int | None = None
    session_id: str
    action_type: AuditAction
    target_resource: str
    details: dict[str, Any]
    result: AuditResult
    timestamp: datetime
    previous_hash: str = ""
    sha256_hash: str


class Session(BaseModel):
    session_id: str
    project_path: str
    created_at: datetime
    title: str = ""
    updated_at: datetime | None = None
    context_summary: str = ""
    context_summary_message_count: int = 0
    model: str = ""
    parent_session_id: str | None = None
    root_session_id: str = ""
    fork_message_count: int | None = None
    branch_name: str = ""
    branch_summary: str = ""
    depth: int = 0
    messages: list[Message] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    _resident_message_offset: int = PrivateAttr(default=0)

    @property
    def resident_history_is_windowed(self) -> bool:
        """Whether ``messages`` omits a compacted durable prefix."""

        return self._resident_message_offset > 0

    @property
    def resident_message_offset(self) -> int:
        """Number of durable leading messages omitted from the live snapshot."""

        return self._resident_message_offset

    def discard_compacted_prefix(self, count: int) -> None:
        """Drop a summarized live prefix while leaving durable history untouched."""

        if count < 0 or count > len(self.messages):
            raise ValueError("compacted message count is outside the resident history")
        if count == 0:
            return
        del self.messages[:count]
        self._resident_message_offset += count


MAX_SESSION_TITLE_CHARS = 256


def _normalize_session_title(title: str, *, allow_empty: bool = False) -> str:
    normalized = " ".join(title.split())
    if not normalized and not allow_empty:
        raise ValueError("session title cannot be empty")
    if len(normalized) > MAX_SESSION_TITLE_CHARS:
        raise ValueError(
            f"session title cannot exceed {MAX_SESSION_TITLE_CHARS} characters"
        )
    return normalized


def _derived_session_title(base: str, suffix: str) -> str:
    normalized = " ".join(base.split())
    available = MAX_SESSION_TITLE_CHARS - len(suffix)
    if available < 1:
        raise ValueError("session title suffix exceeds the title limit")
    fitted = normalized[:available].rstrip()
    if not fitted:
        fitted = "session"[:available]
    return f"{fitted}{suffix}"


def _validate_session_model(model: str) -> str:
    try:
        size = len(model.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("session model must be valid UTF-8 text") from exc
    if size > MAX_MODEL_IDENTIFIER_BYTES:
        raise ValueError(
            f"session model cannot exceed {MAX_MODEL_IDENTIFIER_BYTES} UTF-8 bytes"
        )
    return model


def _truncate_utf8_bytes(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", errors="ignore")


def _render_recovered_tool_message(
    *,
    success: bool,
    output: str,
    error: str | None,
    dispatched: bool,
    ambiguous: bool,
) -> str:
    from ash.providers.messages import MAX_CANONICAL_CONTENT_BYTES

    payload: dict[str, Any] = {
        "success": success,
        "output": output,
        "error": error,
        "provenance": "ash_startup_recovery",
        "dispatched": dispatched,
        "ambiguous": ambiguous,
        "replayed": False,
        "policy_note": (
            "Ash reconstructed this model-visible result during startup recovery "
            "and did not replay the tool call."
        ),
    }

    def render(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)

    rendered = render(payload)
    if len(rendered.encode("utf-8")) <= MAX_CANONICAL_CONTENT_BYTES:
        return rendered

    output_bytes = output.encode("utf-8")
    compact = dict(payload)
    compact["output"] = _truncate_utf8_bytes(
        output,
        min(MAX_RECOVERY_TOOL_OUTPUT_PREVIEW_BYTES, MAX_CANONICAL_CONTENT_BYTES // 4),
    )
    compact["error"] = (
        _truncate_utf8_bytes(
            error,
            min(MAX_RECOVERY_TOOL_ERROR_PREVIEW_BYTES, MAX_CANONICAL_CONTENT_BYTES // 8),
        )
        if error
        else None
    )
    compact["recovery_result_truncated"] = True
    compact["original_output_bytes"] = len(output_bytes)
    compact["original_output_sha256"] = hashlib.sha256(output_bytes).hexdigest()
    rendered = render(compact)
    if len(rendered.encode("utf-8")) > MAX_CANONICAL_CONTENT_BYTES:
        raise ValueError(
            "recovered tool response could not fit the canonical message limit"
        )
    return rendered


def _recovery_audit_output_fields(output: str) -> dict[str, Any]:
    from ash.core.redaction import redact_text

    redacted = redact_text(output)
    encoded = redacted.encode("utf-8")
    if len(encoded) <= MAX_RECOVERY_AUDIT_OUTPUT_PREVIEW_BYTES:
        return {"output": redacted}
    return {
        "output_truncated": True,
        "output_preview": _truncate_utf8_bytes(
            redacted,
            MAX_RECOVERY_AUDIT_OUTPUT_PREVIEW_BYTES,
        ),
        "output_bytes": len(encoded),
        "output_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _bounded_recovery_arguments(arguments: Any) -> dict[str, Any]:
    from ash.providers.messages import MAX_TOOL_CALL_ARGUMENT_BYTES

    if not isinstance(arguments, dict):
        return {}
    try:
        encoded_text = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded = encoded_text.encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeEncodeError):
        return {"_ash_recovery_arguments_invalid": True}
    if len(encoded) <= MAX_TOOL_CALL_ARGUMENT_BYTES:
        return dict(arguments)

    def fits(value: dict[str, Any]) -> bool:
        return (
            len(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
            <= MAX_TOOL_CALL_ARGUMENT_BYTES
        )

    compact: dict[str, Any] = {"_ash_recovery_arguments_truncated": True}
    for key, value in (
        ("original_bytes", len(encoded)),
        ("original_sha256", hashlib.sha256(encoded).hexdigest()),
    ):
        candidate = {**compact, key: value}
        if fits(candidate):
            compact = candidate

    if not fits(compact):
        raise ValueError("tool call argument limit is too small for recovery metadata")

    preview_limit = min(
        MAX_RECOVERY_ARGUMENT_PREVIEW_BYTES,
        MAX_TOOL_CALL_ARGUMENT_BYTES,
    )
    low = 0
    high = preview_limit
    best: dict[str, Any] = compact
    while low <= high:
        midpoint = (low + high) // 2
        candidate = {
            **compact,
            "preview": _truncate_utf8_bytes(encoded_text, midpoint),
        }
        if fits(candidate):
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


class SessionSummary(BaseModel):
    session_id: str
    project_path: str
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0
    model: str = ""
    parent_session_id: str | None = None
    root_session_id: str = ""
    fork_message_count: int | None = None
    branch_name: str = ""
    depth: int = 0
    context_summary: str = ""


class SessionLineage(BaseModel):
    session_id: str
    root_session_id: str
    parent_session_id: str | None = None
    fork_message_count: int | None = None
    branch_name: str = ""
    branch_summary: str = ""
    depth: int = 0
    created_at: datetime
    children: tuple[str, ...] = ()


class SessionUsage(BaseModel):
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    estimated_prompt_tokens: int = 0
    estimated_completion_tokens: int = 0
    estimated_cost_usd: float = 0.0

    @property
    def has_estimates(self) -> bool:
        return bool(self.estimated_prompt_tokens or self.estimated_completion_tokens)


class StoredRuntimeEvent(BaseModel):
    sequence: int
    event: dict[str, Any]


_db_write_locks: weakref.WeakValueDictionary[tuple[int, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)
_db_write_locks_guard = threading.Lock()


def _normalize_db_path(db_path: str | Path) -> str:
    try:
        return str(validate_unlinked_file_path(db_path, label="session database"))
    except ValueError as exc:
        raise SessionStorageError(str(exc)) from exc


def _invalid_stored_data_error(db_path: str | Path) -> SessionStorageError:
    return SessionStorageError(
        f"Could not read session database {db_path}: stored data is invalid. "
        "Run 'ash storage check' and restore a backup if needed."
    )


def normalize_project_path(project_path: str | Path) -> str:
    """Return the stable platform-aware identity used for session scoping."""

    return os.path.normcase(
        os.path.realpath(os.path.expanduser(os.fspath(project_path)))
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_datetime(value: datetime) -> str:
    return value.isoformat()


def _deserialize_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _tool_result_message_exists(rows: list[sqlite3.Row], call_id: str) -> bool:
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(metadata, dict) and metadata.get("call_id") == call_id:
            return True
    return False


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _audit_hash(
    *,
    session_id: str,
    timestamp: datetime,
    action_type: AuditAction,
    target_resource: str,
    details_json: str,
    result: AuditResult,
    previous_hash: str,
) -> str:
    payload = {
        "session_id": session_id,
        "timestamp": _serialize_datetime(timestamp),
        "action_type": action_type,
        "target_resource": target_resource,
        "details_json": details_json,
        "result": result,
        "previous_hash": previous_hash,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _audit_record_from_row(row: sqlite3.Row) -> "AuditLogRecord":
    return AuditLogRecord(
        log_id=int(row["log_id"]),
        session_id=str(row["session_id"]),
        action_type=row["action_type"],
        target_resource=str(row["target_resource"]),
        details=json.loads(str(row["details_json"])),
        result=row["result"],
        timestamp=_deserialize_datetime(str(row["timestamp"])),
        previous_hash=str(row["previous_hash"] or ""),
        sha256_hash=str(row["sha256_hash"] or ""),
    )


def _lineage_from_row(
    row: sqlite3.Row,
    *,
    children: tuple[str, ...] = (),
) -> SessionLineage:
    return SessionLineage(
        session_id=str(row["session_id"]),
        root_session_id=str(row["root_session_id"] or row["session_id"]),
        parent_session_id=row["parent_session_id"],
        fork_message_count=row["fork_message_count"],
        branch_name=str(row["branch_name"] or ""),
        branch_summary=str(row["branch_summary"] or ""),
        depth=int(row["depth"] or 0),
        created_at=_deserialize_datetime(str(row["created_at"])),
        children=children,
    )


def _message_boundary_rows(
    conn: sqlite3.Connection,
    session_id: str,
    message_count: int,
) -> tuple[int, sqlite3.Row | None, sqlite3.Row | None]:
    """Return total count plus adjacent retained/removed rows for one boundary."""

    total = int(
        conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
    )
    if message_count < 0 or message_count > total:
        raise ValueError(f"message_count must be between 0 and {total}")
    retained = None
    removed = None
    if message_count > 0:
        retained = conn.execute(
            "SELECT message_id, role, timestamp, metadata_json, turn_id "
            "FROM messages WHERE session_id = ? ORDER BY message_id "
            "LIMIT 1 OFFSET ?",
            (session_id, message_count - 1),
        ).fetchone()
    if message_count < total:
        removed = conn.execute(
            "SELECT message_id, role, timestamp, metadata_json, turn_id "
            "FROM messages WHERE session_id = ? ORDER BY message_id "
            "LIMIT 1 OFFSET ?",
            (session_id, message_count),
        ).fetchone()
    return total, retained, removed


def _validate_sql_fork_boundary(
    retained: sqlite3.Row | None,
    removed: sqlite3.Row | None,
) -> None:
    if retained is None or removed is None:
        return
    if str(removed["role"]) == "tool":
        raise ValueError("message_count splits an assistant/tool-call pair")
    if str(retained["role"]) == "assistant":
        try:
            metadata = json.loads(retained["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("stored assistant metadata is invalid JSON") from exc
        if isinstance(metadata, dict) and metadata.get("tool_calls"):
            raise ValueError("message_count splits an assistant/tool-call pair")
    retained_turn = retained["turn_id"]
    removed_turn = removed["turn_id"]
    if retained_turn is not None and retained_turn == removed_turn:
        raise ValueError("message_count splits an Ash turn; choose a turn boundary")


def _validate_imported_provider_message(message: Message) -> None:
    """Reject imported transcripts that cannot be replayed to providers safely."""

    from ash.providers.messages import CanonicalMessage

    content: Any = message.content
    content_blocks = message.metadata.get("content_blocks")
    if message.role == "user" and isinstance(content_blocks, list):
        content = content_blocks
    else:
        image_blocks = message.metadata.get("image_blocks")
        if message.role == "user" and isinstance(image_blocks, list):
            content = [
                {"type": "text", "text": message.content},
                *image_blocks,
            ]

    payload: dict[str, Any] = {
        "role": message.role,
        "content": content,
    }
    if message.role == "assistant" and "tool_calls" in message.metadata:
        payload["tool_calls"] = message.metadata["tool_calls"]
    if message.role == "assistant" and "provider_state" in message.metadata:
        payload["provider_state"] = message.metadata["provider_state"]
    if message.role == "tool" and message.metadata.get("call_id"):
        payload["tool_call_id"] = message.metadata["call_id"]
    try:
        CanonicalMessage.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("imported message is not a valid provider message") from exc


def _normalize_branch_metadata(name: str, summary: str) -> tuple[str, str]:
    from ash.core.redaction import redact_text

    normalized_name = " ".join(name.split())
    normalized_summary = summary.strip()
    if len(normalized_name) > 128:
        raise ValueError("branch_name cannot exceed 128 characters")
    if len(normalized_summary) > 12_000:
        raise ValueError("branch_summary cannot exceed 12000 characters")
    return normalized_name, redact_text(normalized_summary)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})")
    )


def _copy_descriptor(source: int, destination: int) -> None:
    """Copy one held regular file into another held descriptor."""

    source_position = os.lseek(source, 0, os.SEEK_CUR)
    try:
        os.lseek(source, 0, os.SEEK_SET)
        os.ftruncate(destination, 0)
        os.lseek(destination, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination, view)
                if written <= 0:
                    raise OSError("short write while creating session backup")
                view = view[written:]
        os.fsync(destination)
    finally:
        os.lseek(source, source_position, os.SEEK_SET)


def _validate_backup_source_connection(source: sqlite3.Connection) -> None:
    """Require the exact SQLite connection being backed up to be healthy."""

    integrity_rows = source.execute("PRAGMA integrity_check").fetchall()
    integrity_errors = [str(row[0]) for row in integrity_rows if row[0] != "ok"]
    if integrity_errors:
        raise SessionStorageError(
            "Refusing to back up an unhealthy database: "
            + "; ".join(integrity_errors)
        )
    foreign_rows = source.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_rows:
        details = [
            "foreign key violation: " + ", ".join(str(value) for value in row)
            for row in foreign_rows
        ]
        raise SessionStorageError(
            "Refusing to back up an unhealthy database: " + "; ".join(details)
        )
    table = source.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if table is not None:
        version = int(
            source.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
        )
        if version > CURRENT_SCHEMA_VERSION:
            raise SessionStorageError(
                f"Session database schema {version} is newer than this Ash version "
                f"supports ({CURRENT_SCHEMA_VERSION})"
            )


def _validate_backup_descriptor(descriptor: int) -> None:
    """Validate the exact bytes held by a completed backup descriptor."""

    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    payload = bytearray()
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            payload.extend(chunk)
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)

    if len(payload) >= 20 and payload[18:20] == b"\x02\x02":
        payload[18:20] = b"\x01\x01"

    with closing(sqlite3.connect(":memory:")) as connection:
        deserialize = getattr(connection, "deserialize", None)
        if not callable(deserialize):
            raise SessionStorageError(
                "Secure session backup validation is unavailable on this platform/build"
            )
        try:
            deserialize(payload)
        except sqlite3.DatabaseError as exc:
            raise SessionStorageError(
                f"Refusing to back up an unhealthy database: {exc}"
            ) from exc
        _validate_backup_source_connection(connection)


def _require_quiescent_backup_source(
    directory: AnchoredDirectory,
    source_path: Path,
) -> None:
    """Reject sidecar state that may contain committed bytes missing from the main DB."""

    wal = Path(f"{source_path}-wal")
    wal_stat = directory.stat(f"{source_path.name}-wal")
    if wal_stat is not None:
        if stat.S_ISLNK(wal_stat.st_mode) or not stat.S_ISREG(wal_stat.st_mode):
            raise SessionStorageError(
                f"Refusing to back up session database with unsafe WAL sidecar: {wal}"
            )
        if wal_stat.st_size:
            raise SessionStorageError(
                "Refusing to back up session database while a SQLite WAL sidecar "
                f"contains data: {wal}"
            )

    shm = Path(f"{source_path}-shm")
    shm_stat = directory.stat(f"{source_path.name}-shm")
    if shm_stat is not None:
        if stat.S_ISLNK(shm_stat.st_mode) or not stat.S_ISREG(shm_stat.st_mode):
            raise SessionStorageError(
                "Refusing to back up session database with unsafe shared-memory "
                f"sidecar: {shm}"
            )

    journal = Path(f"{source_path}-journal")
    journal_stat = directory.stat(f"{source_path.name}-journal")
    if journal_stat is not None:
        if stat.S_ISLNK(journal_stat.st_mode) or not stat.S_ISREG(journal_stat.st_mode):
            raise SessionStorageError(
                "Refusing to back up session database with unsafe rollback journal: "
                f"{journal}"
            )
        raise SessionStorageError(
            "Refusing to back up session database while a SQLite rollback journal "
            f"exists: {journal}"
        )


def get_db_connection(
    db_path: str | Path,
    *,
    _database: PinnedSQLiteDatabase | None = None,
) -> sqlite3.Connection:
    """Open a SQLite connection configured for WAL persistence."""

    try:
        database = _database or PinnedSQLiteDatabase.prepare(
            _normalize_db_path(db_path),
            label="session database",
        )
    except SQLitePathError as exc:
        raise SessionStorageError(str(exc)) from exc

    normalized_path = database.path
    release: Any | None = None
    try:
        with database.parent_directory(label="session database") as parent:
            release = _acquire_database_coordination(
                str(normalized_path),
                exclusive=False,
                coordination_parent_descriptor=parent.descriptor,
            )
            conn = database.connect(
                label="session database",
                check_same_thread=False,
                factory=_CoordinatedConnection,
            )
        conn._ash_coordination_release = release  # type: ignore[attr-defined]
        conn.row_factory = sqlite3.Row
        configure_sqlite_journal_mode(conn)
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn
    except BaseException as exc:
        if release is not None:
            release()
        if isinstance(exc, SQLitePathError):
            raise SessionStorageError(str(exc)) from exc
        raise


@asynccontextmanager
async def write_transaction(db_path: str | Path) -> AsyncIterator[sqlite3.Connection]:
    """Serialize writes for one database within the current event loop."""

    lock_key = _normalize_db_path(db_path)
    loop_key = id(asyncio.get_running_loop())
    with _db_write_locks_guard:
        lock = _db_write_locks.get((loop_key, lock_key))
        if lock is None:
            lock = asyncio.Lock()
            _db_write_locks[(loop_key, lock_key)] = lock

    async with lock:
        conn = get_db_connection(db_path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class SessionStore:
    def __init__(self, db_path: str | Path) -> None:
        try:
            self._database = PinnedSQLiteDatabase.prepare(
                _normalize_db_path(db_path),
                label="session database",
            )
            self.db_path = str(self._database.path)
            path = self._database.path
            existed = self._database.initial_size > 0
            version = self._schema_version()
            if version > CURRENT_SCHEMA_VERSION:
                raise SessionStorageError(
                    f"Session database schema {version} is newer than this Ash version "
                    f"supports ({CURRENT_SCHEMA_VERSION})"
                )
            if existed and version < CURRENT_SCHEMA_VERSION:
                self.backup(reason=f"before-v{CURRENT_SCHEMA_VERSION}-migration")
            self._init_db()
            self._migrate_if_needed(version)
        except (SessionStorageError, SQLitePathError) as exc:
            if isinstance(exc, SQLitePathError):
                raise SessionStorageError(str(exc)) from exc
            raise
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
            raise SessionStorageError(
                f"Could not initialize session database {path}: {exc}. "
                "Run 'ash storage check' and restore a backup if needed."
            ) from exc

    def acquire_session_runtime_lease(self, session_id: str) -> _SessionRuntimeLease:
        """Fail fast when another process is actively mutating this session."""

        if os.name != "posix" or fcntl is None:
            raise SessionStorageError(
                "session runtime locking is unavailable on this platform"
            )
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        lock_name = f".{self._database.path.name}.session-{digest}.lock"
        descriptor = -1
        try:
            with self._database.parent_directory(label="session runtime lease") as parent:
                metadata = parent.stat(lock_name)
                if metadata is None:
                    descriptor = parent.create_file(lock_name, mode=0o600)
                else:
                    if not stat.S_ISREG(metadata.st_mode):
                        raise SessionStorageError(
                            "session runtime lock is not a regular file"
                        )
                    descriptor = parent.open_file(
                        lock_name,
                        os.O_RDWR,
                        expected=metadata,
                        expected_type=stat.S_IFREG,
                    )
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise SessionStorageError(
                        f"session {session_id!r} is active in another Ash process"
                    ) from exc
                if not parent.same_entry(lock_name, descriptor):
                    raise SessionStorageError(
                        "session runtime lock changed while it was being acquired"
                    )
            lease = _SessionRuntimeLease(descriptor)
            descriptor = -1
            return lease
        except (AnchoredFilesystemError, SQLitePathError, OSError) as exc:
            raise SessionStorageError(
                f"could not acquire session runtime lock: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _connect(self) -> sqlite3.Connection:
        return get_db_connection(self.db_path, _database=self._database)

    def open_connection(self) -> sqlite3.Connection:
        """Open a coordinated connection bound to this store's database identity."""

        return self._connect()

    def _schema_version(self) -> int:
        if not Path(self.db_path).exists():
            return 0
        with closing(self._connect()) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'schema_migrations'"
            ).fetchone()
            if table is None:
                return 0
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            return int(row["version"])

    def _migrate_if_needed(self, from_version: int) -> None:
        """Apply ordered, transactional migrations after a safety backup.

        Version 0 covers databases created before explicit schema tracking.
        """

        with closing(self._connect()) as conn, conn:
            if from_version < 1:
                self._migrate_v1(conn)
            if from_version < 2:
                self._migrate_v2(conn)
            if from_version < 3:
                self._migrate_v3(conn)
            if from_version < 4:
                self._migrate_v4(conn)
            if from_version < 5:
                self._migrate_v5(conn)
            if from_version < 6:
                self._migrate_v6(conn)
            if from_version < 7:
                self._migrate_v7(conn)
            if from_version < 8:
                self._migrate_v8(conn)
            if from_version < 9:
                self._migrate_v9(conn)
            if from_version < 10:
                self._migrate_v10(conn)
            if from_version < 11:
                self._migrate_v11(conn)
            if from_version < 12:
                self._migrate_v12(conn)
            if from_version < 13:
                self._migrate_v13(conn)
            if from_version < 14:
                self._migrate_v14(conn)
            if from_version < 15:
                self._migrate_v15(conn)
            if from_version < 16:
                self._migrate_v16(conn)

    def _migrate_v1(self, conn: sqlite3.Connection) -> None:
        """Migrate databases created before explicit schema tracking."""

        # Sessions columns
        for col_spec in (
            ("total_tokens", "INTEGER DEFAULT 0"),
            ("total_cost_inr", "REAL DEFAULT 0"),
            ("total_cost_usd", "REAL DEFAULT 0"),
            ("total_prompt_tokens", "INTEGER DEFAULT 0"),
            ("total_completion_tokens", "INTEGER DEFAULT 0"),
            ("title", "TEXT DEFAULT ''"),
            ("updated_at", "TIMESTAMP"),
            ("context_summary", "TEXT DEFAULT ''"),
            ("model", "TEXT DEFAULT ''"),
        ):
            col_name, col_type = col_spec
            if not _column_exists(conn, "sessions", col_name):
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {col_name} {col_type}")

        # Messages columns
        for col_spec in (
            ("token_count", "INTEGER DEFAULT 0"),
            ("prompt_tokens", "INTEGER DEFAULT 0"),
            ("completion_tokens", "INTEGER DEFAULT 0"),
        ):
            col_name, col_type = col_spec
            if not _column_exists(conn, "messages", col_name):
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col_name} {col_type}")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (1, _serialize_datetime(_utc_now())),
        )

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        """Add tamper-evident audit-log chaining metadata."""

        if not _column_exists(conn, "audit_logs", "previous_hash"):
            conn.execute(
                "ALTER TABLE audit_logs ADD COLUMN previous_hash TEXT DEFAULT ''"
            )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (2, _serialize_datetime(_utc_now())),
        )

    def _migrate_v3(self, conn: sqlite3.Connection) -> None:
        """Add provider prompt-cache usage totals."""

        for col_spec in (
            ("total_cache_read_tokens", "INTEGER DEFAULT 0"),
            ("total_cache_write_tokens", "INTEGER DEFAULT 0"),
        ):
            col_name, col_type = col_spec
            if not _column_exists(conn, "sessions", col_name):
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {col_name} {col_type}")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (3, _serialize_datetime(_utc_now())),
        )

    def _migrate_v4(self, conn: sqlite3.Connection) -> None:
        """Index a canonical project identity for reliable resume filtering."""

        if not _column_exists(conn, "sessions", "project_key"):
            conn.execute("ALTER TABLE sessions ADD COLUMN project_key TEXT DEFAULT ''")
        rows = conn.execute("SELECT session_id, project_path FROM sessions").fetchall()
        conn.executemany(
            "UPDATE sessions SET project_key = ? WHERE session_id = ?",
            (
                (normalize_project_path(row["project_path"]), row["session_id"])
                for row in rows
            ),
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_project_updated "
            "ON sessions(project_key, updated_at DESC)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (4, _serialize_datetime(_utc_now())),
        )

    def _migrate_v5(self, conn: sqlite3.Connection) -> None:
        """Track the estimated portions of persisted token and cost totals."""

        for col_spec in (
            ("estimated_prompt_tokens", "INTEGER DEFAULT 0"),
            ("estimated_completion_tokens", "INTEGER DEFAULT 0"),
            ("estimated_cost_usd", "REAL DEFAULT 0"),
        ):
            col_name, col_type = col_spec
            if not _column_exists(conn, "sessions", col_name):
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {col_name} {col_type}")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (5, _serialize_datetime(_utc_now())),
        )

    def _migrate_v6(self, conn: sqlite3.Connection) -> None:
        """Link transcript records to turns and retain rewindable usage."""

        if not _column_exists(conn, "messages", "turn_id"):
            conn.execute("ALTER TABLE messages ADD COLUMN turn_id TEXT")
        if not _column_exists(conn, "tool_calls", "turn_id"):
            conn.execute("ALTER TABLE tool_calls ADD COLUMN turn_id TEXT")
        if not _column_exists(conn, "turn_journal", "usage_json"):
            conn.execute(
                "ALTER TABLE turn_journal ADD COLUMN usage_json TEXT DEFAULT '{}'"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session_turn "
            "ON messages(session_id, turn_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_calls_session_turn "
            "ON tool_calls(session_id, turn_id)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (6, _serialize_datetime(_utc_now())),
        )

    def _migrate_v7(self, conn: sqlite3.Connection) -> None:
        """Identify checkpointed tool calls and persist crash recovery outcomes."""

        if not _column_exists(conn, "file_checkpoints", "call_id"):
            conn.executescript(
                """
                CREATE TABLE file_checkpoints_v7 (
                    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    existed INTEGER NOT NULL CHECK(existed IN (0, 1)),
                    before_content BLOB,
                    before_mode INTEGER,
                    after_sha256 TEXT,
                    restored INTEGER NOT NULL DEFAULT 0 CHECK(restored IN (0, 1)),
                    created_at TIMESTAMP NOT NULL,
                    call_id TEXT NOT NULL DEFAULT '',
                    UNIQUE(session_id, turn_id, call_id, path),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );
                INSERT INTO file_checkpoints_v7 (
                    checkpoint_id, session_id, turn_id, tool_name, path, existed,
                    before_content, before_mode, after_sha256, restored, created_at,
                    call_id
                )
                SELECT checkpoint_id, session_id, turn_id, tool_name, path, existed,
                       before_content, before_mode, after_sha256, restored, created_at,
                       ''
                FROM file_checkpoints;
                DROP TABLE file_checkpoints;
                ALTER TABLE file_checkpoints_v7 RENAME TO file_checkpoints;
                """
            )
        if not _column_exists(conn, "turn_journal", "recovery_json"):
            conn.execute(
                "ALTER TABLE turn_journal ADD COLUMN recovery_json TEXT DEFAULT '{}'"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_checkpoints_call "
            "ON file_checkpoints(session_id, turn_id, call_id)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (7, _serialize_datetime(_utc_now())),
        )

    def _migrate_v8(self, conn: sqlite3.Connection) -> None:
        """Add the append-only versioned runtime event log."""

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL,
                turn_id TEXT,
                operation_id TEXT,
                event_type TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                event_json TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_runtime_events_session_sequence
                ON runtime_events(session_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_runtime_events_turn_sequence
                ON runtime_events(session_id, turn_id, sequence);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (8, _serialize_datetime(_utc_now())),
        )

    def _migrate_v9(self, conn: sqlite3.Connection) -> None:
        """Add durable conversation-branch lineage."""

        for column, definition in (
            ("parent_session_id", "TEXT"),
            ("root_session_id", "TEXT DEFAULT ''"),
            ("fork_message_count", "INTEGER"),
            ("branch_name", "TEXT DEFAULT ''"),
            ("branch_summary", "TEXT DEFAULT ''"),
            ("depth", "INTEGER DEFAULT 0"),
        ):
            if not _column_exists(conn, "sessions", column):
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} {definition}")
        conn.execute(
            "UPDATE sessions SET root_session_id = session_id "
            "WHERE root_session_id IS NULL OR root_session_id = ''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_parent "
            "ON sessions(parent_session_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_root_depth "
            "ON sessions(root_session_id, depth, created_at)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (9, _serialize_datetime(_utc_now())),
        )

    def _migrate_v10(self, conn: sqlite3.Connection) -> None:
        """Distinguish persisted tool intent from a started tool dispatch."""

        if not _column_exists(conn, "tool_calls", "dispatched"):
            conn.execute(
                "ALTER TABLE tool_calls ADD COLUMN dispatched INTEGER NOT NULL "
                "DEFAULT 0 CHECK(dispatched IN (0, 1))"
            )
            # Older records cannot prove whether the call reached its tool.
            # Preserve safety by treating every approved unfinished legacy call
            # as potentially dispatched during recovery.
            conn.execute(
                "UPDATE tool_calls SET dispatched = 1 "
                "WHERE approved = 1 AND executed = 0"
            )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (10, _serialize_datetime(_utc_now())),
        )

    def _migrate_v11(self, conn: sqlite3.Connection) -> None:
        """Allow non-tool permission decisions in the audit hash chain."""

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS audit_logs_v11 (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                action_type TEXT CHECK(action_type IN (
                    'tool_call', 'command_run', 'file_write',
                    'safety_block', 'user_approval', 'permission_mode'
                )),
                target_resource TEXT NOT NULL,
                details_json TEXT NOT NULL,
                result TEXT CHECK(result IN (
                    'APPROVED', 'DENIED', 'BLOCKED_BY_GUARD', 'SUCCESS', 'FAILURE'
                )),
                sha256_hash TEXT,
                previous_hash TEXT DEFAULT ''
            );

            INSERT INTO audit_logs_v11 (
                session_id, timestamp, action_type, target_resource,
                details_json, result, sha256_hash, previous_hash
            )
            SELECT session_id, timestamp, action_type, target_resource,
                   details_json, result, sha256_hash, previous_hash
            FROM audit_logs ORDER BY log_id;

            DROP TABLE audit_logs;
            ALTER TABLE audit_logs_v11 RENAME TO audit_logs;
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_logs(session_id)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
            "VALUES (?, ?)",
            (11, _serialize_datetime(_utc_now())),
        )

    def _migrate_v12(self, conn: sqlite3.Connection) -> None:
        """Persist MCP 2026 task handles for crash-safe continuation."""

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mcp_tasks (
                task_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                server_name TEXT NOT NULL,
                remote_tool_name TEXT NOT NULL,
                contract_fingerprint TEXT NOT NULL,
                protocol_version TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'working', 'input_required', 'completed', 'cancelled', 'failed'
                )),
                task_json TEXT NOT NULL,
                answered_inputs_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
                PRIMARY KEY(server_name, task_id),
                UNIQUE(session_id, call_id)
            );
            CREATE INDEX IF NOT EXISTS idx_mcp_tasks_session_status
                ON mcp_tasks(session_id, status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_mcp_tasks_session_turn
                ON mcp_tasks(session_id, turn_id);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
            "VALUES (?, ?)",
            (12, _serialize_datetime(_utc_now())),
        )

    def _migrate_v13(self, conn: sqlite3.Connection) -> None:
        """Bind durable MCP task handles to the originating server identity."""

        if not _column_exists(conn, "mcp_tasks", "server_fingerprint"):
            conn.execute(
                "ALTER TABLE mcp_tasks ADD COLUMN server_fingerprint TEXT "
                "NOT NULL DEFAULT ''"
            )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
            "VALUES (?, ?)",
            (13, _serialize_datetime(_utc_now())),
        )

    def _migrate_v14(self, conn: sqlite3.Connection) -> None:
        """Scope durable tool-call and runtime-event identities to sessions."""

        conn.executescript(
            """
            CREATE TABLE tool_calls_v14 (
                call_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                approved INTEGER CHECK(approved IN (0, 1)) DEFAULT 0,
                executed INTEGER CHECK(executed IN (0, 1)) DEFAULT 0,
                dispatched INTEGER CHECK(dispatched IN (0, 1)) DEFAULT 0,
                result TEXT,
                error TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                turn_id TEXT,
                FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
                PRIMARY KEY(session_id, call_id)
            );

            INSERT INTO tool_calls_v14 (
                call_id, session_id, tool_name, arguments_json, approved,
                executed, dispatched, result, error, timestamp, turn_id
            )
            SELECT call_id, session_id, tool_name, arguments_json, approved,
                   executed, dispatched, result, error, timestamp, turn_id
            FROM tool_calls;

            DROP TABLE tool_calls;
            ALTER TABLE tool_calls_v14 RENAME TO tool_calls;

            CREATE INDEX idx_tool_calls_session
                ON tool_calls(session_id);
            CREATE INDEX idx_tool_calls_session_turn
                ON tool_calls(session_id, turn_id);

            CREATE TABLE runtime_events_v14 (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id TEXT,
                operation_id TEXT,
                event_type TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                event_json TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
                UNIQUE(session_id, event_id)
            );

            INSERT INTO runtime_events_v14 (
                sequence, event_id, session_id, turn_id, operation_id,
                event_type, schema_version, timestamp, event_json
            )
            SELECT sequence, event_id, session_id, turn_id, operation_id,
                   event_type, schema_version, timestamp, event_json
            FROM runtime_events ORDER BY sequence;

            DROP TABLE runtime_events;
            ALTER TABLE runtime_events_v14 RENAME TO runtime_events;

            CREATE INDEX idx_runtime_events_session_sequence
                ON runtime_events(session_id, sequence);
            CREATE INDEX idx_runtime_events_turn_sequence
                ON runtime_events(session_id, turn_id, sequence);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
            "VALUES (?, ?)",
            (14, _serialize_datetime(_utc_now())),
        )

    def _migrate_v15(self, conn: sqlite3.Connection) -> None:
        """Persist how much durable history the current context summary covers."""

        if not _column_exists(conn, "sessions", "context_summary_message_count"):
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN context_summary_message_count "
                "INTEGER NOT NULL DEFAULT 0 "
                "CHECK(context_summary_message_count >= 0)"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sprints_session_state_created "
            "ON sprints(session_id, state, created_at DESC)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
            "VALUES (?, ?)",
            (15, _serialize_datetime(_utc_now())),
        )

    def _migrate_v16(self, conn: sqlite3.Connection) -> None:
        """Add durable session-scoped Goal lifecycle state."""

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS goals (
                goal_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                objective TEXT NOT NULL,
                state TEXT NOT NULL
                    CHECK(state IN (
                        'active','paused','complete','budget_limited','cleared'
                    )),
                max_continuations INTEGER NOT NULL
                    CHECK(max_continuations BETWEEN 1 AND 100),
                continuations_used INTEGER NOT NULL DEFAULT 0
                    CHECK(continuations_used >= 0),
                last_evidence TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                completed_at TIMESTAMP,
                FOREIGN KEY(session_id)
                    REFERENCES sessions(session_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_goals_session_updated
                ON goals(session_id, updated_at DESC, goal_id DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_goals_one_current_per_session
                ON goals(session_id)
                WHERE state IN ('active','paused','budget_limited');
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
            "VALUES (?, ?)",
            (16, _serialize_datetime(_utc_now())),
        )

    def backup(
        self, destination: str | Path | None = None, *, reason: str = "manual"
    ) -> Path:
        """Create a consistent SQLite backup without modifying the source."""

        source_path = Path(self.db_path)
        if not source_path.is_file():
            raise SessionStorageError(f"Session database does not exist: {source_path}")
        if destination is None:
            timestamp = _utc_now().strftime("%Y%m%dT%H%M%S%fZ")
            destination_path = source_path.with_name(
                f"{source_path.name}.{reason}.{timestamp}.backup"
            )
        else:
            destination_path = Path(
                os.path.abspath(Path(destination).expanduser())
            )
        if destination_path == source_path:
            raise SessionStorageError(
                "Backup destination must differ from the database"
            )
        try:
            with AnchoredDirectory.open(
                destination_path.parent,
                create=True,
                private=False,
                pin_path=True,
            ) as destination_directory:
                destination_metadata = destination_directory.stat(destination_path.name)
                if destination_metadata is not None and stat.S_ISLNK(
                    destination_metadata.st_mode
                ):
                    raise SessionStorageError(
                        "refusing to use session backup through a symlink or junction: "
                        f"{destination_path}"
                    )
                if destination_metadata is not None:
                    raise FileExistsError(destination_path)
                descriptor = destination_directory.open_file(
                    destination_path.name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL,
                    0o600,
                    expected_type=stat.S_IFREG,
                )
                if hasattr(os, "fchmod") and os.name != "nt":
                    os.fchmod(descriptor, 0o600)
                completed = False
                try:
                    with self._database.parent_directory(
                        label="session database"
                    ) as source_directory:
                        source_metadata = source_directory.stat(source_path.name)
                        if (
                            source_metadata is None
                            or stat.S_ISLNK(source_metadata.st_mode)
                            or not stat.S_ISREG(source_metadata.st_mode)
                            or int(source_metadata.st_dev) != self._database.file_device
                            or int(source_metadata.st_ino) != self._database.file_inode
                        ):
                            raise SessionStorageError(
                                "Session database file identity changed before backup"
                            )

                        with exclusive_database_access(
                            source_path,
                            coordination_parent_descriptor=source_directory.descriptor,
                        ) as locked_source:
                            if locked_source != source_path:
                                raise SessionStorageError(
                                    "Session database coordination changed the backup path"
                                )
                            source_directory.validation_path()
                            _require_quiescent_backup_source(
                                source_directory,
                                locked_source,
                            )
                            source_descriptor = source_directory.open_file(
                                source_path.name,
                                os.O_RDONLY,
                                expected=source_metadata,
                                expected_type=stat.S_IFREG,
                            )
                            try:
                                before = os.fstat(source_descriptor)
                                if (
                                    int(before.st_dev) != self._database.file_device
                                    or int(before.st_ino) != self._database.file_inode
                                ):
                                    raise SessionStorageError(
                                        "Session database file identity changed before backup"
                                    )
                                _require_quiescent_backup_source(
                                    source_directory,
                                    locked_source,
                                )
                                _copy_descriptor(source_descriptor, descriptor)
                                after = os.fstat(source_descriptor)
                                if (
                                    before.st_size,
                                    before.st_mtime_ns,
                                    before.st_ctime_ns,
                                    before.st_nlink,
                                ) != (
                                    after.st_size,
                                    after.st_mtime_ns,
                                    after.st_ctime_ns,
                                    after.st_nlink,
                                ):
                                    raise SessionStorageError(
                                        "Session database changed while secure backup was in progress"
                                    )
                                _require_quiescent_backup_source(
                                    source_directory,
                                    locked_source,
                                )
                                source_directory.validation_path()
                            finally:
                                os.close(source_descriptor)
                            _validate_backup_descriptor(descriptor)
                    os.fsync(descriptor)
                    destination_directory.validation_path()
                    completed = True
                finally:
                    if not completed:
                        try:
                            destination_directory.unlink(
                                destination_path.name,
                                missing_ok=True,
                                expected_descriptor=descriptor,
                            )
                        except BaseException:
                            pass
                    os.close(descriptor)
        except FileExistsError as exc:
            raise SessionStorageError(
                f"Backup destination already exists: {destination_path}"
            ) from exc
        except AnchoredFilesystemError as exc:
            raise SessionStorageError(str(exc)) from exc
        except sqlite3.DatabaseError as exc:
            raise SessionStorageError(
                f"Could not securely back up session database: {exc}"
            ) from exc
        except OSError as exc:
            raise SessionStorageError(
                f"Could not securely back up session database: {exc}"
            ) from exc
        except ValueError as exc:
            raise SessionStorageError(str(exc)) from exc
        return destination_path

    def _init_db(self) -> None:
        """Create session and audit tables if they do not exist."""

        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    project_path TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    title TEXT DEFAULT '',
                    updated_at TIMESTAMP,
                    context_summary TEXT DEFAULT '',
                    context_summary_message_count INTEGER NOT NULL DEFAULT 0
                        CHECK(context_summary_message_count >= 0),
                    model TEXT DEFAULT '',
                    total_tokens INTEGER DEFAULT 0,
                    total_cost_inr REAL DEFAULT 0,
                    total_cost_usd REAL DEFAULT 0,
                    total_prompt_tokens INTEGER DEFAULT 0,
                    total_completion_tokens INTEGER DEFAULT 0,
                    total_cache_read_tokens INTEGER DEFAULT 0,
                    total_cache_write_tokens INTEGER DEFAULT 0,
                    estimated_prompt_tokens INTEGER DEFAULT 0,
                    estimated_completion_tokens INTEGER DEFAULT 0,
                    estimated_cost_usd REAL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT CHECK(role IN ('system', 'user', 'assistant', 'tool')),
                    content TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    metadata_json TEXT,
                    token_count INTEGER DEFAULT 0,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    turn_id TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS tool_calls (
                    call_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    approved INTEGER CHECK(approved IN (0, 1)) DEFAULT 0,
                    executed INTEGER CHECK(executed IN (0, 1)) DEFAULT 0,
                    dispatched INTEGER CHECK(dispatched IN (0, 1)) DEFAULT 0,
                    result TEXT,
                    error TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    turn_id TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
                CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);

                CREATE TABLE IF NOT EXISTS audit_logs (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    action_type TEXT CHECK(action_type IN (
                        'tool_call',
                        'command_run',
                        'file_write',
                        'safety_block',
                        'user_approval',
                        'permission_mode'
                    )),
                    target_resource TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    result TEXT CHECK(result IN (
                        'APPROVED',
                        'DENIED',
                        'BLOCKED_BY_GUARD',
                        'SUCCESS',
                        'FAILURE'
                    )),
                    sha256_hash TEXT,
                    previous_hash TEXT DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_logs(session_id);

                CREATE TABLE IF NOT EXISTS turn_journal (
                    turn_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('started','completed','interrupted')),
                    user_input TEXT NOT NULL,
                    started_at TIMESTAMP NOT NULL,
                    completed_at TIMESTAMP,
                    usage_json TEXT DEFAULT '{}',
                    recovery_json TEXT DEFAULT '{}',
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_turn_journal_session
                    ON turn_journal(session_id, status);

                CREATE TABLE IF NOT EXISTS file_checkpoints (
                    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    existed INTEGER NOT NULL CHECK(existed IN (0, 1)),
                    before_content BLOB,
                    before_mode INTEGER,
                    after_sha256 TEXT,
                    restored INTEGER NOT NULL DEFAULT 0 CHECK(restored IN (0, 1)),
                    created_at TIMESTAMP NOT NULL,
                    call_id TEXT NOT NULL DEFAULT '',
                    UNIQUE(session_id, turn_id, call_id, path),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS sprints (
                    sprint_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('planning','active','complete','aborted')),
                    contract_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    abort_reason TEXT DEFAULT '',
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS checklist_items (
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sprint_id TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    section TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','in_progress','done','skipped','failed')),
                    notes TEXT DEFAULT '',
                    UNIQUE(sprint_id, idx),
                    FOREIGN KEY(sprint_id) REFERENCES sprints(sprint_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_sprints_session ON sprints(session_id);
                CREATE INDEX IF NOT EXISTS idx_sprints_session_state_created
                    ON sprints(session_id, state, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_checklist_sprint ON checklist_items(sprint_id);

                CREATE TABLE IF NOT EXISTS goals (
                    goal_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    state TEXT NOT NULL
                        CHECK(state IN (
                            'active','paused','complete','budget_limited','cleared'
                        )),
                    max_continuations INTEGER NOT NULL
                        CHECK(max_continuations BETWEEN 1 AND 100),
                    continuations_used INTEGER NOT NULL DEFAULT 0
                        CHECK(continuations_used >= 0),
                    last_evidence TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    completed_at TIMESTAMP,
                    FOREIGN KEY(session_id)
                        REFERENCES sessions(session_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_goals_session_updated
                    ON goals(session_id, updated_at DESC, goal_id DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_goals_one_current_per_session
                    ON goals(session_id)
                    WHERE state IN ('active','paused','budget_limited');

                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMP NOT NULL
                );
                """
            )

    def _create_session_record(
        self,
        conn: sqlite3.Connection,
        project_path: str,
        *,
        session_id: str | None = None,
        model: str = "",
        parent_session_id: str | None = None,
        fork_message_count: int | None = None,
        branch_name: str = "",
        branch_summary: str = "",
    ) -> Session:
        canonical_project_path = normalize_project_path(project_path)
        session_id = session_id or str(uuid4())
        model = _validate_session_model(model)
        normalized_branch_name, normalized_branch_summary = _normalize_branch_metadata(
            branch_name, branch_summary
        )
        root_session_id = session_id
        depth = 0
        if parent_session_id is None and fork_message_count is not None:
            raise ValueError("fork_message_count requires parent_session_id")
        if parent_session_id is not None:
            parent = conn.execute(
                "SELECT project_key, root_session_id, depth, "
                "(SELECT COUNT(*) FROM messages WHERE session_id = ?) "
                "AS message_count FROM sessions WHERE session_id = ?",
                (parent_session_id, parent_session_id),
            ).fetchone()
            if parent is None:
                raise KeyError(f"Session not found: {parent_session_id}")
            if parent["project_key"] != canonical_project_path:
                raise ValueError("parent session belongs to a different project")
            if fork_message_count is None or fork_message_count < 0:
                raise ValueError("fork_message_count must be non-negative for a branch")
            parent_message_count = int(parent["message_count"])
            if fork_message_count > parent_message_count:
                raise ValueError(
                    f"fork_message_count cannot exceed parent message count "
                    f"({parent_message_count})"
                )
            root_session_id = parent["root_session_id"] or parent_session_id
            depth = int(parent["depth"] or 0) + 1
        session = Session(
            session_id=session_id,
            project_path=canonical_project_path,
            created_at=_utc_now(),
            updated_at=_utc_now(),
            model=model,
            parent_session_id=parent_session_id,
            root_session_id=root_session_id,
            fork_message_count=fork_message_count,
            branch_name=normalized_branch_name,
            branch_summary=normalized_branch_summary,
            depth=depth,
        )
        updated_at = session.updated_at or session.created_at

        conn.execute(
            """
            INSERT INTO sessions (
                session_id, project_path, project_key, created_at, updated_at,
                model, parent_session_id, root_session_id, fork_message_count,
                branch_name, branch_summary, depth
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.session_id,
                session.project_path,
                canonical_project_path,
                _serialize_datetime(session.created_at),
                _serialize_datetime(updated_at),
                session.model,
                session.parent_session_id,
                session.root_session_id,
                session.fork_message_count,
                session.branch_name,
                session.branch_summary,
                session.depth,
            ),
        )

        return session

    def create_session(
        self,
        project_path: str,
        *,
        model: str = "",
    ) -> Session:
        """Create a new session record in SQLite and return its model."""

        with closing(self._connect()) as conn, conn:
            return self._create_session_record(
                conn,
                project_path,
                model=model,
            )

    def load_session(
        self,
        session_id: str,
        *,
        runtime_window: bool = False,
    ) -> Session:
        """Load a session, optionally using its persisted live-history window."""

        with closing(self._connect()) as conn, conn:
            session_row = conn.execute(
                """
                SELECT session_id, project_path, created_at, title, updated_at,
                       context_summary, context_summary_message_count, model,
                       parent_session_id, root_session_id,
                       fork_message_count, branch_name, branch_summary, depth
                FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()

            if session_row is None:
                raise KeyError(f"Session not found: {session_id}")

            summary_message_count = int(
                session_row["context_summary_message_count"] or 0
            )
            if summary_message_count < 0:
                raise _invalid_stored_data_error(self.db_path)
            if summary_message_count and not str(session_row["context_summary"] or ""):
                raise _invalid_stored_data_error(self.db_path)

            message_offset = 0
            if runtime_window and summary_message_count:
                total_messages = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()[0]
                )
                if summary_message_count > total_messages:
                    raise _invalid_stored_data_error(self.db_path)
                message_offset = summary_message_count

            message_rows = conn.execute(
                """
                SELECT role, content, timestamp, metadata_json
                FROM messages
                WHERE session_id = ?
                ORDER BY message_id ASC
                LIMIT -1 OFFSET ?
                """,
                (session_id, message_offset),
            ).fetchall()

            if runtime_window:
                tool_call_rows = conn.execute(
                    """
                    SELECT call_id, tool_name, arguments_json, approved, executed,
                           dispatched, result, error, timestamp
                    FROM tool_calls
                    WHERE session_id = ?
                    ORDER BY timestamp DESC, call_id DESC
                    LIMIT ?
                    """,
                    (session_id, MAX_RUNTIME_SESSION_TOOL_CALLS),
                ).fetchall()
                tool_call_rows.reverse()
            else:
                tool_call_rows = conn.execute(
                    """
                    SELECT call_id, tool_name, arguments_json, approved, executed,
                           dispatched, result, error, timestamp
                    FROM tool_calls
                    WHERE session_id = ?
                    ORDER BY timestamp ASC, call_id ASC
                    """,
                    (session_id,),
                ).fetchall()

        try:
            session = Session(
                session_id=session_row["session_id"],
                project_path=session_row["project_path"],
                created_at=_deserialize_datetime(session_row["created_at"]),
                title=session_row["title"] or "",
                updated_at=_deserialize_datetime(
                    session_row["updated_at"] or session_row["created_at"]
                ),
                context_summary=session_row["context_summary"] or "",
                context_summary_message_count=summary_message_count,
                model=session_row["model"] or "",
                parent_session_id=session_row["parent_session_id"],
                root_session_id=session_row["root_session_id"]
                or session_row["session_id"],
                fork_message_count=session_row["fork_message_count"],
                branch_name=session_row["branch_name"] or "",
                branch_summary=session_row["branch_summary"] or "",
                depth=int(session_row["depth"] or 0),
                messages=[
                    Message(
                        role=row["role"],
                        content=row["content"],
                        timestamp=_deserialize_datetime(row["timestamp"]),
                        metadata=json.loads(row["metadata_json"] or "{}"),
                    )
                    for row in message_rows
                ],
                tool_calls=[
                    ToolCallRecord(
                        call_id=row["call_id"],
                        tool_name=row["tool_name"],
                        arguments=json.loads(row["arguments_json"]),
                        approved=bool(row["approved"]),
                        executed=bool(row["executed"]),
                        dispatched=bool(row["dispatched"]),
                        result=row["result"],
                        error=row["error"],
                        timestamp=_deserialize_datetime(row["timestamp"]),
                    )
                    for row in tool_call_rows
                ],
            )
            if runtime_window:
                session._resident_message_offset = message_offset
            return session
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise _invalid_stored_data_error(self.db_path) from exc

    def require_session_project(self, session_id: str, project_path: str | Path) -> None:
        """Refuse access to a session outside the requested canonical project."""

        project_key = normalize_project_path(project_path)
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT project_key FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Session not found: {session_id}")
        if str(row["project_key"]) != project_key:
            raise ValueError("session belongs to a different workspace")

    def save_message(
        self,
        session_id: str,
        message: Message,
        token_count: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        turn_id: str | None = None,
    ) -> None:
        """Append a single message to a session."""

        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO messages (
                    session_id, role, content, timestamp, metadata_json,
                    token_count, prompt_tokens, completion_tokens, turn_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    message.role,
                    message.content,
                    _serialize_datetime(message.timestamp),
                    json.dumps(message.metadata),
                    token_count,
                    prompt_tokens,
                    completion_tokens,
                    turn_id,
                ),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (_serialize_datetime(message.timestamp), session_id),
            )

    def list_sessions(
        self,
        *,
        project_path: str | None = None,
        limit: int = 20,
        query: str = "",
    ) -> list[SessionSummary]:
        """List recent sessions, optionally filtered by project and title."""

        if not 1 <= limit <= MAX_SESSION_LIST_LIMIT:
            raise ValueError(
                f"limit must be between 1 and {MAX_SESSION_LIST_LIMIT}"
            )
        clauses: list[str] = []
        params: list[Any] = []
        if project_path is not None:
            clauses.append("s.project_key = ?")
            params.append(normalize_project_path(project_path))
        if query:
            clauses.append(
                "(s.title LIKE ? OR s.session_id LIKE ? OR s.context_summary LIKE ?)"
            )
            pattern = f"%{query}%"
            params.extend((pattern, pattern, pattern))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT s.session_id, s.project_path, s.title, s.created_at,
                       COALESCE(s.updated_at, s.created_at) AS updated_at,
                       COUNT(m.message_id) AS message_count, s.model,
                       s.parent_session_id, s.root_session_id,
                       s.fork_message_count, s.branch_name, s.depth
                       , s.context_summary
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.session_id
                {where}
                GROUP BY s.session_id
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        try:
            return [
                SessionSummary(
                    session_id=row["session_id"],
                    project_path=row["project_path"],
                    title=row["title"] or "",
                    created_at=_deserialize_datetime(row["created_at"]),
                    updated_at=_deserialize_datetime(row["updated_at"]),
                    message_count=int(row["message_count"]),
                    model=row["model"] or "",
                    parent_session_id=row["parent_session_id"],
                    root_session_id=row["root_session_id"] or row["session_id"],
                    fork_message_count=row["fork_message_count"],
                    branch_name=row["branch_name"] or "",
                    depth=int(row["depth"] or 0),
                    context_summary=row["context_summary"] or "",
                )
                for row in rows
            ]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise _invalid_stored_data_error(self.db_path) from exc

    def latest_session(self, project_path: str) -> SessionSummary | None:
        """Return the most recently updated session in one project."""

        sessions = self.list_sessions(project_path=project_path, limit=1)
        return sessions[0] if sessions else None

    def session_exists(self, session_id: str) -> bool:
        """Return whether one session row exists without loading transcript state."""

        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
        return row is not None

    def resolve_session(self, reference: str, project_path: str) -> SessionSummary:
        """Resolve an exact ID or title within one canonical project scope."""

        normalized_reference = " ".join(reference.split())
        if not normalized_reference:
            raise SessionResolutionError("session reference must not be empty")
        project_key = normalize_project_path(project_path)
        with closing(self._connect()) as conn:
            exact = conn.execute(
                """
                SELECT s.session_id, s.project_path, s.title, s.created_at,
                       COALESCE(s.updated_at, s.created_at) AS updated_at,
                       COUNT(m.message_id) AS message_count, s.model,
                       s.parent_session_id, s.root_session_id,
                       s.fork_message_count, s.branch_name, s.depth
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.session_id
                WHERE s.project_key = ? AND s.session_id = ?
                GROUP BY s.session_id
                """,
                (project_key, normalized_reference),
            ).fetchone()
            if exact is not None:
                row = exact
            else:
                rows = conn.execute(
                    """
                    SELECT s.session_id, s.project_path, s.title, s.created_at,
                           COALESCE(s.updated_at, s.created_at) AS updated_at,
                           COUNT(m.message_id) AS message_count, s.model,
                           s.parent_session_id, s.root_session_id,
                           s.fork_message_count, s.branch_name, s.depth
                    FROM sessions s
                    LEFT JOIN messages m ON m.session_id = s.session_id
                    WHERE s.project_key = ? AND s.title = ? COLLATE NOCASE
                    GROUP BY s.session_id
                    ORDER BY updated_at DESC
                    LIMIT 2
                    """,
                    (project_key, normalized_reference),
                ).fetchall()
                if len(rows) > 1:
                    raise SessionResolutionError(
                        f"session title {normalized_reference!r} is ambiguous; "
                        "resume by session ID"
                    )
                if rows:
                    row = rows[0]
                else:
                    row = None
            if row is None:
                elsewhere = conn.execute(
                    "SELECT project_path FROM sessions WHERE session_id = ?",
                    (normalized_reference,),
                ).fetchone()
                if elsewhere is not None:
                    raise SessionResolutionError(
                        "session belongs to a different project: "
                        f"{elsewhere['project_path']}"
                    )
                raise SessionResolutionError(
                    f"no session named or identified by {normalized_reference!r} "
                    "exists in this project"
                )
        try:
            return SessionSummary(
                session_id=row["session_id"],
                project_path=row["project_path"],
                title=row["title"] or "",
                created_at=_deserialize_datetime(row["created_at"]),
                updated_at=_deserialize_datetime(row["updated_at"]),
                message_count=int(row["message_count"]),
                model=row["model"] or "",
                parent_session_id=row["parent_session_id"],
                root_session_id=row["root_session_id"] or row["session_id"],
                fork_message_count=row["fork_message_count"],
                branch_name=row["branch_name"] or "",
                depth=int(row["depth"] or 0),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise _invalid_stored_data_error(self.db_path) from exc

    def get_session_usage(self, session_id: str) -> SessionUsage:
        """Return persisted token and explicitly configured cost totals."""

        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COALESCE(total_tokens, 0) AS total_tokens, "
                "COALESCE(total_prompt_tokens, 0) AS prompt_tokens, "
                "COALESCE(total_completion_tokens, 0) AS completion_tokens, "
                "COALESCE(total_cache_read_tokens, 0) AS cache_read_tokens, "
                "COALESCE(total_cache_write_tokens, 0) AS cache_write_tokens, "
                "COALESCE(total_cost_usd, 0) AS cost_usd, "
                "COALESCE(estimated_prompt_tokens, 0) AS estimated_prompt_tokens, "
                "COALESCE(estimated_completion_tokens, 0) AS estimated_completion_tokens, "
                "COALESCE(estimated_cost_usd, 0) AS estimated_cost_usd "
                "FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Session not found: {session_id}")
        return SessionUsage(**dict(row))

    def local_metrics_summary(self) -> dict[str, Any]:
        """Return aggregate local-only model usage metrics."""

        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS session_count,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(total_prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(total_completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(total_cache_write_tokens), 0) AS cache_write_tokens,
                       COALESCE(SUM(total_cost_usd), 0) AS cost_usd,
                       COALESCE(SUM(estimated_prompt_tokens), 0)
                           AS estimated_prompt_tokens,
                       COALESCE(SUM(estimated_completion_tokens), 0)
                           AS estimated_completion_tokens,
                       COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
                FROM sessions
                """
            ).fetchone()
        assert row is not None
        return dict(row)

    def cleanup_sessions(
        self, retention_days: int, *, project_path: str | None = None
    ) -> int:
        """Delete old sessions and compact the database explicitly."""
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        try:
            cutoff = _utc_now() - timedelta(days=retention_days)
        except OverflowError:
            # No persisted datetime can be older than an unrepresentable cutoff.
            return 0
        clause = " WHERE project_key = ?" if project_path is not None else ""
        params: tuple[Any, ...] = (
            (normalize_project_path(project_path), _serialize_datetime(cutoff))
            if project_path is not None
            else (_serialize_datetime(cutoff),)
        )
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE root_session_id IN ("
                "SELECT root_session_id FROM sessions"
                + clause
                + " GROUP BY root_session_id "
                "HAVING MAX(COALESCE(updated_at, created_at)) < ?) ",
                params,
            )
            deleted = cursor.rowcount
        with closing(self._connect()) as conn:
            conn.execute("VACUUM")
        return deleted

    def start_turn(self, session_id: str, turn_id: str, user_input: str) -> None:
        """Persist intent before provider or tool work starts."""
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO turn_journal "
                "(turn_id, session_id, status, user_input, started_at) "
                "VALUES (?, ?, 'started', ?, ?)",
                (turn_id, session_id, user_input, _serialize_datetime(_utc_now())),
            )

    def complete_turn(self, turn_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE turn_journal SET status = 'completed', completed_at = ? "
                "WHERE turn_id = ?",
                (_serialize_datetime(_utc_now()), turn_id),
            )

    def save_turn_usage(self, turn_id: str, usage: dict[str, Any]) -> None:
        """Persist normalized usage so rewinding can adjust session totals."""

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "UPDATE turn_journal SET usage_json = ? WHERE turn_id = ?",
                (json.dumps(usage, sort_keys=True), turn_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Turn not found: {turn_id}")

    def interrupt_turn(self, turn_id: str) -> None:
        """Mark one in-flight turn interrupted without affecting other sessions."""

        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE turn_journal SET status = 'interrupted', completed_at = ? "
                "WHERE turn_id = ? AND status = 'started'",
                (_serialize_datetime(_utc_now()), turn_id),
            )

    def reconcile_interrupted_turns(self, session_id: str) -> int:
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "UPDATE turn_journal SET status = 'interrupted', completed_at = ? "
                "WHERE session_id = ? AND status = 'started'",
                (_serialize_datetime(_utc_now()), session_id),
            )
            return cursor.rowcount

    def started_turns(self, session_id: str) -> list[sqlite3.Row]:
        """Return unfinished turns newest-first without mutating them."""

        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM turn_journal WHERE session_id = ? "
                "AND status = 'started' ORDER BY started_at DESC, turn_id DESC",
                (session_id,),
            ).fetchall()

    def recoverable_turns(self, session_id: str) -> list[sqlite3.Row]:
        """Return turns that still require crash-consistency recovery."""

        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM turn_journal WHERE session_id = ? "
                "AND (status = 'started' OR (status = 'interrupted' "
                "AND COALESCE(recovery_json, '{}') = '{}')) "
                "ORDER BY started_at DESC, turn_id DESC",
                (session_id,),
            ).fetchall()
            recoverable: list[sqlite3.Row] = []
            for row in rows:
                if str(row["status"]) == "started":
                    recoverable.append(row)
                    continue
                turn_id = str(row["turn_id"])
                pending = conn.execute(
                    "SELECT 1 FROM tool_calls WHERE session_id = ? AND turn_id = ? "
                    "AND approved = 1 AND executed = 0 "
                    "AND result IS NULL AND error IS NULL LIMIT 1",
                    (session_id, turn_id),
                ).fetchone()
                incomplete_checkpoint = conn.execute(
                    "SELECT 1 FROM file_checkpoints WHERE session_id = ? "
                    "AND turn_id = ? AND restored = 0 AND after_sha256 IS NULL "
                    "LIMIT 1",
                    (session_id, turn_id),
                ).fetchone()
                if (
                    pending is not None
                    or incomplete_checkpoint is not None
                    or self._assistant_tool_calls_missing_results_in_connection(
                        conn, session_id, turn_id
                    )
                ):
                    recoverable.append(row)
            return recoverable

    def assistant_tool_calls_missing_results(
        self, session_id: str, turn_id: str
    ) -> list[dict[str, Any]]:
        """Return assistant tool calls that lack a matching model-visible result."""

        with closing(self._connect()) as conn:
            return self._assistant_tool_calls_missing_results_in_connection(
                conn, session_id, turn_id
            )

    @staticmethod
    def _assistant_tool_calls_missing_results_in_connection(
        conn: sqlite3.Connection,
        session_id: str,
        turn_id: str,
    ) -> list[dict[str, Any]]:
        durable_rows = conn.execute(
            "SELECT call_id, tool_name, arguments_json, approved, executed, dispatched, "
            "result, error FROM tool_calls WHERE session_id = ? AND turn_id = ?",
            (session_id, turn_id),
        ).fetchall()
        durable_by_id = {str(row["call_id"]): row for row in durable_rows}
        result_ids: set[str] = set()
        for row in conn.execute(
            "SELECT metadata_json FROM messages WHERE session_id = ? "
            "AND turn_id = ? AND role = 'tool'",
            (session_id, turn_id),
        ).fetchall():
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(metadata, dict) and metadata.get("call_id"):
                result_ids.add(str(metadata["call_id"]))

        missing: dict[str, dict[str, Any]] = {}
        rows = conn.execute(
            "SELECT message_id, metadata_json FROM messages WHERE session_id = ? "
            "AND turn_id = ? AND role = 'assistant' ORDER BY message_id",
            (session_id, turn_id),
        ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            raw_calls = metadata.get("tool_calls") if isinstance(metadata, dict) else None
            if not isinstance(raw_calls, list):
                continue
            for raw_call in raw_calls:
                if not isinstance(raw_call, dict):
                    continue
                call_id = str(raw_call.get("call_id", "")).strip()
                tool_name = str(raw_call.get("name", "")).strip()
                if (
                    not call_id
                    or not tool_name
                    or call_id in result_ids
                    or call_id in missing
                ):
                    continue
                arguments = _bounded_recovery_arguments(raw_call.get("arguments"))
                durable = durable_by_id.get(call_id)
                durable_payload: dict[str, Any] | None = None
                if durable is not None:
                    try:
                        durable_arguments = json.loads(durable["arguments_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        durable_arguments = {}
                    durable_payload = {
                        "tool_name": str(durable["tool_name"]),
                        "arguments": (
                            dict(durable_arguments)
                            if isinstance(durable_arguments, dict)
                            else {}
                        ),
                        "approved": bool(durable["approved"]),
                        "executed": bool(durable["executed"]),
                        "dispatched": bool(durable["dispatched"]),
                        "result": durable["result"],
                        "error": durable["error"],
                    }
                missing[call_id] = {
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "assistant_message_id": int(row["message_id"]),
                    "durable": durable_payload,
                }
        return list(missing.values())

    def pending_tool_calls(self, session_id: str, turn_id: str) -> list[sqlite3.Row]:
        """Return approved calls that never persisted an execution outcome."""

        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM tool_calls WHERE session_id = ? AND turn_id = ? "
                "AND approved = 1 AND executed = 0 "
                "AND result IS NULL AND error IS NULL "
                "ORDER BY timestamp, call_id",
                (session_id, turn_id),
            ).fetchall()

    def existing_tool_call_ids(
        self,
        session_id: str,
        call_ids: Sequence[str],
    ) -> set[str]:
        """Return only candidate tool-call IDs already used by one session."""

        candidates = tuple(dict.fromkeys(str(call_id) for call_id in call_ids))
        if not candidates:
            return set()
        placeholders = ",".join("?" for _ in candidates)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT call_id FROM tool_calls WHERE session_id = ? "
                f"AND call_id IN ({placeholders})",
                (session_id, *candidates),
            ).fetchall()
        return {str(row["call_id"]) for row in rows}

    def file_checkpoints_for_call(
        self, session_id: str, turn_id: str, call_id: str
    ) -> list[sqlite3.Row]:
        """Return unrestored checkpoints belonging to one tool call."""

        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM file_checkpoints WHERE session_id = ? "
                "AND turn_id = ? AND call_id = ? AND restored = 0 "
                "ORDER BY checkpoint_id DESC",
                (session_id, turn_id, call_id),
            ).fetchall()

    def unmatched_incomplete_checkpoints(
        self, session_id: str, turn_id: str, pending_call_ids: list[str]
    ) -> list[sqlite3.Row]:
        """Return legacy/incomplete checkpoints not owned by a pending call."""

        parameters: list[Any] = [session_id, turn_id]
        exclusion = ""
        if pending_call_ids:
            placeholders = ",".join("?" for _ in pending_call_ids)
            exclusion = f" AND call_id NOT IN ({placeholders})"
            parameters.extend(pending_call_ids)
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM file_checkpoints WHERE session_id = ? "
                "AND turn_id = ? AND restored = 0 AND after_sha256 IS NULL"
                + exclusion
                + " ORDER BY checkpoint_id",
                parameters,
            ).fetchall()

    def finalize_interrupted_recovery(
        self,
        session_id: str,
        turn_id: str,
        *,
        call_errors: dict[str, str],
        restored_checkpoint_ids: list[int],
        recovery: dict[str, Any],
        recovered_calls: list[Any],
    ) -> None:
        """Atomically persist one startup recovery decision."""

        with closing(self._connect()) as conn, conn:
            recovered_by_id = {str(call.call_id): call for call in recovered_calls}
            for call in recovered_calls:
                assistant_message_id = getattr(call, "assistant_message_id", None)
                assistant_arguments = getattr(call, "assistant_arguments", None)
                if (
                    assistant_message_id is None
                    or not isinstance(assistant_arguments, dict)
                    or not any(
                        key.startswith("_ash_recovery_arguments_")
                        for key in assistant_arguments
                    )
                ):
                    continue
                row = conn.execute(
                    "SELECT metadata_json FROM messages "
                    "WHERE message_id = ? AND session_id = ? AND turn_id = ? "
                    "AND role = 'assistant'",
                    (assistant_message_id, session_id, turn_id),
                ).fetchone()
                if row is None:
                    continue
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                raw_calls = metadata.get("tool_calls") if isinstance(metadata, dict) else None
                if not isinstance(raw_calls, list):
                    continue
                changed = False
                for raw_call in raw_calls:
                    if not isinstance(raw_call, dict):
                        continue
                    if str(raw_call.get("call_id", "")) != str(call.call_id):
                        continue
                    raw_call["arguments"] = assistant_arguments
                    changed = True
                    break
                if changed:
                    conn.execute(
                        "UPDATE messages SET metadata_json = ? WHERE message_id = ?",
                        (
                            json.dumps(
                                metadata,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ),
                            assistant_message_id,
                        ),
                    )
            for call_id, error in call_errors.items():
                call = recovered_by_id[call_id]
                cursor = conn.execute(
                    "UPDATE tool_calls SET executed = ?, error = ? "
                    "WHERE session_id = ? AND turn_id = ? AND call_id = ?",
                    (
                        int(bool(call.dispatched)),
                        error,
                        session_id,
                        turn_id,
                        call_id,
                    ),
                )
                if cursor.rowcount == 0:
                    assistant_arguments = getattr(call, "assistant_arguments", None)
                    arguments = (
                        dict(assistant_arguments)
                        if isinstance(assistant_arguments, dict)
                        else _bounded_recovery_arguments(
                            getattr(call, "arguments", None)
                        )
                    )
                    conn.execute(
                        """
                        INSERT INTO tool_calls (
                            call_id, session_id, tool_name, arguments_json,
                            approved, executed, dispatched, result, error,
                            timestamp, turn_id
                        ) VALUES (?, ?, ?, ?, 0, ?, ?, NULL, ?, ?, ?)
                        """,
                        (
                            call_id,
                            session_id,
                            str(call.tool_name),
                            json.dumps(
                                arguments,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ),
                            int(bool(call.dispatched)),
                            int(bool(call.dispatched)),
                            error,
                            _serialize_datetime(_utc_now()),
                            turn_id,
                        ),
                    )
            for call in recovered_calls:
                tool_name = str(call.tool_name)
                recovered_success = bool(getattr(call, "success", False))
                recovered_output = str(getattr(call, "output", "") or "")
                action_type: AuditAction = (
                    "command_run"
                    if tool_name == "run_command"
                    else "file_write"
                    if tool_name
                    in {
                        "write_file",
                        "whole_edit",
                        "replace_file_content",
                        "replace_file_edits",
                        "apply_patch",
                    }
                    else "tool_call"
                )
                self._append_audit_log_in_connection(
                    conn,
                    session_id,
                    action_type=action_type,
                    target_resource=tool_name,
                    details={
                        "call_id": str(call.call_id),
                        "error": str(call.error),
                        "dispatched": bool(call.dispatched),
                        "ambiguous": bool(call.ambiguous),
                        "replayed": False,
                        "recovered": True,
                        "replay_policy": "never",
                        "success": recovered_success,
                        **_recovery_audit_output_fields(recovered_output),
                    },
                    result=(
                        "SUCCESS"
                        if recovered_success
                        else "FAILURE"
                    ),
                )
            message_rows = conn.execute(
                "SELECT metadata_json FROM messages WHERE session_id = ? "
                "AND turn_id = ? AND role = 'tool' ORDER BY message_id",
                (session_id, turn_id),
            ).fetchall()
            recovery_message_timestamp: datetime | None = None
            for call in recovered_calls:
                call_id = str(call.call_id)
                if _tool_result_message_exists(message_rows, call_id):
                    continue
                recovery_message_timestamp = _utc_now()
                success = bool(getattr(call, "success", False))
                output = str(getattr(call, "output", "") or "")
                recovery_error: str | None = (
                    str(call.error) if str(call.error) else None
                )
                content = _render_recovered_tool_message(
                    success=success,
                    output=output,
                    error=recovery_error,
                    dispatched=bool(call.dispatched),
                    ambiguous=bool(call.ambiguous),
                )
                conn.execute(
                    """
                    INSERT INTO messages (
                        session_id, role, content, timestamp, metadata_json,
                        token_count, prompt_tokens, completion_tokens, turn_id
                    ) VALUES (?, 'tool', ?, ?, ?, 0, 0, 0, ?)
                    """,
                    (
                        session_id,
                        content,
                        _serialize_datetime(recovery_message_timestamp),
                        json.dumps({"call_id": call_id}),
                        turn_id,
                    ),
                )
            if recovery_message_timestamp is not None:
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                    (_serialize_datetime(recovery_message_timestamp), session_id),
                )
            if restored_checkpoint_ids:
                placeholders = ",".join("?" for _ in restored_checkpoint_ids)
                conn.execute(
                    "UPDATE file_checkpoints SET restored = 1 "
                    f"WHERE session_id = ? AND checkpoint_id IN ({placeholders})",
                    (session_id, *restored_checkpoint_ids),
                )
            conn.execute(
                "UPDATE turn_journal SET status = 'interrupted', completed_at = ?, "
                "recovery_json = ? WHERE session_id = ? AND turn_id = ? "
                "AND (status = 'started' OR ("
                "status = 'interrupted' AND COALESCE(recovery_json, '{}') = '{}'"
                "))",
                (
                    _serialize_datetime(_utc_now()),
                    json.dumps(recovery, sort_keys=True),
                    session_id,
                    turn_id,
                ),
            )

    def interrupted_recovery_reports(self, session_id: str) -> list[dict[str, Any]]:
        """Return persisted non-empty startup recovery reports newest-first."""

        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT recovery_json FROM turn_journal WHERE session_id = ? "
                "AND status = 'interrupted' AND recovery_json != '{}' "
                "ORDER BY completed_at DESC, turn_id DESC",
                (session_id,),
            ).fetchall()
        reports: list[dict[str, Any]] = []
        for row in rows:
            try:
                value = json.loads(row["recovery_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                reports.append(value)
        return reports

    def rewind_turn_ids(
        self,
        session_id: str,
        message_count: int,
        *,
        require_complete_mapping: bool = False,
    ) -> list[str]:
        """Return complete turns removed at a transcript message boundary."""

        with closing(self._connect()) as conn:
            total, retained, removed = _message_boundary_rows(
                conn,
                session_id,
                message_count,
            )
            if retained is not None and removed is not None:
                retained_turn = retained["turn_id"]
                removed_turn = removed["turn_id"]
                if retained_turn is not None and retained_turn == removed_turn:
                    raise ValueError(
                        "message_count splits an Ash turn; choose a user-turn boundary"
                    )
            if message_count == total:
                return []
            assert removed is not None
            first_removed_id = int(removed["message_id"])
            if require_complete_mapping:
                unmapped = conn.execute(
                    "SELECT 1 FROM messages WHERE session_id = ? "
                    "AND message_id >= ? AND turn_id IS NULL LIMIT 1",
                    (session_id, first_removed_id),
                ).fetchone()
                if unmapped is not None:
                    raise ValueError(
                        "combined rewind is unavailable for legacy messages without "
                        "turn IDs; use transcript-only /rewind or start a newer boundary"
                    )
            rows = conn.execute(
                "SELECT turn_id, MIN(message_id) AS first_message_id "
                "FROM messages WHERE session_id = ? AND message_id >= ? "
                "AND turn_id IS NOT NULL GROUP BY turn_id "
                "ORDER BY first_message_id",
                (session_id, first_removed_id),
            ).fetchall()
        return [str(row["turn_id"]) for row in rows]

    def rewind_session(
        self,
        session_id: str,
        message_count: int,
        *,
        restored_checkpoint_turn_ids: list[str] | None = None,
    ) -> Session:
        """Delete transcript records after a confirmed message boundary."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                exists = conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if exists is None:
                    raise KeyError(f"Session not found: {session_id}")

                _, retained, removed = _message_boundary_rows(
                    conn,
                    session_id,
                    message_count,
                )
                if retained is not None and removed is not None:
                    retained_turn = retained["turn_id"]
                    removed_turn = removed["turn_id"]
                    if retained_turn is not None and retained_turn == removed_turn:
                        raise ValueError(
                            "message_count splits an Ash turn; choose a user-turn boundary"
                        )
                first_removed_id = (
                    int(removed["message_id"]) if removed is not None else None
                )
                turn_ids: list[str] = []
                if first_removed_id is not None:
                    rows = conn.execute(
                        "SELECT turn_id, MIN(message_id) AS first_message_id "
                        "FROM messages WHERE session_id = ? AND message_id >= ? "
                        "AND turn_id IS NOT NULL GROUP BY turn_id "
                        "ORDER BY first_message_id",
                        (session_id, first_removed_id),
                    ).fetchall()
                    turn_ids = [str(row["turn_id"]) for row in rows]
                if restored_checkpoint_turn_ids and not set(
                    restored_checkpoint_turn_ids
                ).issubset(turn_ids):
                    raise ValueError("restored checkpoint turns must be part of the rewind")
                cutoff = (
                    _deserialize_datetime(retained["timestamp"])
                    if retained is not None
                    else None
                )
                usage_totals = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "cost_usd": 0.0,
                    "estimated_prompt_tokens": 0,
                    "estimated_completion_tokens": 0,
                    "estimated_cost_usd": 0.0,
                }
                if turn_ids:
                    placeholders = ",".join("?" for _ in turn_ids)
                    usage_rows = conn.execute(
                        f"SELECT usage_json FROM turn_journal WHERE session_id = ? "
                        f"AND turn_id IN ({placeholders})",
                        (session_id, *turn_ids),
                    ).fetchall()
                    for row in usage_rows:
                        try:
                            usage = json.loads(row["usage_json"] or "{}")
                        except (TypeError, json.JSONDecodeError):
                            usage = {}
                        for key in usage_totals:
                            value = usage.get(key, 0)
                            if isinstance(value, (int, float)) and not isinstance(
                                value, bool
                            ):
                                usage_totals[key] += value

                if first_removed_id is not None:
                    conn.execute(
                        "DELETE FROM messages WHERE session_id = ? AND message_id >= ?",
                        (session_id, first_removed_id),
                    )

                if turn_ids:
                    placeholders = ",".join("?" for _ in turn_ids)
                    conn.execute(
                        f"DELETE FROM tool_calls WHERE session_id = ? "
                        f"AND turn_id IN ({placeholders})",
                        (session_id, *turn_ids),
                    )
                if cutoff is None:
                    conn.execute(
                        "DELETE FROM tool_calls WHERE session_id = ?", (session_id,)
                    )
                else:
                    conn.execute(
                        "DELETE FROM tool_calls WHERE session_id = ? AND timestamp > ?",
                        (session_id, _serialize_datetime(cutoff)),
                    )

                conn.execute(
                    "UPDATE sessions SET "
                    "total_tokens = MAX(0, COALESCE(total_tokens, 0) - ?), "
                    "total_prompt_tokens = MAX(0, COALESCE(total_prompt_tokens, 0) - ?), "
                    "total_completion_tokens = MAX(0, COALESCE(total_completion_tokens, 0) - ?), "
                    "total_cache_read_tokens = MAX(0, COALESCE(total_cache_read_tokens, 0) - ?), "
                    "total_cache_write_tokens = MAX(0, COALESCE(total_cache_write_tokens, 0) - ?), "
                    "total_cost_usd = MAX(0, COALESCE(total_cost_usd, 0) - ?), "
                    "estimated_prompt_tokens = MAX(0, COALESCE(estimated_prompt_tokens, 0) - ?), "
                    "estimated_completion_tokens = MAX(0, COALESCE(estimated_completion_tokens, 0) - ?), "
                    "estimated_cost_usd = MAX(0, COALESCE(estimated_cost_usd, 0) - ?) "
                    "WHERE session_id = ?",
                    (
                        usage_totals["prompt_tokens"]
                        + usage_totals["completion_tokens"],
                        usage_totals["prompt_tokens"],
                        usage_totals["completion_tokens"],
                        usage_totals["cache_read_tokens"],
                        usage_totals["cache_write_tokens"],
                        usage_totals["cost_usd"],
                        usage_totals["estimated_prompt_tokens"],
                        usage_totals["estimated_completion_tokens"],
                        usage_totals["estimated_cost_usd"],
                        session_id,
                    ),
                )
                if restored_checkpoint_turn_ids:
                    placeholders = ",".join("?" for _ in restored_checkpoint_turn_ids)
                    conn.execute(
                        "UPDATE file_checkpoints SET restored = 1 "
                        f"WHERE session_id = ? AND turn_id IN ({placeholders})",
                        (session_id, *restored_checkpoint_turn_ids),
                    )
                if turn_ids:
                    placeholders = ",".join("?" for _ in turn_ids)
                    conn.execute(
                        f"DELETE FROM turn_journal WHERE session_id = ? "
                        f"AND turn_id IN ({placeholders})",
                        (session_id, *turn_ids),
                    )
                conn.execute(
                    "UPDATE sessions SET context_summary = '', "
                    "context_summary_message_count = 0, updated_at = ? "
                    "WHERE session_id = ?",
                    (_serialize_datetime(_utc_now()), session_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.load_session(session_id)

    def file_checkpoints_for_turns(
        self, session_id: str, turn_ids: list[str]
    ) -> list[sqlite3.Row]:
        """Return unrestored checkpoints newest-first for selected turns."""

        if not turn_ids:
            return []
        placeholders = ",".join("?" for _ in turn_ids)
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM file_checkpoints WHERE session_id = ? "
                f"AND turn_id IN ({placeholders}) AND restored = 0 "
                "ORDER BY checkpoint_id DESC",
                (session_id, *turn_ids),
            ).fetchall()

    def save_file_checkpoint(
        self,
        session_id: str,
        turn_id: str,
        tool_name: str,
        path: str,
        *,
        existed: bool,
        before_content: bytes | None,
        before_mode: int | None,
        call_id: str = "",
    ) -> None:
        """Save the first pre-edit state for one path in a turn."""
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO file_checkpoints
                    (session_id, turn_id, tool_name, path, existed,
                     before_content, before_mode, created_at, call_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    turn_id,
                    tool_name,
                    path,
                    int(existed),
                    before_content,
                    before_mode,
                    _serialize_datetime(_utc_now()),
                    call_id,
                ),
            )

    def finish_file_checkpoint(
        self,
        session_id: str,
        turn_id: str,
        path: str,
        after_sha256: str,
        *,
        call_id: str = "",
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE file_checkpoints SET after_sha256 = ? "
                "WHERE session_id = ? AND turn_id = ? AND call_id = ? AND path = ?",
                (after_sha256, session_id, turn_id, call_id, path),
            )

    def latest_file_checkpoints(self, session_id: str) -> list[sqlite3.Row]:
        """Return the latest unrestored completed checkpoint group."""
        with closing(self._connect()) as conn:
            turn = conn.execute(
                "SELECT turn_id FROM file_checkpoints "
                "WHERE session_id = ? AND restored = 0 AND after_sha256 IS NOT NULL "
                "ORDER BY checkpoint_id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if turn is None:
                return []
            return conn.execute(
                "SELECT * FROM file_checkpoints "
                "WHERE session_id = ? AND turn_id = ? AND restored = 0 "
                "ORDER BY checkpoint_id DESC",
                (session_id, turn["turn_id"]),
            ).fetchall()

    def mark_file_checkpoints_restored(self, session_id: str, turn_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE file_checkpoints SET restored = 1 "
                "WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            )

    def rename_session(self, session_id: str, title: str) -> None:
        """Set a human-readable session title."""

        normalized = _normalize_session_title(title)
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE session_id = ?",
                (normalized, _serialize_datetime(_utc_now()), session_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Session not found: {session_id}")

    def save_context_summary(
        self,
        session_id: str,
        summary: str,
        *,
        summarized_message_count: int = 0,
    ) -> None:
        """Persist the working compaction summary without deleting history."""

        if summarized_message_count < 0:
            raise ValueError("summarized_message_count cannot be negative")
        if summarized_message_count and not summary:
            raise ValueError("a non-empty summary is required for summarized history")

        with closing(self._connect()) as conn, conn:
            total_messages = int(
                conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            if summarized_message_count > total_messages:
                raise ValueError(
                    "summarized_message_count cannot exceed durable message count"
                )
            cursor = conn.execute(
                "UPDATE sessions SET context_summary = ?, "
                "context_summary_message_count = ?, updated_at = ? "
                "WHERE session_id = ?",
                (
                    summary,
                    summarized_message_count,
                    _serialize_datetime(_utc_now()),
                    session_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Session not found: {session_id}")

    def durable_message_count(self, session_id: str) -> int:
        """Return the number of durable transcript messages for one session."""

        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["count"])

    def save_runtime_events(self, events: list[dict[str, Any]]) -> int:
        """Append a batch of canonical events, ignoring replayed event IDs."""

        if not events:
            return 0
        from ash.core.events import EVENT_SCHEMA_VERSION, envelope_event
        from ash.core.redaction import redact_value

        rows: list[tuple[Any, ...]] = []
        for raw_event in events:
            event = envelope_event(raw_event)
            session_id = event.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("persisted runtime events require a session_id")
            if event["schema_version"] != EVENT_SCHEMA_VERSION:
                raise ValueError("runtime event schema version mismatch")
            redacted = redact_value(event)
            rows.append(
                (
                    event["event_id"],
                    session_id,
                    event.get("turn_id"),
                    event.get("operation_id"),
                    event["type"],
                    event["schema_version"],
                    event["timestamp"],
                    _canonical_json(redacted),
                )
            )
        with closing(self._connect()) as conn, conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO runtime_events (
                    event_id, session_id, turn_id, operation_id, event_type,
                    schema_version, timestamp, event_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            return conn.total_changes - before

    def list_runtime_events(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        turn_id: str | None = None,
        limit: int = 1000,
    ) -> list[StoredRuntimeEvent]:
        """Replay session events in insertion order from an exclusive cursor."""

        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        query = (
            "SELECT sequence, event_json FROM runtime_events "
            "WHERE session_id = ? AND sequence > ?"
        )
        parameters: list[Any] = [session_id, after_sequence]
        if turn_id is not None:
            query += " AND turn_id = ?"
            parameters.append(turn_id)
        query += " ORDER BY sequence ASC LIMIT ?"
        parameters.append(limit)
        with closing(self._connect()) as conn:
            rows = conn.execute(query, parameters).fetchall()
        return [
            StoredRuntimeEvent(
                sequence=int(row["sequence"]),
                event=json.loads(str(row["event_json"])),
            )
            for row in rows
        ]

    def fork_session(
        self,
        session_id: str,
        *,
        message_count: int | None = None,
        branch_name: str = "",
        branch_summary: str = "",
        _child_session_id: str | None = None,
    ) -> Session:
        """Create a durable child branch at a complete message boundary."""

        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                source = conn.execute(
                    "SELECT session_id, project_path, title, context_summary, "
                    "context_summary_message_count, model FROM sessions "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if source is None:
                    raise KeyError(f"Session not found: {session_id}")
                total_count, retained, removed = _message_boundary_rows(
                    conn,
                    session_id,
                    (
                        int(
                            conn.execute(
                                "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                                (session_id,),
                            ).fetchone()[0]
                        )
                        if message_count is None
                        else message_count
                    ),
                )
                count = total_count if message_count is None else message_count
                _validate_sql_fork_boundary(retained, removed)
                fork = self._create_session_record(
                    conn,
                    str(source["project_path"]),
                    session_id=_child_session_id,
                    model=str(source["model"] or ""),
                    parent_session_id=str(source["session_id"]),
                    fork_message_count=count,
                    branch_name=branch_name,
                    branch_summary=branch_summary,
                )
                title = fork.branch_name or (
                    _derived_session_title(
                        str(source["title"] or "") or str(source["session_id"])[:8],
                        " (fork)",
                    )
                )
                conn.execute(
                    "UPDATE sessions SET title = ?, context_summary = ?, "
                    "context_summary_message_count = ? "
                    "WHERE session_id = ?",
                    (
                        title,
                        str(source["context_summary"] or "") if count == total_count else "",
                        (
                            int(source["context_summary_message_count"] or 0)
                            if count == total_count
                            else 0
                        ),
                        fork.session_id,
                    ),
                )
                conn.execute(
                    "INSERT INTO messages ("
                    "session_id, role, content, timestamp, metadata_json, "
                    "token_count, prompt_tokens, completion_tokens, turn_id) "
                    "SELECT ?, role, content, timestamp, metadata_json, "
                    "token_count, prompt_tokens, completion_tokens, NULL "
                    "FROM messages WHERE session_id = ? "
                    "ORDER BY message_id LIMIT ?",
                    (fork.session_id, session_id, count),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.load_session(fork.session_id)

    def discard_unchanged_leaf_fork(
        self,
        session_id: str,
        *,
        parent_session_id: str,
        created_at: datetime,
        updated_at: datetime | None,
    ) -> bool:
        """Discard one unpublished fork only while its exact creation state is intact."""

        expected_updated_at = updated_at or created_at
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "DELETE FROM sessions "
                "WHERE session_id = ? AND parent_session_id = ? "
                "AND fork_message_count IS NOT NULL "
                "AND created_at = ? AND updated_at = ? "
                "AND (SELECT COUNT(*) FROM messages WHERE session_id = ?) "
                "= fork_message_count "
                "AND NOT EXISTS ("
                "SELECT 1 FROM sessions child WHERE child.parent_session_id = ?"
                ") "
                "AND NOT EXISTS (SELECT 1 FROM tool_calls WHERE session_id = ?) "
                "AND NOT EXISTS (SELECT 1 FROM audit_logs WHERE session_id = ?) "
                "AND NOT EXISTS (SELECT 1 FROM turn_journal WHERE session_id = ?) "
                "AND NOT EXISTS (SELECT 1 FROM file_checkpoints WHERE session_id = ?) "
                "AND NOT EXISTS (SELECT 1 FROM runtime_events WHERE session_id = ?) "
                "AND NOT EXISTS (SELECT 1 FROM sprints WHERE session_id = ?)",
                (
                    session_id,
                    parent_session_id,
                    _serialize_datetime(created_at),
                    _serialize_datetime(expected_updated_at),
                    session_id,
                    session_id,
                    session_id,
                    session_id,
                    session_id,
                    session_id,
                    session_id,
                    session_id,
                ),
            )
            return cursor.rowcount == 1

    def get_session_lineage(self, session_id: str) -> SessionLineage:
        """Return one session node and its direct child identifiers."""

        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT session_id, root_session_id, parent_session_id, "
                "fork_message_count, branch_name, branch_summary, depth, created_at "
                "FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Session not found: {session_id}")
            children = conn.execute(
                "SELECT session_id FROM sessions WHERE parent_session_id = ? "
                "ORDER BY created_at, session_id LIMIT ?",
                (session_id, MAX_SESSION_TREE_NODES + 1),
            ).fetchall()
            if len(children) > MAX_SESSION_TREE_NODES:
                raise SessionStorageError(
                    "session lineage exceeds the supported child-node limit"
                )
        try:
            return _lineage_from_row(
                row,
                children=tuple(str(child["session_id"]) for child in children),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise _invalid_stored_data_error(self.db_path) from exc

    def session_tree(self, session_id: str) -> list[SessionLineage]:
        """Return the complete conversation tree in stable parent-first order."""

        node = self.get_session_lineage(session_id)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT session_id, root_session_id, parent_session_id, "
                "fork_message_count, branch_name, branch_summary, depth, created_at "
                "FROM sessions WHERE root_session_id = ? "
                "ORDER BY depth, created_at, session_id LIMIT ?",
                (node.root_session_id, MAX_SESSION_TREE_NODES + 1),
            ).fetchall()
        if len(rows) > MAX_SESSION_TREE_NODES:
            raise SessionStorageError(
                "session tree exceeds the supported node limit"
            )
        children_by_parent: dict[str, list[str]] = {}
        for child in rows:
            parent = child["parent_session_id"]
            if parent is not None:
                children_by_parent.setdefault(str(parent), []).append(
                    str(child["session_id"])
                )
        rows_by_id = {str(row["session_id"]): row for row in rows}
        ordered_ids: list[str] = []
        pending = [node.root_session_id]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current in seen or current not in rows_by_id:
                continue
            seen.add(current)
            ordered_ids.append(current)
            pending.extend(reversed(children_by_parent.get(current, ())))
        # Preserve visibility if a manually modified database contains an orphan.
        ordered_ids.extend(session for session in rows_by_id if session not in seen)
        try:
            return [
                _lineage_from_row(
                    rows_by_id[current],
                    children=tuple(children_by_parent.get(current, ())),
                )
                for current in ordered_ids
            ]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise _invalid_stored_data_error(self.db_path) from exc

    def export_session(self, session_id: str, *, format: str = "jsonl") -> str:
        """Serialize a redacted session transcript for local export."""

        return "".join(self.iter_session_export(session_id, format=format))

    def iter_session_export(
        self,
        session_id: str,
        *,
        format: str = "jsonl",
    ) -> Iterator[str]:
        """Stream a redacted session transcript without materializing its history."""

        from ash.core.redaction import redact_text, redact_value

        if format not in {"jsonl", "markdown"}:
            raise ValueError("format must be 'jsonl' or 'markdown'")

        with closing(self._connect()) as conn:
            session = conn.execute(
                """
                SELECT session_id, created_at, model, title, parent_session_id,
                       root_session_id, fork_message_count, branch_name,
                       branch_summary, depth
                FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if session is None:
                raise KeyError(f"Session not found: {session_id}")

            if format == "jsonl":
                header = {
                    "schema_version": 1,
                    "type": "session",
                    "session_id": str(session["session_id"]),
                    "project_path": "<project>",
                    "model": redact_text(str(session["model"] or "")),
                    "title": redact_text(str(session["title"] or "")),
                    "created_at": _deserialize_datetime(
                        str(session["created_at"])
                    ).isoformat(),
                    "parent_session_id": session["parent_session_id"],
                    "root_session_id": session["root_session_id"],
                    "fork_message_count": session["fork_message_count"],
                    "branch_name": redact_text(str(session["branch_name"] or "")),
                    "branch_summary": redact_text(
                        str(session["branch_summary"] or "")
                    ),
                    "depth": int(session["depth"] or 0),
                }
                yield json.dumps(header, ensure_ascii=False) + "\n"
            else:
                heading = redact_text(
                    str(session["title"] or "")
                    or f'Ash session {session["session_id"]}'
                )
                model = redact_text(str(session["model"] or "") or "unknown")
                yield f"# {heading}\n\nModel: `{model}`"

            rows = conn.execute(
                """
                SELECT role, content, timestamp, metadata_json
                FROM messages
                WHERE session_id = ?
                ORDER BY message_id ASC
                """,
                (session_id,),
            )
            for row in rows:
                if format == "jsonl":
                    try:
                        metadata = json.loads(row["metadata_json"] or "{}")
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise _invalid_stored_data_error(self.db_path) from exc
                    if not isinstance(metadata, dict):
                        raise _invalid_stored_data_error(self.db_path)
                    record = {
                    "schema_version": 1,
                    "type": "message",
                        "role": str(row["role"]),
                        "content": redact_text(str(row["content"])),
                        "timestamp": _deserialize_datetime(
                            str(row["timestamp"])
                        ).isoformat(),
                        "metadata": redact_value(metadata),
                    }
                    yield json.dumps(record, ensure_ascii=False) + "\n"
                else:
                    yield (
                        f"\n\n## {str(row['role']).title()}\n\n"
                        f"{redact_text(str(row['content']))}"
                    )

            if format == "markdown":
                yield "\n"

    def import_session_jsonl(self, content: str, *, project_path: str) -> Session:
        """Import Ash's versioned JSONL format into the current project."""
        from ash.providers.messages import MAX_CANONICAL_MESSAGES

        total_bytes = 0
        try:
            for offset in range(0, len(content), 64 * 1024):
                total_bytes += len(content[offset : offset + 64 * 1024].encode("utf-8"))
                if total_bytes > MAX_SESSION_IMPORT_BYTES:
                    raise ValueError(
                        f"session import exceeds {MAX_SESSION_IMPORT_BYTES} UTF-8 bytes"
                    )
        except UnicodeEncodeError as exc:
            raise ValueError("session import must be valid UTF-8 text") from exc

        def records() -> Any:
            for line in io.StringIO(content):
                if not line.strip():
                    continue
                try:
                    yield strict_json_loads(line)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise ValueError(f"invalid session JSONL: {exc}") from exc

        record_iter = records()
        header = next(record_iter, None)
        if not isinstance(header, dict):
            raise ValueError("session JSONL is empty")
        if (
            type(header.get("schema_version")) is not int
            or header.get("schema_version") != 1
            or header.get("type") != "session"
        ):
            raise ValueError("unsupported session export schema")
        raw_title = header.get("title", "")
        raw_model = header.get("model", "")
        if not isinstance(raw_title, str):
            raise ValueError("imported session title must be a string")
        if not isinstance(raw_model, str):
            raise ValueError("imported session model must be a string")
        title = _normalize_session_title(raw_title, allow_empty=True)
        imported_messages: list[Message] = []
        for record in record_iter:
            if len(imported_messages) >= MAX_CANONICAL_MESSAGES:
                raise ValueError(
                    "imported message count exceeds the limit of "
                    f"{MAX_CANONICAL_MESSAGES}"
                )
            if not isinstance(record, dict) or record.get("type") != "message":
                raise ValueError("session export contains an invalid record")
            if (
                type(record.get("schema_version")) is not int
                or record.get("schema_version") != 1
            ):
                raise ValueError("unsupported imported message schema")
            role = record.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                raise ValueError(f"invalid imported message role: {role!r}")
            message_content = record.get("content")
            if not isinstance(message_content, str):
                raise ValueError("imported message content must be a string")
            raw_timestamp = record.get("timestamp")
            if not isinstance(raw_timestamp, str):
                raise ValueError("imported message timestamp must be a string")
            try:
                timestamp = _deserialize_datetime(raw_timestamp)
            except ValueError as exc:
                raise ValueError("imported message has an invalid timestamp") from exc
            metadata = record.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError("imported message metadata must be an object")
            message = Message(
                role=role,
                content=message_content,
                timestamp=timestamp,
                metadata=metadata,
            )
            _validate_imported_provider_message(message)
            imported_messages.append(message)

        with closing(self._connect()) as conn, conn:
            session = self._create_session_record(
                conn,
                project_path,
                model=raw_model,
            )
            if title:
                conn.execute(
                    "UPDATE sessions SET title = ? WHERE session_id = ?",
                    (_derived_session_title(title, " (imported)"), session.session_id),
                )
            for message in imported_messages:
                conn.execute(
                    """
                    INSERT INTO messages (
                        session_id, role, content, timestamp, metadata_json,
                        token_count, prompt_tokens, completion_tokens, turn_id
                    )
                    VALUES (?, ?, ?, ?, ?, 0, 0, 0, NULL)
                    """,
                    (
                        session.session_id,
                        message.role,
                        message.content,
                        _serialize_datetime(message.timestamp),
                        json.dumps(message.metadata),
                    ),
                )
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                    (_serialize_datetime(message.timestamp), session.session_id),
                )
        return self.load_session(session.session_id)

    def save_session_token_stats(
        self,
        session_id: str,
        total_prompt_tokens: int,
        total_completion_tokens: int,
        turn_cost_usd: float,
        *,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        estimated_prompt_tokens: int = 0,
        estimated_completion_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        """Accumulate one turn's token and explicitly configured cost totals."""

        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE sessions
                SET total_tokens = COALESCE(total_tokens, 0) + ?,
                    total_cost_usd = COALESCE(total_cost_usd, 0) + ?,
                    total_prompt_tokens = COALESCE(total_prompt_tokens, 0) + ?,
                    total_completion_tokens = COALESCE(total_completion_tokens, 0) + ?,
                    total_cache_read_tokens = COALESCE(total_cache_read_tokens, 0) + ?,
                    total_cache_write_tokens = COALESCE(total_cache_write_tokens, 0) + ?,
                    estimated_prompt_tokens = COALESCE(estimated_prompt_tokens, 0) + ?,
                    estimated_completion_tokens = COALESCE(estimated_completion_tokens, 0) + ?,
                    estimated_cost_usd = COALESCE(estimated_cost_usd, 0) + ?
                WHERE session_id = ?
                """,
                (
                    total_prompt_tokens + total_completion_tokens,
                    turn_cost_usd,
                    total_prompt_tokens,
                    total_completion_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                    estimated_prompt_tokens,
                    estimated_completion_tokens,
                    estimated_cost_usd,
                    session_id,
                ),
            )

    def save_tool_call(
        self,
        session_id: str,
        record: ToolCallRecord,
        *,
        turn_id: str | None = None,
    ) -> None:
        """Save or update a tool execution record."""

        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO tool_calls (
                    call_id,
                    session_id,
                    tool_name,
                    arguments_json,
                    approved,
                    executed,
                    dispatched,
                    result,
                    error,
                    timestamp,
                    turn_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, call_id) DO UPDATE SET
                    tool_name = excluded.tool_name,
                    arguments_json = excluded.arguments_json,
                    approved = excluded.approved,
                    executed = excluded.executed,
                    dispatched = excluded.dispatched,
                    result = excluded.result,
                    error = excluded.error,
                    timestamp = excluded.timestamp,
                    turn_id = COALESCE(excluded.turn_id, tool_calls.turn_id)
                """,
                (
                    record.call_id,
                    session_id,
                    record.tool_name,
                    json.dumps(record.arguments),
                    int(record.approved),
                    int(record.executed),
                    int(record.dispatched),
                    record.result,
                    record.error,
                    _serialize_datetime(record.timestamp),
                    turn_id,
                ),
            )

    def finalize_tool_call_with_message(
        self,
        session_id: str,
        record: ToolCallRecord,
        message: Message,
        *,
        turn_id: str | None = None,
    ) -> None:
        """Atomically persist a terminal tool outcome and model-visible result."""

        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO tool_calls (
                    call_id,
                    session_id,
                    tool_name,
                    arguments_json,
                    approved,
                    executed,
                    dispatched,
                    result,
                    error,
                    timestamp,
                    turn_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, call_id) DO UPDATE SET
                    tool_name = excluded.tool_name,
                    arguments_json = excluded.arguments_json,
                    approved = excluded.approved,
                    executed = excluded.executed,
                    dispatched = excluded.dispatched,
                    result = excluded.result,
                    error = excluded.error,
                    timestamp = excluded.timestamp,
                    turn_id = COALESCE(excluded.turn_id, tool_calls.turn_id)
                """,
                (
                    record.call_id,
                    session_id,
                    record.tool_name,
                    json.dumps(record.arguments),
                    int(record.approved),
                    int(record.executed),
                    int(record.dispatched),
                    record.result,
                    record.error,
                    _serialize_datetime(record.timestamp),
                    turn_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO messages (
                    session_id, role, content, timestamp, metadata_json,
                    token_count, prompt_tokens, completion_tokens, turn_id
                )
                VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?)
                """,
                (
                    session_id,
                    message.role,
                    message.content,
                    _serialize_datetime(message.timestamp),
                    json.dumps(message.metadata),
                    turn_id,
                ),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (_serialize_datetime(message.timestamp), session_id),
            )
            conn.execute(
                "DELETE FROM mcp_tasks WHERE session_id = ? AND call_id = ?",
                (session_id, record.call_id),
            )

    def save_mcp_task(
        self,
        *,
        task_id: str,
        session_id: str,
        turn_id: str,
        call_id: str,
        server_name: str,
        remote_tool_name: str,
        contract_fingerprint: str,
        server_fingerprint: str,
        protocol_version: str,
        task: dict[str, Any],
        answered_inputs: dict[str, str],
    ) -> None:
        """Persist the latest durable state for one MCP task-backed tool call."""

        status = task.get("status")
        if status not in {
            "working",
            "input_required",
            "completed",
            "cancelled",
            "failed",
        }:
            raise ValueError("invalid MCP task status")
        if len(answered_inputs) > MAX_DURABLE_MCP_ANSWERED_INPUTS:
            raise ValueError(
                "MCP answered-input history exceeds "
                f"{MAX_DURABLE_MCP_ANSWERED_INPUTS} entries"
            )
        try:
            task_json = json.dumps(
                task,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            answered_inputs_json = json.dumps(
                answered_inputs,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            task_bytes = len(task_json.encode("utf-8"))
            answered_input_bytes = len(answered_inputs_json.encode("utf-8"))
        except (TypeError, ValueError, OverflowError, UnicodeEncodeError) as exc:
            raise ValueError("MCP durable task state must be strict JSON") from exc
        if task_bytes > MAX_DURABLE_MCP_TASK_STATE_BYTES:
            raise ValueError(
                "MCP durable task state exceeds "
                f"{MAX_DURABLE_MCP_TASK_STATE_BYTES} UTF-8 bytes"
            )
        if answered_input_bytes > MAX_DURABLE_MCP_ANSWERED_INPUTS_BYTES:
            raise ValueError(
                "MCP answered-input history exceeds "
                f"{MAX_DURABLE_MCP_ANSWERED_INPUTS_BYTES} UTF-8 bytes"
            )
        now = _serialize_datetime(_utc_now())
        with closing(self._connect()) as conn, conn:
            existing = conn.execute(
                "SELECT session_id, call_id, server_fingerprint FROM mcp_tasks "
                "WHERE server_name = ? AND task_id = ?",
                (server_name, task_id),
            ).fetchone()
            if existing is not None and (
                str(existing["session_id"]) != session_id
                or str(existing["call_id"]) != call_id
            ):
                raise ValueError(
                    "MCP server reused a durable taskId for another Ash tool call"
                )
            if existing is not None and str(existing["server_fingerprint"]) != server_fingerprint:
                raise ValueError("MCP durable task server identity changed")
            if existing is None:
                row = conn.execute(
                    "SELECT COUNT(*) AS count FROM mcp_tasks WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if int(row["count"]) >= MAX_DURABLE_MCP_TASKS_PER_SESSION:
                    raise ValueError(
                        "MCP durable task capacity exceeded for this session"
                    )
            conn.execute(
                """
                INSERT INTO mcp_tasks (
                    task_id, session_id, turn_id, call_id, server_name,
                    remote_tool_name, contract_fingerprint, server_fingerprint,
                    protocol_version,
                    status, task_json, answered_inputs_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(server_name, task_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    turn_id = excluded.turn_id,
                    call_id = excluded.call_id,
                    server_name = excluded.server_name,
                    remote_tool_name = excluded.remote_tool_name,
                    contract_fingerprint = excluded.contract_fingerprint,
                    protocol_version = excluded.protocol_version,
                    status = excluded.status,
                    task_json = excluded.task_json,
                    answered_inputs_json = excluded.answered_inputs_json,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    session_id,
                    turn_id,
                    call_id,
                    server_name,
                    remote_tool_name,
                    contract_fingerprint,
                    server_fingerprint,
                    protocol_version,
                    status,
                    task_json,
                    answered_inputs_json,
                    now,
                    now,
                ),
            )

    def delete_mcp_task(self, server_name: str, task_id: str) -> None:
        """Drop one MCP task handle after Ash no longer needs to resume it."""

        with closing(self._connect()) as conn, conn:
            conn.execute(
                "DELETE FROM mcp_tasks WHERE server_name = ? AND task_id = ?",
                (server_name, task_id),
            )

    def list_mcp_tasks(self, session_id: str) -> list[sqlite3.Row]:
        """Return durable MCP task records for one session oldest-first."""

        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM mcp_tasks WHERE session_id = ? "
                "ORDER BY created_at, task_id LIMIT ?",
                (session_id, MAX_DURABLE_MCP_TASKS_PER_SESSION + 1),
            ).fetchall()
        if len(rows) > MAX_DURABLE_MCP_TASKS_PER_SESSION:
            raise SessionStorageError(
                "durable MCP task count exceeds the supported session capacity"
            )
        return rows

    def delete_mcp_task_for_call(self, session_id: str, call_id: str) -> None:
        """Drop a durable MCP handle after its Ash tool result is committed."""

        with closing(self._connect()) as conn, conn:
            conn.execute(
                "DELETE FROM mcp_tasks WHERE session_id = ? AND call_id = ?",
                (session_id, call_id),
            )

    def tool_call_for_recovery(
        self, session_id: str, turn_id: str, call_id: str
    ) -> sqlite3.Row | None:
        """Return one persisted tool call bound to a recovery task."""

        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM tool_calls WHERE session_id = ? AND turn_id = ? "
                "AND call_id = ?",
                (session_id, turn_id, call_id),
            ).fetchone()

    def finalize_mcp_task_recovery(
        self,
        *,
        server_name: str,
        task_id: str,
        session_id: str,
        turn_id: str,
        call_id: str,
        tool_name: str,
        success: bool,
        result: str,
        error: str | None,
        message: Message,
        audit_details: dict[str, Any],
    ) -> bool:
        """Atomically finalize one resumed task and its model-visible result.

        Returns ``True`` when this transaction inserted the tool-result message.
        """

        with closing(self._connect()) as conn, conn:
            call = conn.execute(
                "SELECT executed, error FROM tool_calls WHERE session_id = ? "
                "AND turn_id = ? AND call_id = ?",
                (session_id, turn_id, call_id),
            ).fetchone()
            if call is None:
                conn.execute(
                    "DELETE FROM mcp_tasks WHERE server_name = ? AND task_id = ?",
                    (server_name, task_id),
                )
                return False
            already_terminal = bool(call["executed"]) or call["error"] is not None
            if not already_terminal:
                conn.execute(
                    "UPDATE tool_calls SET executed = 1, result = ?, error = ? "
                    "WHERE session_id = ? AND turn_id = ? AND call_id = ?",
                    (result, error, session_id, turn_id, call_id),
                )
                self._append_audit_log_in_connection(
                    conn,
                    session_id,
                    action_type=(
                        "command_run"
                        if tool_name == "run_command"
                        else "file_write"
                        if tool_name
                        in {
                            "write_file",
                            "whole_edit",
                            "replace_file_content",
                            "replace_file_edits",
                            "apply_patch",
                        }
                        else "tool_call"
                    ),
                    target_resource=tool_name,
                    details={**audit_details, "recovered": True, "replayed": False},
                    result="SUCCESS" if success else "FAILURE",
                )

            message_rows = conn.execute(
                "SELECT metadata_json FROM messages WHERE session_id = ? "
                "AND turn_id = ? AND role = 'tool' ORDER BY message_id",
                (session_id, turn_id),
            ).fetchall()
            inserted = not _tool_result_message_exists(message_rows, call_id)
            if inserted:
                conn.execute(
                    """
                    INSERT INTO messages (
                        session_id, role, content, timestamp, metadata_json,
                        token_count, prompt_tokens, completion_tokens, turn_id
                    ) VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?)
                    """,
                    (
                        session_id,
                        message.role,
                        message.content,
                        _serialize_datetime(message.timestamp),
                        json.dumps(message.metadata),
                        turn_id,
                    ),
                )
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                    (_serialize_datetime(message.timestamp), session_id),
                )
            conn.execute(
                "DELETE FROM mcp_tasks WHERE server_name = ? AND task_id = ?",
                (server_name, task_id),
            )
            return inserted

    def append_audit_log(
        self,
        session_id: str,
        *,
        action_type: AuditAction,
        target_resource: str,
        details: dict[str, Any],
        result: AuditResult,
        timestamp: datetime | None = None,
    ) -> AuditLogRecord:
        """Append one tamper-evident audit event for a session."""

        with closing(self._connect()) as conn, conn:
            # Reserve the SQLite writer before reading the chain tail. Without
            # this, two processes can hash against the same previous record and
            # then serialize their inserts into a forked audit chain.
            conn.execute("BEGIN IMMEDIATE")
            return self._append_audit_log_in_connection(
                conn,
                session_id,
                action_type=action_type,
                target_resource=target_resource,
                details=details,
                result=result,
                timestamp=timestamp,
            )

    def _append_audit_log_in_connection(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        *,
        action_type: AuditAction,
        target_resource: str,
        details: dict[str, Any],
        result: AuditResult,
        timestamp: datetime | None = None,
    ) -> AuditLogRecord:
        """Append one chained audit entry into an existing transaction."""

        from ash.core.redaction import redact_text, redact_value

        event_time = timestamp or _utc_now()
        redacted_target = redact_text(target_resource)
        redacted_details = redact_value(details)
        if not isinstance(redacted_details, dict):
            raise SessionStorageError("redacted audit details are not an object")
        details_json = _canonical_json(redacted_details)
        previous_row = conn.execute(
            """
            SELECT sha256_hash FROM audit_logs
            WHERE session_id = ?
            ORDER BY log_id DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        previous_hash = (
            str(previous_row["sha256_hash"])
            if previous_row is not None and previous_row["sha256_hash"]
            else ""
        )
        event_hash = _audit_hash(
            session_id=session_id,
            timestamp=event_time,
            action_type=action_type,
            target_resource=redacted_target,
            details_json=details_json,
            result=result,
            previous_hash=previous_hash,
        )
        cursor = conn.execute(
            """
            INSERT INTO audit_logs (
                session_id, timestamp, action_type, target_resource, details_json,
                result, sha256_hash, previous_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                _serialize_datetime(event_time),
                action_type,
                redacted_target,
                details_json,
                result,
                event_hash,
                previous_hash,
            ),
        )
        return AuditLogRecord(
            log_id=int(cursor.lastrowid or 0),
            session_id=session_id,
            action_type=action_type,
            target_resource=redacted_target,
            details=json.loads(details_json),
            result=result,
            timestamp=event_time,
            previous_hash=previous_hash,
            sha256_hash=event_hash,
        )

    def list_audit_logs(self, session_id: str) -> list[AuditLogRecord]:
        """Return audit log records for a session in append order."""

        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                """
                SELECT log_id, session_id, timestamp, action_type, target_resource,
                       details_json, result, sha256_hash, previous_hash
                FROM audit_logs
                WHERE session_id = ?
                ORDER BY log_id ASC
                """,
                (session_id,),
            ).fetchall()
        try:
            return [_audit_record_from_row(row) for row in rows]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise _invalid_stored_data_error(self.db_path) from exc

    def iter_audit_logs(
        self,
        session_id: str,
        *,
        batch_size: int = 256,
    ) -> Iterator[AuditLogRecord]:
        """Yield audit records in append order without materializing the chain."""

        if batch_size < 1 or batch_size > 4096:
            raise ValueError("audit batch_size must be between 1 and 4096")
        try:
            with closing(self._connect()) as conn:
                cursor = conn.execute(
                    """
                    SELECT log_id, session_id, timestamp, action_type, target_resource,
                           details_json, result, sha256_hash, previous_hash
                    FROM audit_logs
                    WHERE session_id = ?
                    ORDER BY log_id ASC
                    """,
                    (session_id,),
                )
                while True:
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        break
                    for row in rows:
                        yield _audit_record_from_row(row)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise _invalid_stored_data_error(self.db_path) from exc

    def verify_audit_log(self, session_id: str) -> list[str]:
        """Return integrity errors for a session audit chain."""

        errors: list[str] = []
        omitted = 0
        previous_hash = ""

        def record_error(message: str) -> None:
            nonlocal omitted
            if len(errors) < MAX_AUDIT_VERIFICATION_ERRORS:
                errors.append(message)
            else:
                omitted += 1

        for record in self.iter_audit_logs(session_id):
            if record.previous_hash != previous_hash:
                record_error(f"audit log {record.log_id} previous_hash mismatch")
            expected_hash = _audit_hash(
                session_id=record.session_id,
                timestamp=record.timestamp,
                action_type=record.action_type,
                target_resource=record.target_resource,
                details_json=_canonical_json(record.details),
                result=record.result,
                previous_hash=record.previous_hash,
            )
            if record.sha256_hash != expected_hash:
                record_error(f"audit log {record.log_id} sha256_hash mismatch")
            previous_hash = record.sha256_hash
        if omitted:
            errors.append(
                f"{omitted} additional audit verification error(s) omitted"
            )
        return errors

    # --- durable Goal persistence ---------------------------------------

    @staticmethod
    def _goal_record_from_row(row: sqlite3.Row) -> GoalRecord:
        try:
            return GoalRecord(
                goal_id=str(row["goal_id"]),
                session_id=str(row["session_id"]),
                objective=str(row["objective"]),
                state=GoalState(str(row["state"])),
                max_continuations=int(row["max_continuations"]),
                continuations_used=int(row["continuations_used"]),
                last_evidence=str(row["last_evidence"] or ""),
                created_at=_deserialize_datetime(row["created_at"]),
                updated_at=_deserialize_datetime(row["updated_at"]),
                completed_at=(
                    _deserialize_datetime(row["completed_at"])
                    if row["completed_at"]
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise SessionStorageError("stored Goal data is invalid") from exc

    def create_goal(
        self,
        session_id: str,
        objective: str,
        *,
        max_continuations: int,
    ) -> GoalRecord:
        """Create the single current Goal for a session."""

        from ash.core.redaction import redact_text

        normalized_objective = bounded_goal_text(
            redact_text(objective),
            label="goal objective",
            maximum=MAX_GOAL_OBJECTIVE_BYTES,
        )
        limit = validate_goal_continuation_limit(max_continuations)
        goal_id = str(uuid4())
        now = _utc_now()
        with closing(self._connect()) as conn, conn:
            if (
                conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                is None
            ):
                raise KeyError(f"Session not found: {session_id}")
            current = conn.execute(
                """
                SELECT goal_id FROM goals
                WHERE session_id = ?
                  AND state IN ('active','paused','budget_limited')
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if current is not None:
                raise ValueError(
                    "session already has a current Goal; clear or complete it first"
                )
            conn.execute(
                """
                INSERT INTO goals (
                    goal_id, session_id, objective, state,
                    max_continuations, continuations_used, last_evidence,
                    created_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, 'active', ?, 0, '', ?, ?, NULL)
                """,
                (
                    goal_id,
                    session_id,
                    normalized_objective,
                    limit,
                    _serialize_datetime(now),
                    _serialize_datetime(now),
                ),
            )
        return self.load_goal(goal_id)

    def load_goal(self, goal_id: str) -> GoalRecord:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT goal_id, session_id, objective, state,
                       max_continuations, continuations_used, last_evidence,
                       created_at, updated_at, completed_at
                FROM goals WHERE goal_id = ?
                """,
                (goal_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Goal not found: {goal_id}")
        return self._goal_record_from_row(row)

    def load_current_goal(self, session_id: str) -> GoalRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT goal_id, session_id, objective, state,
                       max_continuations, continuations_used, last_evidence,
                       created_at, updated_at, completed_at
                FROM goals
                WHERE session_id = ?
                  AND state IN ('active','paused','budget_limited')
                ORDER BY updated_at DESC, goal_id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return None if row is None else self._goal_record_from_row(row)

    def record_goal_progress(
        self,
        goal_id: str,
        evidence: str,
        *,
        complete: bool = False,
    ) -> GoalRecord:
        """Persist bounded evidence, optionally completing an active Goal."""

        from ash.core.redaction import redact_text

        normalized_evidence = bounded_goal_text(
            redact_text(evidence),
            label="goal evidence",
            maximum=MAX_GOAL_EVIDENCE_BYTES,
        )
        now = _utc_now()
        next_state = GoalState.COMPLETE if complete else GoalState.ACTIVE
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT state FROM goals WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Goal not found: {goal_id}")
            if GoalState(str(row["state"])) is not GoalState.ACTIVE:
                raise ValueError("only an active Goal can record progress")
            conn.execute(
                """
                UPDATE goals
                SET state = ?, last_evidence = ?, updated_at = ?, completed_at = ?
                WHERE goal_id = ?
                """,
                (
                    next_state.value,
                    normalized_evidence,
                    _serialize_datetime(now),
                    _serialize_datetime(now) if complete else None,
                    goal_id,
                ),
            )
        return self.load_goal(goal_id)

    def pause_current_goal(self, session_id: str) -> GoalRecord | None:
        """Pause the current Goal without discarding progress evidence."""

        goal = self.load_current_goal(session_id)
        if goal is None:
            return None
        if goal.state is GoalState.PAUSED:
            return goal
        now = _utc_now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE goals
                SET state = 'paused', updated_at = ?
                WHERE goal_id = ?
                  AND state IN ('active','budget_limited')
                """,
                (_serialize_datetime(now), goal.goal_id),
            )
        return self.load_goal(goal.goal_id)

    def resume_current_goal(self, session_id: str) -> GoalRecord:
        """Activate a paused/budget-limited Goal with a fresh continuation window."""

        goal = self.load_current_goal(session_id)
        if goal is None:
            raise KeyError("no current Goal")
        if goal.state is GoalState.ACTIVE:
            return goal
        now = _utc_now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE goals
                SET state = 'active', continuations_used = 0, updated_at = ?
                WHERE goal_id = ?
                  AND state IN ('paused','budget_limited')
                """,
                (_serialize_datetime(now), goal.goal_id),
            )
        return self.load_goal(goal.goal_id)

    def clear_current_goal(self, session_id: str) -> GoalRecord:
        """Clear a current Goal without representing it as completed."""

        goal = self.load_current_goal(session_id)
        if goal is None:
            raise KeyError("no current Goal")
        now = _utc_now()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE goals
                SET state = 'cleared', updated_at = ?
                WHERE goal_id = ?
                  AND state IN ('active','paused','budget_limited')
                """,
                (_serialize_datetime(now), goal.goal_id),
            )
        return self.load_goal(goal.goal_id)

    def claim_goal_continuation(self, goal_id: str) -> GoalRecord:
        """Atomically consume one continuation or mark the Goal budget-limited."""

        now = _utc_now()
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                """
                SELECT state, max_continuations, continuations_used
                FROM goals WHERE goal_id = ?
                """,
                (goal_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Goal not found: {goal_id}")
            state = GoalState(str(row["state"]))
            if state is GoalState.ACTIVE:
                used = int(row["continuations_used"])
                limit = int(row["max_continuations"])
                if used >= limit:
                    conn.execute(
                        """
                        UPDATE goals
                        SET state = 'budget_limited', updated_at = ?
                        WHERE goal_id = ? AND state = 'active'
                        """,
                        (_serialize_datetime(now), goal_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE goals
                        SET continuations_used = continuations_used + 1,
                            updated_at = ?
                        WHERE goal_id = ? AND state = 'active'
                        """,
                        (_serialize_datetime(now), goal_id),
                    )
        return self.load_goal(goal_id)

    # --- sprint + checklist persistence (Sprint 12 / V5) ---------------

    def save_sprint(
        self,
        session_id: str,
        execution: Any,  # ash.core.sprint.SprintExecution; forward-ref to avoid import cycle
    ) -> None:
        """
        Insert or replace a :class:`~ash.core.sprint.SprintExecution`.

        The contract is serialized as JSON; checklist items are written
        one row per item keyed by ``(sprint_id, idx)``. Existing rows
        for the same ``idx`` are updated in place.
        """

        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO sprints (
                    sprint_id, session_id, goal, state,
                    contract_json, created_at, started_at, completed_at, abort_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sprint_id) DO UPDATE SET
                    state = excluded.state,
                    contract_json = excluded.contract_json,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at,
                    abort_reason = excluded.abort_reason
                """,
                (
                    execution.contract.contract_id,
                    session_id,
                    execution.contract.goal,
                    str(execution.state),
                    json.dumps(execution.contract.to_dict()),
                    _serialize_datetime(execution.created_at),
                    _serialize_datetime(execution.started_at)
                    if execution.started_at
                    else None,
                    _serialize_datetime(execution.completed_at)
                    if execution.completed_at
                    else None,
                    execution.abort_reason,
                ),
            )

            for item in execution.items:
                conn.execute(
                    """
                    INSERT INTO checklist_items (
                        sprint_id, idx, section, description, status, notes
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sprint_id, idx) DO UPDATE SET
                        section = excluded.section,
                        description = excluded.description,
                        status = excluded.status,
                        notes = excluded.notes
                    """,
                    (
                        execution.contract.contract_id,
                        item.idx,
                        item.section,
                        item.description,
                        str(item.status),
                        item.notes,
                    ),
                )

    def load_sprint(self, sprint_id: str) -> Any:
        """
        Re-hydrate a :class:`~ash.core.sprint.SprintExecution` from SQLite.

        Returns the fully populated execution including checklist
        items. Raises :class:`KeyError` if the sprint id is unknown.
        """

        # Imported here to avoid a circular import: planner/sprint depend
        # on session typing, session persistence depends on sprint types.
        from ash.core.sprint import (
            ChecklistItem,
            ChecklistStatus,
            SprintContract,
            SprintExecution,
            SprintState,
        )

        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT sprint_id, session_id, goal, state, contract_json,
                       created_at, started_at, completed_at, abort_reason
                FROM sprints WHERE sprint_id = ?
                """,
                (sprint_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Sprint not found: {sprint_id}")

            try:
                contract_data = json.loads(row["contract_json"])
                contract = SprintContract.from_dict(contract_data)
                execution = SprintExecution(
                    contract=contract,
                    state=SprintState(row["state"]),
                    created_at=_deserialize_datetime(row["created_at"]),
                    started_at=_deserialize_datetime(row["started_at"])
                    if row["started_at"]
                    else None,
                    completed_at=_deserialize_datetime(row["completed_at"])
                    if row["completed_at"]
                    else None,
                    abort_reason=row["abort_reason"] or "",
                )

                item_rows = conn.execute(
                    """
                    SELECT idx, section, description, status, notes
                    FROM checklist_items WHERE sprint_id = ?
                    ORDER BY idx ASC
                    """,
                    (sprint_id,),
                ).fetchall()
                execution.set_items(
                    [
                        ChecklistItem(
                            idx=r["idx"],
                            section=r["section"],
                            description=r["description"],
                            status=ChecklistStatus(r["status"]),
                            notes=r["notes"] or "",
                        )
                        for r in item_rows
                    ]
                )
                return execution
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise _invalid_stored_data_error(self.db_path) from exc

    def load_latest_active_sprint(self, session_id: str) -> Any | None:
        """Load only the newest planning/active sprint for a session."""

        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT sprint_id FROM sprints
                WHERE session_id = ? AND state IN ('planning', 'active')
                ORDER BY created_at DESC, sprint_id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        execution = self.load_sprint(str(row["sprint_id"]))
        return None if execution.is_terminal else execution

    def list_session_sprints(self, session_id: str) -> list[str]:
        """Return sprint IDs persisted against a session, newest first."""

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT sprint_id FROM sprints
                WHERE session_id = ?
                ORDER BY created_at DESC, sprint_id DESC
                """,
                (session_id,),
            ).fetchall()
        return [str(row["sprint_id"]) for row in rows]

    def get_recent_session_summaries(
        self,
        project_path: str,
        limit: int = 5,
    ) -> list[str]:
        """Return bounded recent transcript tails for recent project sessions."""

        if not 1 <= limit <= MAX_RECENT_SESSION_CONTEXT_SESSIONS:
            raise ValueError(
                "recent session limit must be between 1 and "
                f"{MAX_RECENT_SESSION_CONTEXT_SESSIONS}"
            )
        summaries: list[str] = []
        with closing(self._connect()) as conn:
            session_rows = conn.execute(
                """
                SELECT session_id
                FROM sessions
                WHERE project_key = ?
                  AND EXISTS (
                      SELECT 1 FROM messages
                      WHERE messages.session_id = sessions.session_id
                  )
                ORDER BY COALESCE(updated_at, created_at) DESC, session_id DESC
                LIMIT ?
                """,
                (normalize_project_path(project_path), limit),
            ).fetchall()
            for session_row in session_rows:
                message_rows = conn.execute(
                    """
                    SELECT substr(content, 1, ?) AS content
                    FROM messages
                    WHERE session_id = ?
                    ORDER BY message_id DESC
                    LIMIT ?
                    """,
                    (
                        MAX_RECENT_SESSION_CONTEXT_CHARS,
                        session_row["session_id"],
                        MAX_RECENT_SESSION_CONTEXT_MESSAGES,
                    ),
                ).fetchall()
                newest_first: list[str] = []
                used = 0
                for row in message_rows:
                    content = str(row["content"] or "")
                    separator = 1 if newest_first else 0
                    remaining = (
                        MAX_RECENT_SESSION_CONTEXT_CHARS - used - separator
                    )
                    if remaining <= 0:
                        break
                    clipped = content[:remaining]
                    newest_first.append(clipped)
                    used += separator + len(clipped)
                if newest_first:
                    summaries.append("\n".join(reversed(newest_first)))
        return summaries
