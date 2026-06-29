"""Tests for the streaming XML parser state machine (Sprint 7)."""

from typing import Iterable


from ui.parser import StreamingXMLParser


def _drive(parser: StreamingXMLParser, *chunks: str) -> list[tuple[str, object]]:
    """Drain the parser over the given chunks and collect every yielded event."""

    events: list[tuple[str, object]] = []
    for chunk in chunks:
        events.extend(parser.feed(chunk))
    return events


def test_simple_text_yields_single_token() -> None:
    parser = StreamingXMLParser()

    events = _drive(parser, "Hello, world!")

    assert events == [("token", "Hello, world!")]


def test_thought_yields_thought_event() -> None:
    parser = StreamingXMLParser()

    events = _drive(parser, "<thought>reasoning</thought>")

    assert events == [("thought", "reasoning")]


def test_complete_tool_call_in_single_feed() -> None:
    parser = StreamingXMLParser()

    events = _drive(
        parser,
        '<call_tool name="read_file"><arg name="file_path">notes.txt</arg></call_tool>',
    )

    assert events == [
        (
            "tool_call",
            {"name": "read_file", "arguments": {"file_path": "notes.txt"}},
        )
    ]


def test_fragmented_tool_call_tag_yields_nothing_until_complete() -> None:
    parser = StreamingXMLParser()

    # Half a tag should buffer without yielding.
    assert _drive(parser, "<call_t") == []
    # Still incomplete even with the rest of the tag name.
    assert _drive(parser, "ool ") == []
    # Now we have a full opening tag — but no args/close yet.
    assert _drive(parser, 'name="read_file">') == []


def test_fragmented_tool_call_assembles_across_many_feeds() -> None:
    parser = StreamingXMLParser()

    _drive(parser, "<call_t")
    _drive(parser, 'ool name="read_file"')

    assert _drive(parser, ">") == []  # open tag complete, no args yet
    assert _drive(parser, '<arg name="file_path">') == []
    # Spec accumulates arg value silently until </arg>; no events yet.
    assert _drive(parser, "notes.") == []
    assert _drive(parser, "txt") == []
    # Closing </arg> commits the arg, but still inside the tool call.
    assert _drive(parser, "</arg>") == []
    # Closing </call_tool> finally yields the tool_call event.
    events = _drive(parser, "</call_tool>")

    assert events == [
        (
            "tool_call",
            {"name": "read_file", "arguments": {"file_path": "notes.txt"}},
        )
    ]


def test_fragmented_thought_streams_partial_content() -> None:
    parser = StreamingXMLParser()

    # Open the thought, then feed reasoning in pieces.
    _drive(parser, "<thought>")
    events_partial = _drive(parser, "rea", "soning")
    # Open + final close
    events_close = _drive(parser, "</thought>")

    # Partial thought yields carry just the new chunk.
    assert events_partial == [("thought", "rea"), ("thought", "soning")]
    # The closing yield carries the cumulative content.
    assert events_close == [("thought", "reasoning")]


def test_thought_then_text_yields_in_order() -> None:
    parser = StreamingXMLParser()

    events = _drive(parser, "before <thought>x</thought> after")

    assert events == [
        ("token", "before "),
        ("thought", "x"),
        ("token", " after"),
    ]


def test_text_between_tool_calls() -> None:
    parser = StreamingXMLParser()

    events = _drive(
        parser,
        'hi <call_tool name="a"></call_tool> mid <call_tool name="b"></call_tool> bye',
    )

    assert events == [
        ("token", "hi "),
        ("tool_call", {"name": "a", "arguments": {}}),
        ("token", " mid "),
        ("tool_call", {"name": "b", "arguments": {}}),
        ("token", " bye"),
    ]


def test_tool_call_with_multiple_args() -> None:
    parser = StreamingXMLParser()

    events = _drive(
        parser,
        '<call_tool name="write_file">'
        '<arg name="file_path">a.py</arg>'
        '<arg name="content">print("hi")</arg>'
        '<arg name="overwrite">true</arg>'
        "</call_tool>",
    )

    assert events == [
        (
            "tool_call",
            {
                "name": "write_file",
                "arguments": {
                    "file_path": "a.py",
                    "content": 'print("hi")',
                    "overwrite": "true",
                },
            },
        )
    ]


def test_single_quoted_attribute_name_works() -> None:
    parser = StreamingXMLParser()

    events = _drive(parser, "<call_tool name='my-tool'></call_tool>")

    assert events == [("tool_call", {"name": "my-tool", "arguments": {}})]


def test_partial_tag_at_chunk_boundary_does_not_split_tag() -> None:
    parser = StreamingXMLParser()

    # Buffer reaches an exact `<` at the chunk boundary; parser must wait.
    assert _drive(parser, "hello <") == [("token", "hello ")]
    assert _drive(parser, 'call_tool name="x"></call_tool>') == [
        ("tool_call", {"name": "x", "arguments": {}})
    ]


def test_multiple_tool_calls_in_single_feed() -> None:
    parser = StreamingXMLParser()

    events = _drive(
        parser,
        '<call_tool name="a"></call_tool><call_tool name="b"></call_tool>',
    )

    assert events == [
        ("tool_call", {"name": "a", "arguments": {}}),
        ("tool_call", {"name": "b", "arguments": {}}),
    ]


