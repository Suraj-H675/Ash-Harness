"""Session persistence and SQLite storage for Ash."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


Role = Literal["system", "user", "assistant", "tool"]


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


class Session(BaseModel):
    session_id: str
    project_path: str
    created_at: datetime
    messages: list[Message] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


_db_write_locks: dict[str, asyncio.Lock] = {}
_db_write_locks_guard = threading.Lock()


def _normalize_db_path(db_path: str | Path) -> str:
    return str(Path(db_path).expanduser().resolve())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_datetime(value: datetime) -> str:
    return value.isoformat()


def _deserialize_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


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
        self._init_db()

    def _init_db(self) -> None:
        """Create session and audit tables if they do not exist."""

        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    project_path TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT CHECK(role IN ('system', 'user', 'assistant', 'tool')),
                    content TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    metadata_json TEXT,
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
                    sha256_hash TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_logs(session_id);

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
                """
            )

    def create_session(self, project_path: str) -> Session:
        """Create a new session record in SQLite and return its model."""

        session = Session(
            session_id=str(uuid4()),
            project_path=project_path,
            created_at=_utc_now(),
        )

        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, project_path, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    session.session_id,
                    session.project_path,
                    _serialize_datetime(session.created_at),
                ),
            )

        return session

    def load_session(self, session_id: str) -> Session:
        """Load a session with all messages and tool call records."""

        with closing(get_db_connection(self.db_path)) as conn, conn:
            session_row = conn.execute(
                """
                SELECT session_id, project_path, created_at
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

    def save_message(self, session_id: str, message: Message) -> None:
        """Append a single message to a session."""

        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO messages (session_id, role, content, timestamp, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    message.role,
                    message.content,
                    _serialize_datetime(message.timestamp),
                    json.dumps(message.metadata),
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
        from ash.core.sprint import (
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
