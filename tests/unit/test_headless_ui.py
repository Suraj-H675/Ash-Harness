import io
import json

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
