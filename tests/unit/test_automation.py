from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ash.automation.schedules import (
    build_schedule,
    first_fire_time,
    next_fire_time,
    parse_duration,
)
from ash.automation.models import AutomationWorkerSummary
from ash.automation.runner import _execute as execute_automation_subprocess_request
from ash.automation.store import (
    AUTOMATION_SCHEMA_VERSION,
    AutomationError,
    AutomationStore,
)
from ash.automation.worker import AutomationWorkerService
from ash.commands.automation import automation_config_loader
from ash.config import AshConfig
from ash.safety.guard import SafetyGuard
from ash.safety.policy import PermissionPolicy, PolicyAction
from ash.sdk import AshResult
from ash.tools.automation import ListAutomationsTool, ManageAutomationTool


def test_schedule_validation_and_named_timezone() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert parse_duration("1h30m") == 5400
    interval = build_schedule(every="30m", now=now)
    assert first_fire_time(interval, now=now) == now + timedelta(minutes=30)

    cron = build_schedule(
        cron="30 9 * * mon-fri",
        timezone_name="Asia/Kolkata",
        now=now,
    )
    assert first_fire_time(cron, now=now) == datetime(
        2026, 1, 1, 4, 0, tzinfo=timezone.utc
    )

    with pytest.raises(ValueError, match="exactly one"):
        build_schedule(every="1h", cron="0 * * * *", now=now)
    with pytest.raises(ValueError, match="day-of-week"):
        build_schedule(cron="0 9 * * 1", now=now)
    with pytest.raises(ValueError, match="future"):
        build_schedule(at=now.isoformat(), now=now)


@pytest.mark.asyncio
async def test_subprocess_runner_rechecks_workspace_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    config = AshConfig(workspace_root=workspace)
    monkeypatch.setattr("ash.automation.runner.is_workspace_trusted", lambda path: False)

    with pytest.raises(ValueError, match="not trusted"):
        await execute_automation_subprocess_request(
            {
                "config": config.model_dump(mode="json"),
                "workspace": str(workspace),
                "prompt": "Do not run",
                "user_metadata": None,
            }
        )


def test_cron_trigger_handles_spring_forward_and_fall_back() -> None:
    before_transition = datetime(2026, 3, 28, 12, tzinfo=timezone.utc)
    schedule = build_schedule(
        cron="30 2 * * sun",
        timezone_name="Europe/Berlin",
        now=before_transition,
    )
    first = first_fire_time(schedule, now=before_transition)
    second = next_fire_time(schedule, previous=first, now=first)

    # The nonexistent 02:30 local occurrence advances to the first valid time.
    assert first == datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)
    assert second == datetime(2026, 4, 5, 0, 30, tzinfo=timezone.utc)

    before_fall_back = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    fall_back = build_schedule(
        cron="30 2 * * sun",
        timezone_name="Europe/Berlin",
        now=before_fall_back,
    )
    first_fold = first_fire_time(fall_back, now=before_fall_back)
    second_fold = next_fire_time(fall_back, previous=first_fold, now=first_fold)
    assert second_fold is not None
    following_week = next_fire_time(fall_back, previous=second_fold, now=second_fold)

    assert first_fold == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    assert second_fold == datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
    assert following_week == datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc)


def test_daily_midnight_cron_does_not_skip_spring_dst_day() -> None:
    zone = "America/Los_Angeles"
    before = datetime(2026, 3, 7, 12, tzinfo=timezone.utc)
    schedule = build_schedule(cron="0 0 * * *", timezone_name=zone, now=before)
    march_eighth = first_fire_time(schedule, now=before)
    march_ninth = next_fire_time(schedule, previous=march_eighth, now=march_eighth)

    assert march_eighth == datetime(2026, 3, 8, 8, tzinfo=timezone.utc)
    assert march_ninth == datetime(2026, 3, 9, 7, tzinfo=timezone.utc)


@pytest.fixture
def clock() -> list[float]:
    return [datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()]


@pytest.fixture
def store(tmp_path: Path, clock: list[float]) -> AutomationStore:
    value = AutomationStore(tmp_path / "automation.db", clock=lambda: clock[0])
    yield value
    value.close()


def test_due_claim_is_atomic_advances_and_completes(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="hourly review",
        prompt="Review the repository",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
    )
    assert job.next_run_at == now + timedelta(hours=1)

    clock[0] += 3600
    second_store = AutomationStore(store.db_path, clock=lambda: clock[0])
    try:
        first_claim = store.claim_due(
            workspace=workspace, worker_id="worker-a", lease_seconds=30
        )
        second_claim = second_store.claim_due(
            workspace=workspace, worker_id="worker-b", lease_seconds=30
        )
    finally:
        second_store.close()

    assert len(first_claim) == 1
    assert second_claim == []
    claim = first_claim[0]
    assert claim.run.status == "running"
    advanced = store.get_job(job.job_id, workspace=workspace)
    assert advanced is not None
    assert advanced.next_run_at == now + timedelta(hours=2)

    completed = store.finish_run(
        claim.run.run_id,
        claim.token,
        status="succeeded",
        session_id="session-1",
        response="done",
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.01,
    )
    assert completed.status == "succeeded"
    assert completed.session_id == "session-1"
    assert store.get_job(job.job_id, workspace=workspace).consecutive_failures == 0


