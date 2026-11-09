"""Real-CLI activity inspection stays local, bounded, and content-free."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ash.core.events import envelope_event
from ash.core.session import SessionStore


def test_session_activity_inspects_bounded_pages_without_disclosing_content(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    foreign_project = tmp_path / "foreign"
    home = tmp_path / "home"
    for directory in (project, foreign_project, home):
        directory.mkdir()
    database = tmp_path / "database"
    store = SessionStore(database / "sessions.db")
    session = store.create_session(str(project))
    foreign = store.create_session(str(foreign_project))
    private_content = "UNREDACTED_PRIVATE_FIXTURE_CONTENT"
    activity = [
        envelope_event(
            {
                "type": "turn.started",
                "session_id": session.session_id,
                "event_id": "activity-1",
                "timestamp": "2026-11-09T03:00:00+00:00",
                "prompt": private_content,
            }
        ),
        envelope_event(
            {
                "type": "model.request.started",
                "session_id": session.session_id,
                "event_id": "activity-2",
                "operation_id": "request-one",
                "timestamp": "2026-11-09T03:00:01+00:00",
                "tool_count": 10,
            }
        ),
        envelope_event(
            {
                "type": "model.request.first_output",
                "session_id": session.session_id,
                "event_id": "activity-first",
                "operation_id": "request-one",
                "timestamp": "2026-11-09T03:00:03+00:00",
                "content": private_content,
            }
        ),
        envelope_event(
            {
                "type": "model.request.completed",
                "session_id": session.session_id,
                "event_id": "activity-3",
                "operation_id": "request-one",
                "timestamp": "2026-11-09T03:00:05+00:00",
                "prompt_tokens": 800,
                "completion_tokens": 42,
                "output": private_content,
            }
        ),
        envelope_event(
            {
                "type": "tool.completed",
                "session_id": session.session_id,
                "event_id": "activity-4",
                "timestamp": "2026-11-09T03:00:06+00:00",
                "tool": "write_file",
                "success": True,
                "arguments": {"file_path": private_content},
                "output": private_content,
            }
        ),
        envelope_event(
            {
                "type": "turn.completed",
                "session_id": session.session_id,
                "event_id": "activity-5",
                "timestamp": "2026-11-09T03:00:07+00:00",
                "stop_reason": "completed",
                "iterations": 2,
            }
        ),
    ]
    assert store.save_runtime_events(activity) == len(activity)

    environment = {
        "PATH": os.defpath,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
        "TERM": "dumb",
    }

    def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "ash",
                "--db-directory",
                str(database),
                "sessions",
                "activity",
                *arguments,
            ],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )

    complete = invoke("--json", "--limit", "20")
    assert complete.returncode == 0, complete.stderr
    assert private_content not in complete.stdout
    payload = json.loads(complete.stdout)
    assert payload["session_id"] == session.session_id
    assert payload["next_cursor"] > 0
    assert payload["has_more"] is False
    assert payload["activity"]["model_requests"] == 1
    assert payload["activity"]["model_requests_completed"] == 1
    assert payload["activity"]["completed_model_seconds"] == 4
    assert payload["activity"]["completed_requests_with_first_output"] == 1
    assert payload["activity"]["completed_model_first_output_seconds"] == 2
    assert payload["activity"]["completed_model_after_first_output_seconds"] == 2
    assert payload["activity"]["last_successful_edit_seconds"] == 6
    assert payload["activity"]["timeline"][-1]["type"] == "turn.completed"

    first = invoke("--json", "--session", session.session_id, "--limit", "2")
    assert first.returncode == 0, first.stderr
    first_payload = json.loads(first.stdout)
    assert first_payload["has_more"] is True
    assert first_payload["activity"]["model_requests_still_open"] == 1

    second = invoke(
        "--json",
        "--session",
        session.session_id,
        "--limit",
        "4",
        "--after-sequence",
        str(first_payload["next_cursor"]),
    )
    assert second.returncode == 0, second.stderr
    second_payload = json.loads(second.stdout)
    assert second_payload["has_more"] is False
    assert second_payload["next_cursor"] == payload["next_cursor"]
    assert len(second_payload["activity"]["timeline"]) == 4

    readable = invoke("--limit", "20")
    assert readable.returncode == 0, readable.stderr
    assert "Model requests: 1 started, 1 completed, 4.00s measured" in readable.stdout
    assert "Model first-output split: 2.000s waiting, 2.000s after" in readable.stdout
    assert "write_file" in readable.stdout
    assert private_content not in readable.stdout

    for args in (
        ("--session", foreign.session_id, "--json"),
        ("--all-projects",),
        ("--limit", "0"),
        ("--limit", "301"),
        ("--after-sequence", "-1"),
    ):
        refused = invoke(*args)
        assert refused.returncode == 2, (args, refused.stderr)
        assert private_content not in refused.stdout + refused.stderr
