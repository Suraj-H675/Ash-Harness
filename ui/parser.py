"""Streaming XML parser state machine for Ash model output."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Generator, Literal


class _State(StrEnum):
    TEXT = "TEXT"
    THOUGHT = "THOUGHT"
    TOOL = "TOOL"
    ARG = "ARG"


EventType = Literal["token", "thought", "tool_call"]
Event = tuple[EventType, str | dict[str, Any]]


class StreamingXMLParser:
    """
    Character-buffered state machine that parses streamed LLM responses.

    Recognizes the tag schema defined in ``SYSTEM_PROMPTS_AND_TEMPLATES.md``
    section 3: ``<thought>`` reasoning blocks, ``<call_tool name="...">``
    invocations, and ``<arg name="...">`` parameter values. Partial input
    fragments are buffered until a tag boundary can be resolved, so the
    parser can drive events live off the wire without waiting for the
    full completion of a packet.

    Events yielded by :meth:`feed`:
        - ``("token", str)`` — a chunk of plain text outside any tag
        - ``("thought", str)`` — a reasoning segment, emitted incrementally
        - ``("tool_call", {"name": str, "arguments": dict[str, str]})`` —
          a fully parsed tool invocation
    """

    _TOOL_OPEN_PATTERN = re.compile(r'name=["\']([^"\']+)["\']\s*>')
    _ARG_OPEN_PATTERN = re.compile(r'<arg\s+name=["\']([^"\']+)["\']\s*>')

    def __init__(self) -> None:
        self._state: _State = _State.TEXT
        self._buffer: str = ""
        self._current_tool_name: str | None = None
        self._current_arg_name: str | None = None
        self._current_args: dict[str, str] = {}
        self._accumulated_text: str = ""

    def feed(self, chunk: str) -> Generator[Event, None, None]:
        """Append ``chunk`` to the buffer and yield every parseable event."""

        if not chunk:
            return

        self._buffer += chunk

        while self._buffer:
            if self._state == _State.TEXT:
                events, progressed = self._process_text_state()
            elif self._state == _State.THOUGHT:
                events, progressed = self._process_thought_state()
            elif self._state == _State.TOOL:
                events, progressed = self._process_tool_state()
            elif self._state == _State.ARG:
                events, progressed = self._process_arg_state()
            else:  # pragma: no cover - defensive
                break

            yield from events

            if not progressed:
                # Tag boundary not yet resolvable; wait for more input.
                break

    def reset(self) -> None:
        """Reset all parser state to a fresh instance's defaults."""

        self._state = _State.TEXT
        self._buffer = ""
        self._current_tool_name = None
        self._current_arg_name = None
        self._current_args = {}
        self._accumulated_text = ""

    # --- state processors ------------------------------------------------

    def _process_text_state(self) -> tuple[list[Event], bool]:
        if "<thought>" in self._buffer:
            pre, post = self._buffer.split("<thought>", 1)
            events: list[Event] = []
            if pre:
                events.append(("token", pre))
            self._state = _State.THOUGHT
            self._buffer = post
            self._accumulated_text = ""
            return events, True

        if "<response>" in self._buffer:
            # The model wraps its final user-facing text in <response> tags.
            # We strip the wrappers and emit the inner content as a token
            # so the loop can deliver it to the terminal/REPL.
            pre, post = self._buffer.split("<response>", 1)
            resp_events: list[Event] = []
            if pre:
                resp_events.append(("token", pre))
            if "</response>" in post:
                inner, rest = post.split("</response>", 1)
                if inner:
                    resp_events.append(("token", inner))
                self._state = _State.TEXT
                self._buffer = rest
            else:
                # Tag opened but not yet closed — wait for the rest.
                self._buffer = post
                return resp_events, False
            return resp_events, True

        if "<call_tool" in self._buffer:
            pre, post = self._buffer.split("<call_tool", 1)
            events = []
            if pre:
                events.append(("token", pre))
            self._state = _State.TOOL
            self._buffer = post
            self._current_args = {}
            self._current_tool_name = None
            return events, True

        idx = self._buffer.find("<")
        if idx == -1:
            chunk = self._buffer
            self._buffer = ""
            return [("token", chunk)], True
        if idx > 0:
            chunk = self._buffer[:idx]
            self._buffer = self._buffer[idx:]
            return [("token", chunk)], True
        # Buffer starts with `<`. If it's a known orphan closer we can drop
        # it and continue; otherwise wait for more input.
        if self._buffer.startswith("</response>"):
            self._buffer = self._buffer[len("</response>") :]
            return [], True
        if self._buffer.startswith("</thought>"):
            # Stray closer without a matching opener — drop it.
            self._buffer = self._buffer[len("</thought>") :]
            return [], True
        return [], False

    def _process_thought_state(self) -> tuple[list[Event], bool]:
        if "</thought>" in self._buffer:
            thought_content, post = self._buffer.split("</thought>", 1)
            self._accumulated_text += thought_content
            events: list[Event] = [("thought", self._accumulated_text)]
            self._state = _State.TEXT
            self._buffer = post
            return events, True

        idx = self._buffer.find("<")
        if idx == -1:
            self._accumulated_text += self._buffer
            chunk_text = self._buffer
            self._buffer = ""
            return [("thought", chunk_text)], True
        if idx > 0:
            chunk_text = self._buffer[:idx]
            self._accumulated_text += chunk_text
            self._buffer = self._buffer[idx:]
            return [("thought", chunk_text)], True
        return [], False

    def _process_tool_state(self) -> tuple[list[Event], bool]:
        if self._current_tool_name is None:
            match = self._TOOL_OPEN_PATTERN.search(self._buffer)
            if match is None:
                return [], False
            self._current_tool_name = match.group(1)
            self._buffer = self._buffer[match.end() :]
            return self._process_tool_state()

        arg_match = self._ARG_OPEN_PATTERN.search(self._buffer)
        if arg_match is not None:
            self._current_arg_name = arg_match.group(1)
            self._state = _State.ARG
            self._buffer = self._buffer[arg_match.end() :]
            self._accumulated_text = ""
            return [], True

        if "</call_tool>" in self._buffer:
            _, post = self._buffer.split("</call_tool>", 1)
            events: list[Event] = [
                (
                    "tool_call",
                    {
                        "name": self._current_tool_name,
                        "arguments": dict(self._current_args),
                    },
                )
            ]
            self._state = _State.TEXT
            self._buffer = post
            self._current_tool_name = None
            return events, True

        return [], False

    def _process_arg_state(self) -> tuple[list[Event], bool]:
        if "</arg>" in self._buffer:
            val, post = self._buffer.split("</arg>", 1)
            self._accumulated_text += val
            if self._current_arg_name is not None:
                self._current_args[self._current_arg_name] = self._accumulated_text
            self._state = _State.TOOL
            self._buffer = post
            self._current_arg_name = None
            return [], True

        idx = self._buffer.find("<")
        if idx == -1:
            self._accumulated_text += self._buffer
            self._buffer = ""
            return [], True
        if idx > 0:
            self._accumulated_text += self._buffer[:idx]
            self._buffer = self._buffer[idx:]
            return [], True
        return [], False
