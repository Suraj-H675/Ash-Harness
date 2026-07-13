"""SQLite schedule, run, lease, worker, and lifecycle event storage."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, cast

from ash.automation.models import (
    AutomationJob,
    AutomationRun,
    AutomationRunLease,
    AutomationRunStatus,
    AutomationWorker,
    ScheduleSpec,
    UsageSource,
)
from ash.automation.schedules import first_fire_time, next_fire_time
from ash.core.events import EventContext, envelope_event
from ash.core.redaction import redact_text


MAX_JOB_NAME_BYTES = 256
MAX_PROMPT_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
MAX_ERROR_BYTES = 16 * 1024
MAX_EVENT_BYTES = 128 * 1024
AUTOMATION_SCHEMA_VERSION = 2

_V2_RUN_COLUMNS = {
    "cache_read_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cache_write_tokens": "INTEGER NOT NULL DEFAULT 0",
    "usage_source": (
        "TEXT NOT NULL DEFAULT 'unavailable' CHECK(usage_source IN "
        "('unavailable','provider','estimated','mixed'))"
    ),
    "estimated_prompt_tokens": "INTEGER NOT NULL DEFAULT 0",
    "estimated_completion_tokens": "INTEGER NOT NULL DEFAULT 0",
    "estimated_cost_usd": "REAL NOT NULL DEFAULT 0",
}


class AutomationError(RuntimeError):
    """A durable automation operation violated its state contract."""


class AutomationRestartRequired(AutomationError):
    """A worker startup invariant changed and requires process replacement."""


class AutomationStore:
    """Concurrency-safe durable automations scoped by canonical workspace."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        busy_timeout_ms: int = 5000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = str(Path(db_path).expanduser().resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=busy_timeout_ms / 1000,
            )
        except sqlite3.Error as exc:
            self._closed = True
            raise AutomationError(
                f"cannot open automation database {self.db_path}: {exc}"
            ) from exc
        self._conn.row_factory = sqlite3.Row
        try:
            self._init_db(busy_timeout_ms)
        except Exception as exc:
            self._conn.close()
            self._closed = True
            if isinstance(exc, sqlite3.Error):
                raise AutomationError(
                    f"cannot initialize automation database {self.db_path}: {exc}"
                ) from exc
            raise
        if os.name != "nt":
            self._restrict_file_permissions()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def __enter__(self) -> "AutomationStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _init_db(self, busy_timeout_ms: int) -> None:
        with self._lock, self._conn:
            schema_version = int(
                self._conn.execute("PRAGMA user_version").fetchone()[0]
            )
            if schema_version > AUTOMATION_SCHEMA_VERSION:
                raise AutomationError(
                    "automation database schema "
                    f"v{schema_version} is newer than supported "
                    f"v{AUTOMATION_SCHEMA_VERSION}"
                )
            self._conn.executescript(
                f"""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                PRAGMA foreign_keys=ON;
                PRAGMA busy_timeout={int(busy_timeout_ms)};

                CREATE TABLE IF NOT EXISTS automation_jobs (
                    job_id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    name TEXT NOT NULL COLLATE NOCASE,
                    prompt TEXT NOT NULL,
                    schedule_kind TEXT NOT NULL CHECK(schedule_kind IN ('at','every','cron')),
                    schedule_value TEXT NOT NULL,
                    schedule_timezone TEXT NOT NULL,
                    schedule_anchor_at REAL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    next_run_at REAL,
                    misfire_grace_seconds INTEGER NOT NULL
                        CHECK(misfire_grace_seconds BETWEEN 0 AND 2592000),
                    timeout_seconds REAL NOT NULL CHECK(timeout_seconds BETWEEN 1 AND 86400),
                    token_budget INTEGER NOT NULL CHECK(token_budget BETWEEN 1 AND 10000000),
                    last_run_at REAL,
                    last_run_status TEXT CHECK(last_run_status IS NULL OR last_run_status IN
                        ('running','succeeded','failed','cancelled','interrupted','skipped')),
                    last_error TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    deleted_at REAL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_job_name
                    ON automation_jobs(workspace, name) WHERE deleted_at IS NULL;
                CREATE INDEX IF NOT EXISTS idx_automation_jobs_due
                    ON automation_jobs(workspace, enabled, next_run_at)
                    WHERE deleted_at IS NULL;

                CREATE TABLE IF NOT EXISTS automation_runs (
                    run_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES automation_jobs(job_id),
                    scheduled_for REAL NOT NULL,
                    trigger TEXT NOT NULL CHECK(trigger IN ('scheduled','manual')),
                    status TEXT NOT NULL CHECK(status IN
                        ('running','succeeded','failed','cancelled','interrupted','skipped')),
                    attempt INTEGER NOT NULL DEFAULT 1,
                    worker_id TEXT,
                    lease_token_hash TEXT,
                    lease_expires_at REAL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
                    session_id TEXT,
                    response TEXT,
                    error TEXT,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    usage_source TEXT NOT NULL DEFAULT 'unavailable' CHECK(usage_source IN
                        ('unavailable','provider','estimated','mixed')),
                    estimated_prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_completion_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_automation_runs_job
                    ON automation_runs(job_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_automation_runs_active
                    ON automation_runs(status, lease_expires_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_runs_scheduled_once
                    ON automation_runs(job_id, scheduled_for)
                    WHERE trigger = 'scheduled';

                CREATE TABLE IF NOT EXISTS automation_workers (
                    worker_id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    started_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    max_concurrent_runs INTEGER NOT NULL CHECK(max_concurrent_runs BETWEEN 1 AND 32)
                );
                CREATE INDEX IF NOT EXISTS idx_automation_workers_workspace
                    ON automation_workers(workspace, heartbeat_at);

                CREATE TABLE IF NOT EXISTS automation_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    job_id TEXT REFERENCES automation_jobs(job_id),
                    run_id TEXT REFERENCES automation_runs(run_id) ON DELETE SET NULL,
                    event_type TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_automation_events_job_sequence
                    ON automation_events(job_id, sequence);
                """
            )
            if schema_version < 2:
                self._migrate_runs_to_v2()
            self._conn.execute(f"PRAGMA user_version = {AUTOMATION_SCHEMA_VERSION}")

    def _migrate_runs_to_v2(self) -> None:
        existing = {
            str(row["name"])
            for row in self._conn.execute(
                "PRAGMA table_info(automation_runs)"
            ).fetchall()
        }
        for name, declaration in _V2_RUN_COLUMNS.items():
            if name in existing:
                continue
            self._conn.execute(
                f"ALTER TABLE automation_runs ADD COLUMN {name} {declaration}"
            )

    def _restrict_file_permissions(self) -> None:
        for path in (
            Path(self.db_path),
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            if path.exists():
                with suppress(OSError):
                    os.chmod(path, 0o600)

    def create_job(
        self,
        *,
        name: str,
        prompt: str,
        workspace: Path | str,
        schedule: ScheduleSpec,
        job_id: str | None = None,
        enabled: bool = True,
        misfire_grace_seconds: int = 86_400,
        timeout_seconds: float = 1800.0,
        token_budget: int = 100_000,
    ) -> AutomationJob:
        normalized_name = _bounded_text(name, "job name", MAX_JOB_NAME_BYTES)
        normalized_prompt = _bounded_text(prompt, "job prompt", MAX_PROMPT_BYTES)
        root = _workspace(workspace)
        identifier = _identifier(job_id or str(uuid.uuid4()), "job id")
        grace = int(misfire_grace_seconds)
        timeout = float(timeout_seconds)
        budget = int(token_budget)
        if not 0 <= grace <= 2_592_000:
            raise ValueError("misfire_grace_seconds must be between 0 and 2592000")
        if not 1 <= timeout <= 86_400:
            raise ValueError("timeout_seconds must be between 1 and 86400")
        if not 1 <= budget <= 10_000_000:
            raise ValueError("token_budget must be between 1 and 10000000")
        now = self._clock()
        next_run = first_fire_time(schedule, now=_from_epoch(now)) if enabled else None
        with self._transaction():
            try:
                self._conn.execute(
                    """
                    INSERT INTO automation_jobs (
                        job_id, workspace, name, prompt, schedule_kind, schedule_value,
                        schedule_timezone, schedule_anchor_at, enabled, next_run_at,
                        misfire_grace_seconds, timeout_seconds, token_budget,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        root,
                        normalized_name,
                        normalized_prompt,
                        schedule.kind,
                        schedule.value,
                        schedule.timezone,
                        _to_epoch(schedule.anchor_at),
                        int(enabled),
                        _to_epoch(next_run),
                        grace,
                        timeout,
                        budget,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AutomationError(
                    f"cannot create automation {normalized_name!r}: {exc}"
                ) from exc
            self._record_event_locked(
                "automation.job.created",
                job_id=identifier,
                run_id=None,
                now=now,
                name=normalized_name,
                workspace=root,
                schedule={"kind": schedule.kind, "value": schedule.value},
                enabled=enabled,
            )
        return self._required_job(identifier, workspace=root)

    def list_jobs(
        self,
        workspace: Path | str,
        *,
        include_disabled: bool = False,
        limit: int = 200,
    ) -> list[AutomationJob]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        root = _workspace(workspace)
        enabled_clause = "" if include_disabled else " AND enabled = 1"
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM automation_jobs WHERE workspace = ? AND deleted_at IS NULL"
                + enabled_clause
                + " ORDER BY COALESCE(next_run_at, 9e99), created_at, job_id LIMIT ?",
                (root, limit),
            ).fetchall()
        return [_job_from_row(row) for row in rows]

    def get_job(
        self,
        reference: str,
        *,
        workspace: Path | str,
        include_deleted: bool = False,
    ) -> AutomationJob | None:
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("job reference must be non-empty")
        root = _workspace(workspace)
        value = reference.strip()
        deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM automation_jobs
                WHERE workspace = ?
                  AND (job_id = ? OR name = ? COLLATE NOCASE)
                """
                + deleted_clause
                + """
                ORDER BY CASE WHEN job_id = ? THEN 0 ELSE 1 END,
                         CASE WHEN deleted_at IS NULL THEN 0 ELSE 1 END,
                         created_at DESC
                LIMIT 1
                """,
                (root, value, value, value),
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def set_enabled(
        self,
        reference: str,
        *,
        workspace: Path | str,
        enabled: bool,
    ) -> AutomationJob:
        root = _workspace(workspace)
        with self._transaction():
            now = self._clock()
            job = self._required_job(reference, workspace=root)
            if job.enabled == enabled:
                return job
            if (
                enabled
                and job.schedule.kind == "at"
                and first_fire_time(job.schedule, now=_from_epoch(now))
                <= _from_epoch(now)
            ):
                raise AutomationError(
                    "completed or expired one-shot automation cannot be resumed; "
                    "create a new schedule"
                )
            next_run = (
                first_fire_time(job.schedule, now=_from_epoch(now)) if enabled else None
            )
            self._conn.execute(
                """
                UPDATE automation_jobs
                SET enabled = ?, next_run_at = ?, updated_at = ?
                WHERE job_id = ? AND workspace = ? AND deleted_at IS NULL
                """,
                (int(enabled), _to_epoch(next_run), now, job.job_id, root),
            )
            self._record_event_locked(
                "automation.job.resumed" if enabled else "automation.job.paused",
                job_id=job.job_id,
                run_id=None,
                now=now,
            )
        return self._required_job(job.job_id, workspace=root)

    def remove_job(
        self,
        reference: str,
        *,
        workspace: Path | str,
    ) -> AutomationJob:
        root = _workspace(workspace)
        self.recover_expired(workspace=root)
        with self._transaction():
            now = self._clock()
            job = self._required_job(reference, workspace=root)
            active = self._conn.execute(
                "SELECT COUNT(*) FROM automation_runs WHERE job_id = ? AND status = 'running'",
                (job.job_id,),
            ).fetchone()[0]
            if int(active):
                raise AutomationError("cannot remove an automation with an active run")
            self._conn.execute(
                """
                UPDATE automation_jobs
                SET enabled = 0, next_run_at = NULL, deleted_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (now, now, job.job_id),
            )
            self._record_event_locked(
                "automation.job.removed", job_id=job.job_id, run_id=None, now=now
            )
        return job

    def claim_due(
        self,
        *,
        workspace: Path | str,
        worker_id: str,
        lease_seconds: float = 60.0,
        limit: int = 1,
    ) -> list[AutomationRunLease]:
        """Atomically claim due runs, omitting coalesced misfire records."""

        claims, _ = self.claim_due_batch(
            workspace=workspace,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            limit=limit,
        )
        return claims

    def claim_due_batch(
        self,
        *,
        workspace: Path | str,
        worker_id: str,
        lease_seconds: float = 60.0,
        limit: int = 1,
    ) -> tuple[list[AutomationRunLease], list[AutomationRun]]:
        """Claim due runs and return skipped misfires committed in the same batch."""

        root = _workspace(workspace)
        owner = _identifier(worker_id, "worker id")
        lease_duration = _lease_seconds(lease_seconds)
        if not 1 <= limit <= 32:
            raise ValueError("limit must be between 1 and 32")
        claims: list[tuple[str, str]] = []
        skipped_ids: list[str] = []
        with self._transaction():
            now = self._clock()
            recovered_ids = self._recover_expired_locked(now, workspace=root)
            rows = self._conn.execute(
                """
                SELECT * FROM automation_jobs
                WHERE workspace = ? AND enabled = 1 AND deleted_at IS NULL
                  AND next_run_at IS NOT NULL AND next_run_at <= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM automation_runs AS active
                      WHERE active.job_id = automation_jobs.job_id
                        AND active.status = 'running'
                  )
                ORDER BY next_run_at, created_at, job_id
                """,
                (root, now),
            ).fetchall()
            for row in rows:
                if len(claims) >= limit:
                    break
                job = _job_from_row(row)
                scheduled_for = float(row["next_run_at"])
                if now - scheduled_for > job.misfire_grace_seconds:
                    self._record_skipped_locked(job, scheduled_for, now)
                    skipped_ids.append(
                        self._scheduled_run_id(job.job_id, scheduled_for)
                    )
                    continue
                token = self._claim_job_locked(
                    job,
                    scheduled_for=scheduled_for,
                    trigger="scheduled",
                    worker_id=owner,
                    lease_seconds=lease_duration,
                    now=now,
                    advance_schedule=True,
                )
                if token is not None:
                    claims.append(
                        (self._scheduled_run_id(job.job_id, scheduled_for), token)
                    )
        terminal_ids = [*recovered_ids, *skipped_ids]
        return (
            [self._lease_from_ids(run_id, token) for run_id, token in claims],
            [self._required_run(run_id) for run_id in terminal_ids],
        )

    def claim_manual(
        self,
        reference: str,
        *,
        workspace: Path | str,
        worker_id: str,
        lease_seconds: float = 60.0,
    ) -> AutomationRunLease:
        root = _workspace(workspace)
        owner = _identifier(worker_id, "worker id")
        run_id = str(uuid.uuid4())
        self.recover_expired(workspace=root)
        with self._transaction():
            now = self._clock()
            scheduled_for = now
            job = self._required_job(reference, workspace=root)
            active = self._conn.execute(
                "SELECT 1 FROM automation_runs WHERE job_id = ? AND status = 'running'",
                (job.job_id,),
            ).fetchone()
            if active is not None:
                raise AutomationError(
                    f"automation {job.name!r} already has an active run"
                )
            token = self._claim_job_locked(
                job,
                scheduled_for=scheduled_for,
                trigger="manual",
                worker_id=owner,
                lease_seconds=_lease_seconds(lease_seconds),
                now=now,
                advance_schedule=False,
                run_id=run_id,
            )
            if token is None:  # pragma: no cover - transaction and UUID invariant
                raise AutomationError("could not create manual automation run")
        return self._lease_from_ids(run_id, token)

    def renew_lease(
        self,
        run_id: str,
        token: str,
        *,
        lease_seconds: float = 60.0,
    ) -> AutomationRun:
        identifier = _identifier(run_id, "run id")
        with self._transaction():
            now = self._clock()
            self._owned_run_locked(identifier, token, now)
            self._conn.execute(
                "UPDATE automation_runs SET lease_expires_at = ? WHERE run_id = ?",
                (now + _lease_seconds(lease_seconds), identifier),
            )
        return self._required_run(identifier)

    def request_cancel(
        self,
        run_id: str,
        *,
        workspace: Path | str | None = None,
    ) -> AutomationRun:
        identifier = _identifier(run_id, "run id")
        root = _workspace(workspace) if workspace is not None else None
        self.recover_expired(workspace=root)
        with self._transaction():
            now = self._clock()
            row = self._conn.execute(
                """
                SELECT runs.* FROM automation_runs AS runs
                JOIN automation_jobs AS jobs ON jobs.job_id = runs.job_id
                WHERE runs.run_id = ? AND (? IS NULL OR jobs.workspace = ?)
                """,
                (identifier, root, root),
            ).fetchone()
            if row is None:
                raise AutomationError(f"automation run not found: {identifier}")
            if str(row["status"]) != "running":
                raise AutomationError(f"automation run {identifier!r} is not running")
            if not bool(row["cancel_requested"]):
                self._conn.execute(
                    "UPDATE automation_runs SET cancel_requested = 1 WHERE run_id = ?",
                    (identifier,),
                )
                self._record_event_locked(
                    "automation.run.cancel_requested",
                    job_id=str(row["job_id"]),
                    run_id=identifier,
                    now=now,
                )
        return self._required_run(identifier)

    def cancel_requested(self, run_id: str, token: str) -> bool:
        identifier = _identifier(run_id, "run id")
        with self._lock:
            now = self._clock()
            row = self._owned_run_locked(identifier, token, now)
            return bool(row["cancel_requested"])

    def finish_run(
        self,
        run_id: str,
        token: str,
        *,
        status: Literal["succeeded", "failed", "cancelled"],
        session_id: str | None = None,
        response: str | None = None,
        error: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cost_usd: float = 0.0,
        usage_source: UsageSource = "unavailable",
        estimated_prompt_tokens: int = 0,
        estimated_completion_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> AutomationRun:
        identifier = _identifier(run_id, "run id")
        token_values = (
            prompt_tokens,
            completion_tokens,
            cache_read_tokens,
            cache_write_tokens,
            estimated_prompt_tokens,
            estimated_completion_tokens,
        )
        if any(value < 0 for value in token_values) or not all(
            math.isfinite(value) and value >= 0
            for value in (cost_usd, estimated_cost_usd)
        ):
            raise ValueError("usage values must be non-negative")
        if usage_source not in {"unavailable", "provider", "estimated", "mixed"}:
            raise ValueError("usage_source is invalid")
        normalized_response = _optional_bounded_text(
            response, MAX_RESPONSE_BYTES, middle=True
        )
        normalized_error = _optional_bounded_text(error, MAX_ERROR_BYTES)
        with self._transaction():
            now = self._clock()
            row = self._owned_run_locked(identifier, token, now)
            job_id = str(row["job_id"])
            if bool(row["cancel_requested"]) and status != "cancelled":
                status = "cancelled"
                normalized_response = None
                normalized_error = (
                    normalized_error
                    or "automation cancellation was requested before completion"
                )
            self._conn.execute(
                """
                UPDATE automation_runs
                SET status = ?, session_id = ?, response = ?, error = ?,
                    prompt_tokens = ?, completion_tokens = ?,
                    cache_read_tokens = ?, cache_write_tokens = ?, cost_usd = ?,
                    usage_source = ?, estimated_prompt_tokens = ?,
                    estimated_completion_tokens = ?, estimated_cost_usd = ?,
                    finished_at = ?, lease_token_hash = NULL, lease_expires_at = NULL
                WHERE run_id = ?
                """,
                (
                    status,
                    session_id,
                    normalized_response,
                    normalized_error,
                    int(prompt_tokens),
                    int(completion_tokens),
                    int(cache_read_tokens),
                    int(cache_write_tokens),
                    float(cost_usd),
                    usage_source,
                    int(estimated_prompt_tokens),
                    int(estimated_completion_tokens),
                    float(estimated_cost_usd),
                    now,
                    identifier,
                ),
            )
            failure = status == "failed"
            self._conn.execute(
                """
                UPDATE automation_jobs
                SET last_run_at = ?, last_run_status = ?, last_error = ?,
                    consecutive_failures = CASE WHEN ? THEN consecutive_failures + 1 ELSE 0 END,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (now, status, normalized_error, int(failure), now, job_id),
            )
            self._record_event_locked(
                f"automation.run.{status}",
                job_id=job_id,
                run_id=identifier,
                now=now,
                session_id=session_id,
                error=normalized_error,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                cost_usd=cost_usd,
                usage_source=usage_source,
                estimated_prompt_tokens=estimated_prompt_tokens,
                estimated_completion_tokens=estimated_completion_tokens,
                estimated_cost_usd=estimated_cost_usd,
            )
        return self._required_run(identifier)

    def interrupt_run(self, run_id: str, token: str, *, error: str) -> AutomationRun:
        """Abandon an owned run whose execution outcome cannot be proven."""

        identifier = _identifier(run_id, "run id")
        normalized_error = _optional_bounded_text(error, MAX_ERROR_BYTES)
        if normalized_error is None:
            raise ValueError("interrupted run error must be non-empty")
        with self._transaction():
            now = self._clock()
            row = self._conn.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?", (identifier,)
            ).fetchone()
            if row is None:
                raise AutomationError(f"automation run not found: {identifier}")
            if str(row["status"]) != "running":
                raise AutomationError(f"automation run {identifier!r} is not running")
            if not secrets.compare_digest(
                str(row["lease_token_hash"] or ""), _token_hash(token)
            ):
                raise AutomationError(
                    f"automation run {identifier!r} belongs to another lease"
                )
            job_id = str(row["job_id"])
            self._conn.execute(
                """
                UPDATE automation_runs
                SET status = 'interrupted', error = ?, finished_at = ?,
                    lease_token_hash = NULL, lease_expires_at = NULL
                WHERE run_id = ? AND status = 'running'
                """,
                (normalized_error, now, identifier),
            )
            self._conn.execute(
                """
                UPDATE automation_jobs
                SET last_run_at = ?, last_run_status = 'interrupted', last_error = ?,
                    consecutive_failures = consecutive_failures + 1, updated_at = ?
                WHERE job_id = ?
                """,
                (now, normalized_error, now, job_id),
            )
            self._record_event_locked(
                "automation.run.interrupted",
                job_id=job_id,
                run_id=identifier,
                now=now,
                reason="worker_execution_abandoned",
                error=normalized_error,
            )
        return self._required_run(identifier)

    def get_run(self, run_id: str) -> AutomationRun | None:
        identifier = _identifier(run_id, "run id")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?", (identifier,)
            ).fetchone()
        return _run_from_row(row) if row is not None else None

    def list_runs(
        self,
        *,
        workspace: Path | str,
        job_id: str | None = None,
        limit: int = 100,
    ) -> list[AutomationRun]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        root = _workspace(workspace)
        params: list[Any] = [root]
        job_clause = ""
        if job_id is not None:
            job_clause = " AND runs.job_id = ?"
            params.append(_identifier(job_id, "job id"))
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT runs.* FROM automation_runs AS runs
                JOIN automation_jobs AS jobs ON jobs.job_id = runs.job_id
                WHERE jobs.workspace = ?
                """
                + job_clause
                + " ORDER BY runs.created_at DESC, runs.run_id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def recover_expired(
        self, *, workspace: Path | str | None = None
    ) -> list[str]:
        root = _workspace(workspace) if workspace is not None else None
        with self._transaction():
            now = self._clock()
            return self._recover_expired_locked(now, workspace=root)

    def heartbeat_worker(
        self,
        *,
        worker_id: str,
        workspace: Path | str,
        pid: int,
        max_concurrent_runs: int,
    ) -> AutomationWorker:
        identifier = _identifier(worker_id, "worker id")
        root = _workspace(workspace)
        if pid < 1:
            raise ValueError("worker pid must be positive")
        if not 1 <= max_concurrent_runs <= 32:
            raise ValueError("max_concurrent_runs must be between 1 and 32")
        with self._transaction():
            now = self._clock()
            self._conn.execute(
                """
                INSERT INTO automation_workers (
                    worker_id, workspace, pid, started_at, heartbeat_at, max_concurrent_runs
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    workspace = excluded.workspace,
                    pid = excluded.pid,
                    heartbeat_at = excluded.heartbeat_at,
                    max_concurrent_runs = excluded.max_concurrent_runs
                """,
                (identifier, root, pid, now, now, max_concurrent_runs),
            )
        worker = self.get_worker(identifier)
        assert worker is not None
        return worker

    def remove_worker(self, worker_id: str) -> None:
        identifier = _identifier(worker_id, "worker id")
        with self._transaction():
            self._conn.execute(
                "DELETE FROM automation_workers WHERE worker_id = ?", (identifier,)
            )

    def get_worker(self, worker_id: str) -> AutomationWorker | None:
        identifier = _identifier(worker_id, "worker id")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM automation_workers WHERE worker_id = ?", (identifier,)
            ).fetchone()
        return _worker_from_row(row) if row is not None else None

    def list_workers(
        self,
        workspace: Path | str,
        *,
        stale_after_seconds: float = 30.0,
    ) -> list[AutomationWorker]:
        root = _workspace(workspace)
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        cutoff = self._clock() - float(stale_after_seconds)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM automation_workers
                WHERE workspace = ? AND heartbeat_at >= ?
                ORDER BY started_at, worker_id
                """,
                (root, cutoff),
            ).fetchall()
        return [_worker_from_row(row) for row in rows]

    def job_counts(self, workspace: Path | str) -> tuple[int, int]:
        root = _workspace(workspace)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END), 0)
                           AS enabled
                FROM automation_jobs
                WHERE workspace = ? AND deleted_at IS NULL
                """,
                (root,),
            ).fetchone()
        assert row is not None
        return int(row["total"]), int(row["enabled"])

    def count_running_runs(self, workspace: Path | str) -> int:
        root = _workspace(workspace)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS running FROM automation_runs AS runs
                JOIN automation_jobs AS jobs ON jobs.job_id = runs.job_id
                WHERE jobs.workspace = ? AND runs.status = 'running'
                """,
                (root,),
            ).fetchone()
        assert row is not None
        return int(row["running"])

    def prune_runs(
        self,
        *,
        workspace: Path | str,
        older_than_days: int = 30,
    ) -> int:
        if not 1 <= older_than_days <= 3650:
            raise ValueError("older_than_days must be between 1 and 3650")
        root = _workspace(workspace)
        with self._transaction():
            cutoff = self._clock() - older_than_days * 86400
            # Keep lifecycle events as an audit ledger after bulky run output expires.
            # The explicit unlink also supports databases created by pre-release builds
            # whose foreign key did not yet include ON DELETE SET NULL.
            self._conn.execute(
                """
                UPDATE automation_events SET run_id = NULL
                WHERE run_id IN (
                    SELECT run_id FROM automation_runs
                    WHERE job_id IN (
                            SELECT job_id FROM automation_jobs WHERE workspace = ?
                        )
                      AND status <> 'running' AND finished_at IS NOT NULL
                      AND finished_at < ?
                )
                """,
                (root, cutoff),
            )
            cursor = self._conn.execute(
                """
                DELETE FROM automation_runs
                WHERE job_id IN (
                        SELECT job_id FROM automation_jobs WHERE workspace = ?
                    )
                  AND status <> 'running' AND finished_at IS NOT NULL AND finished_at < ?
                """,
                (root, cutoff),
            )
            return int(cursor.rowcount)

    def _claim_job_locked(
        self,
        job: AutomationJob,
        *,
        scheduled_for: float,
        trigger: Literal["scheduled", "manual"],
        worker_id: str,
        lease_seconds: float,
        now: float,
        advance_schedule: bool,
        run_id: str | None = None,
    ) -> str | None:
        identifier = run_id or self._scheduled_run_id(job.job_id, scheduled_for)
        token = secrets.token_urlsafe(32)
        active = self._conn.execute(
            "SELECT 1 FROM automation_runs WHERE job_id = ? AND status = 'running'",
            (job.job_id,),
        ).fetchone()
        if active is not None:
            return None
        try:
            self._conn.execute(
                """
                INSERT INTO automation_runs (
                    run_id, job_id, scheduled_for, trigger, status, worker_id,
                    lease_token_hash, lease_expires_at, created_at, started_at
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    job.job_id,
                    scheduled_for,
                    trigger,
                    worker_id,
                    _token_hash(token),
                    now + lease_seconds,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            return None
        if advance_schedule:
            next_run = next_fire_time(
                job.schedule,
                previous=_from_epoch(scheduled_for),
                now=_from_epoch(now),
            )
            enabled = next_run is not None
            self._conn.execute(
                """
                UPDATE automation_jobs
                SET enabled = ?, next_run_at = ?, last_run_status = 'running',
                    last_error = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (int(enabled), _to_epoch(next_run), now, job.job_id),
            )
        self._record_event_locked(
            "automation.run.started",
            job_id=job.job_id,
            run_id=identifier,
            now=now,
            scheduled_for=_from_epoch(scheduled_for).isoformat(),
            trigger=trigger,
            worker_id=worker_id,
        )
        return token

    def _record_skipped_locked(
        self, job: AutomationJob, scheduled_for: float, now: float
    ) -> None:
        run_id = self._scheduled_run_id(job.job_id, scheduled_for)
        next_run = next_fire_time(
            job.schedule,
            previous=_from_epoch(scheduled_for),
            now=_from_epoch(now),
        )
        enabled = next_run is not None
        inserted = self._conn.execute(
            """
            INSERT OR IGNORE INTO automation_runs (
                run_id, job_id, scheduled_for, trigger, status, error,
                created_at, finished_at
            ) VALUES (?, ?, ?, 'scheduled', 'skipped', ?, ?, ?)
            """,
            (
                run_id,
                job.job_id,
                scheduled_for,
                "missed schedule exceeded its grace period",
                now,
                now,
            ),
        )
        self._conn.execute(
            """
            UPDATE automation_jobs
            SET enabled = ?, next_run_at = ?, last_run_at = ?,
                last_run_status = 'skipped', last_error = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (
                int(enabled),
                _to_epoch(next_run),
                now,
                "missed schedule exceeded its grace period",
                now,
                job.job_id,
            ),
        )
        if inserted.rowcount:
            self._record_event_locked(
                "automation.run.skipped",
                job_id=job.job_id,
                run_id=run_id,
                now=now,
                scheduled_for=_from_epoch(scheduled_for).isoformat(),
                reason="misfire_grace_exceeded",
            )

    def _recover_expired_locked(
        self, now: float, *, workspace: str | None = None
    ) -> list[str]:
        workspace_clause = " AND jobs.workspace = ?" if workspace is not None else ""
        parameters: tuple[Any, ...] = (
            (now, workspace) if workspace is not None else (now,)
        )
        rows = self._conn.execute(
            """
            SELECT runs.* FROM automation_runs AS runs
            JOIN automation_jobs AS jobs ON jobs.job_id = runs.job_id
            WHERE runs.status = 'running' AND runs.lease_expires_at IS NOT NULL
              AND runs.lease_expires_at <= ?
            """
            + workspace_clause
            + " ORDER BY runs.lease_expires_at, runs.run_id",
            parameters,
        ).fetchall()
        recovered: list[str] = []
        for row in rows:
            run_id = str(row["run_id"])
            job_id = str(row["job_id"])
            error = "automation worker lease expired; run outcome is ambiguous"
            self._conn.execute(
                """
                UPDATE automation_runs
                SET status = 'interrupted', error = ?, finished_at = ?,
                    lease_token_hash = NULL, lease_expires_at = NULL
                WHERE run_id = ? AND status = 'running'
                """,
                (error, now, run_id),
            )
            self._conn.execute(
                """
                UPDATE automation_jobs
                SET last_run_at = ?, last_run_status = 'interrupted', last_error = ?,
                    consecutive_failures = consecutive_failures + 1, updated_at = ?
                WHERE job_id = ?
                """,
                (now, error, now, job_id),
            )
            self._record_event_locked(
                "automation.run.interrupted",
                job_id=job_id,
                run_id=run_id,
                now=now,
                reason="lease_expired",
            )
            recovered.append(run_id)
        return recovered

    def _owned_run_locked(self, run_id: str, token: str, now: float) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM automation_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise AutomationError(f"automation run not found: {run_id}")
        if str(row["status"]) != "running":
            raise AutomationError(f"automation run {run_id!r} is not running")
        if row["lease_expires_at"] is None or float(row["lease_expires_at"]) <= now:
            raise AutomationError(f"automation run {run_id!r} lease expired")
        if not secrets.compare_digest(
            str(row["lease_token_hash"] or ""), _token_hash(token)
        ):
            raise AutomationError(f"automation run {run_id!r} belongs to another lease")
        return row

    def _required_job(self, reference: str, *, workspace: Path | str) -> AutomationJob:
        job = self.get_job(reference, workspace=workspace)
        if job is None:
            raise AutomationError(f"automation not found: {reference}")
        return job

    def _required_run(self, run_id: str) -> AutomationRun:
        run = self.get_run(run_id)
        if run is None:  # pragma: no cover - internal invariant
            raise AutomationError(f"automation run not found: {run_id}")
        return run

    def _lease_from_ids(self, run_id: str, token: str) -> AutomationRunLease:
        run = self._required_run(run_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT workspace FROM automation_jobs WHERE job_id = ?",
                (run.job_id,),
            ).fetchone()
        assert row is not None
        job = self._required_job(run.job_id, workspace=str(row["workspace"]))
        return AutomationRunLease(job=job, run=run, token=token)

    def _scheduled_run_id(self, job_id: str, scheduled_for: float) -> str:
        key = f"ash-automation:{job_id}:{scheduled_for:.6f}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, key))

    def _record_event_locked(
        self,
        event_type: str,
        *,
        job_id: str | None,
        run_id: str | None,
        now: float,
        **payload: Any,
    ) -> None:
        event = envelope_event(
            {"type": event_type, "job_id": job_id, "run_id": run_id, **payload},
            context=EventContext(operation_id=run_id or job_id),
            source={"type": "automation", "id": job_id or "scheduler"},
            timestamp_factory=lambda: _from_epoch(now).isoformat(),
        )
        encoded = json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            raise AutomationError("automation event exceeds storage limit")
        self._conn.execute(
            """
            INSERT INTO automation_events (
                event_id, job_id, run_id, event_type, event_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event["event_id"], job_id, run_id, event_type, encoded, now),
        )

    def _transaction(self) -> "_ImmediateTransaction":
        return _ImmediateTransaction(self)


