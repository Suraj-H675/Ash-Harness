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
                "type": "context.usage",
                "session_id": session.session_id,
                "event_id": "activity-2",
                "context_id": "11111111-1111-4111-8111-111111111111",
                "timestamp": "2026-11-09T03:00:00+00:00",
                "assembly_purpose": "request",
                "estimate_source": "local",
                "estimator_route": "synthetic/model",
                "estimator_count_mode": "offline_text_heuristic",
                "current": 8000,
                "maximum": 16000,
                "combined_limit": 20000,
                "response_limit": 4000,
                "input_limit": 16000,
                "message_estimate_tokens": 7900,
                "serialized_message_tokens": 6876,
                "native_tool_schema_tokens": 100,
                "system_tokens": 1000,
                "tool_schema_tokens": 100,
                "history_tokens": 6000,
                "repo_map_tokens": 250,
                "memory_tokens": 0,
                "assistant_text_chars": "SYNTHETIC-CONTEXT-CONTENT",
                "assistant_tool_call_argument_chars": 1200,
                "serialized_tool_result_chars": 30000,
                "image_count": 1,
                "image_estimate_tokens": 1024,
                "opaque_transport_estimate_enabled": True,
                "opaque_transport_estimate_tokens": 0,
                "provider_replay_state_preserved": False,
            }
        ),
        envelope_event(
            {
                "type": "model.request.started",
                "session_id": session.session_id,
                "event_id": "activity-3",
                "operation_id": "request-one",
                "timestamp": "2026-11-09T03:00:01+00:00",
                "tool_count": 10,
                "requested_completion_limit": 1200,
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
                "event_id": "activity-4",
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
                "event_id": "activity-5",
                "timestamp": "2026-11-09T03:00:06+00:00",
                "tool": "write_file",
                "success": True,
                "arguments": {"file_path": private_content},
                "output": private_content,
            }
        ),
        envelope_event(
            {
                "type": "context.request.rejected",
                "session_id": session.session_id,
                "event_id": "activity-7",
                "context_id": "22222222-2222-4222-8222-222222222222",
                "timestamp": "2026-11-09T03:00:06+00:00",
                "cause": "input_limit",
                "scope": "request_attempt",
                "dispatch_status": "not_dispatched",
                "required_input_tokens": 58_773,
                "input_limit": 16_000,
                "protected_current_turn": True,
                "opaque_provider_replay_state_preserved": False,
                "required_history_summary": False,
            }
        ),
        envelope_event(
            {
                "type": "turn.completed",
                "session_id": session.session_id,
                "event_id": "activity-6",
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
    context_usage = next(
        item for item in payload["activity"]["timeline"]
        if item["type"] == "context.usage"
    )
    context_rejection = next(
        item for item in payload["activity"]["timeline"]
        if item["type"] == "context.request.rejected"
    )
    assert context_usage["current"] == 8000
    assert context_usage["input_limit"] == 16_000
    assert context_usage["message_estimate_tokens"] == 7900
    assert context_usage["serialized_message_tokens"] == 6876
    assert context_usage["native_tool_schema_tokens"] == 100
    assert context_usage["assistant_tool_call_argument_chars"] == 1200
    assert context_usage["opaque_transport_estimate_tokens"] == 0
    assert "assistant_text_chars" not in context_usage
    assert context_rejection["required_input_tokens"] == 58_773
    request_started = next(
        item for item in payload["activity"]["timeline"]
        if item["type"] == "model.request.started"
    )
    assert request_started["requested_completion_limit"] == 1200
    assert "SYNTHETIC-CONTEXT-CONTENT" not in complete.stdout

    first = invoke("--json", "--session", session.session_id, "--limit", "3")
    assert first.returncode == 0, first.stderr
    first_payload = json.loads(first.stdout)
    assert first_payload["has_more"] is True
    assert first_payload["activity"]["model_requests_still_open"] == 1

    second = invoke(
        "--json",
        "--session",
        session.session_id,
        "--limit",
        "5",
        "--after-sequence",
        str(first_payload["next_cursor"]),
    )
    assert second.returncode == 0, second.stderr
    second_payload = json.loads(second.stdout)
    assert second_payload["has_more"] is False
    assert second_payload["next_cursor"] == payload["next_cursor"]
    assert len(second_payload["activity"]["timeline"]) == 5

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
