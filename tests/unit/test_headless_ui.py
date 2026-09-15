import io
import json

import pytest

from ash.ui.headless import HeadlessUI


def _assert_envelope(payload: dict) -> None:
    assert payload["schema_version"] == 1
    assert payload["event_id"]
    assert payload["timestamp"]
    assert payload["source"] == {"type": "runtime", "id": "ash"}


def test_json_result_is_single_machine_readable_event() -> None:
    stream = io.StringIO()
    ui = HeadlessUI(output_format="json", stream=stream)
    ui.print_token("ignored")
    ui.emit_result({"response": "done", "session_id": "s1"})
    payload = json.loads(stream.getvalue())
    _assert_envelope(payload)
    assert payload["type"] == "turn.completed"
    assert payload["response"] == "done"
    assert payload["session_id"] == "s1"


def test_stream_json_emits_deltas_and_completion() -> None:
    stream = io.StringIO()
    ui = HeadlessUI(output_format="stream-json", stream=stream)
    ui.print_token("a")
    ui.emit_result({"response": "a", "session_id": "s1"})
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["type"] for event in events] == [
        "assistant.delta",
        "turn.completed",
    ]


def test_runtime_bound_stream_json_does_not_duplicate_turn_completion() -> None:
    stream = io.StringIO()
    ui = HeadlessUI(output_format="stream-json", stream=stream)
    ui.set_event_enricher(lambda payload: dict(payload))

    ui.emit_event({"type": "turn.completed", "response": "done", "session_id": "s1"})
    ui.emit_result({"response": "done", "session_id": "s1"})

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["type"] for event in events] == ["turn.completed"]


@pytest.mark.asyncio
async def test_one_shot_stream_json_has_one_authoritative_completion(tmp_path) -> None:
    from ash.cli import _bootstrap_and_headless
    from ash.config import AshConfig
    from ash.core.loop import AshLoop
    from ash.core.session import SessionStore
    from ash.providers.base import ProviderABC, StreamChunk
    from ash.safety.guard import SafetyGuard

    class PlainProvider(ProviderABC):
        model_name = "plain-test"

        def count_tokens(self, text):
            return len(text)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            yield StreamChunk(
                content="done",
                is_done=True,
                prompt_tokens=1,
                completion_tokens=1,
                usage_source="provider",
            )

    stream = io.StringIO()
    ui = HeadlessUI(output_format="stream-json", stream=stream)
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        model="openai/plain-test",
    )
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        PlainProvider(),
        SafetyGuard(tmp_path),
        ui,
        tmp_path,
        config=config,
    )

    try:
        assert (
            await _bootstrap_and_headless(
                loop, config, prompt="hello", session_id=None, ui=ui
            )
            == 0
        )
    finally:
        await loop.aclose()

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert sum(event["type"] == "turn.completed" for event in events) == 1
    assert events[-1]["type"] == "turn.completed"
    assert events[-1]["response"] == "done"
    assert events[-1]["model"] == "plain-test"
    assert events[-1]["model_id"] == "openai/plain-test"


@pytest.mark.asyncio
async def test_one_shot_json_reports_canonical_active_model(tmp_path) -> None:
    from ash.cli import _bootstrap_and_headless
    from ash.config import AshConfig
    from ash.core.loop import AshLoop
    from ash.core.session import SessionStore
    from ash.providers.base import ProviderABC, StreamChunk
    from ash.safety.guard import SafetyGuard

    class PlainProvider(ProviderABC):
        model_name = "plain-test"

        def count_tokens(self, text):
            return len(text)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            yield StreamChunk(content="done", is_done=True)

    stream = io.StringIO()
    ui = HeadlessUI(output_format="json", stream=stream)
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        model="openai/plain-test",
    )
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        PlainProvider(),
        SafetyGuard(tmp_path),
        ui,
        tmp_path,
        config=config,
    )

    try:
        assert (
            await _bootstrap_and_headless(
                loop, config, prompt="hello", session_id=None, ui=ui
            )
            == 0
        )
    finally:
        await loop.aclose()

    payload = json.loads(stream.getvalue())
    assert payload["type"] == "turn.completed"
    assert payload["model"] == "openai/plain-test"
    assert payload["model_id"] == "openai/plain-test"
    persisted = loop.session_store.list_runtime_events(payload["session_id"], limit=100)
    completions = [item.event for item in persisted if item.event["type"] == "turn.completed"]
    assert len(completions) == 1
    assert payload["event_id"] == completions[0]["event_id"]
    assert payload["turn_id"] == completions[0]["turn_id"]



def test_json_error_is_structured_machine_readable_event() -> None:
    stream = io.StringIO()
    ui = HeadlessUI(output_format="json", stream=stream)

    ui.emit_error(
        {
            "category": "provider",
            "message": "missing key",
            "remedy": "run setup",
            "exit_code": 1,
            "retriable": False,
        }
    )

    payload = json.loads(stream.getvalue())
    _assert_envelope(payload)
    assert payload["type"] == "error"
    assert payload["error"] == {
        "category": "provider",
        "message": "missing key",
        "remedy": "run setup",
        "exit_code": 1,
        "retriable": False,
    }


def test_headless_approval_fails_closed() -> None:
    ui = HeadlessUI(output_format="text", stream=io.StringIO())
    assert ui.request_tool_approval("run_command", {"command": "x"}) is False


def test_stream_json_emits_tool_lifecycle_events() -> None:
    stream = io.StringIO()
    ui = HeadlessUI(output_format="stream-json", stream=stream)
    observed = []
    unsubscribe = ui.subscribe(observed.append)

    ui.emit_event({"type": "tool.started", "call_id": "c1", "tool": "read_file"})
    unsubscribe()

    assert json.loads(stream.getvalue())["type"] == "tool.started"
    assert len(observed) == 1
    _assert_envelope(observed[0])
    assert observed[0]["type"] == "tool.started"
    assert observed[0]["call_id"] == "c1"
    assert observed[0]["tool"] == "read_file"


def test_headless_ui_never_claims_interactive_mcp_review() -> None:
    ui = HeadlessUI(output_format="text")

    assert ui.supports_mcp_interactions is False
    assert ui.review_mcp_sampling("docs", "request", {}) is False
    assert ui.request_mcp_elicitation("docs", "question", {}) == {
        "action": "decline"
    }