class _ImmediateTransaction:
    def __init__(self, store: AutomationStore) -> None:
        self.store = store

    def __enter__(self) -> None:
        self.store._lock.acquire()
        if self.store._closed:
            self.store._lock.release()
            raise AutomationError("automation store is closed")
        try:
            self.store._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            self.store._lock.release()
            raise AutomationError(
                f"cannot begin automation transaction: {exc}"
            ) from exc

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            action = "ROLLBACK" if exc_type else "COMMIT"
            try:
                self.store._conn.execute(action)
            except sqlite3.Error as db_error:
                raise AutomationError(
                    f"cannot {action.casefold()} automation transaction: {db_error}"
                ) from db_error
        finally:
            self.store._lock.release()


def _job_from_row(row: sqlite3.Row) -> AutomationJob:
    anchor = _optional_from_epoch(row["schedule_anchor_at"])
    return AutomationJob(
        job_id=str(row["job_id"]),
        name=str(row["name"]),
        prompt=str(row["prompt"]),
        workspace=str(row["workspace"]),
        schedule=ScheduleSpec(
            kind=str(row["schedule_kind"]),  # type: ignore[arg-type]
            value=str(row["schedule_value"]),
            timezone=str(row["schedule_timezone"]),
            anchor_at=anchor,
        ),
        enabled=bool(row["enabled"]),
        next_run_at=_optional_from_epoch(row["next_run_at"]),
        misfire_grace_seconds=int(row["misfire_grace_seconds"]),
        timeout_seconds=float(row["timeout_seconds"]),
        token_budget=int(row["token_budget"]),
        created_at=_from_epoch(float(row["created_at"])),
        updated_at=_from_epoch(float(row["updated_at"])),
        last_run_at=_optional_from_epoch(row["last_run_at"]),
        last_run_status=cast(
            AutomationRunStatus | None,
            str(row["last_run_status"]) if row["last_run_status"] else None,
        ),
        last_error=str(row["last_error"]) if row["last_error"] else None,
        consecutive_failures=int(row["consecutive_failures"]),
    )


