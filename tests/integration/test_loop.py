"""Integration tests for the AshLoop orchestrator (Sprint 8)."""

from __future__ import annotations

import asyncio
import io
import json
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, AsyncGenerator
from xml.sax.saxutils import escape

import pytest
from pydantic import BaseModel

from ash.core.goals import GoalState
from ash.core.loop import AshLoop
from ash.core.recovery import CircuitBreaker
from ash.core.session import SessionStorageError, SessionStore
from ash.providers.base import StreamChunk
from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult
from ash.tools.command import RunCommandTool
from ash.tools.filesystem import ReadFileTool
from ash.tools.goals import UpdateGoalTool
from ash.tools.patch import ApplyPatchTool
from ash.ui.terminal import TerminalUI


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
        tools: list[dict[str, Any]] | None = None,
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

    async def aclose(self) -> None:
        return None


class ReadArgs(BaseModel):
    file_path: str


class CountingReadTool(BaseTool):
    name = "read_file"
    description = "Fake read_file that returns a fixed string."
    args_schema = ReadArgs

    def __init__(
        self, safety_guard: SafetyGuard, output: str = "hello world", fail: bool = False
    ) -> None:
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


def test_default_prompt_distinguishes_test_passes_from_task_verification(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    provider = FakeProvider(scripts=[["Acknowledged."]])
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=tmp_workspace,
    )

    assert asyncio.run(loop.run_turn("Fix an existing function")) == "Acknowledged."

    system = provider.received_messages[0][0]["content"]
    assert "Passing available tests does not prove untested requirements" in system
    assert "Run focused checks when permitted" in system
    assert "Never claim a check was run when it was not" in system


