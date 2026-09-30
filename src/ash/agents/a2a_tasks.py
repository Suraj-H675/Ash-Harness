"""Durable workspace-scoped handles for outbound A2A tasks."""

from __future__ import annotations

import builtins
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from ash.core.session import normalize_project_path
from ash.sqlite_utils import (
    PinnedSQLiteDatabase,
    SQLitePathError,
    configure_sqlite_journal_mode,
)


REMOTE_TASK_SCHEMA_VERSION = 2
MAX_NONTERMINAL_REMOTE_TASKS_PER_WORKSPACE = 1024
MAX_TERMINAL_REMOTE_TASK_HISTORY_PER_WORKSPACE = 10_000
TERMINAL_REMOTE_TASK_STATES = frozenset(
    {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
        "MESSAGE",
    }
)


@dataclass(frozen=True)
class RemoteTaskHandle:
    agent: str
    endpoint: str
    task_id: str
    context_id: str
    state: str
    updated_at: str


@dataclass(frozen=True)
class RemoteTaskIntent:
    agent: str
    endpoint: str
    context_id: str
    updated_at: str


class RemoteTaskStore:
    """Persist remote task identities without prompts, output, or credentials."""

    def __init__(self, db_path: Path, workspace: Path) -> None:
        try:
            self._database = PinnedSQLiteDatabase.prepare(
                db_path,
                label="A2A remote task database",
            )
        except SQLitePathError as exc:
            raise ValueError(str(exc)) from exc
        self.db_path = self._database.path
        self.workspace = normalize_project_path(workspace)
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._conn = self._database.connect(
                label="A2A remote task database",
                check_same_thread=False,
            )
        except (SQLitePathError, sqlite3.Error) as exc:
            raise ValueError(f"cannot open A2A remote task database: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        try:
            with self._lock, self._conn:
                schema_version = int(
                    self._conn.execute("PRAGMA user_version").fetchone()[0]
                )
                if schema_version > REMOTE_TASK_SCHEMA_VERSION:
                    raise RuntimeError(
                        "A2A remote task database schema "
                        f"v{schema_version} is newer than supported "
                        f"v{REMOTE_TASK_SCHEMA_VERSION}"
                    )
                configure_sqlite_journal_mode(self._conn)
                self._conn.executescript(
                    """
                    PRAGMA synchronous=FULL;
                    PRAGMA busy_timeout=5000;

                    CREATE TABLE IF NOT EXISTS remote_agent_tasks (
                        workspace TEXT NOT NULL,
                        agent TEXT NOT NULL,
                        endpoint TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        context_id TEXT NOT NULL,
                        state TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (workspace, agent, endpoint, task_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_remote_agent_tasks_workspace
                        ON remote_agent_tasks(workspace, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_remote_agent_tasks_workspace_state
                        ON remote_agent_tasks(workspace, state, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS remote_agent_task_intents (
                        workspace TEXT NOT NULL,
                        agent TEXT NOT NULL,
                        endpoint TEXT NOT NULL,
                        context_id TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (workspace, agent, endpoint, context_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_remote_agent_task_intents_workspace
                        ON remote_agent_task_intents(workspace, updated_at DESC);
                    """
                )
                self._conn.execute(
                    f"PRAGMA user_version = {REMOTE_TASK_SCHEMA_VERSION}"
                )
        except BaseException:
            self._conn.close()
            self._closed = True
            raise
        try:
            self._restrict_file_permissions()
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def save(
        self,
        *,
        agent: str,
        endpoint: str,
        task_id: str,
        context_id: str,
        state: str,
    ) -> RemoteTaskHandle:
        self._require_open()
        values = self._validated_values(
            agent=agent,
            endpoint=endpoint,
            task_id=task_id,
            context_id=context_id,
            state=state,
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    """
                    SELECT state FROM remote_agent_tasks
                    WHERE workspace = ? AND agent = ? AND endpoint = ? AND task_id = ?
                    """,
                    (self.workspace, values[0], values[1], values[2]),
                ).fetchone()
                matching_intent = (
                    self._conn.execute(
                        """
                        SELECT 1 FROM remote_agent_task_intents
                        WHERE workspace = ? AND agent = ? AND endpoint = ?
                            AND context_id = ?
                        LIMIT 1
                        """,
                        (self.workspace, values[0], values[1], values[3]),
                    ).fetchone()
                    if values[3]
                    else None
                )
                existing_counts_live = bool(
                    existing is not None
                    and str(existing["state"]) not in TERMINAL_REMOTE_TASK_STATES
                )
                if (
                    values[4] not in TERMINAL_REMOTE_TASK_STATES
                    and not existing_counts_live
                    and matching_intent is None
                ):
                    self._require_nonterminal_capacity_locked()

                self._conn.execute(
                """
                INSERT INTO remote_agent_tasks
                    (workspace, agent, endpoint, task_id, context_id, state)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace, agent, endpoint, task_id) DO UPDATE SET
                    context_id = excluded.context_id,
                    state = excluded.state,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (self.workspace, *values),
                )
                row = self._conn.execute(
                """
                SELECT agent, endpoint, task_id, context_id, state, updated_at
                FROM remote_agent_tasks
                WHERE workspace = ? AND agent = ? AND endpoint = ? AND task_id = ?
                """,
                (self.workspace, values[0], values[1], values[2]),
                ).fetchone()
                if values[3]:
                    self._conn.execute(
                    """
                    DELETE FROM remote_agent_task_intents
                    WHERE workspace = ? AND agent = ? AND endpoint = ?
                        AND context_id = ?
                    """,
                    (self.workspace, values[0], values[1], values[3]),
                    )
                if values[4] in TERMINAL_REMOTE_TASK_STATES:
                    self._prune_terminal_history_locked()
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        assert row is not None
        self._restrict_file_permissions()
        return self._row(row)

    def list(
        self,
        *,
        agent: str | None = None,
        limit: int = 100,
    ) -> list[RemoteTaskHandle]:
        self._require_open()
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("remote task list limit must be between 1 and 500")
        params: tuple[str | int, ...]
        query = (
            "SELECT agent, endpoint, task_id, context_id, state, updated_at "
            "FROM remote_agent_tasks WHERE workspace = ?"
        )
        if agent is None:
            params = (self.workspace,)
        else:
            normalized_agent = self._bounded(agent, "remote agent name", 64)
            query += " AND agent = ?"
            params = (self.workspace, normalized_agent)
        query += " ORDER BY updated_at DESC, agent, task_id LIMIT ?"
        params = (*params, limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row(row) for row in rows]

    def save_intent(
        self,
        *,
        agent: str,
        endpoint: str,
        context_id: str,
    ) -> RemoteTaskIntent:
        self._require_open()
        normalized_agent = self._bounded(agent, "remote agent name", 64)
        normalized_endpoint = self._bounded(endpoint, "remote agent endpoint", 4096)
        normalized_context = self._bounded(context_id, "remote context ID", 512)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    """
                    SELECT 1 FROM remote_agent_task_intents
                    WHERE workspace = ? AND agent = ? AND endpoint = ?
                        AND context_id = ?
                    LIMIT 1
                    """,
                    (
                        self.workspace,
                        normalized_agent,
                        normalized_endpoint,
                        normalized_context,
                    ),
                ).fetchone()
                if existing is None:
                    self._require_nonterminal_capacity_locked()
                self._conn.execute(
                """
                INSERT INTO remote_agent_task_intents
                    (workspace, agent, endpoint, context_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace, agent, endpoint, context_id) DO UPDATE SET
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    self.workspace,
                    normalized_agent,
                    normalized_endpoint,
                    normalized_context,
                ),
                )
                row = self._conn.execute(
                """
                SELECT agent, endpoint, context_id, updated_at
                FROM remote_agent_task_intents
                WHERE workspace = ? AND agent = ? AND endpoint = ?
                    AND context_id = ?
                """,
                (
                    self.workspace,
                    normalized_agent,
                    normalized_endpoint,
                    normalized_context,
                ),
                ).fetchone()
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        assert row is not None
        self._restrict_file_permissions()
        return self._intent_row(row)

    def list_intents(
        self,
        *,
        agent: str | None = None,
        limit: int = 100,
    ) -> builtins.list[RemoteTaskIntent]:
        self._require_open()
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("remote task intent list limit must be between 1 and 500")
        params: tuple[str | int, ...]
        query = (
            "SELECT agent, endpoint, context_id, updated_at "
            "FROM remote_agent_task_intents WHERE workspace = ?"
        )
        if agent is None:
            params = (self.workspace,)
        else:
            normalized_agent = self._bounded(agent, "remote agent name", 64)
            query += " AND agent = ?"
            params = (self.workspace, normalized_agent)
        query += " ORDER BY updated_at DESC, agent, context_id LIMIT ?"
        params = (*params, limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._intent_row(row) for row in rows]

    def get_intent_endpoint(self, *, agent: str, context_id: str) -> str | None:
        self._require_open()
        normalized_agent = self._bounded(agent, "remote agent name", 64)
        normalized_context = self._bounded(context_id, "remote context ID", 512)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT endpoint FROM remote_agent_task_intents
                WHERE workspace = ? AND agent = ? AND context_id = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (self.workspace, normalized_agent, normalized_context),
            ).fetchone()
        if row is None:
            return None
        return self._bounded(
            str(row["endpoint"]), "stored remote agent endpoint", 4096
        )

    def delete_intent(self, *, agent: str, endpoint: str, context_id: str) -> None:
        self._require_open()
        normalized_agent = self._bounded(agent, "remote agent name", 64)
        normalized_endpoint = self._bounded(endpoint, "remote agent endpoint", 4096)
        normalized_context = self._bounded(context_id, "remote context ID", 512)
        with self._lock, self._conn:
            self._conn.execute(
                """
                DELETE FROM remote_agent_task_intents
                WHERE workspace = ? AND agent = ? AND endpoint = ? AND context_id = ?
                """,
                (
                    self.workspace,
                    normalized_agent,
                    normalized_endpoint,
                    normalized_context,
                ),
            )

    def ensure_nonterminal_capacity(self) -> None:
        """Fail before network dispatch when another remote task cannot be tracked."""

        self._require_open()
        with self._lock:
            self._require_nonterminal_capacity_locked()

    def conflicting_endpoint(
        self,
        *,
        agent: str,
        endpoint: str,
        task_id: str,
    ) -> str | None:
        """Return a stored endpoint if this agent/task handle is bound elsewhere."""

        self._require_open()
        normalized_agent = self._bounded(agent, "remote agent name", 64)
        normalized_endpoint = self._bounded(endpoint, "remote agent endpoint", 4096)
        normalized_task = self._bounded(task_id, "remote task ID", 512)
        with self._lock:
            exact = self._conn.execute(
                """
                SELECT 1 FROM remote_agent_tasks
                WHERE workspace = ? AND agent = ? AND task_id = ? AND endpoint = ?
                LIMIT 1
                """,
                (
                    self.workspace,
                    normalized_agent,
                    normalized_task,
                    normalized_endpoint,
                ),
            ).fetchone()
            if exact is not None:
                return None
            row = self._conn.execute(
                """
                SELECT endpoint FROM remote_agent_tasks
                WHERE workspace = ? AND agent = ? AND task_id = ? AND endpoint != ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (
                    self.workspace,
                    normalized_agent,
                    normalized_task,
                    normalized_endpoint,
                ),
            ).fetchone()
        if row is None:
            return None
        return self._bounded(
            str(row["endpoint"]), "stored remote agent endpoint", 4096
        )

    def _row(self, row: sqlite3.Row) -> RemoteTaskHandle:
        return RemoteTaskHandle(
            agent=self._bounded(str(row["agent"]), "stored remote agent name", 64),
            endpoint=self._bounded(
                str(row["endpoint"]), "stored remote agent endpoint", 4096
            ),
            task_id=self._bounded(str(row["task_id"]), "stored remote task ID", 512),
            context_id=self._bounded(
                str(row["context_id"]), "stored remote context ID", 512, allow_empty=True
            ),
            state=self._bounded(
                str(row["state"]), "stored remote task state", 128, allow_empty=True
            ),
            updated_at=self._bounded(
                str(row["updated_at"]), "stored remote task timestamp", 128
            ),
        )

    def _require_nonterminal_capacity_locked(self) -> None:
        terminal_placeholders = ",".join("?" for _ in TERMINAL_REMOTE_TASK_STATES)
        active = int(
            self._conn.execute(
                f"""
                SELECT COUNT(*) FROM remote_agent_tasks
                WHERE workspace = ? AND state NOT IN ({terminal_placeholders})
                """,
                (self.workspace, *sorted(TERMINAL_REMOTE_TASK_STATES)),
            ).fetchone()[0]
        )
        intents = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM remote_agent_task_intents WHERE workspace = ?",
                (self.workspace,),
            ).fetchone()[0]
        )
        if active + intents >= MAX_NONTERMINAL_REMOTE_TASKS_PER_WORKSPACE:
            raise ValueError(
                "A2A remote nonterminal task tracking limit reached for workspace: "
                f"maximum {MAX_NONTERMINAL_REMOTE_TASKS_PER_WORKSPACE}"
            )

    def _prune_terminal_history_locked(self) -> None:
        terminal_placeholders = ",".join("?" for _ in TERMINAL_REMOTE_TASK_STATES)
        self._conn.execute(
            f"""
            DELETE FROM remote_agent_tasks
            WHERE rowid IN (
                SELECT rowid FROM remote_agent_tasks
                WHERE workspace = ? AND state IN ({terminal_placeholders})
                ORDER BY updated_at DESC, rowid DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (
                self.workspace,
                *sorted(TERMINAL_REMOTE_TASK_STATES),
                MAX_TERMINAL_REMOTE_TASK_HISTORY_PER_WORKSPACE,
            ),
        )

    def _intent_row(self, row: sqlite3.Row) -> RemoteTaskIntent:
        return RemoteTaskIntent(
            agent=self._bounded(str(row["agent"]), "stored remote agent name", 64),
            endpoint=self._bounded(
                str(row["endpoint"]), "stored remote agent endpoint", 4096
            ),
            context_id=self._bounded(
                str(row["context_id"]), "stored remote context ID", 512
            ),
            updated_at=self._bounded(
                str(row["updated_at"]), "stored remote task timestamp", 128
            ),
        )

    @classmethod
    def _validated_values(
        cls,
        *,
        agent: str,
        endpoint: str,
        task_id: str,
        context_id: str,
        state: str,
    ) -> tuple[str, str, str, str, str]:
        return (
            cls._bounded(agent, "remote agent name", 64),
            cls._bounded(endpoint, "remote agent endpoint", 4096),
            cls._bounded(task_id, "remote task ID", 512),
            cls._bounded(context_id, "remote context ID", 512, allow_empty=True),
            cls._bounded(state, "remote task state", 128, allow_empty=True),
        )

    @staticmethod
    def _bounded(
        value: str,
        label: str,
        max_bytes: int,
        *,
        allow_empty: bool = False,
    ) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{label} must be text")
        if (not value and not allow_empty) or len(value.encode("utf-8")) > max_bytes:
            qualifier = "may be empty and " if allow_empty else "must be non-empty and "
            raise ValueError(f"{label} {qualifier}at most {max_bytes} bytes")
        return value

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("A2A remote task store is closed")

    def _restrict_file_permissions(self) -> None:
        try:
            self._database.restrict_permissions(label="A2A remote task database")
        except SQLitePathError as exc:
            raise ValueError(str(exc)) from exc