def _run_from_row(row: sqlite3.Row) -> AutomationRun:
    return AutomationRun(
        run_id=str(row["run_id"]),
        job_id=str(row["job_id"]),
        scheduled_for=_from_epoch(float(row["scheduled_for"])),
        status=str(row["status"]),  # type: ignore[arg-type]
        attempt=int(row["attempt"]),
        created_at=_from_epoch(float(row["created_at"])),
        started_at=_optional_from_epoch(row["started_at"]),
        finished_at=_optional_from_epoch(row["finished_at"]),
        worker_id=str(row["worker_id"]) if row["worker_id"] else None,
        lease_expires_at=_optional_from_epoch(row["lease_expires_at"]),
        cancel_requested=bool(row["cancel_requested"]),
        session_id=str(row["session_id"]) if row["session_id"] else None,
        response=str(row["response"]) if row["response"] else None,
        error=str(row["error"]) if row["error"] else None,
        prompt_tokens=int(row["prompt_tokens"]),
        completion_tokens=int(row["completion_tokens"]),
        cache_read_tokens=int(row["cache_read_tokens"]),
        cache_write_tokens=int(row["cache_write_tokens"]),
        cost_usd=float(row["cost_usd"]),
        usage_source=cast(UsageSource, str(row["usage_source"])),
        estimated_prompt_tokens=int(row["estimated_prompt_tokens"]),
        estimated_completion_tokens=int(row["estimated_completion_tokens"]),
        estimated_cost_usd=float(row["estimated_cost_usd"]),
        trigger=str(row["trigger"]),  # type: ignore[arg-type]
    )


