import io
import json
from types import SimpleNamespace

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_reason", "expected_code"),
    [
        ("completed", 0),
        ("iteration_limit", 1),
        ("token_budget", 1),
        ("circuit_breaker", 1),
    ],
)
async def test_headless_unfinished_turn_preserves_result_and_exit_status(
    stop_reason: str, expected_code: int
) -> None:
    from ash.cli import _bootstrap_and_headless

    class FakeLoop:
        provider = None
        safety_guard = None
        recovered_turns = 0
        recovery_summary = None
        active_model_id = "fixture/model"
        _last_context_tokens = 100
        last_turn_usage = {"prompt_tokens": 10, "completion_tokens": 3}
        _last_turn_stop_reason = stop_reason

        async def start_session(self, session_id=None):
            assert session_id is None
            return SimpleNamespace(session_id="fixture-session")

        async def run_turn(self, prompt: str) -> str:
            assert prompt == "Run task."
            return "fixture-response"

        async def aclose(self) -> None:
            return None

    stream = io.StringIO()
    ui = HeadlessUI(output_format="json", stream=stream)
    code = await _bootstrap_and_headless(
        FakeLoop(), SimpleNamespace(), prompt="Run task.", session_id=None, ui=ui
    )
    assert code == expected_code
    result = json.loads(stream.getvalue())
    assert result["type"] == "turn.completed"
    assert result["response"] == "fixture-response"
    assert result["session_id"] == "fixture-session"


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [True, False])
async def test_one_shot_reasoning_effort_is_applied_before_model_request_and_cleaned_up(
    supported: bool,
) -> None:
    from ash.cli import _bootstrap_and_headless

    calls: list[str] = []

    class FakeLoop:
        provider = None
        safety_guard = None
        recovered_turns = 0
        recovery_summary = None
        active_model_id = "openai/gpt-6-luna"
        _last_context_tokens = 12
        last_turn_usage = {"prompt_tokens": 1, "completion_tokens": 1}
        _last_turn_stop_reason = "completed"

        def set_reasoning_effort(self, effort: str) -> None:
            calls.append("effort:" + effort)
            if not supported:
                raise ValueError("effort unavailable for model")

        async def start_session(self, session_id=None):
            calls.append("session")
            return SimpleNamespace(session_id="test-session")

        async def run_turn(self, prompt: str) -> str:
            calls.append("turn")
            return "done"

        async def aclose(self):
            calls.append("close")

    stream = io.StringIO()
    ui = HeadlessUI(output_format="json", stream=stream)
    code = await _bootstrap_and_headless(
        FakeLoop(),
        SimpleNamespace(),
        prompt="Fix bug",
        session_id=None,
        ui=ui,
        reasoning_effort="xhigh",
    )
    payload = json.loads(stream.getvalue())
    if supported:
        assert code == 0
        assert calls == ["effort:xhigh", "session", "turn", "close"]
        assert payload["type"] == "turn.completed"
    else:
        assert code != 0
        assert calls == ["effort:xhigh", "close"]
        assert payload["type"] == "error"


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


