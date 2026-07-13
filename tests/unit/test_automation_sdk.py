from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from ash.automation import AutomationError, AutomationRunLease
from ash.automation.schedules import build_schedule
from ash.automation.store import AutomationStore
from ash.config import AshConfig
from ash.core.loop import AshLoop
from ash.sdk import AshClient


def _client(workspace: Path, *, automation_enabled: bool = True) -> AshClient:
    config = AshConfig(
        model="ollama/sdk-model",
        workspace_root=workspace,
        db_directory=workspace.parent / "db",
        memory_backend="off",
        automation_enabled=automation_enabled,
    )
    loop = cast(AshLoop, SimpleNamespace(project_root=workspace.resolve()))
    return AshClient(loop, config)


def test_sdk_manages_and_claims_workspace_automation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    client = _client(workspace)
    monkeypatch.setattr("ash.sdk.is_workspace_trusted", lambda path: True)

    created = client.create_automation(
        "Repository review",
        "Review outstanding repository risks",
        every="30m",
        misfire_grace_seconds=120,
        timeout_seconds=90,
        token_budget=5000,
    )

    assert client.automation(created.job_id) == created
    assert client.automation("repository REVIEW") == created
    assert client.automations() == [created]
    assert client.pause_automation(created.name).enabled is False
    assert client.automations() == []
    assert client.automations(include_disabled=True)[0].job_id == created.job_id
    assert client.resume_automation(created.job_id).enabled is True

    lease = client.claim_automation(created.name, worker_id="sdk-worker")
    assert isinstance(lease, AutomationRunLease)
    assert lease.job.job_id == created.job_id
    assert lease.run.trigger == "manual"
    assert lease.run.worker_id == "sdk-worker"
    assert lease.token
    assert client.automation_runs(created.name) == [lease.run]

    cancelled = client.cancel_automation_run(lease.run.run_id)
    assert cancelled.cancel_requested is True
    with AutomationStore(client.config.db_directory / "automation.db") as store:
        finished = store.finish_run(
            lease.run.run_id,
            lease.token,
            status="cancelled",
            error="cancelled by SDK test",
        )
    assert client.automation_runs(limit=1) == [finished]

    removed = client.remove_automation(created.job_id)
    assert removed.job_id == created.job_id
    assert client.automation(created.job_id) is None
    assert client.automation_runs(removed.job_id) == [finished]


def test_sdk_rejects_cross_workspace_run_cancellation(tmp_path: Path) -> None:
    own_workspace = tmp_path / "own"
    other_workspace = tmp_path / "other"
    own_workspace.mkdir()
    other_workspace.mkdir()
    client = _client(own_workspace)
    with AutomationStore(client.config.db_directory / "automation.db") as store:
        foreign = store.create_job(
            name="foreign",
            prompt="do not expose this run",
            workspace=other_workspace,
            schedule=build_schedule(every="1h"),
        )
        lease = store.claim_manual(
            foreign.job_id,
            workspace=other_workspace,
            worker_id="other-worker",
        )

    assert client.automation_runs() == []
    with pytest.raises(AutomationError, match="run not found"):
        client.cancel_automation_run(lease.run.run_id)


def test_sdk_guards_operations_that_enable_unattended_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    client = _client(workspace)
    monkeypatch.setattr("ash.sdk.is_workspace_trusted", lambda path: False)

    with pytest.raises(AutomationError, match="trusted"):
        client.create_automation("blocked", "prompt", every="1h")

    with AutomationStore(client.config.db_directory / "automation.db") as store:
        job = store.create_job(
            name="existing",
            prompt="prompt",
            workspace=workspace,
            schedule=build_schedule(every="1h"),
            enabled=False,
        )

    with pytest.raises(AutomationError, match="trusted"):
        client.resume_automation(job.job_id)
    with pytest.raises(AutomationError, match="trusted"):
        client.claim_automation(job.job_id, worker_id="sdk-worker")

    assert client.pause_automation(job.job_id).enabled is False
    assert client.remove_automation(job.job_id).job_id == job.job_id

    disabled_client = _client(workspace, automation_enabled=False)
    monkeypatch.setattr("ash.sdk.is_workspace_trusted", lambda path: True)
    with pytest.raises(AutomationError, match="disabled"):
        disabled_client.create_automation("blocked", "prompt", every="1h")


def test_sdk_validates_schedules_and_history_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    client = _client(workspace)
    monkeypatch.setattr("ash.sdk.is_workspace_trusted", lambda path: True)

    with pytest.raises(ValueError, match="exactly one"):
        client.create_automation("invalid", "prompt", every="1h", cron="0 * * * mon")
    with pytest.raises(ValueError, match="day-of-week"):
        client.create_automation("invalid", "prompt", cron="0 * * * 1")
    with pytest.raises(AutomationError, match="automation not found"):
        client.automation_runs("missing")
    with pytest.raises(ValueError, match="limit"):
        client.automations(limit=0)
