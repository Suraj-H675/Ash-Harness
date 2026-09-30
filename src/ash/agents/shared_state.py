"""Shared state for V6 subagent orchestration.

The :class:`SharedState` is the SQLite-backed coordination layer that
the lead orchestrator and its subagents read from / write to while
running concurrently. Per the V6 architecture in
ASH_MASTER_PLAN_V2.md, the database MUST run in WAL mode so multiple
agents can read and write without blocking each other.

Tables (per ARCHITECTURAL_SPECIFICATION.md section 3.3):

* ``agent_status`` — every registered agent, its current status, the
  task it is working on, and the most recent heartbeat timestamp.
* ``ipc_messages`` — point-to-point JSON-RPC-shaped messages between
  agents. ``delivered=0`` means the message has not yet been
  consumed by the recipient.
* ``sprints`` — the V5 sprint contract id, the lead agent that owns
  it, the human-readable goal, and the lifecycle state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from ash.agents.tasks import AgentTaskStore
from ash.sqlite_utils import PinnedSQLiteDatabase, SQLitePathError


# --- public type aliases ---------------------------------------------------


AgentStatusValue = str  # one of {"idle", "working", "failed", "completed"}
SprintStateValue = str  # one of {"planning", "active", "complete", "aborted"}
MAX_IPC_MESSAGE_BYTES = 512 * 1024
MAX_PENDING_IPC_MESSAGES_PER_RECIPIENT = 1000
MAX_DELIVERED_IPC_HISTORY_PER_WORKSPACE = 10_000
MAX_IPC_FETCH_MESSAGES = 10_000
MAX_TERMINAL_AGENT_STATUS_PER_WORKSPACE = 2048
MAX_TERMINAL_SPRINT_HISTORY_PER_WORKSPACE = 2048


# --- row dataclasses -------------------------------------------------------


@dataclass(frozen=True)
class AgentStatus:
    agent_id: str
    role: str
    status: AgentStatusValue
    current_task: str
    last_heartbeat: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IPCMessage:
    message_id: int
    sender_id: str
    recipient_id: str
    message_type: str
    content: dict[str, Any]
    delivered: bool
    timestamp: datetime


@dataclass(frozen=True)
class SharedSprint:
    sprint_id: str
    lead_agent_id: str
    goal: str
    state: SprintStateValue
    created_at: datetime


# --- the connection wrapper ----------------------------------------------


class SharedState:
    """SQLite-backed coordination layer with WAL concurrency."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        workspace: Path | str | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.workspace = (
            str(Path(workspace).expanduser().resolve())
            if workspace is not None
            else None
        )
        self._agent_namespace_prefix = (
            "ws:"
            + hashlib.sha256(self.workspace.encode("utf-8")).hexdigest()
            + ":"
            if self.workspace is not None
            else ""
        )
        try:
            self._database = PinnedSQLiteDatabase.prepare(
                db_path,
                label="agent shared-state database",
            )
            self.db_path = str(self._database.path)
            # check_same_thread=False because the connection is used by the
            # orchestrator thread and any spawned subagent threads.
            self._conn = self._database.connect(
                label="agent shared-state database",
                check_same_thread=False,
                timeout=busy_timeout_ms / 1000,
            )
        except SQLitePathError as exc:
            raise ValueError(str(exc)) from exc
        self._conn.row_factory = sqlite3.Row
        self._conn_lock = threading.RLock()
        self._closed = False
        try:
            self._init_db()
            self.tasks = AgentTaskStore(
                self.db_path,
                busy_timeout_ms=busy_timeout_ms,
                _database=self._database,
            )
        except BaseException as primary_error:
            self._closed = True
            try:
                self._conn.close()
            except BaseException as cleanup_error:
                primary_error.add_note(
                    f"agent shared-state connection cleanup failed: {cleanup_error}"
                )
            raise

    def _scope_agent_id(self, agent_id: str) -> str:
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("agent id must be non-empty text")
        return f"{self._agent_namespace_prefix}{agent_id}" if self.workspace else agent_id

    def _unscope_agent_id(self, stored_agent_id: str) -> str:
        if self.workspace and stored_agent_id.startswith(self._agent_namespace_prefix):
            return stored_agent_id[len(self._agent_namespace_prefix) :]
        return stored_agent_id

    def _scoped_agent_like(self) -> str | None:
        return f"{self._agent_namespace_prefix}%" if self.workspace else None

    def _row_to_agent_status(self, row: sqlite3.Row) -> AgentStatus:
        value = _row_to_agent_status(row)
        return AgentStatus(
            agent_id=self._unscope_agent_id(value.agent_id),
            role=value.role,
            status=value.status,
            current_task=value.current_task,
            last_heartbeat=value.last_heartbeat,
            metadata=value.metadata,
        )

    def _row_to_ipc(self, row: sqlite3.Row) -> IPCMessage:
        value = _row_to_ipc(row)
        return IPCMessage(
            message_id=value.message_id,
            sender_id=self._unscope_agent_id(value.sender_id),
            recipient_id=self._unscope_agent_id(value.recipient_id),
            message_type=value.message_type,
            content=value.content,
            delivered=value.delivered,
            timestamp=value.timestamp,
        )

    def _row_to_sprint(self, row: sqlite3.Row) -> SharedSprint:
        return SharedSprint(
            sprint_id=str(row["sprint_id"]),
            lead_agent_id=self._unscope_agent_id(str(row["lead_agent_id"])),
            goal=str(row["sprint_goal"]),
            state=str(row["state"]),
            created_at=_parse_iso(row["created_at"])
            or datetime.now(timezone.utc),
        )

    def _encode_ipc_content(self, content: dict[str, Any]) -> str:
        if not isinstance(content, dict):
            raise ValueError("IPC content must be an object")
        try:
            payload = json.dumps(
                content,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("IPC content must contain valid JSON values") from exc
        if len(payload.encode("utf-8")) > MAX_IPC_MESSAGE_BYTES:
            raise ValueError(
                f"IPC content exceeds {MAX_IPC_MESSAGE_BYTES} bytes"
            )
        return payload

    def _prune_delivered_ipc_locked(self) -> None:
        scoped = self._scoped_agent_like()
        if scoped is None:
            return
        self._conn.execute(
            """
            DELETE FROM ipc_messages
            WHERE message_id IN (
                SELECT message_id
                FROM ipc_messages
                WHERE delivered = 1
                  AND (sender_id LIKE ? OR recipient_id LIKE ?)
                ORDER BY message_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (scoped, scoped, MAX_DELIVERED_IPC_HISTORY_PER_WORKSPACE),
        )

    def _require_ipc_capacity_locked(self, scoped_recipient: str) -> None:
        pending = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM ipc_messages "
                "WHERE recipient_id = ? AND delivered = 0",
                (scoped_recipient,),
            ).fetchone()[0]
        )
        if pending >= MAX_PENDING_IPC_MESSAGES_PER_RECIPIENT:
            raise ValueError(
                "IPC recipient pending-message limit reached: maximum "
                f"{MAX_PENDING_IPC_MESSAGES_PER_RECIPIENT}"
            )

    def _prune_terminal_agent_status_locked(self) -> None:
        scoped = self._scoped_agent_like()
        if scoped is None:
            return
        self._conn.execute(
            """
            DELETE FROM agent_status
            WHERE agent_id IN (
                SELECT agent_id
                FROM agent_status
                WHERE agent_id LIKE ? AND status IN ('completed','failed')
                ORDER BY last_heartbeat DESC, agent_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (scoped, MAX_TERMINAL_AGENT_STATUS_PER_WORKSPACE),
        )

    def _prune_terminal_sprints_locked(self) -> None:
        scoped = self._scoped_agent_like()
        if scoped is None:
            return
        self._conn.execute(
            """
            DELETE FROM sprints
            WHERE sprint_id IN (
                SELECT sprint_id
                FROM sprints
                WHERE lead_agent_id LIKE ? AND state IN ('complete','aborted')
                ORDER BY created_at DESC, sprint_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (scoped, MAX_TERMINAL_SPRINT_HISTORY_PER_WORKSPACE),
        )

    # --- lifecycle -------------------------------------------------------

    def close(self) -> None:
        with self._conn_lock:
            if self._closed:
                return
            primary_error: BaseException | None = None
            try:
                self.tasks.close()
            except BaseException as exc:
                primary_error = exc
            try:
                self._conn.close()
            except BaseException as exc:
                if primary_error is None:
                    primary_error = exc
                else:
                    primary_error.add_note(
                        f"agent shared-state connection cleanup also failed: {exc}"
                    )
            if primary_error is not None:
                raise primary_error
            self._closed = True

    def __enter__(self) -> "SharedState":
        return self

    def __exit__(
        self,
        exc_type: Any,
        exc: BaseException | None,
        tb: Any,
    ) -> Literal[False]:
        del exc_type, tb
        try:
            self.close()
        except BaseException as cleanup_error:
            if exc is None:
                raise
            exc.add_note(f"agent shared-state cleanup failed: {cleanup_error}")
        return False

    def _init_db(self) -> None:
        with self._conn_lock, self._conn:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                PRAGMA foreign_keys=ON;
                PRAGMA busy_timeout=5000;

                CREATE TABLE IF NOT EXISTS agent_status (
                    agent_id TEXT PRIMARY KEY,
                    role TEXT NOT NULL DEFAULT 'general',
                    status TEXT CHECK(status IN ('idle','working','failed','completed')) NOT NULL,
                    current_task TEXT NOT NULL DEFAULT '',
                    last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS ipc_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id TEXT NOT NULL,
                    recipient_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    delivered INTEGER CHECK(delivered IN (0, 1)) DEFAULT 0,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS sprints (
                    sprint_id TEXT PRIMARY KEY,
                    lead_agent_id TEXT NOT NULL,
                    sprint_goal TEXT NOT NULL,
                    state TEXT CHECK(state IN ('planning','active','complete','aborted')) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_ipc_recipient
                    ON ipc_messages(recipient_id, delivered);
                CREATE INDEX IF NOT EXISTS idx_ipc_timestamp
                    ON ipc_messages(timestamp);
                """
            )

    # --- agent lifecycle ------------------------------------------------

    def register_agent(
        self,
        agent_id: str,
        role: str = "general",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register an agent in the ``agent_status`` table (idempotent)."""

        scoped_agent_id = self._scope_agent_id(agent_id)
        stored_metadata = dict(metadata or {})
        if self.workspace is not None:
            stored_metadata["workspace"] = self.workspace
        meta_json = json.dumps(stored_metadata)
        with self._conn_lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO agent_status (agent_id, role, status, current_task, metadata_json)
                VALUES (?, ?, 'idle', '', ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    role = excluded.role,
                    metadata_json = excluded.metadata_json,
                    last_heartbeat = CURRENT_TIMESTAMP
                """,
                (scoped_agent_id, role, meta_json),
            )

    def update_status(
        self,
        agent_id: str,
        status: AgentStatusValue,
        current_task: str = "",
    ) -> None:
        if status not in {"idle", "working", "failed", "completed"}:
            raise ValueError(f"Invalid status: {status!r}")
        scoped_agent_id = self._scope_agent_id(agent_id)
        with self._conn_lock, self._conn:
            self._conn.execute(
                """
                UPDATE agent_status
                SET status = ?, current_task = ?, last_heartbeat = CURRENT_TIMESTAMP
                WHERE agent_id = ?
                """,
                (status, current_task, scoped_agent_id),
            )
            if status in {"completed", "failed"}:
                self._prune_terminal_agent_status_locked()

    async def update_status_async(
        self,
        agent_id: str,
        status: AgentStatusValue,
        current_task: str = "",
    ) -> None:
        """Async wrapper around the serialized connection owner."""
        await asyncio.to_thread(self.update_status, agent_id, status, current_task)

    def heartbeat(self, agent_id: str) -> None:
        """Touch the last_heartbeat timestamp for an agent."""

        scoped_agent_id = self._scope_agent_id(agent_id)
        with self._conn_lock, self._conn:
            self._conn.execute(
                "UPDATE agent_status SET last_heartbeat = CURRENT_TIMESTAMP WHERE agent_id = ?",
                (scoped_agent_id,),
            )

    async def register_agent_async(
        self,
        agent_id: str,
        role: str = "general",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Async wrapper around the serialized connection owner."""
        await asyncio.to_thread(self.register_agent, agent_id, role, metadata)

    def get_status(self, agent_id: str) -> AgentStatus | None:
        scoped_agent_id = self._scope_agent_id(agent_id)
        with self._conn_lock, closing(self._conn.cursor()) as cur:
            row = cur.execute(
                "SELECT * FROM agent_status WHERE agent_id = ?", (scoped_agent_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_agent_status(row)

    def list_agents(self) -> list[AgentStatus]:
        with self._conn_lock, closing(self._conn.cursor()) as cur:
            scoped = self._scoped_agent_like()
            if scoped is None:
                rows = cur.execute(
                    "SELECT * FROM agent_status ORDER BY last_heartbeat DESC"
                ).fetchall()
            else:
                rows = cur.execute(
                    "SELECT * FROM agent_status WHERE agent_id LIKE ? "
                    "ORDER BY last_heartbeat DESC",
                    (scoped,),
                ).fetchall()
        return [self._row_to_agent_status(r) for r in rows]

    def reap_stale_agents(self, max_age_seconds: float) -> list[str]:
        """Mark agents whose last heartbeat is older than the cutoff as failed.

        Returns the list of agent ids that were reaped. Useful for
        the orchestrator's watchdog loop.
        """

        cutoff = time.time() - max_age_seconds
        reaped: list[str] = []
        with self._conn_lock, self._conn:
            scoped = self._scoped_agent_like()
            if scoped is None:
                rows = self._conn.execute(
                    """
                    SELECT agent_id, last_heartbeat FROM agent_status
                    WHERE status IN ('idle', 'working')
                    """,
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT agent_id, last_heartbeat FROM agent_status
                    WHERE status IN ('idle', 'working') AND agent_id LIKE ?
                    """,
                    (scoped,),
                ).fetchall()
            for row in rows:
                last = _parse_iso(row["last_heartbeat"])
                if last is None:
                    continue
                if last.timestamp() < cutoff:
                    self._conn.execute(
                        "UPDATE agent_status SET status = 'failed' WHERE agent_id = ?",
                        (row["agent_id"],),
                    )
                    reaped.append(self._unscope_agent_id(str(row["agent_id"])))
            if reaped:
                self._prune_terminal_agent_status_locked()
        return reaped

    # --- IPC ------------------------------------------------------------

    def send_message(
        self,
        sender_id: str,
        recipient_id: str,
        message_type: str,
        content: dict[str, Any],
    ) -> int:
        """Enqueue a JSON-RPC-shaped message. Returns the new message id."""

        payload = self._encode_ipc_content(content)
        scoped_sender = self._scope_agent_id(sender_id)
        scoped_recipient = self._scope_agent_id(recipient_id)
        with self._conn_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._require_ipc_capacity_locked(scoped_recipient)
                cur = self._conn.execute(
                    """
                    INSERT INTO ipc_messages
                        (sender_id, recipient_id, message_type, content_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (scoped_sender, scoped_recipient, message_type, payload),
                )
                self._prune_delivered_ipc_locked()
                message_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
                self._conn.commit()
                return message_id
            except BaseException:
                self._conn.rollback()
                raise

    async def send_message_async(
        self,
        sender_id: str,
        recipient_id: str,
        message_type: str,
        content: dict[str, Any],
    ) -> int:
        """Async wrapper around the serialized connection owner."""
        return await asyncio.to_thread(
            self.send_message,
            sender_id,
            recipient_id,
            message_type,
            content,
        )

    def fetch_messages(
        self,
        recipient_id: str,
        *,
        undelivered_only: bool = True,
        limit: int = 100,
        message_type: str | None = None,
    ) -> list[IPCMessage]:
        """Return messages addressed to ``recipient_id``, oldest first."""

        if type(limit) is not int or not 1 <= limit <= MAX_IPC_FETCH_MESSAGES:
            raise ValueError(
                "IPC message fetch limit must be between 1 and "
                f"{MAX_IPC_FETCH_MESSAGES}"
            )

        clauses = ["recipient_id = ?"]
        params: list[Any] = [self._scope_agent_id(recipient_id)]
        if undelivered_only:
            clauses.append("delivered = 0")
        if message_type is not None:
            clauses.append("message_type = ?")
            params.append(message_type)
        params.append(limit)
        sql = (
            "SELECT * FROM ipc_messages WHERE "
            + " AND ".join(clauses)
            + " ORDER BY timestamp ASC, message_id ASC LIMIT ?"
        )
        with self._conn_lock, closing(self._conn.cursor()) as cur:
            rows = cur.execute(sql, params).fetchall()
        return [self._row_to_ipc(r) for r in rows]

    def mark_delivered(self, message_ids: Iterable[int]) -> int:
        """Mark messages as delivered. Returns the row count affected."""

        ids = list(message_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._conn_lock, self._conn:
            scoped = self._scoped_agent_like()
            if scoped is None:
                cur = self._conn.execute(
                    f"UPDATE ipc_messages SET delivered = 1 WHERE message_id IN ({placeholders})",
                    ids,
                )
            else:
                cur = self._conn.execute(
                    f"""
                    UPDATE ipc_messages SET delivered = 1
                    WHERE message_id IN ({placeholders})
                      AND (sender_id LIKE ? OR recipient_id LIKE ?)
                    """,
                    (*ids, scoped, scoped),
                )
                self._prune_delivered_ipc_locked()
            return int(cur.rowcount)

    def resolve_approval_request(
        self,
        request_message_id: int,
        *,
        approved: bool,
        feedback: str = "",
        resolver_id: str = "lead",
    ) -> dict[str, Any]:
        """Atomically resolve one pending background-agent approval request."""

        if type(request_message_id) is not int or request_message_id < 1:
            raise ValueError("approval request id must be a positive integer")
        if type(approved) is not bool:
            raise ValueError("approved must be a boolean")
        if not isinstance(feedback, str):
            raise ValueError("approval feedback must be text")
        feedback = feedback.strip()[:500]
        if not resolver_id.strip():
            raise ValueError("resolver id must not be empty")

        with self._conn_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                scoped_lead = self._scope_agent_id("lead")
                row = self._conn.execute(
                    "SELECT * FROM ipc_messages "
                    "WHERE message_id = ? AND recipient_id = ?",
                    (request_message_id, scoped_lead),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        f"unknown approval request {request_message_id}"
                    )
                request = self._row_to_ipc(row)
                if request.recipient_id != "lead" or request.message_type != "approval_request":
                    raise ValueError(
                        f"message {request_message_id} is not an approval request"
                    )
                if request.delivered:
                    raise ValueError(
                        f"approval request {request_message_id} is already resolved"
                    )
                content = request.content
                task_id = content.get("task_id")
                attempt = content.get("attempt")
                agent_id = content.get("agent_id")
                tool_name = content.get("tool_name")
                arguments_sha256 = content.get("arguments_sha256")
                if (
                    not isinstance(task_id, str)
                    or type(attempt) is not int
                    or attempt < 1
                    or not isinstance(agent_id, str)
                    or agent_id != request.sender_id
                    or not isinstance(tool_name, str)
                    or not isinstance(arguments_sha256, str)
                    or len(arguments_sha256) != 64
                ):
                    raise ValueError(
                        f"approval request {request_message_id} is malformed"
                    )
                if self.workspace is None:
                    task_row = self._conn.execute(
                        """
                        SELECT state, owner_agent_id, attempt
                        FROM agent_tasks
                        WHERE task_id = ?
                        """,
                        (task_id,),
                    ).fetchone()
                else:
                    task_row = self._conn.execute(
                        """
                        SELECT state, owner_agent_id, attempt
                        FROM agent_tasks
                        WHERE task_id = ?
                          AND json_extract(metadata_json, '$.workspace') = ?
                        """,
                        (task_id, self.workspace),
                    ).fetchone()
                if (
                    task_row is None
                    or task_row["state"] not in {"leased", "running"}
                    or task_row["owner_agent_id"] != agent_id
                    or int(task_row["attempt"]) != attempt
                ):
                    raise ValueError(
                        f"approval request {request_message_id} is stale"
                    )
                updated = self._conn.execute(
                    """
                    UPDATE ipc_messages
                    SET delivered = 1
                    WHERE message_id = ? AND delivered = 0
                    """,
                    (request_message_id,),
                )
                if int(updated.rowcount) != 1:
                    raise ValueError(
                        f"approval request {request_message_id} is already resolved"
                    )
                response_content = {
                    "request_message_id": request_message_id,
                    "task_id": task_id,
                    "attempt": attempt,
                    "agent_id": agent_id,
                    "tool_name": tool_name,
                    "arguments_sha256": arguments_sha256,
                    "approved": approved,
                    "feedback": feedback,
                }
                response_payload = self._encode_ipc_content(response_content)
                cur = self._conn.execute(
                    """
                    INSERT INTO ipc_messages
                        (sender_id, recipient_id, message_type, content_json)
                    VALUES (?, ?, 'approval_response', ?)
                    """,
                    (
                        self._scope_agent_id(resolver_id.strip()),
                        self._scope_agent_id(agent_id),
                        response_payload,
                    ),
                )
                response_message_id = int(cur.lastrowid or 0)
                self._prune_delivered_ipc_locked()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {
            "request_message_id": request_message_id,
            "response_message_id": response_message_id,
            **response_content,
        }

    def retire_stale_approval_requests(self, *, limit: int = 1000) -> list[int]:
        """Mark unresolved approval requests stale when their task attempt is no longer active."""

        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        retired: list[int] = []
        with self._conn_lock, self._conn:
            scoped_lead = self._scope_agent_id("lead")
            rows = self._conn.execute(
                """
                SELECT * FROM ipc_messages
                WHERE recipient_id = ?
                  AND message_type = 'approval_request'
                  AND delivered = 0
                ORDER BY message_id
                LIMIT ?
                """,
                (scoped_lead, limit),
            ).fetchall()
            for row in rows:
                message = self._row_to_ipc(row)
                content = message.content
                task_id = content.get("task_id")
                attempt = content.get("attempt")
                agent_id = content.get("agent_id")
                if not isinstance(task_id, str):
                    task_row = None
                elif self.workspace is None:
                    task_row = self._conn.execute(
                        """
                        SELECT state, owner_agent_id, attempt
                        FROM agent_tasks
                        WHERE task_id = ?
                        """,
                        (task_id,),
                    ).fetchone()
                else:
                    task_row = self._conn.execute(
                        """
                        SELECT state, owner_agent_id, attempt
                        FROM agent_tasks
                        WHERE task_id = ?
                          AND json_extract(metadata_json, '$.workspace') = ?
                        """,
                        (task_id, self.workspace),
                    ).fetchone()
                active = bool(
                    task_row is not None
                    and task_row["state"] in {"leased", "running"}
                    and task_row["owner_agent_id"] == agent_id
                    and type(attempt) is int
                    and int(task_row["attempt"]) == attempt
                )
                if active:
                    continue
                updated = self._conn.execute(
                    """
                    UPDATE ipc_messages
                    SET delivered = 1
                    WHERE message_id = ? AND delivered = 0
                    """,
                    (message.message_id,),
                )
                if int(updated.rowcount) == 1:
                    retired.append(message.message_id)
            if retired:
                self._prune_delivered_ipc_locked()
        return retired

    def send_to_agent(
        self,
        sender_id: str,
        recipient_id: str,
        message_type: str,
        content: Any,
    ) -> int:
        """Send a message directly from one agent to another."""
        return self.send_message(
            sender_id, recipient_id, message_type, {"content": content}
        )

    def broadcast(
        self,
        sender_id: str,
        message_type: str,
        content: dict[str, Any],
    ) -> int:
        """Send the same message to every registered agent. Returns the count sent."""

        recipients = [status.agent_id for status in self.list_agents()]
        count = 0
        for recipient in recipients:
            if recipient == sender_id:
                continue
            self.send_message(sender_id, recipient, message_type, content)
            count += 1
        return count

    # --- sprints --------------------------------------------------------

    def create_sprint(self, lead_agent_id: str, goal: str) -> str:
        """Create a sprint row owned by ``lead_agent_id`` and return its id."""

        sprint_id = str(uuid.uuid4())
        with self._conn_lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO sprints (sprint_id, lead_agent_id, sprint_goal, state)
                VALUES (?, ?, ?, 'planning')
                """,
                (sprint_id, self._scope_agent_id(lead_agent_id), goal),
            )
        return sprint_id

    async def create_sprint_async(self, lead_agent_id: str, goal: str) -> str:
        """Async wrapper around the serialized connection owner."""
        return await asyncio.to_thread(self.create_sprint, lead_agent_id, goal)

    def update_sprint_state(self, sprint_id: str, state: SprintStateValue) -> None:
        if state not in {"planning", "active", "complete", "aborted"}:
            raise ValueError(f"Invalid sprint state: {state!r}")
        with self._conn_lock, self._conn:
            scoped = self._scoped_agent_like()
            if scoped is None:
                self._conn.execute(
                    "UPDATE sprints SET state = ? WHERE sprint_id = ?",
                    (state, sprint_id),
                )
            else:
                self._conn.execute(
                    "UPDATE sprints SET state = ? "
                    "WHERE sprint_id = ? AND lead_agent_id LIKE ?",
                    (state, sprint_id, scoped),
                )
            if state in {"complete", "aborted"}:
                self._prune_terminal_sprints_locked()

    async def update_sprint_state_async(
        self, sprint_id: str, state: SprintStateValue
    ) -> None:
        """Async wrapper around the serialized connection owner."""
        await asyncio.to_thread(self.update_sprint_state, sprint_id, state)

    def get_sprint(self, sprint_id: str) -> SharedSprint | None:
        with self._conn_lock, closing(self._conn.cursor()) as cur:
            scoped = self._scoped_agent_like()
            if scoped is None:
                row = cur.execute(
                    "SELECT * FROM sprints WHERE sprint_id = ?", (sprint_id,)
                ).fetchone()
            else:
                row = cur.execute(
                    "SELECT * FROM sprints "
                    "WHERE sprint_id = ? AND lead_agent_id LIKE ?",
                    (sprint_id, scoped),
                ).fetchone()
        if row is None:
            return None
        return self._row_to_sprint(row)

    def list_sprints(self) -> list[SharedSprint]:
        with self._conn_lock, closing(self._conn.cursor()) as cur:
            scoped = self._scoped_agent_like()
            if scoped is None:
                rows = cur.execute(
                    "SELECT * FROM sprints ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = cur.execute(
                    "SELECT * FROM sprints WHERE lead_agent_id LIKE ? "
                    "ORDER BY created_at DESC",
                    (scoped,),
                ).fetchall()
        return [self._row_to_sprint(r) for r in rows]


# --- internal helpers -----------------------------------------------------


def _row_to_agent_status(row: sqlite3.Row) -> AgentStatus:
    meta = json.loads(row["metadata_json"] or "{}")
    return AgentStatus(
        agent_id=row["agent_id"],
        role=row["role"],
        status=row["status"],
        current_task=row["current_task"] or "",
        last_heartbeat=_parse_iso(row["last_heartbeat"]) or datetime.now(timezone.utc),
        metadata=meta,
    )


def _row_to_ipc(row: sqlite3.Row) -> IPCMessage:
    return IPCMessage(
        message_id=int(row["message_id"]),
        sender_id=row["sender_id"],
        recipient_id=row["recipient_id"],
        message_type=row["message_type"],
        content=json.loads(row["content_json"]),
        delivered=bool(row["delivered"]),
        timestamp=_parse_iso(row["timestamp"]) or datetime.now(timezone.utc),
    )


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value)
        return (
            parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        )
    except (TypeError, ValueError):
        return None