def test_store_claims_both_fall_back_occurrences_without_stalling(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    clock[0] = datetime(2026, 10, 24, 12, tzinfo=timezone.utc).timestamp()
    job = store.create_job(
        name="fall back",
        prompt="Run at both real instants",
        workspace=workspace,
        schedule=build_schedule(
            cron="30 2 * * sun",
            timezone_name="Europe/Berlin",
            now=datetime.fromtimestamp(clock[0], tz=timezone.utc),
        ),
    )

    clock[0] = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).timestamp()
    first = store.claim_due(workspace=workspace, worker_id="worker")[0]
    after_first = store.get_job(job.job_id, workspace=workspace)
    assert after_first is not None
    assert after_first.next_run_at == datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
    store.finish_run(first.run.run_id, first.token, status="succeeded")

    clock[0] = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc).timestamp()
    second = store.claim_due(workspace=workspace, worker_id="worker")[0]
    assert second.run.run_id != first.run.run_id
    after_second = store.get_job(job.job_id, workspace=workspace)
    assert after_second is not None
    assert after_second.next_run_at == datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc)


def test_expired_one_shot_is_interrupted_and_not_replayed(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="one shot",
        prompt="Run once",
        workspace=workspace,
        schedule=build_schedule(at=(now + timedelta(seconds=20)).isoformat(), now=now),
    )
    clock[0] += 20
    claim = store.claim_due(workspace=workspace, worker_id="worker-a", lease_seconds=5)[
        0
    ]
    assert store.get_job(job.job_id, workspace=workspace).enabled is False

    clock[0] += 6
    assert store.recover_expired() == [claim.run.run_id]
    recovered = store.get_run(claim.run.run_id)
    assert recovered is not None
    assert recovered.status == "interrupted"
    assert "ambiguous" in (recovered.error or "")
    assert store.claim_due(workspace=workspace, worker_id="worker-b") == []


@pytest.mark.asyncio
async def test_misfire_is_skipped_and_recurring_schedule_coalesces(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="frequent",
        prompt="Check state",
        workspace=workspace,
        schedule=build_schedule(every="10s", now=now),
        misfire_grace_seconds=5,
    )
    clock[0] += 60

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(
        store,
        workspace,
        config=AshConfig(workspace_root=workspace),
        worker_id="worker",
    )
    summary = await asyncio.wait_for(worker.run_forever(once=True), timeout=2)

    assert summary.completed == 1
    assert summary.skipped == 1
    assert summary.ok is True
    runs = store.list_runs(workspace=workspace, job_id=job.job_id)
    assert len(runs) == 1
    assert runs[0].status == "skipped"
    updated = store.get_job(job.job_id, workspace=workspace)
    assert updated is not None
    assert updated.next_run_at == now + timedelta(seconds=70)


