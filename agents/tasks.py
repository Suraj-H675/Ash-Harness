"""Durable task DAG and lease primitives for multi-agent orchestration."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from core.events import envelope_event
from core.redaction import redact_text

TaskState = Literal[
    "queued",
    "leased",
    "running",
    "succeeded",
    "failed",
    "cancelled",
]
ACTIVE_TASK_STATES = ("leased", "running")
TERMINAL_TASK_STATES = ("succeeded", "failed", "cancelled")
MAX_TASK_JSON_BYTES = 128 * 1024
TASK_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class AgentTaskError(RuntimeError):
    """A durable task operation violated its state or ownership contract."""


class AgentTaskBudgetExceeded(AgentTaskError):
    """A worker attempted to consume more than its durable task budget."""


@dataclass(frozen=True)
class AgentTask:
    task_id: str
    description: str
    role: str
    state: TaskState
    owner_agent_id: str | None
    parent_task_id: str | None
    sprint_id: str | None
    attempt: int
    max_attempts: int
    token_budget: int
    used_tokens: int
    time_budget_seconds: float
    lease_expires_at: datetime | None
    dependencies: tuple[str, ...] = ()
    result: dict[str, Any] | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class AgentTaskLease:
    task: AgentTask
    token: str


@dataclass(frozen=True)
class AgentArtifact:
    artifact_id: str
    task_id: str
    kind: str
    uri: str
    sha256: str | None
    metadata: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class AgentTaskEvent:
    sequence: int
    task_id: str
    event: dict[str, Any]


class AgentTaskStore:
    """SQLite task scheduler with atomic claims and renewable ownership leases."""

    def __init__(self, db_path: Path | str, *, busy_timeout_ms: int = 5000) -> None:
        self.db_path = str(Path(db_path).expanduser().resolve())
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=busy_timeout_ms / 1000,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._closed = False
        self._init_db(busy_timeout_ms)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def _init_db(self, busy_timeout_ms: int) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                f"""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                PRAGMA foreign_keys=ON;
                PRAGMA busy_timeout={int(busy_timeout_ms)};

                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    role TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('queued','leased','running','succeeded','failed','cancelled')),
                    owner_agent_id TEXT,
                    parent_task_id TEXT REFERENCES agent_tasks(task_id),
                    sprint_id TEXT REFERENCES sprints(sprint_id),
                    lease_token_hash TEXT,
                    lease_expires_at REAL,
                    attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
                    max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 10),
                    token_budget INTEGER NOT NULL CHECK(token_budget > 0),
                    used_tokens INTEGER NOT NULL DEFAULT 0 CHECK(used_tokens >= 0),
                    time_budget_seconds REAL NOT NULL CHECK(time_budget_seconds > 0),
                    result_json TEXT,
                    error TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{{}}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_task_dependencies (
                    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
                    depends_on_task_id TEXT NOT NULL REFERENCES agent_tasks(task_id),
                    PRIMARY KEY (task_id, depends_on_task_id),
                    CHECK(task_id <> depends_on_task_id)
                );

                CREATE TABLE IF NOT EXISTS agent_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    sha256 TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{{}}',
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_task_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_agent_tasks_claim
                    ON agent_tasks(state, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_tasks_owner
                    ON agent_tasks(owner_agent_id, state);
                CREATE INDEX IF NOT EXISTS idx_agent_task_dependencies_dep
                    ON agent_task_dependencies(depends_on_task_id);
                CREATE INDEX IF NOT EXISTS idx_agent_artifacts_task
                    ON agent_artifacts(task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_task_events_task_sequence
                    ON agent_task_events(task_id, sequence);
                """
            )

    def create_task(
        self,
        description: str,
        *,
        role: str = "general",
        task_id: str | None = None,
        parent_task_id: str | None = None,
        sprint_id: str | None = None,
        dependencies: Sequence[str] = (),
        max_attempts: int = 1,
        token_budget: int = 4000,
        time_budget_seconds: float = 900.0,
        metadata: dict[str, Any] | None = None,
    ) -> AgentTask:
        description = _bounded_text(description, "task description", 20_000)
        role = _identifier(role, "task role")
        identifier = _identifier(task_id or str(uuid.uuid4()), "task id")
        parent = _optional_identifier(parent_task_id, "parent task id")
        sprint = _optional_identifier(sprint_id, "sprint id")
        dependency_ids = tuple(
            dict.fromkeys(_identifier(item, "dependency") for item in dependencies)
        )
        if identifier in dependency_ids:
            raise ValueError("task cannot depend on itself")
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if token_budget < 1:
            raise ValueError("token_budget must be positive")
        if not 0.1 <= float(time_budget_seconds) <= 86_400:
            raise ValueError("time_budget_seconds must be between 0.1 and 86400")
        metadata_json = _bounded_json(metadata or {}, "task metadata")
        now = time.time()
        with self._transaction():
            self._require_references(parent, sprint, dependency_ids)
            try:
                self._conn.execute(
                    """
                    INSERT INTO agent_tasks (
                        task_id, description, role, state, parent_task_id, sprint_id,
                        max_attempts, token_budget, time_budget_seconds,
                        metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        description,
                        role,
                        parent,
                        sprint,
                        max_attempts,
                        token_budget,
                        float(time_budget_seconds),
                        metadata_json,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AgentTaskError(
                    f"cannot create task {identifier!r}: {exc}"
                ) from exc
            self._conn.executemany(
                "INSERT INTO agent_task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
                ((identifier, dependency) for dependency in dependency_ids),
            )
            self._record_event_locked(
                identifier,
                "agent.task.created",
                now,
                state="queued",
                role=role,
                dependencies=list(dependency_ids),
                parent_task_id=parent,
                sprint_id=sprint,
                token_budget=token_budget,
                time_budget_seconds=float(time_budget_seconds),
            )
        task = self.get_task(identifier)
        assert task is not None
        return task

    def claim_task(
        self,
        owner_agent_id: str,
        *,
        task_id: str | None = None,
        lease_seconds: float = 30.0,
        max_active: int = 4,
    ) -> AgentTaskLease | None:
        owner = _identifier(owner_agent_id, "owner agent id")
        requested = _optional_identifier(task_id, "task id")
        if not 1 <= max_active <= 128:
            raise ValueError("max_active must be between 1 and 128")
        if not 1 <= float(lease_seconds) <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        token = secrets.token_urlsafe(32)
        token_hash = _token_hash(token)
        now = time.time()
        with self._transaction():
            self._recover_expired_locked(now)
            self._fail_dependency_blocked_locked(now)
            active = int(
                self._conn.execute(
                    """
                    SELECT COUNT(*) FROM agent_tasks
                    WHERE state IN ('leased','running') AND lease_expires_at > ?
                    """,
                    (now,),
                ).fetchone()[0]
            )
            if active >= max_active:
                return None
            task_filter = ""
            params: tuple[str, ...] = ()
            if requested is not None:
                task_filter = " AND task_id = ?"
                params = (requested,)
            row = self._conn.execute(
                """
                SELECT * FROM agent_tasks AS candidate
                WHERE state = 'queued'
                  AND NOT EXISTS (
                    SELECT 1 FROM agent_task_dependencies AS dependency
                    JOIN agent_tasks AS prerequisite
                      ON prerequisite.task_id = dependency.depends_on_task_id
                    WHERE dependency.task_id = candidate.task_id
                      AND prerequisite.state <> 'succeeded'
                  )
                """
                + task_filter
                + " ORDER BY created_at, task_id LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                return None
            expires = now + float(lease_seconds)
            updated = self._conn.execute(
                """
                UPDATE agent_tasks
                SET state = 'leased', owner_agent_id = ?, lease_token_hash = ?,
                    lease_expires_at = ?, attempt = attempt + 1, updated_at = ?
                WHERE task_id = ? AND state = 'queued'
                """,
                (owner, token_hash, expires, now, row["task_id"]),
            )
            if updated.rowcount != 1:
                return None
            self._record_event_locked(
                str(row["task_id"]),
                "agent.task.leased",
                now,
                state="leased",
                owner_agent_id=owner,
                attempt=int(row["attempt"]) + 1,
                lease_expires_at=_from_epoch(expires).isoformat(),
            )
        task = self.get_task(str(row["task_id"]))
        assert task is not None
        return AgentTaskLease(task=task, token=token)

    def start_task(self, task_id: str, token: str) -> AgentTask:
        return self._transition_owned(task_id, token, "leased", "running")

    def renew_lease(
        self,
        task_id: str,
        token: str,
        *,
        lease_seconds: float = 30.0,
    ) -> AgentTask:
        if not 1 <= float(lease_seconds) <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        identifier = _identifier(task_id, "task id")
        now = time.time()
        with self._transaction():
            self._owned_row(identifier, token, now)
            self._conn.execute(
                "UPDATE agent_tasks SET lease_expires_at = ?, updated_at = ? WHERE task_id = ?",
                (now + float(lease_seconds), now, identifier),
            )
        return self._required_task(identifier)

    def record_tokens(self, task_id: str, token: str, token_count: int) -> AgentTask:
        if token_count < 0:
            raise ValueError("token_count must be non-negative")
        identifier = _identifier(task_id, "task id")
        now = time.time()
        exceeded_error: str | None = None
        with self._transaction():
            row = self._owned_row(identifier, token, now)
            used = int(row["used_tokens"]) + token_count
            budget = int(row["token_budget"])
            if used > budget:
                self._conn.execute(
                    """
                    UPDATE agent_tasks SET state = 'failed', error = ?, used_tokens = ?,
                        lease_token_hash = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        f"token budget exceeded: {used} > {budget}",
                        used,
                        now,
                        identifier,
                    ),
                )
                self._record_event_locked(
                    identifier,
                    "agent.task.failed",
                    now,
                    state="failed",
                    owner_agent_id=str(row["owner_agent_id"]),
                    reason="token_budget_exceeded",
                    used_tokens=used,
                    token_budget=budget,
                )
                exceeded_error = f"task {identifier!r} exceeded token budget {budget}"
            else:
                self._conn.execute(
                    "UPDATE agent_tasks SET used_tokens = ?, updated_at = ? WHERE task_id = ?",
                    (used, now, identifier),
                )
                self._record_event_locked(
                    identifier,
                    "agent.task.tokens_recorded",
                    now,
                    state=str(row["state"]),
                    owner_agent_id=str(row["owner_agent_id"]),
                    added_tokens=token_count,
                    used_tokens=used,
                    token_budget=budget,
                )
        if exceeded_error is not None:
            raise AgentTaskBudgetExceeded(exceeded_error)
        return self._required_task(identifier)

    def complete_task(
        self,
        task_id: str,
        token: str,
        result: dict[str, Any] | None = None,
    ) -> AgentTask:
        return self._finish_task(task_id, token, success=True, result=result)

    def fail_task(
        self,
        task_id: str,
        token: str,
        error: str,
        *,
        retryable: bool = False,
    ) -> AgentTask:
        return self._finish_task(
            task_id,
            token,
            success=False,
            error=error,
            retryable=retryable,
        )

    def cancel_task(self, task_id: str, *, reason: str = "cancelled") -> list[str]:
        identifier = _identifier(task_id, "task id")
        reason = _bounded_text(reason, "cancellation reason", 4096)
        now = time.time()
        with self._transaction():
            if (
                self._conn.execute(
                    "SELECT 1 FROM agent_tasks WHERE task_id = ?", (identifier,)
                ).fetchone()
                is None
            ):
                raise AgentTaskError(f"unknown task: {identifier}")
            rows = self._conn.execute(
                """
                WITH RECURSIVE descendants(task_id) AS (
                    SELECT ?
                    UNION
                    SELECT dependency.task_id
                    FROM agent_task_dependencies AS dependency
                    JOIN descendants
                      ON dependency.depends_on_task_id = descendants.task_id
                )
                SELECT task_id FROM descendants
                """,
                (identifier,),
            ).fetchall()
            task_ids = [str(row["task_id"]) for row in rows]
            placeholders = ",".join("?" for _ in task_ids)
            transitioning = [
                str(row["task_id"])
                for row in self._conn.execute(
                    f"""
                    SELECT task_id FROM agent_tasks
                    WHERE task_id IN ({placeholders})
                      AND state NOT IN ('succeeded','failed','cancelled')
                    """,
                    task_ids,
                ).fetchall()
            ]
            self._conn.execute(
                f"""
                UPDATE agent_tasks SET state = 'cancelled', error = ?,
                    lease_token_hash = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE task_id IN ({placeholders}) AND state NOT IN ('succeeded','failed','cancelled')
                """,
                (reason, now, *task_ids),
            )
            for transitioning_id in transitioning:
                self._record_event_locked(
                    transitioning_id,
                    "agent.task.cancelled",
                    now,
                    state="cancelled",
                    reason=reason,
                    root_task_id=identifier,
                )
        return task_ids

    def recover_expired(self) -> list[str]:
        now = time.time()
        with self._transaction():
            return self._recover_expired_locked(now)

    def get_task(self, task_id: str) -> AgentTask | None:
        identifier = _identifier(task_id, "task id")
        with self._lock, closing(self._conn.cursor()) as cursor:
            row = cursor.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ?", (identifier,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_task(row)

    def list_tasks(
        self,
        *,
        state: TaskState | None = None,
        owner_agent_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentTask]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        clauses: list[str] = []
        params: list[Any] = []
        if state is not None:
            if state not in (*ACTIVE_TASK_STATES, *TERMINAL_TASK_STATES, "queued"):
                raise ValueError(f"invalid task state: {state}")
            clauses.append("state = ?")
            params.append(state)
        if owner_agent_id is not None:
            clauses.append("owner_agent_id = ?")
            params.append(_identifier(owner_agent_id, "owner agent id"))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock, closing(self._conn.cursor()) as cursor:
            rows = cursor.execute(
                "SELECT * FROM agent_tasks"
                + where
                + " ORDER BY created_at DESC, task_id LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [self._row_to_task(row) for row in rows]

    def add_artifact(
        self,
        task_id: str,
        *,
        kind: str,
        uri: str,
        sha256: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentArtifact:
        identifier = _identifier(task_id, "task id")
        kind = _identifier(kind, "artifact kind")
        uri = _bounded_text(uri, "artifact uri", 16_384)
        if sha256 is not None and (
            len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256)
        ):
            raise ValueError("artifact sha256 must be 64 lowercase hex characters")
        metadata_json = _bounded_json(metadata or {}, "artifact metadata")
        artifact_id = str(uuid.uuid4())
        now = time.time()
        with self._transaction():
            task = self._conn.execute(
                "SELECT state FROM agent_tasks WHERE task_id = ?", (identifier,)
            ).fetchone()
            if task is None:
                raise AgentTaskError(f"unknown task: {identifier}")
            self._conn.execute(
                """
                INSERT INTO agent_artifacts
                    (artifact_id, task_id, kind, uri, sha256, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (artifact_id, identifier, kind, uri, sha256, metadata_json, now),
            )
            self._record_event_locked(
                identifier,
                "agent.task.artifact.created",
                now,
                artifact_id=artifact_id,
                kind=kind,
                uri=uri,
                sha256=sha256,
            )
        return AgentArtifact(
            artifact_id,
            identifier,
            kind,
            uri,
            sha256,
            metadata or {},
            _from_epoch(now),
        )

    def list_artifacts(self, task_id: str) -> list[AgentArtifact]:
        identifier = _identifier(task_id, "task id")
        with self._lock, closing(self._conn.cursor()) as cursor:
            rows = cursor.execute(
                "SELECT * FROM agent_artifacts WHERE task_id = ? ORDER BY created_at, artifact_id",
                (identifier,),
            ).fetchall()
        return [
            AgentArtifact(
                artifact_id=str(row["artifact_id"]),
                task_id=str(row["task_id"]),
                kind=str(row["kind"]),
                uri=str(row["uri"]),
                sha256=str(row["sha256"]) if row["sha256"] else None,
                metadata=_json_object(row["metadata_json"]),
                created_at=_from_epoch(float(row["created_at"])),
            )
            for row in rows
        ]

    def list_events(
        self,
        *,
        task_id: str | None = None,
        event_type: str | None = None,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[AgentTaskEvent]:
        """Replay durable task events in global insertion order."""

        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        clauses = ["sequence > ?"]
        params: list[Any] = [after_sequence]
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(_identifier(task_id, "task id"))
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(_identifier(event_type, "event type"))
        with self._lock, closing(self._conn.cursor()) as cursor:
            rows = cursor.execute(
                "SELECT sequence, task_id, event_json FROM agent_task_events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY sequence LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [
            AgentTaskEvent(
                sequence=int(row["sequence"]),
                task_id=str(row["task_id"]),
                event=_json_object(row["event_json"]),
            )
            for row in rows
        ]

    def _transition_owned(
        self, task_id: str, token: str, source: TaskState, target: TaskState
    ) -> AgentTask:
        identifier = _identifier(task_id, "task id")
        now = time.time()
        with self._transaction():
            row = self._owned_row(identifier, token, now)
            if row["state"] != source:
                raise AgentTaskError(
                    f"task {identifier!r} is {row['state']}, expected {source}"
                )
            self._conn.execute(
                "UPDATE agent_tasks SET state = ?, updated_at = ? WHERE task_id = ?",
                (target, now, identifier),
            )
            self._record_event_locked(
                identifier,
                f"agent.task.{target}",
                now,
                state=target,
                owner_agent_id=str(row["owner_agent_id"]),
                attempt=int(row["attempt"]),
            )
        return self._required_task(identifier)

    def _finish_task(
        self,
        task_id: str,
        token: str,
        *,
        success: bool,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        retryable: bool = False,
    ) -> AgentTask:
        identifier = _identifier(task_id, "task id")
        result_json = _bounded_json(result or {}, "task result") if success else None
        error_text = _bounded_text(error or "task failed", "task error", 16_384)
        now = time.time()
        with self._transaction():
            row = self._owned_row(identifier, token, now)
            if row["state"] not in ACTIVE_TASK_STATES:
                raise AgentTaskError(f"task {identifier!r} is not active")
            retry = (
                not success
                and retryable
                and int(row["attempt"]) < int(row["max_attempts"])
            )
            state = "queued" if retry else ("succeeded" if success else "failed")
            self._conn.execute(
                """
                UPDATE agent_tasks SET state = ?, result_json = ?, error = ?,
                    owner_agent_id = CASE WHEN ? = 'queued' THEN NULL ELSE owner_agent_id END,
                    lease_token_hash = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    state,
                    result_json,
                    None if success or retry else error_text,
                    state,
                    now,
                    identifier,
                ),
            )
            event_type = (
                "agent.task.retrying"
                if retry
                else "agent.task.succeeded"
                if success
                else "agent.task.failed"
            )
            self._record_event_locked(
                identifier,
                event_type,
                now,
                state=state,
                owner_agent_id=str(row["owner_agent_id"]),
                attempt=int(row["attempt"]),
                retryable=retry,
                reason=None if success else error_text,
            )
        return self._required_task(identifier)

    def _owned_row(self, task_id: str, token: str, now: float) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise AgentTaskError(f"unknown task: {task_id}")
        if row["state"] not in ACTIVE_TASK_STATES:
            raise AgentTaskError(f"task {task_id!r} is not actively leased")
        if not secrets.compare_digest(
            str(row["lease_token_hash"] or ""), _token_hash(token)
        ):
            raise AgentTaskError(f"task {task_id!r} is owned by another lease")
        if float(row["lease_expires_at"] or 0) <= now:
            raise AgentTaskError(f"task {task_id!r} lease has expired")
        return row

    def _recover_expired_locked(self, now: float) -> list[str]:
        rows = self._conn.execute(
            """
            SELECT task_id, attempt, max_attempts FROM agent_tasks
            WHERE state IN ('leased','running') AND lease_expires_at <= ?
            """,
            (now,),
        ).fetchall()
        recovered: list[str] = []
        for row in rows:
            retry = int(row["attempt"]) < int(row["max_attempts"])
            self._conn.execute(
                """
                UPDATE agent_tasks SET state = ?, owner_agent_id = NULL,
                    lease_token_hash = NULL, lease_expires_at = NULL, error = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    "queued" if retry else "failed",
                    None if retry else "worker lease expired",
                    now,
                    row["task_id"],
                ),
            )
            self._record_event_locked(
                str(row["task_id"]),
                "agent.task.recovered" if retry else "agent.task.failed",
                now,
                state="queued" if retry else "failed",
                attempt=int(row["attempt"]),
                retryable=retry,
                reason="worker lease expired",
            )
            recovered.append(str(row["task_id"]))
        return recovered

    def _fail_dependency_blocked_locked(self, now: float) -> None:
        while True:
            blocked = self._conn.execute(
                """
                SELECT candidate.task_id
                FROM agent_tasks AS candidate
                WHERE state = 'queued' AND EXISTS (
                    SELECT 1 FROM agent_task_dependencies AS dependency
                    JOIN agent_tasks AS prerequisite
                      ON prerequisite.task_id = dependency.depends_on_task_id
                    WHERE dependency.task_id = candidate.task_id
                      AND prerequisite.state IN ('failed','cancelled')
                )
                """
            ).fetchall()
            if not blocked:
                return
            task_ids = [str(row["task_id"]) for row in blocked]
            placeholders = ",".join("?" for _ in task_ids)
            changed = self._conn.execute(
                """
                UPDATE agent_tasks
                SET state = 'failed', error = 'dependency did not succeed', updated_at = ?
                WHERE state = 'queued' AND task_id IN ("""
                + placeholders
                + ")",
                (now, *task_ids),
            ).rowcount
            for task_id in task_ids:
                self._record_event_locked(
                    task_id,
                    "agent.task.failed",
                    now,
                    state="failed",
                    reason="dependency did not succeed",
                )
            if not changed:
                return

    def _record_event_locked(
        self,
        task_id: str,
        event_type: str,
        now: float,
        **data: Any,
    ) -> None:
        payload = _redact_event_value(
            {
                "type": event_type,
                "task_id": task_id,
                **data,
            }
        )
        assert isinstance(payload, dict)
        event = envelope_event(
            payload,
            source={"type": "agent_task", "id": "ash"},
            timestamp_factory=lambda: _from_epoch(now).isoformat(),
        )
        event_json = _bounded_json(event, "task event")
        self._conn.execute(
            """
            INSERT INTO agent_task_events
                (event_id, task_id, event_type, event_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event["event_id"], task_id, event_type, event_json, now),
        )

    def _require_references(
        self,
        parent_task_id: str | None,
        sprint_id: str | None,
        dependencies: Sequence[str],
    ) -> None:
        if (
            parent_task_id is not None
            and self._conn.execute(
                "SELECT 1 FROM agent_tasks WHERE task_id = ?", (parent_task_id,)
            ).fetchone()
            is None
        ):
            raise AgentTaskError(f"unknown parent task: {parent_task_id}")
        if (
            sprint_id is not None
            and self._conn.execute(
                "SELECT 1 FROM sprints WHERE sprint_id = ?", (sprint_id,)
            ).fetchone()
            is None
        ):
            raise AgentTaskError(f"unknown sprint: {sprint_id}")
        for dependency in dependencies:
            if (
                self._conn.execute(
                    "SELECT 1 FROM agent_tasks WHERE task_id = ?", (dependency,)
                ).fetchone()
                is None
            ):
                raise AgentTaskError(f"unknown dependency task: {dependency}")

    def _required_task(self, task_id: str) -> AgentTask:
        task = self.get_task(task_id)
        if task is None:  # pragma: no cover - internal invariant
            raise AgentTaskError(f"unknown task: {task_id}")
        return task

    def _row_to_task(self, row: sqlite3.Row) -> AgentTask:
        dependencies = tuple(
            str(item["depends_on_task_id"])
            for item in self._conn.execute(
                """
                SELECT depends_on_task_id FROM agent_task_dependencies
                WHERE task_id = ? ORDER BY depends_on_task_id
                """,
                (row["task_id"],),
            ).fetchall()
        )
        return AgentTask(
            task_id=str(row["task_id"]),
            description=str(row["description"]),
            role=str(row["role"]),
            state=str(row["state"]),  # type: ignore[arg-type]
            owner_agent_id=str(row["owner_agent_id"])
            if row["owner_agent_id"]
            else None,
            parent_task_id=str(row["parent_task_id"])
            if row["parent_task_id"]
            else None,
            sprint_id=str(row["sprint_id"]) if row["sprint_id"] else None,
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            token_budget=int(row["token_budget"]),
            used_tokens=int(row["used_tokens"]),
            time_budget_seconds=float(row["time_budget_seconds"]),
            lease_expires_at=(
                _from_epoch(float(row["lease_expires_at"]))
                if row["lease_expires_at"] is not None
                else None
            ),
            dependencies=dependencies,
            result=_json_object(row["result_json"]) if row["result_json"] else None,
            error=str(row["error"]) if row["error"] else None,
            metadata=_json_object(row["metadata_json"]),
            created_at=_from_epoch(float(row["created_at"])),
            updated_at=_from_epoch(float(row["updated_at"])),
        )

    def _transaction(self):
        return _ImmediateTransaction(self)


class _ImmediateTransaction:
    def __init__(self, store: AgentTaskStore) -> None:
        self.store = store

    def __enter__(self) -> None:
        self.store._lock.acquire()
        if self.store._closed:
            self.store._lock.release()
            raise AgentTaskError("task store is closed")
        try:
            self.store._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            self.store._lock.release()
            raise AgentTaskError(f"cannot begin task transaction: {exc}") from exc

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self.store._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.store._lock.release()


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not TASK_IDENTIFIER.fullmatch(value):
        raise ValueError(
            f"{label} must be a portable identifier of at most 128 characters"
        )
    return value


def _optional_identifier(value: str | None, label: str) -> str | None:
    return _identifier(value, label) if value is not None else None


def _redact_event_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: _redact_event_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_event_value(item) for item in value]
    return value


def _bounded_text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    value = value.strip()
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds {maximum} bytes")
    return value


def _bounded_json(value: dict[str, Any], label: str) -> str:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain JSON values") from exc
    if len(payload.encode("utf-8")) > MAX_TASK_JSON_BYTES:
        raise ValueError(f"{label} exceeds {MAX_TASK_JSON_BYTES} bytes")
    return payload


def _json_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value, parse_constant=_reject_json_constant)
    if not isinstance(parsed, dict):
        raise AgentTaskError("stored task JSON is not an object")
    return parsed


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _from_epoch(value: float) -> datetime:
    return datetime.fromtimestamp(value, timezone.utc)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")
