from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agents.shared_state import SharedState
from ash.cli import main
from cli.agents import (
    list_agent_reports,
    list_agent_statuses,
    render_agent_reports,
    render_agent_statuses,
)


def test_agent_status_renderer_emits_json(tmp_path: Path) -> None:
    state = SharedState(tmp_path / "agents.db")
    state.register_agent("worker", role="reviewer")
    state.update_status("worker", "completed", "done")
    state.close()

    statuses = list_agent_statuses(tmp_path / "agents.db")
    payload = json.loads(render_agent_statuses(statuses, json_output=True))

    assert payload["agents"][0]["agent_id"] == "worker"
    assert payload["agents"][0]["role"] == "reviewer"
    assert payload["agents"][0]["status"] == "completed"


def test_agent_report_renderer_emits_json(tmp_path: Path) -> None:
    state = SharedState(tmp_path / "agents.db")
    state.send_message(
        "worker",
        "lead",
        "agent_report",
        {
            "agent_id": "worker",
            "role": "reviewer",
            "task": "review",
            "success": True,
            "summary": "looks good",
            "artifacts": {},
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    state.close()

    reports = list_agent_reports(tmp_path / "agents.db")
    payload = json.loads(render_agent_reports(reports, json_output=True))

    assert payload["reports"][0]["agent_id"] == "worker"
    assert payload["reports"][0]["summary"] == "looks good"


def test_agents_cli_lists_persisted_statuses(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    state = SharedState(db_dir / "agents.db")
    state.register_agent("agent-a", role="coder")
    state.update_status("agent-a", "working", "fix tests")
    state.close()
    monkeypatch.setenv("ASH_MODEL", "ollama/test-model")
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(db_dir))
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))

    assert main(["agents", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["agents"][0]["agent_id"] == "agent-a"
    assert payload["agents"][0]["current_task"] == "fix tests"


def test_agents_cli_lists_reports_and_rejects_invalid_limit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    state = SharedState(db_dir / "agents.db")
    state.send_message(
        "agent-a",
        "lead",
        "agent_report",
        {
            "agent_id": "agent-a",
            "role": "tester",
            "task": "test",
            "success": False,
            "summary": "failed tests",
            "artifacts": {},
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    state.close()
    monkeypatch.setenv("ASH_MODEL", "ollama/test-model")
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(db_dir))
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))

    assert main(["agents", "reports", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reports"][0]["summary"] == "failed tests"

    assert main(["agents", "reports", "--limit", "0"]) == 2
    assert "limit must be positive" in capsys.readouterr().err
