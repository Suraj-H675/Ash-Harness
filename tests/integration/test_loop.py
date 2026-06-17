"""Integration tests for the AshLoop orchestrator (Sprint 8)."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest

from core.loop import AshLoop
from core.recovery import CircuitBreaker, CircuitBreakerError
from core.session import Session, SessionStore
from providers.base import StreamChunk
from safety.guard import SafetyGuard
from tools.base import BaseTool, ToolResult
from ui.terminal import TerminalUI


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeProvider:
    """A canned provider that streams predetermined chunk sequences per call."""

    def __init__(self, scripts: list[list[str]]) -> None:
        # One entry per stream_chat() invocation: a list of text fragments
        # to yield in order. A fragment can be plain text or include a
        # tool_call XML block; the loop's parser handles the latter.
        self._scripts = [list(s) for s in scripts]
        self._call_count = 0
        self.received_messages: list[list[dict[str, Any]]] = []

    @property
    def model_name(self) -> str:
        return "fake-model"

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.received_messages.append(list(messages))
        if self._call_count >= len(self._scripts):
            # No more scripted output — return empty terminal text.
            yield StreamChunk(content="", is_done=True)
            return
        script = self._scripts[self._call_count]
        self._call_count += 1
        for fragment in script:
            yield StreamChunk(content=fragment)
        yield StreamChunk(content="", is_done=True)


class CountingReadTool(BaseTool):
    name = "read_file"
    description = "Fake read_file that returns a fixed string."
    args_schema = type("Args", (), {"__call__": lambda self, **kw: None})

    def __init__(self, safety_guard: SafetyGuard, output: str = "hello world", fail: bool = False) -> None:
        super().__init__(safety_guard)
        self.output = output
        self.fail = fail
        self.calls = 0

    async def run(self, **kwargs: Any) -> ToolResult:
        self.calls += 1
        if self.fail:
            return ToolResult(success=False, output="", error="boom")
        return ToolResult(success=True, output=self.output, token_count=2)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture
def safety_guard(tmp_workspace: Path) -> SafetyGuard:
    return SafetyGuard(project_root=tmp_workspace)


@pytest.fixture
def session_store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.db")


def _make_ui() -> TerminalUI:
    return TerminalUI(
        safety_tier="auto_approve",
        console=_silent_console(),
    )


def _silent_console():
    from rich.console import Console

    return Console(file=io.StringIO(), force_terminal=False, width=120)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_text_only_turn_persists_user_and_assistant_messages(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    provider = FakeProvider(scripts=[["Hello, ", "world!"]])
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=tmp_workspace,
    )

    response = asyncio.run(loop.run_turn("hi"))

    assert response == "Hello, world!"
    session = session_store.load_session(loop.current_session.session_id)
    roles = [m.role for m in session.messages]
    assert roles == ["user", "assistant"]
    assert session.messages[0].content == "hi"
    assert session.messages[1].content == "Hello, world!"


def test_tool_call_turn_executes_and_loops_back_to_provider(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    # First call: model emits a tool call. Second call: model returns terminal text.
    provider = FakeProvider(
        scripts=[
            [
                '<thought>reading</thought>',
                '<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>',
            ],
            ["File contents were: <response>done</response>"],
        ]
    )
    read_tool = CountingReadTool(safety_guard, output="FILE CONTENT")
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=tmp_workspace,
        tools={read_tool.name: read_tool},
    )

    response = asyncio.run(loop.run_turn("please read x.py"))

    assert response == "File contents were: done"
    assert read_tool.calls == 1
    assert provider._call_count == 2  # tool-call round then terminal text

    # Verify persistence: user, assistant (tool call), tool response, assistant (final).
    session = session_store.load_session(loop.current_session.session_id)
    roles = [m.role for m in session.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    tool_response = session.messages[2].content
    assert '<tool_response name="read_file"' in tool_response
    assert "FILE CONTENT" in tool_response


def test_tool_call_record_persisted_with_approval_and_result(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    provider = FakeProvider(
        scripts=[
            ['<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'],
            ["<response>finished</response>"],
        ]
    )
    read_tool = CountingReadTool(safety_guard, output="payload")
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=tmp_workspace,
        tools={read_tool.name: read_tool},
    )

    asyncio.run(loop.run_turn("read it"))

    session = session_store.load_session(loop.current_session.session_id)
    assert len(session.tool_calls) == 1
    record = session.tool_calls[0]
    assert record.tool_name == "read_file"
    assert record.approved is True
    assert record.executed is True
    assert record.result == "payload"
    assert record.error is None


def test_circuit_breaker_trips_after_repeated_failures(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    # Provider always emits a tool call, then continues forever. The breaker
    # should trip after max_failures consecutive failures of the same tool.
    provider = FakeProvider(
        scripts=[
            ['<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'],
            ['<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'],
            ['<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'],
            ['<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'],
        ]
    )
    read_tool = CountingReadTool(safety_guard, fail=True)
    cb = CircuitBreaker(max_failures=3)
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=tmp_workspace,
        tools={read_tool.name: read_tool},
        circuit_breaker=cb,
        max_turn_iterations=10,
    )

    # Three consecutive failures should trip and surface a CircuitBreakerError.
    with pytest.raises(CircuitBreakerError):
        asyncio.run(loop.run_turn("keep trying"))

    # Three tool calls were attempted before the trip.
    assert read_tool.calls == 3
    assert cb.is_tripped


def test_unknown_tool_breaks_loop_without_tripping_breaker(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    # First call: unknown tool call. Breaker should record a failure but
    # because the next iteration will produce only text, the loop terminates.
    provider = FakeProvider(
        scripts=[
            ['<call_tool name="nonexistent"><arg name="x">1</arg></call_tool>'],
            ["<response>ok</response>"],
        ]
    )
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=tmp_workspace,
        tools={},  # no tools registered
    )

    response = asyncio.run(loop.run_turn("go"))

    assert "ok" in response
    # Unknown-tool calls count as failures but do not trip on a single
    # occurrence.
    assert not loop.circuit_breaker.is_tripped


def test_session_can_be_restored_by_id(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    provider1 = FakeProvider(scripts=[["first response"]])
    loop1 = AshLoop(
        session_store=session_store,
        provider=provider1,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=tmp_workspace,
    )

    asyncio.run(loop1.run_turn("hello"))
    saved_id = loop1.current_session.session_id

    # New loop, same store: restore the session by id.
    provider2 = FakeProvider(scripts=[["second response"]])
    loop2 = AshLoop(
        session_store=session_store,
        provider=provider2,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=tmp_workspace,
    )
    asyncio.run(loop2.start_session(saved_id))

    assert loop2.current_session.session_id == saved_id
    # Restored session should already contain the first turn's messages.
    assert len(loop2.current_session.messages) == 2

    response = asyncio.run(loop2.run_turn("again"))
    assert response == "second response"
    # After the second turn, the session has 4 messages (user/asst x2).
    session = session_store.load_session(saved_id)
    assert len(session.messages) == 4


def test_denial_does_not_execute_tool(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    provider = FakeProvider(
        scripts=[
            ['<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'],
            ["<response>denied path</response>"],
        ]
    )
    read_tool = CountingReadTool(safety_guard)

    def deny(_name: str, _args: dict[str, Any]) -> bool:
        return False

    ui = TerminalUI(approval_callback=deny, console=_silent_console())
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=ui,
        project_root=tmp_workspace,
        tools={read_tool.name: read_tool},
    )

    response = asyncio.run(loop.run_turn("read"))

    assert read_tool.calls == 0
    assert "denied path" in response
    session = session_store.load_session(loop.current_session.session_id)
    record = session.tool_calls[0]
    assert record.approved is False
    assert record.executed is False
    assert record.error == "Denied by user"


def test_max_iterations_terminates_infinite_tool_loop(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    # Provider emits a tool call on every call — the loop should stop at
    # max_turn_iterations.
    infinite_scripts = [
        ['<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>']
        for _ in range(20)
    ]
    provider = FakeProvider(scripts=infinite_scripts)
    read_tool = CountingReadTool(safety_guard, output="data")
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=tmp_workspace,
        tools={read_tool.name: read_tool},
        max_turn_iterations=4,
    )

    response = asyncio.run(loop.run_turn("loop"))

    assert "max iterations" in response.lower()
    # We expect the loop to have made exactly max_turn_iterations calls.
    assert read_tool.calls == 4
