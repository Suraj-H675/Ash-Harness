"""Durable workspace-scoped handles for outbound A2A tasks."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ash.core.session import normalize_project_path
from ash.safe_io import validate_unlinked_file_path


REMOTE_TASK_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RemoteTaskHandle:
    agent: str
    endpoint: str
    task_id: str
    context_id: str
    state: str
    updated_at: str


class RemoteTaskStore:
    """Persist remote task identities without prompts, output, or credentials."""

    def __init__(self, db_path: Path, workspace: Path) -> None:
        self.db_path = validate_unlinked_file_path(
            db_path,
            label="A2A remote task database",
        )
        self.workspace = normalize_project_path(workspace)
        self._lock = threading.RLock()
        self._closed = False
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = validate_unlinked_file_path(
            self.db_path,
            label="A2A remote task database",
        )
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
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
                self._conn.executescript(
                    """
                    PRAGMA journal_mode=WAL;
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
                    """
                )
                self._conn.execute(
                    f"PRAGMA user_version = {REMOTE_TASK_SCHEMA_VERSION}"
                )
        except BaseException:
            self._conn.close()
            self._closed = True
            raise
        self._restrict_file_permissions()

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
        with self._lock, self._conn:
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
        if os.name == "nt":
            return
        for path in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            if path.exists():
                with suppress(OSError):
                    os.chmod(path, 0o600)
