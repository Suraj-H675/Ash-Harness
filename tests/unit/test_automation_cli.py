from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ash.automation.models import AutomationRun
from ash.automation.models import AutomationWorkerSummary
from ash.automation.store import AutomationStore
from ash.cli import main
from ash.safety.trust import set_workspace_trusted


@pytest.fixture
def cron_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    db_directory = tmp_path / "db"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ASH_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("ASH_DB_DIRECTORY", raising=False)
    monkeypatch.delenv("ASH_AUTOMATION_ENABLED", raising=False)
    monkeypatch.chdir(workspace)
    return workspace, db_directory


def _cron(db_directory: Path, *arguments: str) -> list[str]:
    return ["--db-directory", str(db_directory), "cron", *arguments]


def _add_interval_job(
    db_directory: Path,
    name: str = "repository review",
    *,
    prompt: str = "Review the repository",
) -> list[str]:
    return _cron(
        db_directory,
        "add",
        name,
        "--prompt",
        prompt,
        "--every",
        "30m",
        "--json",
    )


def test_cron_cli_json_lifecycle_and_removal_confirmation(
    cron_environment: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, db_directory = cron_environment
    set_workspace_trusted(workspace, True)

    assert main(_add_interval_job(db_directory)) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["name"] == "repository review"
    assert created["prompt"] == "Review the repository"
    assert created["workspace"] == str(workspace.resolve())
    assert created["schedule"]["kind"] == "every"
    assert created["schedule"]["value"] == "1800"
    assert created["enabled"] is True

    assert main(_cron(db_directory, "list", "--json")) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [job["job_id"] for job in listed] == [created["job_id"]]
    assert "prompt" not in listed[0]

    assert main(_cron(db_directory, "show", "repository review", "--json")) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["job_id"] == created["job_id"]
    assert shown["prompt"] == "Review the repository"

    assert main(_cron(db_directory, "pause", created["job_id"], "--json")) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is False
    assert main(_cron(db_directory, "status")) == 0
    assert "Warning: no worker is active" not in capsys.readouterr().out
    assert main(_cron(db_directory, "list", "--json")) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(_cron(db_directory, "list", "--all", "--json")) == 0
    assert json.loads(capsys.readouterr().out)[0]["enabled"] is False

    set_workspace_trusted(workspace, False)
    assert main(_cron(db_directory, "resume", created["job_id"], "--json")) == 2
    assert "workspace must be trusted" in json.loads(capsys.readouterr().out)["error"]
    set_workspace_trusted(workspace, True)
    assert main(_cron(db_directory, "resume", created["job_id"], "--json")) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is True

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(_cron(db_directory, "remove", created["job_id"])) == 2
    assert "removal cancelled; pass --yes" in capsys.readouterr().err

    assert (
        main(_cron(db_directory, "remove", created["job_id"], "--yes", "--json")) == 0
    )
    removed = json.loads(capsys.readouterr().out)
    assert removed == {
        "job_id": created["job_id"],
        "name": "repository review",
        "removed": True,
    }

    assert main(_cron(db_directory, "show", created["job_id"], "--json")) == 2
    error = json.loads(capsys.readouterr().out)
    assert error == {"error": f"automation not found: {created['job_id']}"}
    assert main(_cron(db_directory, "history", created["job_id"], "--json")) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_cron_cli_requires_trust_and_reports_machine_readable_errors(
    cron_environment: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, db_directory = cron_environment

    assert main(_add_interval_job(db_directory)) == 2
    error = json.loads(capsys.readouterr().out)
    assert "workspace must be trusted" in error["error"]

    set_workspace_trusted(workspace, True)
    monkeypatch.setattr("sys.stdin", io.StringIO("Read the prompt from stdin"))
    assert (
        main(
            _cron(
                db_directory,
                "add",
                "stdin prompt",
                "--prompt",
                "-",
                "--every",
                "1h",
                "--json",
            )
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["prompt"] == "Read the prompt from stdin"

    assert (
        main(
            _cron(
                db_directory,
                "add",
                "stdin prompt",
                "--prompt",
                "duplicate",
                "--every",
                "1h",
                "--json",
            )
        )
        == 2
    )
    duplicate = json.loads(capsys.readouterr().out)
    assert "cannot create automation" in duplicate["error"]

    assert main(_cron(db_directory, "history", "--limit", "0", "--json")) == 2
    invalid_limit = json.loads(capsys.readouterr().out)
    assert "limit must be between 1 and 1000" in invalid_limit["error"]


def test_cron_cli_disabled_mode_blocks_enabling_operations(
    cron_environment: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, db_directory = cron_environment
    set_workspace_trusted(workspace, True)
    monkeypatch.setenv("ASH_AUTOMATION_ENABLED", "false")

    assert main(_add_interval_job(db_directory)) == 2
    assert "disabled" in json.loads(capsys.readouterr().out)["error"]


def test_cron_cli_status_and_idle_worker_once(
    cron_environment: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, db_directory = cron_environment
    set_workspace_trusted(workspace, True)

    assert main(_cron(db_directory, "status", "--json")) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty["enabled"] is True
    assert empty["jobs"] == 0
    assert empty["workers"] == []

    assert main(_add_interval_job(db_directory)) == 0
    capsys.readouterr()
    assert main(_cron(db_directory, "status")) == 0
    status = capsys.readouterr().out
    assert "Jobs: 1 active / 1 total" in status
    assert "Warning: no worker is active" in status

    assert main(_cron(db_directory, "worker", "--once", "--json")) == 0
    assert json.loads(capsys.readouterr().out) == {
        "cancelled": 0,
        "completed": 0,
        "failed": 0,
        "interrupted": 0,
        "ok": True,
        "skipped": 0,
        "stopped": False,
        "succeeded": 0,
    }


def test_cron_cli_manual_run_json_and_exit_status(
    cron_environment: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, db_directory = cron_environment
    set_workspace_trusted(workspace, True)
    assert main(_add_interval_job(db_directory, "manual job")) == 0
    job = json.loads(capsys.readouterr().out)
    now = datetime.now(timezone.utc)

    async def succeed(config, reference: str) -> AutomationRun:
        assert config.workspace_root == workspace.resolve()
        assert reference == job["job_id"]
        return AutomationRun(
            run_id="manual-run-success",
            job_id=job["job_id"],
            scheduled_for=now,
            status="succeeded",
            attempt=1,
            created_at=now,
            started_at=now,
            finished_at=now,
            response="finished",
            trigger="manual",
        )

    monkeypatch.setattr("ash.commands.automation.run_manual", succeed)
    assert main(_cron(db_directory, "run", job["job_id"], "--json")) == 0
    succeeded = json.loads(capsys.readouterr().out)
    assert succeeded["status"] == "succeeded"
    assert succeeded["response"] == "finished"

    async def fail(config, reference: str) -> AutomationRun:
        return AutomationRun(
            run_id="manual-run-failure",
            job_id=job["job_id"],
            scheduled_for=now,
            status="failed",
            attempt=1,
            created_at=now,
            started_at=now,
            finished_at=now,
            error="provider unavailable",
            trigger="manual",
        )

    monkeypatch.setattr("ash.commands.automation.run_manual", fail)
    assert main(_cron(db_directory, "run", "manual job", "--json")) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["status"] == "failed"
    assert failed["error"] == "provider unavailable"


def test_cron_cli_cancel_is_scoped_and_rejects_terminal_runs(
    cron_environment: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, db_directory = cron_environment
    other_workspace = workspace.parent / "other-workspace"
    other_workspace.mkdir()
    set_workspace_trusted(workspace, True)
    set_workspace_trusted(other_workspace, True)

    monkeypatch.chdir(other_workspace)
    assert main(_add_interval_job(db_directory, "other job")) == 0
    other_job = json.loads(capsys.readouterr().out)

    with AutomationStore(db_directory / "automation.db") as store:
        claim = store.claim_manual(
            other_job["job_id"],
            workspace=other_workspace,
            worker_id="test-worker",
        )

        monkeypatch.chdir(workspace)
        assert main(_cron(db_directory, "cancel", claim.run.run_id, "--json")) == 2
        hidden = json.loads(capsys.readouterr().out)
        assert hidden == {"error": f"automation run not found: {claim.run.run_id}"}
        assert store.get_run(claim.run.run_id).cancel_requested is False

        monkeypatch.chdir(other_workspace)
        assert main(_cron(db_directory, "cancel", claim.run.run_id, "--json")) == 0
        requested = json.loads(capsys.readouterr().out)
        assert requested["cancel_requested"] is True

        store.finish_run(
            claim.run.run_id,
            claim.token,
            status="cancelled",
            error="cancelled by test",
        )
        assert main(_cron(db_directory, "cancel", claim.run.run_id, "--json")) == 2
        terminal = json.loads(capsys.readouterr().out)
        assert "status=cancelled" in terminal["error"]

        assert main(_cron(db_directory, "history", "other job", "--json")) == 0
        history = json.loads(capsys.readouterr().out)
        assert [(run["run_id"], run["status"]) for run in history] == [
            (claim.run.run_id, "cancelled")
        ]


def test_cron_cli_returns_130_when_worker_is_interrupted(
    cron_environment: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, db_directory = cron_environment

    async def interrupt(config, *, once: bool = False, on_run_finished=None):
        raise KeyboardInterrupt

    monkeypatch.setattr("ash.commands.automation.run_worker", interrupt)
    assert main(_cron(db_directory, "worker", "--once")) == 130
    assert "Interrupted." in capsys.readouterr().err


def test_cron_worker_once_returns_failure_for_failed_run(
    cron_environment: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, db_directory = cron_environment

    async def failed(config, *, once=False, on_run_finished=None):
        return AutomationWorkerSummary(completed=1, failed=1)

    monkeypatch.setattr("ash.commands.automation.run_worker", failed)
    assert main(_cron(db_directory, "worker", "--once", "--json")) == 1
    assert json.loads(capsys.readouterr().out) == {
        "cancelled": 0,
        "completed": 1,
        "failed": 1,
        "interrupted": 0,
        "ok": False,
        "skipped": 0,
        "stopped": False,
        "succeeded": 0,
    }
