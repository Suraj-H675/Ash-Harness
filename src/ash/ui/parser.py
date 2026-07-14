"""Streaming XML parser state machine for Ash model output."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Generator, Literal


class _State(StrEnum):
    TEXT = "TEXT"
    THOUGHT = "THOUGHT"
    RESPONSE = "RESPONSE"
    TOOL = "TOOL"
    ARG = "ARG"


EventType = Literal["token", "thought", "tool_call"]
Event = tuple[EventType, str | dict[str, Any]]


class StreamingXMLParseError(ValueError):
    """Raised when a fallback-model stream ends inside a control element."""


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
            elif self._state == _State.RESPONSE:
                events, progressed = self._process_response_state()
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

    def finish(self) -> list[Event]:
        """Finalize an end-of-stream buffer without silently losing content."""

        if self._state in {_State.TOOL, _State.ARG}:
            element = "tool argument" if self._state == _State.ARG else "tool call"
            self.reset()
            raise StreamingXMLParseError(
                f"model stream ended inside an incomplete {element}"
            )

        events: list[Event] = []
        if self._buffer:
            kind: EventType = "thought" if self._state == _State.THOUGHT else "token"
            events.append((kind, self._buffer))
        self.reset()
        return events

    # --- state processors ------------------------------------------------

    def _process_text_state(self) -> tuple[list[Event], bool]:
        openers = (
            ("<thought>", _State.THOUGHT),
            ("<response>", _State.RESPONSE),
            ("<call_tool", _State.TOOL),
        )
        matches = [
            (index, opener, state)
            for opener, state in openers
            if (index := self._buffer.find(opener)) >= 0
        ]
        if matches:
            index, opener, state = min(matches, key=lambda match: match[0])
            pre = self._buffer[:index]
            events: list[Event] = [("token", pre)] if pre else []
            self._buffer = self._buffer[index + len(opener) :]
            self._state = state
            if state == _State.THOUGHT:
                self._accumulated_text = ""
            elif state == _State.TOOL:
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

    def _process_response_state(self) -> tuple[list[Event], bool]:
        closer = "</response>"
        if closer in self._buffer:
            inner, post = self._buffer.split(closer, 1)
            self._state = _State.TEXT
            self._buffer = post
            return ([("token", inner)] if inner else []), True

        retained = _partial_suffix_length(self._buffer, closer)
        safe_length = len(self._buffer) - retained
        if safe_length <= 0:
            return [], False
        safe = self._buffer[:safe_length]
        self._buffer = self._buffer[safe_length:]
        return [("token", safe)], True

    def _process_thought_state(self) -> tuple[list[Event], bool]:
        if "</thought>" in self._buffer:
            thought_content, post = self._buffer.split("</thought>", 1)
            events: list[Event] = (
                [("thought", thought_content)] if thought_content else []
            )
            self._state = _State.TEXT
            self._buffer = post
            self._accumulated_text = ""
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
        close_index = self._buffer.find("</call_tool>")
        if close_index >= 0 and (arg_match is None or close_index < arg_match.start()):
            post = self._buffer[close_index + len("</call_tool>") :]
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

        if arg_match is not None:
            self._current_arg_name = arg_match.group(1)
            self._state = _State.ARG
            self._buffer = self._buffer[arg_match.end() :]
            self._accumulated_text = ""
            return [], True

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


def _partial_suffix_length(value: str, marker: str) -> int:
    """Return the longest suffix of ``value`` that could start ``marker``."""

    maximum = min(len(value), len(marker) - 1)
    for length in range(maximum, 0, -1):
        if marker.startswith(value[-length:]):
            return length
    return 0
