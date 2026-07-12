from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ash.agents.shared_state import SharedState
from ash.cli import main
from ash.commands.agents import (
    cancel_agent_graph,
    list_agent_messages,
    list_agent_reports,
    list_agent_statuses,
    list_agent_task_events,
    list_agent_tasks,
    render_agent_branches,
    render_agent_messages,
    render_agent_reports,
    render_agent_statuses,
    render_agent_task_events,
    render_agent_tasks,
    render_cancelled_agent_graph,
    render_sent_agent_message,
    send_agent_message,
)


def test_render_agent_branches() -> None:
    assert render_agent_branches([]) == "No isolated agent branches."
    assert render_agent_branches([("ash-agent/coder", "a" * 40)]) == (
        "ash-agent/coder aaaaaaaaaaaa"
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


def test_agent_task_renderer_includes_durable_artifacts(tmp_path: Path) -> None:
    state = SharedState(tmp_path / "agents.db")
    state.tasks.create_task("implement", task_id="task-1", token_budget=20)
    lease = state.tasks.claim_task("worker", task_id="task-1")
    assert lease is not None
    state.tasks.start_task("task-1", lease.token)
    state.tasks.record_tokens("task-1", lease.token, 5)
    state.tasks.complete_task("task-1", lease.token, {"summary": "done"})
    state.tasks.add_artifact(
        "task-1", kind="git-commit", uri="ash-agent/worker", metadata={"commit": "abc"}
    )
    state.close()

    tasks = list_agent_tasks(tmp_path / "agents.db", task_state="succeeded")
    payload = json.loads(render_agent_tasks(tasks, json_output=True))

    assert payload["tasks"][0]["task_id"] == "task-1"
    assert payload["tasks"][0]["used_tokens"] == 5
    assert payload["tasks"][0]["result"] == {"summary": "done"}
    assert payload["tasks"][0]["artifacts"][0]["kind"] == "git-commit"


def test_agent_task_event_renderer_supports_cursor_and_type_filters(
    tmp_path: Path,
) -> None:
    state = SharedState(tmp_path / "agents.db")
    state.tasks.create_task("trace", task_id="trace")
    lease = state.tasks.claim_task("worker", task_id="trace")
    assert lease is not None
    state.tasks.start_task("trace", lease.token)
    state.close()

    all_events = list_agent_task_events(tmp_path / "agents.db", task_id="trace")
    events = list_agent_task_events(
        tmp_path / "agents.db",
        event_type="agent.task.running",
        after_sequence=all_events[0]["sequence"],
    )
    payload = json.loads(render_agent_task_events(events, json_output=True))

    assert [item["event"]["type"] for item in payload["events"]] == [
        "agent.task.running"
    ]


def test_cancel_agent_graph_renderer_emits_json(tmp_path: Path) -> None:
    state = SharedState(tmp_path / "agents.db")
    state.tasks.create_task(
        "cancel",
        task_id="cancel",
        metadata={"graph_id": "graph-cli"},
    )
    state.close()

    cancellation = cancel_agent_graph(
        tmp_path / "agents.db", graph_id="graph-cli", reason="operator"
    )
    payload = json.loads(render_cancelled_agent_graph(cancellation, json_output=True))

    assert payload["cancellation"]["task_ids"] == ["cancel"]


def test_agent_message_renderer_emits_json(tmp_path: Path) -> None:
    state = SharedState(tmp_path / "agents.db")
    state.register_agent("agent-a")
    message_id = state.send_to_agent("lead", "agent-a", "note", "hello")
    state.close()

    messages = list_agent_messages(tmp_path / "agents.db", recipient_id="agent-a")
    payload = json.loads(render_agent_messages(messages, json_output=True))

    assert payload["messages"][0]["message_id"] == message_id
    assert payload["messages"][0]["content"] == {"content": "hello"}
    assert payload["messages"][0]["delivered"] is False


def test_send_agent_message_renderer_emits_json(tmp_path: Path) -> None:
    state = SharedState(tmp_path / "agents.db")
    state.register_agent("agent-a")
    state.close()

    message = send_agent_message(
        tmp_path / "agents.db",
        recipient_id="agent-a",
        content='{"summary": "continue"}',
        json_content=True,
    )
    payload = json.loads(render_sent_agent_message(message, json_output=True))

    assert payload["message"]["recipient_id"] == "agent-a"
    assert payload["message"]["content"] == {"summary": "continue"}


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


def test_agents_cli_lists_filtered_durable_tasks(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    state = SharedState(db_dir / "agents.db")
    state.tasks.create_task("queued", task_id="queued")
    state.tasks.create_task("active", task_id="active")
    lease = state.tasks.claim_task("worker", task_id="active")
    assert lease is not None
    state.tasks.start_task("active", lease.token)
    state.close()
    monkeypatch.setenv("ASH_MODEL", "ollama/test-model")
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(db_dir))
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))

    assert main(["agents", "tasks", "--state", "running", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert [task["task_id"] for task in payload["tasks"]] == ["active"]

    assert main(["agents", "tasks", "--limit", "0"]) == 2
    assert "limit must be between" in capsys.readouterr().err


def test_agents_cli_replays_filtered_task_events(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    state = SharedState(db_dir / "agents.db")
    state.tasks.create_task("trace", task_id="trace")
    state.close()
    monkeypatch.setenv("ASH_MODEL", "ollama/test-model")
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(db_dir))
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))

    assert main(["agents", "events", "--task", "trace", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["events"][0]["task_id"] == "trace"
    assert payload["events"][0]["event"]["type"] == "agent.task.created"


def test_agents_cli_filters_and_cancels_graph_with_confirmation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    state = SharedState(db_dir / "agents.db")
    state.tasks.create_task(
        "graph task",
        task_id="graph-task",
        metadata={"graph_id": "graph-cli"},
    )
    state.tasks.create_task("unrelated", task_id="unrelated")
    state.close()
    monkeypatch.setenv("ASH_MODEL", "ollama/test-model")
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(db_dir))
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))

    assert main(["agents", "tasks", "--graph", "graph-cli", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [task["task_id"] for task in payload["tasks"]] == ["graph-task"]

    assert main(["agents", "cancel", "graph-cli"]) == 2
    assert "requires --yes" in capsys.readouterr().err
    assert main(["agents", "cancel", "graph-cli", "--yes", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cancellation"]["task_ids"] == ["graph-task"]


def test_agents_cli_discard_requires_explicit_confirmation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("ASH_MODEL", "ollama/test-model")
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(tmp_path / "db"))
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))

    assert main(["agents", "discard", "ash-agent/coder"]) == 2
    assert "requires --yes" in capsys.readouterr().err


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


def test_agents_cli_lists_messages_without_marking_delivered(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    state = SharedState(db_dir / "agents.db")
    state.register_agent("agent-a", role="coder")
    state.send_to_agent("lead", "agent-a", "steer", "continue")
    state.close()
    monkeypatch.setenv("ASH_MODEL", "ollama/test-model")
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(db_dir))
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))

    assert main(["agents", "messages", "--recipient", "agent-a", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["messages"][0]["message_type"] == "steer"
    followup_state = SharedState(db_dir / "agents.db")
    try:
        assert len(followup_state.fetch_messages("agent-a", undelivered_only=True)) == 1
    finally:
        followup_state.close()


def test_agents_cli_messages_all_includes_delivered_and_rejects_invalid_limit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    state = SharedState(db_dir / "agents.db")
    state.register_agent("agent-a", role="coder")
    delivered_id = state.send_to_agent("lead", "agent-a", "steer", "delivered")
    state.send_to_agent("lead", "agent-a", "steer", "pending")
    state.mark_delivered([delivered_id])
    state.close()
    monkeypatch.setenv("ASH_MODEL", "ollama/test-model")
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(db_dir))
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))

    assert main(["agents", "messages", "--recipient", "agent-a", "--json"]) == 0
    pending_payload = json.loads(capsys.readouterr().out)
    assert len(pending_payload["messages"]) == 1
    assert pending_payload["messages"][0]["content"] == {"content": "pending"}

    assert (
        main(["agents", "messages", "--recipient", "agent-a", "--all", "--json"]) == 0
    )
    all_payload = json.loads(capsys.readouterr().out)
    assert len(all_payload["messages"]) == 2

    assert main(["agents", "messages", "--limit", "0"]) == 2
    assert "limit must be positive" in capsys.readouterr().err