@pytest.mark.asyncio
async def test_worker_reports_only_its_workspace_expired_leases(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    first = store.create_job(
        name="first crashed",
        prompt="Crash",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    second = store.create_job(
        name="second crashed",
        prompt="Crash",
        workspace=other_workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    first_claim = store.claim_manual(
        first.job_id, workspace=workspace, worker_id="dead-a", lease_seconds=5
    )
    second_claim = store.claim_manual(
        second.job_id,
        workspace=other_workspace,
        worker_id="dead-b",
        lease_seconds=5,
    )
    clock[0] += 6

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(
        store,
        workspace,
        config=AshConfig(workspace_root=workspace),
    )
    summary = await worker.run_forever(once=True)

    assert summary.completed == 1
    assert summary.interrupted == 1
    assert summary.ok is False
    assert store.get_run(first_claim.run.run_id).status == "interrupted"
    assert store.get_run(second_claim.run.run_id).status == "running"
    assert store.recover_expired(workspace=other_workspace) == [
        second_claim.run.run_id
    ]


@pytest.mark.asyncio
async def test_once_worker_scans_past_large_misfire_prefix_to_fill_capacity(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    initial_time = clock[0]
    now = datetime.fromtimestamp(initial_time, tz=timezone.utc)
    for index in range(4):
        store.create_job(
            name=f"stale-{index}",
            prompt="Skip",
            workspace=workspace,
            schedule=build_schedule(every="10s", now=now),
            misfire_grace_seconds=0,
        )
        clock[0] += 0.001
    eligible = store.create_job(
        name="eligible after stale prefix",
        prompt="Run",
        workspace=workspace,
        schedule=build_schedule(every="10s", now=now),
        misfire_grace_seconds=60,
    )
    clock[0] = initial_time + 20

    async def factory(config, root):
        return _FakeClient()

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(
        store,
        workspace,
        max_concurrent_runs=1,
        config=AshConfig(workspace_root=workspace),
        client_factory=factory,
    )
    summary = await worker.run_forever(once=True)

    assert summary.completed == 5
    assert summary.skipped == 4
    assert summary.succeeded == 1
    assert store.list_runs(workspace=workspace, job_id=eligible.job_id)[0].status == (
        "succeeded"
    )


def test_manual_run_cancel_and_failure_state(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="manual",
        prompt="Run manually",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )

    claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="operator", lease_seconds=30
    )
    cancelled = store.request_cancel(claim.run.run_id)
    assert cancelled.cancel_requested is True
    assert store.cancel_requested(claim.run.run_id, claim.token) is True
    result = store.finish_run(
        claim.run.run_id,
        claim.token,
        status="cancelled",
        error="cancelled by operator",
    )
    assert result.status == "cancelled"
    assert store.get_job(job.job_id, workspace=workspace).consecutive_failures == 0

    with pytest.raises(AutomationError, match="not running"):
        store.finish_run(claim.run.run_id, claim.token, status="failed")
    with pytest.raises(AutomationError, match="not running"):
        store.request_cancel(claim.run.run_id, workspace=workspace)

    second_claim = store.claim_manual(
        job.job_id,
        workspace=workspace,
        worker_id="operator",
        lease_seconds=30,
    )
    assert second_claim.run.run_id != claim.run.run_id
    assert second_claim.run.scheduled_for == claim.run.scheduled_for
    assert store.renew_lease(second_claim.run.run_id, second_claim.token).status == (
        "running"
    )


def test_one_shot_cannot_be_resumed_after_its_fire_time(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="one shot pause",
        prompt="Run once",
        workspace=workspace,
        schedule=build_schedule(at=(now + timedelta(seconds=20)).isoformat(), now=now),
    )
    store.set_enabled(job.job_id, workspace=workspace, enabled=False)
    clock[0] += 21

    with pytest.raises(AutomationError, match="cannot be resumed"):
        store.set_enabled(job.job_id, workspace=workspace, enabled=True)


def test_cancellation_is_scoped_to_the_workspace(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="private run",
        prompt="Run privately",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="worker", lease_seconds=30
    )

    with pytest.raises(AutomationError, match="not found"):
        store.request_cancel(claim.run.run_id, workspace=other_workspace)
    assert store.get_run(claim.run.run_id).cancel_requested is False


def test_run_retention_preserves_event_ledger(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="retained audit",
        prompt="Finish",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="worker", lease_seconds=30
    )
    store.finish_run(claim.run.run_id, claim.token, status="succeeded")
    clock[0] += 2 * 86400

    assert store.prune_runs(workspace=workspace, older_than_days=1) == 1
    assert store.get_run(claim.run.run_id) is None
    with sqlite3.connect(store.db_path) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM automation_events WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()[0]
        linked_count = connection.execute(
            "SELECT COUNT(*) FROM automation_events WHERE run_id = ?",
            (claim.run.run_id,),
        ).fetchone()[0]
    assert event_count >= 3
    assert linked_count == 0


def test_retention_is_scoped_to_one_workspace(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    run_ids: list[str] = []
    for index, workspace in enumerate((first_workspace, second_workspace)):
        job = store.create_job(
            name=f"retention-{index}",
            prompt="Finish",
            workspace=workspace,
            schedule=build_schedule(every="1h", now=now),
            enabled=False,
        )
        claim = store.claim_manual(
            job.job_id, workspace=workspace, worker_id=f"worker-{index}"
        )
        store.finish_run(claim.run.run_id, claim.token, status="succeeded")
        run_ids.append(claim.run.run_id)
    clock[0] += 2 * 86400

    assert store.prune_runs(workspace=first_workspace, older_than_days=1) == 1
    assert store.get_run(run_ids[0]) is None
    assert store.get_run(run_ids[1]) is not None


def test_expired_run_is_recovered_before_cancel_or_remove(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="crashed",
        prompt="Crash",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="crashed-worker", lease_seconds=5
    )
    clock[0] += 6

    with pytest.raises(AutomationError, match="not running"):
        store.request_cancel(claim.run.run_id, workspace=workspace)
    assert store.get_run(claim.run.run_id).status == "interrupted"
    assert store.remove_job(job.job_id, workspace=workspace).job_id == job.job_id


def test_finish_samples_lease_time_after_transaction_lock(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="lock wait",
        prompt="Wait for lock",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="worker", lease_seconds=5
    )
    original_transaction = store._transaction

    @contextmanager
    def delayed_transaction():
        with original_transaction():
            clock[0] += 6
            yield

    monkeypatch.setattr(store, "_transaction", delayed_transaction)
    with pytest.raises(AutomationError, match="lease expired"):
        store.finish_run(claim.run.run_id, claim.token, status="succeeded")
    assert store.get_run(claim.run.run_id).status == "running"


def test_running_count_is_not_limited_by_history_window(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    active_job = store.create_job(
        name="old active",
        prompt="Stay active",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    store.claim_manual(
        active_job.job_id, workspace=workspace, worker_id="active-worker"
    )
    completed_job = store.create_job(
        name="many completed",
        prompt="Finish",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    for index in range(101):
        clock[0] += 0.001
        claim = store.claim_manual(
            completed_job.job_id,
            workspace=workspace,
            worker_id=f"history-worker-{index}",
        )
        store.finish_run(claim.run.run_id, claim.token, status="succeeded")

    assert all(run.status != "running" for run in store.list_runs(workspace=workspace))
    assert store.count_running_runs(workspace) == 1
    assert store.job_counts(workspace) == (2, 0)


def test_store_refuses_future_schema_version(tmp_path: Path) -> None:
    database = tmp_path / "future.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 999")

    with pytest.raises(AutomationError, match="newer than supported"):
        AutomationStore(database)


def test_store_migrates_true_v1_run_rows_to_v2(tmp_path: Path) -> None:
    database = tmp_path / "legacy-v1.db"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    with AutomationStore(database) as initial:
        job = initial.create_job(
            name="legacy run",
            prompt="Finish",
            workspace=workspace,
            schedule=build_schedule(every="1h"),
            enabled=False,
        )
        claim = initial.claim_manual(
            job.job_id, workspace=workspace, worker_id="legacy-worker"
        )
        initial.finish_run(
            claim.run.run_id,
            claim.token,
            status="succeeded",
            prompt_tokens=7,
            completion_tokens=3,
        )

    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=OFF;
            DROP TABLE automation_events;
            ALTER TABLE automation_runs RENAME TO automation_runs_v2;
            CREATE TABLE automation_runs (
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
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                session_id TEXT,
                response TEXT,
                error TEXT,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            );
            INSERT INTO automation_runs
            SELECT run_id, job_id, scheduled_for, trigger, status, attempt,
                   worker_id, lease_token_hash, lease_expires_at, cancel_requested,
                   session_id, response, error, prompt_tokens, completion_tokens,
                   cost_usd, created_at, started_at, finished_at
            FROM automation_runs_v2;
            DROP TABLE automation_runs_v2;
            PRAGMA user_version=1;
            """
        )

    with AutomationStore(database) as migrated:
        runs = migrated.list_runs(workspace=workspace)
        columns = {
            str(row["name"])
            for row in migrated._conn.execute(
                "PRAGMA table_info(automation_runs)"
            ).fetchall()
        }
        version = int(migrated._conn.execute("PRAGMA user_version").fetchone()[0])

    assert version == AUTOMATION_SCHEMA_VERSION == 2
    assert {
        "cache_read_tokens",
        "cache_write_tokens",
        "usage_source",
        "estimated_prompt_tokens",
        "estimated_completion_tokens",
        "estimated_cost_usd",
    } <= columns
    assert len(runs) == 1
    assert runs[0].prompt_tokens == 7
    assert runs[0].completion_tokens == 3
    assert runs[0].usage_source == "unavailable"
    assert runs[0].cache_read_tokens == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are unavailable")
def test_store_restricts_database_and_wal_sidecar_permissions(
    tmp_path: Path, clock: list[float]
) -> None:
    database = tmp_path / "automation.db"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    with AutomationStore(database, clock=lambda: clock[0]) as private_store:
        private_store.create_job(
            name="private prompt",
            prompt="SECRET_AUTOMATION_PROMPT",
            workspace=workspace,
            schedule=build_schedule(every="1h", now=now),
        )
        sidecars = [database, Path(f"{database}-wal"), Path(f"{database}-shm")]
        assert all(path.exists() for path in sidecars)
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in sidecars)


def test_worker_heartbeat_and_soft_delete(
    tmp_path: Path, clock: list[float], store: AutomationStore
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="cleanup",
        prompt="Clean up",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
    )
    worker = store.heartbeat_worker(
        worker_id="worker-one",
        workspace=workspace,
        pid=123,
        max_concurrent_runs=2,
    )
    assert worker.pid == 123
    assert [item.worker_id for item in store.list_workers(workspace)] == ["worker-one"]

    removed = store.remove_job(job.name, workspace=workspace)
    assert removed.job_id == job.job_id
    assert store.get_job(job.job_id, workspace=workspace) is None
    assert (
        store.get_job(job.job_id, workspace=workspace, include_deleted=True).job_id
        == job.job_id
    )
    assert store.list_jobs(workspace, include_disabled=True) == []

    store.remove_worker(worker.worker_id)
    assert store.list_workers(workspace) == []


class _FakeClient:
    def __init__(
        self,
        *,
        result: AshResult | None = None,
        wait: bool = False,
    ) -> None:
        self.result = result or AshResult(
            response="automation complete",
            session_id="scheduled-session",
            model="fake/model",
            context_tokens=20,
            prompt_tokens=12,
            completion_tokens=8,
            cache_read_tokens=3,
            cache_write_tokens=2,
            cost_usd=0.02,
            usage_source="mixed",
            estimated_prompt_tokens=4,
            estimated_completion_tokens=2,
            estimated_cost_usd=0.006,
        )
        self.wait = wait
        self.closed = False
        self.started = asyncio.Event()

    async def prompt(self, text: str, *, user_metadata=None) -> AshResult:
        self.started.set()
        if self.wait:
            await asyncio.Event().wait()
        assert text
        assert user_metadata["source"] == "automation"
        return self.result

    async def close(self) -> None:
        self.closed = True


class _GatedClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def prompt(self, text: str, *, user_metadata=None) -> AshResult:
        self.started.set()
        await self.release.wait()
        return self.result


class _CancellationResistantClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()

    async def prompt(self, text: str, *, user_metadata=None) -> AshResult:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.release.wait()
        return self.result


@pytest.mark.asyncio
async def test_worker_executes_due_prompt_through_client(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="scheduled agent",
        prompt="Inspect the project",
        workspace=workspace,
        schedule=build_schedule(every="10s", now=now),
    )
    clock[0] += 10
    client = _FakeClient()
    worker_config = AshConfig(
        workspace_root=workspace,
        db_directory=tmp_path / "session-db",
        model="ollama/automation-test",
    )

    async def factory(config, root):
        assert config.workspace_root == workspace.resolve()
        assert config.db_directory == tmp_path / "session-db"
        assert config.model == "ollama/automation-test"
        assert root == workspace.resolve()
        return client

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(
        store,
        workspace,
        worker_id="test-worker",
        config=worker_config,
        client_factory=factory,
    )
    summary = await worker.run_forever(once=True)
    assert summary.completed == 1
    assert summary.succeeded == 1
    assert summary.ok is True

    runs = store.list_runs(workspace=workspace, job_id=job.job_id)
    assert len(runs) == 1
    assert runs[0].status == "succeeded"
    assert runs[0].response == "automation complete"
    assert runs[0].session_id == "scheduled-session"
    assert runs[0].cache_read_tokens == 3
    assert runs[0].cache_write_tokens == 2
    assert runs[0].usage_source == "mixed"
    assert runs[0].estimated_prompt_tokens == 4
    assert runs[0].estimated_completion_tokens == 2
    assert runs[0].estimated_cost_usd == 0.006
    assert client.closed is True
    assert store.list_workers(workspace) == []


@pytest.mark.asyncio
async def test_worker_collector_marks_unfinalizable_owned_run_interrupted(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="finalization fault",
        prompt="Return",
        workspace=workspace,
        schedule=build_schedule(every="10s", now=now),
        enabled=False,
    )
    claim = store.claim_manual(
        job.job_id, workspace=workspace, worker_id="fault-worker"
    )

    async def fail_execution():
        raise AutomationError("simulated finalization failure")

    worker = AutomationWorkerService(
        store,
        workspace,
        config=AshConfig(workspace_root=workspace),
        worker_id="fault-worker",
    )
    task = asyncio.create_task(fail_execution())
    await asyncio.sleep(0)
    assert task.done()
    worker._tasks[claim.run.run_id] = task
    worker._claims[claim.run.run_id] = claim
    summary = AutomationWorkerSummary()
    worker._collect_finished(summary)

    assert summary.completed == 1
    assert summary.interrupted == 1
    assert summary.ok is False
    run = store.list_runs(workspace=workspace, job_id=job.job_id)[0]
    assert run.status == "interrupted"
    assert "simulated finalization failure" in (run.error or "")


@pytest.mark.asyncio
async def test_worker_refuses_untrusted_workspace(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="trust required",
        prompt="Do not run",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    factory_called = False

    async def factory(config, root):
        nonlocal factory_called
        factory_called = True
        return _FakeClient()

    monkeypatch.setattr(
        "ash.automation.worker.is_workspace_trusted", lambda path: False
    )
    worker = AutomationWorkerService(store, workspace, client_factory=factory)
    with pytest.raises(AutomationError, match="not trusted"):
        await worker.run_manual(job.job_id)

    assert store.list_runs(workspace=workspace, job_id=job.job_id) == []
    assert factory_called is False


@pytest.mark.asyncio
async def test_worker_timeout_covers_runtime_initialization(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="slow startup",
        prompt="Never reached",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
        timeout_seconds=1,
    )
    startup_cancelled = asyncio.Event()

    async def factory(config, root):
        try:
            await asyncio.Event().wait()
        finally:
            startup_cancelled.set()

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(store, workspace, client_factory=factory)
    result = await worker.run_manual(job.job_id)

    assert result.status == "failed"
    assert "wall-clock timeout" in (result.error or "")
    assert startup_cancelled.is_set()


@pytest.mark.asyncio
async def test_worker_timeout_does_not_wait_forever_for_ignored_cancellation(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="resists cancellation",
        prompt="Wait forever",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
        timeout_seconds=1,
    )
    client = _CancellationResistantClient()

    async def factory(config, root):
        return client

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(store, workspace, client_factory=factory)
    started_at = asyncio.get_running_loop().time()
    result = await worker.run_manual(job.job_id)
    elapsed = asyncio.get_running_loop().time() - started_at

    assert result.status == "failed"
    assert "wall-clock timeout" in (result.error or "")
    assert elapsed < 2.0
    assert client.cancel_seen.is_set()
    client.release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_once_worker_stop_cancels_in_flight_batch(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="stop batch",
        prompt="Wait",
        workspace=workspace,
        schedule=build_schedule(every="10s", now=now),
    )
    clock[0] += 10
    client = _FakeClient(wait=True)

    async def factory(config, root):
        return client

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(store, workspace, client_factory=factory)
    service_task = asyncio.create_task(worker.run_forever(once=True))
    await client.started.wait()
    worker.request_stop()
    summary = await asyncio.wait_for(service_task, timeout=2)

    assert summary.stopped is True
    assert summary.cancelled == 1
    assert summary.ok is False
    assert store.list_runs(workspace=workspace, job_id=job.job_id)[0].status == "cancelled"


@pytest.mark.asyncio
async def test_worker_reloads_disabled_setting_before_claim(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="disabled live",
        prompt="Must not run",
        workspace=workspace,
        schedule=build_schedule(every="10s", now=now),
    )
    clock[0] += 10
    enabled = AshConfig(workspace_root=workspace, automation_enabled=True)
    disabled = enabled.model_copy(update={"automation_enabled": False})
    first_load = True

    def load_config() -> AshConfig:
        nonlocal first_load
        if first_load:
            first_load = False
            return enabled
        return disabled

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(store, workspace, config_loader=load_config)

    with pytest.raises(AutomationError, match="disabled"):
        await worker.run_forever(once=True)
    assert store.list_runs(workspace=workspace, job_id=job.job_id) == []
    assert store.get_worker(worker.worker_id) is None


@pytest.mark.asyncio
async def test_continuous_worker_pauses_and_resumes_with_live_configuration(
    tmp_path: Path,
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    automation_enabled = True

    def load_config() -> AshConfig:
        return AshConfig(
            workspace_root=workspace,
            automation_enabled=automation_enabled,
        )

    async def wait_until(predicate) -> None:
        for _ in range(50):
            if predicate():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("worker state did not settle")

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(
        store,
        workspace,
        config_loader=load_config,
        poll_seconds=0.1,
    )
    service = asyncio.create_task(worker.run_forever())
    await wait_until(lambda: store.get_worker(worker.worker_id) is not None)

    automation_enabled = False
    await wait_until(lambda: store.get_worker(worker.worker_id) is None)
    assert service.done() is False

    automation_enabled = True
    await wait_until(lambda: store.get_worker(worker.worker_id) is not None)
    worker.request_stop()
    summary = await asyncio.wait_for(service, timeout=2)

    assert summary.stopped is True
    assert summary.completed == 0
    assert store.get_worker(worker.worker_id) is None


def test_worker_config_loader_preserves_cli_overrides_and_refreshes_user_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    database = tmp_path / "cli-db"
    user_config = home / ".ash" / "ash.toml"
    workspace.mkdir()
    user_config.parent.mkdir(parents=True)
    user_config.write_text(
        'sandbox_network = true\nallowed_web_domains = ["old.example"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    startup = AshConfig.load(
        _override_source="cli",
        _override_detail="test CLI",
        workspace_root=workspace,
        db_directory=database,
        safety_tier="plan",
    )
    load = automation_config_loader(startup)

    user_config.write_text(
        'sandbox_network = false\nallowed_web_domains = ["new.example"]\n',
        encoding="utf-8",
    )
    refreshed = load()

    assert refreshed.workspace_root == workspace.resolve()
    assert refreshed.db_directory == database
    assert refreshed.safety_tier == "plan"
    assert refreshed.sandbox_network is False
    assert refreshed.allowed_web_domains == ["new.example"]
    assert refreshed.config_source("db_directory")[0] == "cli"
    assert refreshed.config_source("safety_tier")[0] == "cli"
    assert refreshed.config_source("sandbox_network")[0] == "user"


def test_worker_config_loader_requires_restart_when_database_path_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    first_database = tmp_path / "first-db"
    second_database = tmp_path / "second-db"
    user_config = home / ".ash" / "ash.toml"
    workspace.mkdir()
    user_config.parent.mkdir(parents=True)
    user_config.write_text(
        f'db_directory = "{first_database}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    startup = AshConfig.load(
        _override_source="cli",
        workspace_root=workspace,
    )
    load = automation_config_loader(startup)

    user_config.write_text(
        f'db_directory = "{second_database}"\n', encoding="utf-8"
    )

    with pytest.raises(AutomationError, match="restart the worker"):
        load()


@pytest.mark.asyncio
async def test_worker_refreshes_runtime_policy_after_claim_before_client_creation(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="tighten live policy",
        prompt="Use current policy",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    unsafe = AshConfig(
        workspace_root=workspace,
        safety_tier="auto_approve",
        allow_unsafe_auto_approve=True,
        sandbox_backend="direct",
        sandbox_network=True,
        allowed_web_domains=[],
    )
    tightened = unsafe.model_copy(
        update={
            "safety_tier": "dry_run",
            "allow_unsafe_auto_approve": False,
            "sandbox_backend": "auto",
            "sandbox_network": False,
            "allowed_web_domains": ["approved.example"],
        }
    )
    loads = iter((unsafe, unsafe, tightened))
    seen: list[AshConfig] = []

    def load_config() -> AshConfig:
        return next(loads)

    async def factory(config, root):
        seen.append(config)
        return _FakeClient()

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(
        store,
        workspace,
        config_loader=load_config,
        client_factory=factory,
    )
    result = await worker.run_manual(job.job_id)

    assert result.status == "succeeded"
    assert len(seen) == 1
    assert seen[0].safety_tier == "dry_run"
    assert seen[0].allow_unsafe_auto_approve is False
    assert seen[0].sandbox_backend == "auto"
    assert seen[0].sandbox_network is False
    assert seen[0].allowed_web_domains == ["approved.example"]


@pytest.mark.asyncio
async def test_manual_claim_failure_removes_worker_heartbeat(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="already running",
        prompt="Wait",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    existing = store.claim_manual(
        job.job_id,
        workspace=workspace,
        worker_id="existing-worker",
    )
    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(store, workspace, worker_id="manual-worker")

    with pytest.raises(AutomationError, match="already has an active run"):
        await worker.run_manual(job.job_id)
    assert store.get_worker(worker.worker_id) is None
    store.finish_run(existing.run.run_id, existing.token, status="cancelled")


@pytest.mark.asyncio
async def test_worker_finalizes_claim_cancelled_before_coroutine_start(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="pre-start stop",
        prompt="Do not start",
        workspace=workspace,
        schedule=build_schedule(every="10s", now=now),
    )
    clock[0] += 10
    real_create_task = asyncio.create_task

    def cancel_execution_task(coro, *, name=None, context=None):
        task = real_create_task(coro, name=name, context=context)
        if (
            name
            and name.startswith("ash-automation-")
            and not name.startswith(("ash-automation-turn-", "ash-automation-lease-"))
        ):
            task.cancel()
        return task

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    monkeypatch.setattr(
        "ash.automation.worker.asyncio.create_task", cancel_execution_task
    )
    worker = AutomationWorkerService(store, workspace, worker_id="stopping-worker")

    summary = await worker.run_forever(once=True)
    assert summary.completed == 1
    assert summary.cancelled == 1
    run = store.list_runs(workspace=workspace, job_id=job.job_id)[0]
    assert run.status == "cancelled"
    assert "before execution began" in (run.error or "")


@pytest.mark.asyncio
async def test_worker_cancels_agent_operation_when_heartbeat_fails(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="heartbeat failure",
        prompt="Wait",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    claim = store.claim_manual(
        job.job_id,
        workspace=workspace,
        worker_id="heartbeat-worker",
        lease_seconds=5,
    )
    client = _FakeClient(wait=True)

    async def factory(config, root):
        return client

    async def no_wait(_: float) -> None:
        return None

    def fail_heartbeat() -> None:
        raise AutomationError("heartbeat write failed")

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    monkeypatch.setattr("ash.automation.worker.asyncio.sleep", no_wait)
    worker = AutomationWorkerService(
        store,
        workspace,
        worker_id="heartbeat-worker",
        lease_seconds=5,
        client_factory=factory,
    )
    monkeypatch.setattr(worker, "_heartbeat", fail_heartbeat)
    result = await worker.execute(claim)

    assert result.status == "failed"
    assert "heartbeat write failed" in (result.error or "")
    assert client.closed is True


@pytest.mark.asyncio
async def test_worker_honors_cross_process_cancellation(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="cancel me",
        prompt="Wait",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    client = _FakeClient(wait=True)

    async def factory(config, root):
        return client

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(
        store,
        workspace,
        worker_id="cancel-worker",
        lease_seconds=5,
        client_factory=factory,
    )
    claim = store.claim_manual(
        job.job_id,
        workspace=workspace,
        worker_id=worker.worker_id,
        lease_seconds=5,
    )
    execution = asyncio.create_task(worker.execute(claim))
    await client.started.wait()
    store.request_cancel(claim.run.run_id)
    result = await asyncio.wait_for(execution, timeout=2)

    assert result.status == "cancelled"
    assert client.closed is True


@pytest.mark.asyncio
async def test_committed_cancellation_wins_over_immediate_success(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="cancel race",
        prompt="Return immediately after cancellation",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    client = _GatedClient()

    async def factory(config, root):
        return client

    monkeypatch.setattr("ash.automation.worker.is_workspace_trusted", lambda path: True)
    worker = AutomationWorkerService(store, workspace, client_factory=factory)
    claim = store.claim_manual(
        job.job_id,
        workspace=workspace,
        worker_id=worker.worker_id,
    )
    execution = asyncio.create_task(worker.execute(claim))
    await client.started.wait()
    store.request_cancel(claim.run.run_id, workspace=workspace)
    client.release.set()
    result = await asyncio.wait_for(execution, timeout=2)

    assert result.status == "cancelled"
    assert result.cancel_requested is True
    assert result.response is None


@pytest.mark.asyncio
async def test_lease_monitor_cancels_prompt_when_ownership_is_lost(
    tmp_path: Path,
    clock: list[float],
    store: AutomationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    now = datetime.fromtimestamp(clock[0], tz=timezone.utc)
    job = store.create_job(
        name="lease loss",
        prompt="Wait",
        workspace=workspace,
        schedule=build_schedule(every="1h", now=now),
        enabled=False,
    )
    worker = AutomationWorkerService(store, workspace, lease_seconds=5)
    claim = store.claim_manual(
        job.job_id,
        workspace=workspace,
        worker_id=worker.worker_id,
        lease_seconds=5,
    )
    prompt_task = asyncio.create_task(asyncio.Event().wait())

    async def no_wait(_: float) -> None:
        return None

    def lose_lease(*args, **kwargs):
        raise AutomationError("lease ownership lost")

    monkeypatch.setattr("ash.automation.worker.asyncio.sleep", no_wait)
    monkeypatch.setattr(store, "renew_lease", lose_lease)
    with pytest.raises(AutomationError, match="ownership lost"):
        await worker._monitor_lease(claim, prompt_task)
    await asyncio.gather(prompt_task, return_exceptions=True)
    assert prompt_task.cancelled()


@pytest.mark.asyncio
async def test_automation_tools_separate_read_and_mutating_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    guard = SafetyGuard(workspace)
    store_path = tmp_path / "automation.db"
    list_tool = ListAutomationsTool(guard, store_path)
    manage_tool = ManageAutomationTool(guard, store_path)
    policy = PermissionPolicy("interactive")

    assert policy.evaluate(list_tool.name, {}).action == PolicyAction.ALLOW
    assert policy.evaluate(manage_tool.name, {}).action == PolicyAction.ASK
    monkeypatch.setattr("ash.tools.automation.is_workspace_trusted", lambda path: True)

    created = await manage_tool.run(
        action="create",
        name="nightly review",
        prompt="Review project health",
        every="1h",
    )
    assert created.success is True
    listed = await list_tool.run(include_disabled=False)
    assert listed.success is True
    assert "nightly review" in listed.output

    payload = json.loads(created.output)
    paused = await manage_tool.run(action="pause", job=payload["job_id"])
    assert paused.success is True
    assert json.loads(paused.output)["enabled"] is False

    monkeypatch.setattr(
        "ash.tools.automation.is_workspace_trusted", lambda path: False
    )
    resumed = await manage_tool.run(action="resume", job=payload["job_id"])
    assert resumed.success is False
    assert "trusted" in (resumed.error or "")
    removed = await manage_tool.run(action="remove", job=payload["job_id"])
    assert removed.success is True


@pytest.mark.asyncio
async def test_list_automations_is_side_effect_free_when_store_is_absent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store_path = tmp_path / "state" / "automation.db"
    tool = ListAutomationsTool(SafetyGuard(workspace), store_path)

    result = await tool.run()

    assert result.success is True
    assert json.loads(result.output) == []
    assert not store_path.exists()
    assert not store_path.parent.exists()
