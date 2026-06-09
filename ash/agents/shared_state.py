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

import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# --- public type aliases ---------------------------------------------------


AgentStatusValue = str  # one of {"idle", "working", "failed", "completed"}
SprintStateValue = str  # one of {"planning", "active", "complete", "aborted"}


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

    def __init__(self, db_path: Path | str, *, busy_timeout_ms: int = 5000) -> None:
        self.db_path = str(Path(db_path).expanduser().resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because the connection is used by the
        # orchestrator thread and any spawned subagent threads.
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=busy_timeout_ms / 1000
        )
        self._conn.row_factory = sqlite3.Row
        self._write_lock = threading.Lock()
        self._init_db()

    # --- lifecycle -------------------------------------------------------

    def close(self) -> None:
        with self._write_lock:
            self._conn.close()

    def _init_db(self) -> None:
        with self._write_lock, self._conn:
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

        meta_json = json.dumps(metadata or {})
        with self._write_lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO agent_status (agent_id, role, status, current_task, metadata_json)
                VALUES (?, ?, 'idle', '', ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    role = excluded.role,
                    metadata_json = excluded.metadata_json,
                    last_heartbeat = CURRENT_TIMESTAMP
                """,
                (agent_id, role, meta_json),
            )

    def update_status(
        self,
        agent_id: str,
        status: AgentStatusValue,
        current_task: str = "",
    ) -> None:
        if status not in {"idle", "working", "failed", "completed"}:
            raise ValueError(f"Invalid status: {status!r}")
        with self._write_lock, self._conn:
            self._conn.execute(
                """
                UPDATE agent_status
                SET status = ?, current_task = ?, last_heartbeat = CURRENT_TIMESTAMP
                WHERE agent_id = ?
                """,
                (status, current_task, agent_id),
            )

    def heartbeat(self, agent_id: str) -> None:
        """Touch the last_heartbeat timestamp for an agent."""

        with self._write_lock, self._conn:
            self._conn.execute(
                "UPDATE agent_status SET last_heartbeat = CURRENT_TIMESTAMP WHERE agent_id = ?",
                (agent_id,),
            )

    def get_status(self, agent_id: str) -> AgentStatus | None:
        with closing(self._conn.cursor()) as cur:
            row = cur.execute(
                "SELECT * FROM agent_status WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        if row is None:
            return None
        return _row_to_agent_status(row)

    def list_agents(self) -> list[AgentStatus]:
        with closing(self._conn.cursor()) as cur:
            rows = cur.execute(
                "SELECT * FROM agent_status ORDER BY last_heartbeat DESC"
            ).fetchall()
        return [_row_to_agent_status(r) for r in rows]

    def reap_stale_agents(self, max_age_seconds: float) -> list[str]:
        """Mark agents whose last heartbeat is older than the cutoff as failed.

        Returns the list of agent ids that were reaped. Useful for
        the orchestrator's watchdog loop.
        """

        cutoff = time.time() - max_age_seconds
        reaped: list[str] = []
        with self._write_lock, self._conn:
            rows = self._conn.execute(
                """
                SELECT agent_id, last_heartbeat FROM agent_status
                WHERE status IN ('idle', 'working')
                """,
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
                    reaped.append(row["agent_id"])
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

        payload = json.dumps(content, ensure_ascii=False)
        with self._write_lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO ipc_messages
                    (sender_id, recipient_id, message_type, content_json)
                VALUES (?, ?, ?, ?)
                """,
                (sender_id, recipient_id, message_type, payload),
            )
            return int(cur.lastrowid) if cur.lastrowid is not None else 0

    def fetch_messages(
        self,
        recipient_id: str,
        *,
        undelivered_only: bool = True,
        limit: int = 100,
    ) -> list[IPCMessage]:
        """Return messages addressed to ``recipient_id``, oldest first."""

        sql = (
            "SELECT * FROM ipc_messages WHERE recipient_id = ?"
            + (" AND delivered = 0" if undelivered_only else "")
            + " ORDER BY timestamp ASC, message_id ASC LIMIT ?"
        )
        with closing(self._conn.cursor()) as cur:
            rows = cur.execute(sql, (recipient_id, limit)).fetchall()
        return [_row_to_ipc(r) for r in rows]

    def mark_delivered(self, message_ids: Iterable[int]) -> int:
        """Mark messages as delivered. Returns the row count affected."""

        ids = list(message_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._write_lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE ipc_messages SET delivered = 1 WHERE message_id IN ({placeholders})",
                ids,
            )
            return int(cur.rowcount)

    def broadcast(
        self,
        sender_id: str,
        message_type: str,
        content: dict[str, Any],
    ) -> int:
        """Send the same message to every registered agent. Returns the count sent."""

        recipients = [
            row["agent_id"]
            for row in self._conn.execute(
                "SELECT agent_id FROM agent_status"
            ).fetchall()
        ]
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
        with self._write_lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO sprints (sprint_id, lead_agent_id, sprint_goal, state)
                VALUES (?, ?, ?, 'planning')
                """,
                (sprint_id, lead_agent_id, goal),
            )
        return sprint_id

    def update_sprint_state(self, sprint_id: str, state: SprintStateValue) -> None:
        if state not in {"planning", "active", "complete", "aborted"}:
            raise ValueError(f"Invalid sprint state: {state!r}")
        with self._write_lock, self._conn:
            self._conn.execute(
                "UPDATE sprints SET state = ? WHERE sprint_id = ?",
                (state, sprint_id),
            )

    def get_sprint(self, sprint_id: str) -> SharedSprint | None:
        with closing(self._conn.cursor()) as cur:
            row = cur.execute(
                "SELECT * FROM sprints WHERE sprint_id = ?", (sprint_id,)
            ).fetchone()
        if row is None:
            return None
        return SharedSprint(
            sprint_id=row["sprint_id"],
            lead_agent_id=row["lead_agent_id"],
            goal=row["sprint_goal"],
            state=row["state"],
            created_at=_parse_iso(row["created_at"]) or datetime.now(timezone.utc),
        )

    def list_sprints(self) -> list[SharedSprint]:
        with closing(self._conn.cursor()) as cur:
            rows = cur.execute(
                "SELECT * FROM sprints ORDER BY created_at DESC"
            ).fetchall()
        return [
            SharedSprint(
                sprint_id=r["sprint_id"],
                lead_agent_id=r["lead_agent_id"],
                goal=r["sprint_goal"],
                state=r["state"],
                created_at=_parse_iso(r["created_at"]) or datetime.now(timezone.utc),
            )
            for r in rows
        ]


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
        return value
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