def test_adjacent_tool_calls_with_arguments_remain_separate() -> None:
    parser = StreamingXMLParser()

    events = _drive(
        parser,
        '<call_tool name="first"><arg name="query">one</arg></call_tool>'
        '<call_tool name="second"><arg name="query">two</arg></call_tool>',
    )

    assert events == [
        ("tool_call", {"name": "first", "arguments": {"query": "one"}}),
        ("tool_call", {"name": "second", "arguments": {"query": "two"}}),
    ]


def test_special_characters_in_arg_value_are_preserved() -> None:
    parser = StreamingXMLParser()

    events = _drive(
        parser,
        '<call_tool name="run_command">'
        '<arg name="command_line">echo "hi &amp; bye" | grep hi</arg>'
        "</call_tool>",
    )

    payload = events[0][1]
    assert isinstance(payload, dict)
    assert payload["name"] == "run_command"
    assert payload["arguments"]["command_line"] == 'echo "hi &amp; bye" | grep hi'


def test_incomplete_tool_call_buffers_until_close() -> None:
    parser = StreamingXMLParser()

    _drive(parser, '<call_tool name="x">')
    _drive(parser, '<arg name="k">v')

    # No events yet because </call_tool> never appeared.
    assert _drive(parser, "more text") == []
    assert _drive(parser, "</arg></call_tool>") == [
        ("tool_call", {"name": "x", "arguments": {"k": "vmore text"}})
    ]


def test_empty_chunk_yields_nothing() -> None:
    parser = StreamingXMLParser()

    assert _drive(parser, "") == []
    # After a no-op, parser is still healthy.
    assert _drive(parser, "ok") == [("token", "ok")]


def test_reset_clears_all_state() -> None:
    parser = StreamingXMLParser()

    _drive(parser, "<thought>partial")
    parser.reset()

    # After reset, prior partial thought is gone.
    assert _drive(parser, "fresh text") == [("token", "fresh text")]


def test_state_persists_across_feed_calls() -> None:
    parser = StreamingXMLParser()

    # Single character feeds must still produce the correct parse.
    payload = '<call_tool name="x"><arg name="k">v</arg></call_tool>'
    events: list[tuple[str, object]] = []
    for char in payload:
        events.extend(parser.feed(char))

    assert events == [("tool_call", {"name": "x", "arguments": {"k": "v"}})]


def test_unicode_content_in_text_and_thought() -> None:
    parser = StreamingXMLParser()

    events = _drive(parser, "héllo <thought>wörld 🌍</thought> ✓")

    assert events == [
        ("token", "héllo "),
        ("thought", "wörld 🌍"),
        ("token", " ✓"),
    ]


def test_thought_with_partial_chunk_emits_only_new_content() -> None:
    parser = StreamingXMLParser()

    _drive(parser, "<thought>")
    first = _drive(parser, "alpha")
    second = _drive(parser, " beta")
    final = _drive(parser, "</thought>")

    # Partial yields should contain only the just-arrived slice.
    assert first == [("thought", "alpha")]
    assert second == [("thought", " beta")]
    # Final yield contains the full accumulated thought text.
    assert final == [("thought", "alpha beta")]


def test_consumer_can_iterate_during_streaming() -> None:
    """feed() must be re-entrant: each call should yield events as the buffer resolves."""

    parser = StreamingXMLParser()
    collected: list[tuple[str, object]] = []

    # Simulate a live stream: each chunk drives a separate feed call.
    stream: Iterable[str] = iter(
        ["He", 'llo <call_tool name="r"><arg name="p"', ">path.txt</arg></call_tool>"]
    )
    for chunk in stream:
        for event in parser.feed(chunk):
            collected.append(event)

    assert collected == [
        ("token", "He"),
        ("token", "llo "),
        ("tool_call", {"name": "r", "arguments": {"p": "path.txt"}}),
    ]


def test_arg_value_with_xml_like_text_handles_correctly() -> None:
    parser = StreamingXMLParser()

    # Angle bracket inside an arg value is fine; only </arg> closes it.
    events = _drive(
        parser,
        '<call_tool name="x"><arg name="data">a < b > c</arg></call_tool>',
    )

    payload = events[0][1]
    assert isinstance(payload, dict)
    assert payload["arguments"]["data"] == "a < b > c"


def test_partial_tool_open_without_name_attribute_waits() -> None:
    parser = StreamingXMLParser()

    # `<call_tool>` without a name attribute is malformed; parser must wait
    # for more input rather than emitting a tool_call with name=None.
    assert _drive(parser, "<call_tool") == []
    # Even when we feed the closing `>`, the parser cannot name the tool.
    assert _drive(parser, ">") == []
    # Once a proper name appears, we recover.
    assert _drive(parser, ' name="recovered"></call_tool>') == [
        ("tool_call", {"name": "recovered", "arguments": {}})
    ]


def test_parser_does_not_yield_during_empty_state() -> None:
    parser = StreamingXMLParser()

    # No feeds yet.
    assert list(parser.feed("")) == []


def test_full_interaction_trace_example_from_spec() -> None:
    """Mirror the example interaction shown in the spec, fragmented across feeds."""

    parser = StreamingXMLParser()
    events = _drive(
        parser,
        "<thought>I need to read config.py</thought>",
        '<call_tool name="read_file">',
        '<arg name="file_path">config.py</arg>',
        "</call_tool>",
    )

    assert events == [
        ("thought", "I need to read config.py"),
        (
            "tool_call",
            {
                "name": "read_file",
                "arguments": {"file_path": "config.py"},
            },
        ),
    ]
