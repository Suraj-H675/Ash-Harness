"""Session persistence and SQLite storage for Ash."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


Role = Literal["system", "user", "assistant", "tool"]
AuditAction = Literal[
    "tool_call", "command_run", "file_write", "safety_block", "user_approval"
]
AuditResult = Literal["APPROVED", "DENIED", "BLOCKED_BY_GUARD", "SUCCESS", "FAILURE"]
CURRENT_SCHEMA_VERSION = 4


class SessionStorageError(RuntimeError):
    """Session database cannot be opened or migrated safely."""


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
    model: str = ""
    messages: list[Message] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class SessionSummary(BaseModel):
    session_id: str
    project_path: str
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0
    model: str = ""


class SessionUsage(BaseModel):
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0


_db_write_locks: dict[str, asyncio.Lock] = {}
_db_write_locks_guard = threading.Lock()


def _normalize_db_path(db_path: str | Path) -> str:
    return str(Path(db_path).expanduser().resolve())


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


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})")
    )


def _restrict_file_permissions(path: Path) -> None:
    if os.name != "nt" and path.exists():
        path.chmod(0o600)


def get_db_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection configured for WAL persistence."""

    normalized_path = Path(db_path).expanduser()
    normalized_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(normalized_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@asynccontextmanager
async def write_transaction(db_path: str | Path) -> AsyncIterator[sqlite3.Connection]:
    """Serialize asynchronous SQLite write transactions for a database path."""

    lock_key = _normalize_db_path(db_path)
    with _db_write_locks_guard:
        lock = _db_write_locks.setdefault(lock_key, asyncio.Lock())

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
        self.db_path = _normalize_db_path(db_path)
        path = Path(self.db_path)
        existed = path.is_file() and path.stat().st_size > 0
        try:
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
            _restrict_file_permissions(path)
        except SessionStorageError:
            raise
        except (OSError, sqlite3.DatabaseError) as exc:
            raise SessionStorageError(
                f"Could not initialize session database {path}: {exc}. "
                "Run 'ash storage check' and restore a backup if needed."
            ) from exc

    def _schema_version(self) -> int:
        if not Path(self.db_path).exists():
            return 0
        with closing(get_db_connection(self.db_path)) as conn:
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

        with closing(get_db_connection(self.db_path)) as conn, conn:
            if from_version < 1:
                self._migrate_v1(conn)
            if from_version < 2:
                self._migrate_v2(conn)
            if from_version < 3:
                self._migrate_v3(conn)
            if from_version < 4:
                self._migrate_v4(conn)

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
            destination_path = Path(destination).expanduser().resolve()
        if destination_path == source_path:
            raise SessionStorageError(
                "Backup destination must differ from the database"
            )
        if destination_path.exists():
            raise SessionStorageError(
                f"Backup destination already exists: {destination_path}"
            )
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            closing(get_db_connection(source_path)) as source,
            closing(sqlite3.connect(destination_path)) as target,
        ):
            source.backup(target)
        _restrict_file_permissions(destination_path)
        return destination_path

    def _init_db(self) -> None:
        """Create session and audit tables if they do not exist."""

        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    project_path TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    title TEXT DEFAULT '',
                    updated_at TIMESTAMP,
                    context_summary TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    total_tokens INTEGER DEFAULT 0,
                    total_cost_inr REAL DEFAULT 0,
                    total_cost_usd REAL DEFAULT 0,
                    total_prompt_tokens INTEGER DEFAULT 0,
                    total_completion_tokens INTEGER DEFAULT 0,
                    total_cache_read_tokens INTEGER DEFAULT 0,
                    total_cache_write_tokens INTEGER DEFAULT 0
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
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS tool_calls (
                    call_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    approved INTEGER CHECK(approved IN (0, 1)) DEFAULT 0,
                    executed INTEGER CHECK(executed IN (0, 1)) DEFAULT 0,
                    result TEXT,
                    error TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
                        'user_approval'
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
                    UNIQUE(session_id, turn_id, path),
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
                CREATE INDEX IF NOT EXISTS idx_checklist_sprint ON checklist_items(sprint_id);

                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMP NOT NULL
                );
                """
            )

    def create_session(self, project_path: str, *, model: str = "") -> Session:
        """Create a new session record in SQLite and return its model."""

        canonical_project_path = normalize_project_path(project_path)
        session = Session(
            session_id=str(uuid4()),
            project_path=canonical_project_path,
            created_at=_utc_now(),
            updated_at=_utc_now(),
            model=model,
        )
        updated_at = session.updated_at or session.created_at

        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, project_path, project_key, created_at, updated_at, model
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.project_path,
                    canonical_project_path,
                    _serialize_datetime(session.created_at),
                    _serialize_datetime(updated_at),
                    session.model,
                ),
            )

        return session

    def load_session(self, session_id: str) -> Session:
        """Load a session with all messages and tool call records."""

        with closing(get_db_connection(self.db_path)) as conn, conn:
            session_row = conn.execute(
                """
                SELECT session_id, project_path, created_at, title, updated_at,
                       context_summary, model
                FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()

            if session_row is None:
                raise KeyError(f"Session not found: {session_id}")

            message_rows = conn.execute(
                """
                SELECT role, content, timestamp, metadata_json
                FROM messages
                WHERE session_id = ?
                ORDER BY message_id ASC
                """,
                (session_id,),
            ).fetchall()

            tool_call_rows = conn.execute(
                """
                SELECT call_id, tool_name, arguments_json, approved, executed, result, error, timestamp
                FROM tool_calls
                WHERE session_id = ?
                ORDER BY timestamp ASC, call_id ASC
                """,
                (session_id,),
            ).fetchall()

        return Session(
            session_id=session_row["session_id"],
            project_path=session_row["project_path"],
            created_at=_deserialize_datetime(session_row["created_at"]),
            title=session_row["title"] or "",
            updated_at=_deserialize_datetime(
                session_row["updated_at"] or session_row["created_at"]
            ),
            context_summary=session_row["context_summary"] or "",
            model=session_row["model"] or "",
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
                    result=row["result"],
                    error=row["error"],
                    timestamp=_deserialize_datetime(row["timestamp"]),
                )
                for row in tool_call_rows
            ],
        )

    def save_message(
        self,
        session_id: str,
        message: Message,
        token_count: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """Append a single message to a session."""

        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO messages (session_id, role, content, timestamp, metadata_json, token_count, prompt_tokens, completion_tokens)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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

        if limit < 1:
            raise ValueError("limit must be positive")
        clauses: list[str] = []
        params: list[Any] = []
        if project_path is not None:
            clauses.append("s.project_key = ?")
            params.append(normalize_project_path(project_path))
        if query:
            clauses.append("(s.title LIKE ? OR s.session_id LIKE ?)")
            pattern = f"%{query}%"
            params.extend((pattern, pattern))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with closing(get_db_connection(self.db_path)) as conn:
            rows = conn.execute(
                f"""
                SELECT s.session_id, s.project_path, s.title, s.created_at,
                       COALESCE(s.updated_at, s.created_at) AS updated_at,
                       COUNT(m.message_id) AS message_count, s.model
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.session_id
                {where}
                GROUP BY s.session_id
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            SessionSummary(
                session_id=row["session_id"],
                project_path=row["project_path"],
                title=row["title"] or "",
                created_at=_deserialize_datetime(row["created_at"]),
                updated_at=_deserialize_datetime(row["updated_at"]),
                message_count=int(row["message_count"]),
                model=row["model"] or "",
            )
            for row in rows
        ]

    def get_session_usage(self, session_id: str) -> SessionUsage:
        """Return persisted token and explicitly configured cost totals."""

        with closing(get_db_connection(self.db_path)) as conn:
            row = conn.execute(
                "SELECT COALESCE(total_tokens, 0) AS total_tokens, "
                "COALESCE(total_prompt_tokens, 0) AS prompt_tokens, "
                "COALESCE(total_completion_tokens, 0) AS completion_tokens, "
                "COALESCE(total_cache_read_tokens, 0) AS cache_read_tokens, "
                "COALESCE(total_cache_write_tokens, 0) AS cache_write_tokens, "
                "COALESCE(total_cost_usd, 0) AS cost_usd "
                "FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Session not found: {session_id}")
        return SessionUsage(**dict(row))

    def cleanup_sessions(
        self, retention_days: int, *, project_path: str | None = None
    ) -> int:
        """Delete old sessions and compact the database explicitly."""
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        cutoff = _utc_now() - timedelta(days=retention_days)
        clause = " AND project_key = ?" if project_path is not None else ""
        params: tuple[Any, ...] = (
            (_serialize_datetime(cutoff), normalize_project_path(project_path))
            if project_path is not None
            else (_serialize_datetime(cutoff),)
        )
        with closing(get_db_connection(self.db_path)) as conn, conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE COALESCE(updated_at, created_at) < ?"
                + clause,
                params,
            )
            deleted = cursor.rowcount
        with closing(get_db_connection(self.db_path)) as conn:
            conn.execute("VACUUM")
        return deleted

    def start_turn(self, session_id: str, turn_id: str, user_input: str) -> None:
        """Persist intent before provider or tool work starts."""
        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO turn_journal "
                "(turn_id, session_id, status, user_input, started_at) "
                "VALUES (?, ?, 'started', ?, ?)",
                (turn_id, session_id, user_input, _serialize_datetime(_utc_now())),
            )

    def complete_turn(self, turn_id: str) -> None:
        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE turn_journal SET status = 'completed', completed_at = ? "
                "WHERE turn_id = ?",
                (_serialize_datetime(_utc_now()), turn_id),
            )

    def interrupt_turn(self, turn_id: str) -> None:
        """Mark one in-flight turn interrupted without affecting other sessions."""

        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE turn_journal SET status = 'interrupted', completed_at = ? "
                "WHERE turn_id = ? AND status = 'started'",
                (_serialize_datetime(_utc_now()), turn_id),
            )

    def reconcile_interrupted_turns(self, session_id: str) -> int:
        with closing(get_db_connection(self.db_path)) as conn, conn:
            cursor = conn.execute(
                "UPDATE turn_journal SET status = 'interrupted', completed_at = ? "
                "WHERE session_id = ? AND status = 'started'",
                (_serialize_datetime(_utc_now()), session_id),
            )
            return cursor.rowcount

    def rewind_session(self, session_id: str, message_count: int) -> Session:
        """Delete transcript records after a confirmed message boundary."""
        session = self.load_session(session_id)
        if message_count < 0 or message_count > len(session.messages):
            raise ValueError(
                f"message_count must be between 0 and {len(session.messages)}"
            )
        retained = session.messages[:message_count]
        cutoff = retained[-1].timestamp if retained else None
        with closing(get_db_connection(self.db_path)) as conn, conn:
            ids = conn.execute(
                "SELECT message_id FROM messages WHERE session_id = ? "
                "ORDER BY message_id LIMIT -1 OFFSET ?",
                (session_id, message_count),
            ).fetchall()
            conn.executemany(
                "DELETE FROM messages WHERE message_id = ?",
                [(row["message_id"],) for row in ids],
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
                "UPDATE sessions SET context_summary = '', updated_at = ? "
                "WHERE session_id = ?",
                (_serialize_datetime(_utc_now()), session_id),
            )
        return self.load_session(session_id)

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
    ) -> None:
        """Save the first pre-edit state for one path in a turn."""
        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO file_checkpoints
                    (session_id, turn_id, tool_name, path, existed,
                     before_content, before_mode, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )

    def finish_file_checkpoint(
        self, session_id: str, turn_id: str, path: str, after_sha256: str
    ) -> None:
        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE file_checkpoints SET after_sha256 = ? "
                "WHERE session_id = ? AND turn_id = ? AND path = ?",
                (after_sha256, session_id, turn_id, path),
            )

    def latest_file_checkpoints(self, session_id: str) -> list[sqlite3.Row]:
        """Return the latest unrestored completed checkpoint group."""
        with closing(get_db_connection(self.db_path)) as conn:
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
                "ORDER BY checkpoint_id",
                (session_id, turn["turn_id"]),
            ).fetchall()

    def mark_file_checkpoints_restored(self, session_id: str, turn_id: str) -> None:
        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE file_checkpoints SET restored = 1 "
                "WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            )

    def rename_session(self, session_id: str, title: str) -> None:
        """Set a human-readable session title."""

        normalized = " ".join(title.split())
        if not normalized:
            raise ValueError("session title cannot be empty")
        with closing(get_db_connection(self.db_path)) as conn, conn:
            cursor = conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE session_id = ?",
                (normalized, _serialize_datetime(_utc_now()), session_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Session not found: {session_id}")

    def save_context_summary(self, session_id: str, summary: str) -> None:
        """Persist the working compaction summary without deleting history."""

        with closing(get_db_connection(self.db_path)) as conn, conn:
            cursor = conn.execute(
                "UPDATE sessions SET context_summary = ?, updated_at = ? "
                "WHERE session_id = ?",
                (summary, _serialize_datetime(_utc_now()), session_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Session not found: {session_id}")

    def fork_session(
        self, session_id: str, *, message_count: int | None = None
    ) -> Session:
        """Create a new session containing a prefix of another transcript."""

        source = self.load_session(session_id)
        count = len(source.messages) if message_count is None else message_count
        if count < 0 or count > len(source.messages):
            raise ValueError(
                f"message_count must be between 0 and {len(source.messages)}"
            )
        fork = self.create_session(source.project_path, model=source.model)
        title = f"{source.title or source.session_id[:8]} (fork)"
        self.rename_session(fork.session_id, title)
        for message in source.messages[:count]:
            self.save_message(fork.session_id, message)
        if source.context_summary and count == len(source.messages):
            self.save_context_summary(fork.session_id, source.context_summary)
        return self.load_session(fork.session_id)

    def export_session(self, session_id: str, *, format: str = "jsonl") -> str:
        """Serialize a redacted session transcript for local export."""

        from core.redaction import redact_text, redact_value

        session = self.load_session(session_id)
        if format == "jsonl":
            records = [
                {
                    "schema_version": 1,
                    "type": "session",
                    "session_id": session.session_id,
                    "project_path": session.project_path,
                    "model": session.model,
                    "title": session.title,
                    "created_at": session.created_at.isoformat(),
                }
            ]
            records.extend(
                {
                    "schema_version": 1,
                    "type": "message",
                    "role": message.role,
                    "content": redact_text(message.content),
                    "timestamp": message.timestamp.isoformat(),
                    "metadata": redact_value(message.metadata),
                }
                for message in session.messages
            )
            return (
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
                + "\n"
            )
        if format == "markdown":
            heading = redact_text(session.title or f"Ash session {session.session_id}")
            sections = [f"# {heading}", f"Model: `{session.model or 'unknown'}`"]
            sections.extend(
                f"## {message.role.title()}\n\n{redact_text(message.content)}"
                for message in session.messages
            )
            return "\n\n".join(sections) + "\n"
        raise ValueError("format must be 'jsonl' or 'markdown'")

    def import_session_jsonl(self, content: str, *, project_path: str) -> Session:
        """Import Ash's versioned JSONL format into the current project."""
        try:
            records = [
                json.loads(line) for line in content.splitlines() if line.strip()
            ]
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid session JSONL: {exc}") from exc
        if not records or not isinstance(records[0], dict):
            raise ValueError("session JSONL is empty")
        header = records[0]
        if header.get("schema_version") != 1 or header.get("type") != "session":
            raise ValueError("unsupported session export schema")
        session = self.create_session(project_path, model=str(header.get("model", "")))
        title = str(header.get("title", "")).strip()
        if title:
            self.rename_session(session.session_id, f"{title} (imported)")
        for record in records[1:]:
            if not isinstance(record, dict) or record.get("type") != "message":
                raise ValueError("session export contains an invalid record")
            role = record.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                raise ValueError(f"invalid imported message role: {role!r}")
            try:
                timestamp = _deserialize_datetime(str(record["timestamp"]))
            except (KeyError, ValueError) as exc:
                raise ValueError("imported message has an invalid timestamp") from exc
            metadata = record.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError("imported message metadata must be an object")
            self.save_message(
                session.session_id,
                Message(
                    role=role,
                    content=str(record.get("content", "")),
                    timestamp=timestamp,
                    metadata=metadata,
                ),
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
    ) -> None:
        """Accumulate one turn's token and explicitly configured cost totals."""

        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE sessions
                SET total_tokens = COALESCE(total_tokens, 0) + ?,
                    total_cost_usd = COALESCE(total_cost_usd, 0) + ?,
                    total_prompt_tokens = COALESCE(total_prompt_tokens, 0) + ?,
                    total_completion_tokens = COALESCE(total_completion_tokens, 0) + ?,
                    total_cache_read_tokens = COALESCE(total_cache_read_tokens, 0) + ?,
                    total_cache_write_tokens = COALESCE(total_cache_write_tokens, 0) + ?
                WHERE session_id = ?
                """,
                (
                    total_prompt_tokens + total_completion_tokens,
                    turn_cost_usd,
                    total_prompt_tokens,
                    total_completion_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                    session_id,
                ),
            )

    def save_tool_call(self, session_id: str, record: ToolCallRecord) -> None:
        """Save or update a tool execution record."""

        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO tool_calls (
                    call_id,
                    session_id,
                    tool_name,
                    arguments_json,
                    approved,
                    executed,
                    result,
                    error,
                    timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(call_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    tool_name = excluded.tool_name,
                    arguments_json = excluded.arguments_json,
                    approved = excluded.approved,
                    executed = excluded.executed,
                    result = excluded.result,
                    error = excluded.error,
                    timestamp = excluded.timestamp
                """,
                (
                    record.call_id,
                    session_id,
                    record.tool_name,
                    json.dumps(record.arguments),
                    int(record.approved),
                    int(record.executed),
                    record.result,
                    record.error,
                    _serialize_datetime(record.timestamp),
                ),
            )

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

        event_time = timestamp or _utc_now()
        details_json = _canonical_json(details)
        with closing(get_db_connection(self.db_path)) as conn, conn:
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
                target_resource=target_resource,
                details_json=details_json,
                result=result,
                previous_hash=previous_hash,
            )
            cursor = conn.execute(
                """
                INSERT INTO audit_logs (
                    session_id,
                    timestamp,
                    action_type,
                    target_resource,
                    details_json,
                    result,
                    sha256_hash,
                    previous_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    _serialize_datetime(event_time),
                    action_type,
                    target_resource,
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
                target_resource=target_resource,
                details=json.loads(details_json),
                result=result,
                timestamp=event_time,
                previous_hash=previous_hash,
                sha256_hash=event_hash,
            )

    def list_audit_logs(self, session_id: str) -> list[AuditLogRecord]:
        """Return audit log records for a session in append order."""

        with closing(get_db_connection(self.db_path)) as conn, conn:
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
        return [_audit_record_from_row(row) for row in rows]

    def verify_audit_log(self, session_id: str) -> list[str]:
        """Return integrity errors for a session audit chain."""

        errors: list[str] = []
        previous_hash = ""
        for record in self.list_audit_logs(session_id):
            if record.previous_hash != previous_hash:
                errors.append(f"audit log {record.log_id} previous_hash mismatch")
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
                errors.append(f"audit log {record.log_id} sha256_hash mismatch")
            previous_hash = record.sha256_hash
        return errors

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

        with closing(get_db_connection(self.db_path)) as conn, conn:
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
        from core.sprint import (
            ChecklistItem,
            ChecklistStatus,
            SprintContract,
            SprintExecution,
            SprintState,
        )

        with closing(get_db_connection(self.db_path)) as conn:
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

    def list_session_sprints(self, session_id: str) -> list[str]:
        """Return the sprint ids persisted against a session, newest first."""

        with closing(get_db_connection(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT sprint_id FROM sprints WHERE session_id = ?
                ORDER BY created_at DESC
                """,
                (session_id,),
            ).fetchall()
        return [r["sprint_id"] for r in rows]

    def get_recent_session_summaries(
        self,
        project_path: str,
        limit: int = 5,
    ) -> list[str]:
        """Return plain-text content of the N most recent sessions for a project."""
        with closing(get_db_connection(self.db_path)) as conn, conn:
            rows = conn.execute(
                """
                SELECT s.session_id,
                       GROUP_CONCAT(m.content, '\n') as messages
                FROM sessions s
                JOIN messages m ON m.session_id = s.session_id
                WHERE s.project_key = ?
                GROUP BY s.session_id
                ORDER BY s.created_at DESC
                LIMIT ?
                """,
                (normalize_project_path(project_path), limit),
            ).fetchall()
        return [row["messages"] for row in rows]
