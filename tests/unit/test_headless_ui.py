import io
import json

from ui.headless import HeadlessUI


def test_json_result_is_single_machine_readable_event() -> None:
    stream = io.StringIO()
    ui = HeadlessUI(output_format="json", stream=stream)
    ui.print_token("ignored")
    ui.emit_result({"response": "done", "session_id": "s1"})
    payload = json.loads(stream.getvalue())
    assert payload == {
        "type": "turn.completed",
        "response": "done",
        "session_id": "s1",
    }


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
    assert observed == [{"type": "tool.started", "call_id": "c1", "tool": "read_file"}]