def test_continuous_turns_finalize_each_step_usage_before_follow_up(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    class UsageProvider(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            async for chunk in super().stream_chat(messages, temperature, tools):
                if chunk.is_done:
                    yield chunk.model_copy(
                        update={
                            "prompt_tokens": 7,
                            "completion_tokens": 3,
                            "usage_source": "provider",
                        }
                    )
                else:
                    yield chunk

    provider = UsageProvider(scripts=[["initial answer"], ["follow-up answer"]])
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=tmp_workspace,
        continuous_mode=True,
        max_continuous_turns=1,
    )
    events: list[dict[str, Any]] = []
    old_emit = loop._emit_event

    def capture(event: dict[str, Any]) -> None:
        events.append(event.copy())
        old_emit(event)

    loop._emit_event = capture  # type: ignore[method-assign]
    assert asyncio.run(loop.run_turn("Start work")) == "follow-up answer"

    session = loop.current_session
    assert session is not None
    assert provider._call_count == 2
    usage = session_store.get_session_usage(session.session_id)
    assert usage.prompt_tokens == 14
    assert usage.completion_tokens == 6
    assert session_store.started_turns(session.session_id) == []
    assert [event["type"] for event in events].count("turn.completed") == 2
    assert [message.content for message in session_store.load_session(session.session_id).messages] == [
        "Start work",
        "initial answer",
        "Continue the previous task. What is the next step?",
        "follow-up answer",
    ]


def test_tool_call_turn_executes_and_loops_back_to_provider(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    # First call: model emits a tool call. Second call: model returns terminal text.
    provider = FakeProvider(
        scripts=[
            [
                "<thought>reading</thought>",
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


@pytest.mark.asyncio
async def test_quiet_command_exit_code_reaches_provider_during_repair(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    (tmp_workspace / "mathlib.py").write_text(
        "def double(value):\n    return value\n\n", encoding="utf-8"
    )
    (tmp_workspace / "quiet_check.py").write_text(
        "import io\n"
        "import unittest\n"
        "from mathlib import double\n\n"
        "class TestDouble(unittest.TestCase):\n"
        "    def test_double(self):\n"
        "        self.assertEqual(double(4), 8)\n\n"
        "result = unittest.TextTestRunner(\n"
        "    stream=io.StringIO(), verbosity=0\n"
        ").run(unittest.defaultTestLoader.loadTestsFromTestCase(TestDouble))\n"
        "raise SystemExit(0 if result.wasSuccessful() else 23)\n",
        encoding="utf-8",
    )
    git_init = subprocess.run(
        ["git", "init", "-q"],
        cwd=tmp_workspace,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert git_init.returncode == 0, git_init.stderr

    command = f"{shlex.quote(sys.executable)} -B quiet_check.py"
    patch = (
        "--- a/mathlib.py\n"
        "+++ b/mathlib.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def double(value):\n"
        "-    return value\n"
        "+    return value * 2\n"
        " \n"
    )
    provider = FakeProvider(
        scripts=[
            [
                '<call_tool name="run_command"><arg name="command_line">'
                + escape(command)
                + "</arg></call_tool>"
            ],
            [
                '<call_tool name="read_file"><arg name="file_path">'
                "quiet_check.py</arg></call_tool>"
            ],
            [
                '<call_tool name="read_file"><arg name="file_path">'
                "mathlib.py</arg></call_tool>"
            ],
            [
                '<call_tool name="apply_patch"><arg name="patch">'
                + escape(patch)
                + "</arg></call_tool>"
            ],
            [
                '<call_tool name="run_command"><arg name="command_line">'
                + escape(command)
                + "</arg></call_tool>"
            ],
            ["The quiet check passes after fixing double."],
        ]
    )
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=tmp_workspace,
        tools={
            "read_file": ReadFileTool(safety_guard),
            "apply_patch": ApplyPatchTool(safety_guard),
            "run_command": RunCommandTool(
                safety_guard,
                project_root=tmp_workspace,
            ),
        },
        safety_tier="auto_approve",
    )
    events: list[dict[str, Any]] = []
    original_emit_event = loop.ui.emit_event

    def capture_event(event: dict[str, Any]) -> None:
        events.append(event.copy())
        original_emit_event(event)

    loop.ui.emit_event = capture_event  # type: ignore[method-assign]

    try:
        response = await loop.run_turn(
            "Fix the bug in double and run the quiet regression check."
        )
        failure_message = next(
            message
            for message in provider.received_messages[1]
            if message.get("role") == "tool"
            and '<tool_response name="run_command"' in str(message.get("content"))
        )
        failure_payload = json.loads(
            ET.fromstring(str(failure_message["content"])).text or "{}"
        )
        assert failure_payload["success"] is False
        assert failure_payload["output"] == ""
        assert failure_payload["error"] is None
        assert failure_payload.get("exit_code") == 23

        latest_command_result = next(
            message
            for message in reversed(provider.received_messages[-1])
            if message.get("role") == "tool"
            and '<tool_response name="run_command"' in str(message.get("content"))
        )
        final_payload = json.loads(
            ET.fromstring(str(latest_command_result["content"])).text or "{}"
        )
        assert final_payload["success"] is True
        assert final_payload.get("exit_code") == 0
        command_events = [
            event
            for event in events
            if event.get("type") == "tool.completed"
            and event.get("tool") == "run_command"
        ]
        assert [event.get("exit_code") for event in command_events] == [23, 0]
        assert response == "The quiet check passes after fixing double."
        assert (tmp_workspace / "mathlib.py").read_text(encoding="utf-8") == (
            "def double(value):\n    return value * 2\n\n"
        )
        final_test = subprocess.run(
            [sys.executable, "-B", "quiet_check.py"],
            cwd=tmp_workspace,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        assert final_test.returncode == 0, final_test.stderr

        session = session_store.load_session(loop.current_session.session_id)
        assert len(session.tool_calls) == 5
        assert all(
            call.approved and call.executed and call.dispatched
            for call in session.tool_calls
        )
        command_audits = [
            entry
            for entry in session_store.list_audit_logs(session.session_id)
            if entry.action_type == "command_run"
        ]
        assert [entry.details.get("exit_code") for entry in command_audits] == [
            23,
            0,
        ]
        assert session_store.verify_audit_log(session.session_id) == []
    finally:
        await loop.aclose()


def _attach_goal_tool(loop: AshLoop, safety_guard: SafetyGuard) -> None:
    goal_tool = UpdateGoalTool(safety_guard, loop.update_goal)
    loop.tools[goal_tool.name] = goal_tool


@pytest.mark.asyncio
async def test_goal_controls_refuse_another_process_active_turn(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    loop = AshLoop(
        session_store=session_store,
        provider=FakeProvider(scripts=[]),
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=tmp_workspace,
    )
    session = await loop.start_session()

    lease = session_store.acquire_session_runtime_lease(session.session_id)
    try:
        with pytest.raises(SessionStorageError, match="active in another Ash process"):
            loop.create_goal("Original objective")
    finally:
        lease.close()

    goal = loop.create_goal("Original objective")
    lease = session_store.acquire_session_runtime_lease(session.session_id)
    try:
        for action in (loop.pause_goal, loop.resume_goal, loop.clear_goal):
            with pytest.raises(
                SessionStorageError, match="active in another Ash process"
            ):
                action()
    finally:
        lease.close()

    assert session_store.load_current_goal(session.session_id) == goal


def test_active_goal_auto_continues_real_work_until_verified_complete(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    provider = FakeProvider(
        scripts=[
            [
                '<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'
            ],
            ["<response>Initial evidence collected.</response>"],
            [
                '<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'
            ],
            [
                '<call_tool name="update_goal">'
                '<arg name="action">complete</arg>'
                '<arg name="evidence">Verified x.py after a second read</arg>'
                "</call_tool>"
            ],
            ["<response>Goal verified complete.</response>"],
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
        max_goal_continuations=3,
    )
    _attach_goal_tool(loop, safety_guard)
    asyncio.run(loop.start_session())
    goal = loop.create_goal("Read x.py twice and verify the result")
    emitted: list[dict[str, Any]] = []
    original_emit = loop._emit_event

    def capture_event(event: dict[str, Any]) -> None:
        emitted.append(dict(event))
        original_emit(event)

    loop._emit_event = capture_event  # type: ignore[method-assign]

    response = asyncio.run(loop.run_turn("Start the active Goal"))

    assert response == "Goal verified complete."
    assert read_tool.calls == 2
    assert provider._call_count == 5
    assert session_store.load_goal(goal.goal_id).state is GoalState.COMPLETE
    assert session_store.load_current_goal(goal.session_id) is None
    first_system = provider.received_messages[0][0]["content"]
    assert "## Active Goal" in first_system
    assert goal.objective in first_system
    assert [event["type"] for event in emitted].count("goal.step.completed") == 2
    terminal = [event for event in emitted if event["type"] == "turn.completed"]
    assert len(terminal) == 1
    step_usage = [
        event["usage"]
        for event in emitted
        if event["type"] == "goal.step.completed"
    ]
    assert terminal[0]["usage"]["prompt_tokens"] == sum(
        int(usage["prompt_tokens"]) for usage in step_usage
    )
    assert terminal[0]["usage"]["completion_tokens"] == sum(
        int(usage["completion_tokens"]) for usage in step_usage
    )


def test_active_goal_does_not_spin_after_no_tool_work(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    provider = FakeProvider(scripts=[["<response>I need more input.</response>"]])
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=tmp_workspace,
    )
    asyncio.run(loop.start_session())
    goal = loop.create_goal("Investigate the issue")

    response = asyncio.run(loop.run_turn("Start the active Goal"))

    assert response == "I need more input."
    assert provider._call_count == 1
    assert session_store.load_goal(goal.goal_id).state is GoalState.ACTIVE
    assert session_store.load_goal(goal.goal_id).continuations_used == 0


def test_goal_bookkeeping_alone_does_not_justify_auto_continuation(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    provider = FakeProvider(
        scripts=[
            [
                '<call_tool name="update_goal">'
                '<arg name="action">progress</arg>'
                '<arg name="evidence">No external work completed yet</arg>'
                "</call_tool>"
            ],
            ["<response>Blocked pending more information.</response>"],
        ]
    )
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=tmp_workspace,
    )
    _attach_goal_tool(loop, safety_guard)
    asyncio.run(loop.start_session())
    goal = loop.create_goal("Resolve the blocker")

    response = asyncio.run(loop.run_turn("Start the active Goal"))

    assert response == "Blocked pending more information."
    assert provider._call_count == 2
    persisted = session_store.load_goal(goal.goal_id)
    assert persisted.state is GoalState.ACTIVE
    assert persisted.continuations_used == 0
    assert persisted.last_evidence == "No external work completed yet"


def test_active_goal_stops_at_automatic_continuation_budget(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    provider = FakeProvider(
        scripts=[
            [
                '<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'
            ],
            ["<response>First pass.</response>"],
            [
                '<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'
            ],
            ["<response>Second pass still needs work.</response>"],
        ]
    )
    read_tool = CountingReadTool(safety_guard)
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=tmp_workspace,
        tools={read_tool.name: read_tool},
        max_goal_continuations=1,
    )
    asyncio.run(loop.start_session())
    goal = loop.create_goal("Keep reading until verified")

    response = asyncio.run(loop.run_turn("Start the active Goal"))

    persisted = session_store.load_goal(goal.goal_id)
    assert persisted.state is GoalState.BUDGET_LIMITED
    assert persisted.continuations_used == 1
    assert provider._call_count == 4
    assert read_tool.calls == 2
    assert "automatic-continuation budget exhausted (1/1)" in response


@pytest.mark.asyncio
async def test_cancelling_active_goal_pauses_it_durably(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    started = asyncio.Event()

    class BlockingProvider(FakeProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            del messages, temperature, tools
            started.set()
            await asyncio.Event().wait()
            yield StreamChunk(content="", is_done=True)  # pragma: no cover

    provider = BlockingProvider(scripts=[])
    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=_make_ui(),
        project_root=tmp_workspace,
    )
    await loop.start_session()
    goal = loop.create_goal("Finish work unless interrupted")
    turn = asyncio.create_task(loop.run_turn("Start the active Goal"))
    await asyncio.wait_for(started.wait(), timeout=1)

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    persisted = session_store.load_goal(goal.goal_id)
    assert persisted.state is GoalState.PAUSED
    assert session_store.load_current_goal(goal.session_id) == persisted


def test_tool_call_record_persisted_with_approval_and_result(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    provider = FakeProvider(
        scripts=[
            [
                '<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'
            ],
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
            [
                '<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'
            ],
            [
                '<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'
            ],
            [
                '<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'
            ],
            [
                '<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'
            ],
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

    # Three consecutive failures should trip the breaker.
    # The loop catches CircuitBreakerError internally and returns a message.
    result = asyncio.run(loop.run_turn("keep trying"))

    # Three tool calls were attempted before the trip.
    assert read_tool.calls == 3
    assert cb.is_tripped
    # Loop returns a friendly message instead of propagating the error.
    assert "circuit breaker" in result.lower()


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
            [
                '<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'
            ],
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


def test_user_denial_feedback_is_returned_to_model(
    tmp_workspace: Path,
    safety_guard: SafetyGuard,
    session_store: SessionStore,
) -> None:
    provider = FakeProvider(
        scripts=[
            [
                '<call_tool name="read_file"><arg name="file_path">x.py</arg></call_tool>'
            ],
            ["<response>adjusted after feedback</response>"],
        ]
    )
    read_tool = CountingReadTool(safety_guard)

    def deny_with_feedback(_name: str, _args: dict[str, Any]) -> str:
        return "Read only the public docs directory"

    ui = TerminalUI(
        approval_callback=deny_with_feedback,
        console=_silent_console(),
    )
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
    assert "adjusted after feedback" in response
    record = session_store.load_session(loop.current_session.session_id).tool_calls[0]
    assert record.approved is False
    assert record.error == (
        "Denied by user: Read only the public docs directory"
    )


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
    completed = next(
        event.event
        for event in reversed(
            session_store.list_runtime_events(loop.current_session.session_id)
        )
        if event.event["type"] == "turn.completed"
    )
    assert completed["stop_reason"] == "iteration_limit"
    assert completed["iterations"] == 4
    assert completed["iteration_limit"] == 4