def _worker_from_row(row: sqlite3.Row) -> AutomationWorker:
    return AutomationWorker(
        worker_id=str(row["worker_id"]),
        workspace=str(row["workspace"]),
        pid=int(row["pid"]),
        started_at=_from_epoch(float(row["started_at"])),
        heartbeat_at=_from_epoch(float(row["heartbeat_at"])),
        max_concurrent_runs=int(row["max_concurrent_runs"]),
    )


def _workspace(value: Path | str) -> str:
    return str(Path(value).expanduser().resolve())


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(
            f"{label} must be a portable identifier of at most 128 characters"
        )
    if not all(character.isalnum() or character in "._:-" for character in value):
        raise ValueError(
            f"{label} must be a portable identifier of at most 128 characters"
        )
    if not value[0].isalnum():
        raise ValueError(
            f"{label} must be a portable identifier of at most 128 characters"
        )
    return value


def _lease_seconds(value: float) -> float:
    duration = float(value)
    if not 5 <= duration <= 3600:
        raise ValueError("lease_seconds must be between 5 and 3600")
    return duration


def _token_hash(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise AutomationError("lease token is invalid")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    normalized = value.strip()
    if len(normalized.encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds {maximum} bytes")
    return normalized


def _optional_bounded_text(
    value: str | None, maximum: int, *, middle: bool = False
) -> str | None:
    if value is None:
        return None
    normalized = redact_text(str(value)).strip()
    encoded = normalized.encode("utf-8")
    if len(encoded) <= maximum:
        return normalized
    if middle:
        half = maximum // 2
        return (
            encoded[:half].decode("utf-8", errors="ignore")
            + "\n...[automation output truncated]...\n"
            + encoded[-half:].decode("utf-8", errors="ignore")
        )
    return encoded[:maximum].decode("utf-8", errors="ignore")


def _from_epoch(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _optional_from_epoch(value: Any) -> datetime | None:
    return _from_epoch(float(value)) if value is not None else None


def _to_epoch(value: datetime | None) -> float | None:
    return value.astimezone(timezone.utc).timestamp() if value is not None else None