def test_runtime_bound_headless_events_share_text_bound(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    import ash.core.loop as loop_module
    from ash.core.loop import AshLoop
    from ash.core.session import SessionStore
    from ash.providers.base import ProviderABC
    from ash.safety.guard import SafetyGuard

    class IdleProvider(ProviderABC):
        model_name = "idle"

        def count_tokens(self, text):
            return len(text)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            if False:
                yield

    monkeypatch.setattr(loop_module, "MAX_RUNTIME_EVENT_TEXT_BYTES", 8)
    stream = io.StringIO()
    ui = HeadlessUI(output_format="stream-json", stream=stream)
    AshLoop(
        SessionStore(tmp_path / "events.db"),
        IdleProvider(),
        SafetyGuard(tmp_path),
        ui,
        tmp_path,
    )

    ui.show_plan(
        SimpleNamespace(contract=SimpleNamespace(contract_id="plan-1"))
    )

    event = json.loads(stream.getvalue())
    assert event["reason"] == "headless"
    assert event["event_text_truncated"] is True


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
@pytest.mark.parametrize(
    ("response", "expected_code", "terminal_type"),
    [('{"ok":true}', 0, "turn.completed"), ("not-json", 1, "error")],
)
async def test_stream_json_schema_has_one_validated_terminal_event(
    tmp_path, response, expected_code, terminal_type
) -> None:
    from ash.cli import _bootstrap_and_headless
    from ash.config import AshConfig
    from ash.core.loop import AshLoop
    from ash.core.session import SessionStore
    from ash.providers.base import ProviderABC, StreamChunk
    from ash.safety.guard import SafetyGuard

    class StructuredProvider(ProviderABC):
        model_name = "structured-test"

        def count_tokens(self, text):
            return len(text)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            yield StreamChunk(content=response, is_done=True)

    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        '{"type":"object","properties":{"ok":{"type":"boolean"}},'
        '"required":["ok"],"additionalProperties":false}'
    )
    stream = io.StringIO()
    ui = HeadlessUI(output_format="stream-json", stream=stream)
    config = AshConfig(
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        model="openai/structured-test",
    )
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        StructuredProvider(),
        SafetyGuard(tmp_path),
        ui,
        tmp_path,
        config=config,
    )

    code = await _bootstrap_and_headless(
        loop,
        config,
        prompt="structured",
        session_id=None,
        ui=ui,
        json_schema_path=schema_path,
    )

    assert code == expected_code
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    terminals = [event for event in events if event["type"] in {"turn.completed", "error"}]
    assert [event["type"] for event in terminals] == [terminal_type]
    if terminal_type == "turn.completed":
        assert terminals[0]["structured_output"] == {"ok": True}
    else:
        assert terminals[0]["error"]["category"] == "output"


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


def test_headless_text_result_neutralizes_terminal_controls() -> None:
    stream = io.StringIO()
    ui = HeadlessUI(output_format="text", stream=stream)

    ui.emit_result({"response": "safe\x1b[2J\u202ehidden\u202c", "session_id": "s1"})

    rendered = stream.getvalue()
    assert "\x1b[2J" not in rendered
    assert "\u202e" not in rendered
    assert "safe\\x1b[2J\\u202ehidden\\u202c" in rendered


def test_headless_text_error_neutralizes_terminal_controls(capsys) -> None:
    ui = HeadlessUI(output_format="text", stream=io.StringIO())

    ui.emit_error(
        {
            "category": "provider\nforged",
            "message": "bad\x1b]0;owned\x07\u202ehidden\u202c",
            "remedy": "retry\x1b[2J",
        }
    )

    rendered = capsys.readouterr().err
    assert "\x1b]0;" not in rendered
    assert "\x1b[2J" not in rendered
    assert "\u202e" not in rendered
    assert "provider\\x0aforged" in rendered
    assert "bad\\x1b]0;owned\\x07\\u202ehidden\\u202c" in rendered
    assert "retry\\x1b[2J" in rendered


def test_stream_json_emits_tool_lifecycle_events() -> None:
    stream = io.StringIO()
    ui = HeadlessUI(output_format="stream-json", stream=stream)
    observed = []
    unsubscribe = ui.subscribe(observed.append)

    ui.emit_event({"type": "tool.started", "call_id": "c1", "tool": "read_file"})
    ui.emit_event(
        {
            "type": "tool.completed",
            "call_id": "c2",
            "tool": "run_command",
            "success": False,
            "exit_code": 23,
        }
    )
    unsubscribe()

    rendered_events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["type"] for event in rendered_events] == [
        "tool.started",
        "tool.completed",
    ]
    assert rendered_events[1]["exit_code"] == 23
    assert len(observed) == 2
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