def test_agents_cli_sends_plain_steering_message(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    state = SharedState(db_dir / "agents.db")
    state.register_agent("agent-a", role="coder")
    state.close()
    monkeypatch.setenv("ASH_MODEL", "ollama/test-model")
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(db_dir))
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))

    assert main(["agents", "send", "agent-a", "continue", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["message"]["recipient_id"] == "agent-a"
    assert payload["message"]["content"] == {"content": "continue"}
    followup_state = SharedState(db_dir / "agents.db")
    try:
        messages = followup_state.fetch_messages("agent-a")
        assert messages[0].message_type == "steer"
        assert messages[0].content == {"content": "continue"}
    finally:
        followup_state.close()


def test_agents_cli_send_validates_recipient_and_json_content(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    db_dir = tmp_path / "db"
    SharedState(db_dir / "agents.db").close()
    monkeypatch.setenv("ASH_MODEL", "ollama/test-model")
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(db_dir))
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))

    assert main(["agents", "send", "missing", "hello"]) == 2
    assert "is not registered" in capsys.readouterr().err

    assert (
        main(
            [
                "agents",
                "send",
                "missing",
                "[1]",
                "--json-content",
                "--force",
            ]
        )
        == 2
    )
    assert "JSON content must be an object" in capsys.readouterr().err

    assert (
        main(
            [
                "agents",
                "send",
                "missing",
                '{"summary": "queued"}',
                "--json-content",
                "--force",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["message"]["content"] == {"summary": "queued"}
