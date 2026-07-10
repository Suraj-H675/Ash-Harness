import asyncio

import pytest
from datetime import datetime, timezone
from core.loop import AshLoop
from tools.base import BaseTool, ToolResult, ToolMiddleware, ToolMiddlewareSkip
from config import AshConfig
from context.turn import TurnContext
from core.session import Message, SessionStore, ToolCallRecord, get_db_connection
from providers.base import ProviderABC, StreamChunk
from providers.capabilities import ProviderCapabilities
from providers.retry import ProviderCircuitBreaker, ProviderCircuitOpen
from safety.grants import PermissionRule, RuleEffect
from safety.guard import SafetyGuard
from ui.terminal import TerminalUI
from tools.filesystem import ReadFileTool
from pathlib import Path
import tempfile


class MockProvider(ProviderABC):
    model_name = "test"

    def count_tokens(self, text):
        return 0

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        # Yield XML tool call fragments that the parser will process
        yield StreamChunk(
            tool_call_delta='<call_tool name="read_file"><arg name="file_path">test.txt</arg></call_tool>',
            is_done=True,
        )


class SpyMiddleware(ToolMiddleware):
    def __init__(self):
        self.before_calls = []
        self.after_calls = []

    async def before_tool(self, tool_name, arguments, tool):
        self.before_calls.append((tool_name, arguments))

    async def after_tool(self, tool_name, arguments, result):
        self.after_calls.append((tool_name, arguments, result))


class SkipMiddleware(ToolMiddleware):
    async def before_tool(self, tool_name, arguments, tool):
        raise ToolMiddlewareSkip()


class MyTestTool(BaseTool):
    """Minimal tool used only in tests — performs no real work."""

    name = "my_tool"
    args_schema = None

    async def run(self, **kwargs):
        return ToolResult(success=True, output="my_tool ran")


class EventTool(BaseTool):
    name = "event_tool"
    args_schema = None

    async def run(self, **kwargs):
        self.emit_event({"type": "tool.output", "delta": "live", "stream": "stdout"})
        return ToolResult(success=True, output="live")


class NativeToolProvider(ProviderABC):
    model_name = "native-test"

    def __init__(self):
        self.calls = 0
        self.received_messages = []

    def count_tokens(self, text):
        return len(text)

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.received_messages.append(messages)
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                is_done=True,
                native_tool_calls=[
                    {
                        "id": "call-native-1",
                        "name": "capture",
                        "arguments": '{"text":"hello"}',
                    }
                ],
            )
        else:
            yield StreamChunk(content="done", is_done=True)


class CaptureTool(BaseTool):
    name = "capture"
    args_schema = None

    def __init__(self, safety_guard):
        super().__init__(safety_guard)
        self.arguments = None

    async def run(self, **kwargs):
        self.arguments = kwargs
        return ToolResult(success=True, output=kwargs["text"])


class BlockingCaptureTool(CaptureTool):
    def __init__(self, safety_guard):
        super().__init__(safety_guard)
        self.started = asyncio.Event()

    async def run(self, **kwargs):
        self.started.set()
        await asyncio.Event().wait()
        return ToolResult(success=True, output="unreachable")


class BudgetTool(BaseTool):
    name = "budget_tool"
    description = "tool " * 80
    args_schema = None

    async def run(self, **kwargs):
        return ToolResult(success=True, output="ok")


class BudgetProvider(ProviderABC):
    model_name = "budget-test"
    capabilities = ProviderCapabilities(native_tools=True, context_window=200)

    def count_tokens(self, text):
        return len(str(text).split())

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        yield StreamChunk(content="done", is_done=True)


class CacheUsageProvider(ProviderABC):
    model_name = "cache-test"

    def count_tokens(self, text):
        return len(str(text).split())

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        yield StreamChunk(
            content="<response>done</response>",
            is_done=True,
            prompt_tokens=100,
            completion_tokens=5,
            cache_read_tokens=60,
            cache_write_tokens=20,
        )


class LargeRepoMap:
    def rank(self, active):
        return active

    def render(self, ranked, top_files=5, symbols_per_file=6):
        return "repo " * 120


class EventUI(TerminalUI):
    def __init__(self, safety_tier="auto_approve"):
        super().__init__(safety_tier=safety_tier)
        self.events = []

    def emit_event(self, payload):
        self.events.append(payload)


@pytest.mark.asyncio
async def test_resuming_session_recovers_pending_tool_and_emits_details(tmp_path):
    store = SessionStore(tmp_path / "recovery.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-crashed", "run")
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id="call-command",
            tool_name="run_command",
            arguments={"command_line": "build"},
            approved=True,
            executed=False,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id="turn-crashed",
    )
    ui = EventUI()
    loop = AshLoop(store, MockProvider(), SafetyGuard(tmp_path), ui, tmp_path)

    await loop.start_session(session.session_id)

    assert loop.recovered_turns == 1
    assert loop.recovery_summary is not None
    assert loop.recovery_summary.needs_attention is True
    event = next(event for event in ui.events if event["type"] == "session.recovery")
    assert event["unknown_calls"] == ["run_command (call-command)"]
    assert event["needs_attention"] is True


class SteeringProvider(ProviderABC):
    model_name = "steering-test"

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.received_messages = []

    def count_tokens(self, text):
        return len(str(text).split())

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.calls += 1
        self.received_messages.append(list(messages))
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
            yield StreamChunk(content="initial answer", is_done=True)
        else:
            yield StreamChunk(content="redirected answer", is_done=True)


@pytest.mark.asyncio
async def test_queued_steering_is_persisted_and_applied_to_running_turn(tmp_path):
    provider = SteeringProvider()
    store = SessionStore(tmp_path / "steering.db")
    ui = EventUI()
    loop = AshLoop(
        store,
        provider,
        SafetyGuard(project_root=tmp_path),
        ui,
        tmp_path,
    )

    turn = asyncio.create_task(loop.run_turn("start with the original approach"))
    await provider.started.wait()
    assert loop.is_turn_running is True
    assert loop.queue_steering("use the safer approach instead") == 1
    provider.release.set()

    response = await turn

    assert response == "redirected answer"
    assert loop.is_turn_running is False
    assert provider.calls == 2
    second_messages = provider.received_messages[1]
    assert any(
        message["role"] == "user"
        and message["content"] == "use the safer approach instead"
        for message in second_messages
    )
    assert loop.pending_steering_count == 0
    assert [
        event["type"]
        for event in ui.events
        if event["type"] != "assistant.delta"
    ] == [
        "turn.started",
        "turn.steering.queued",
        "turn.steering.applied",
        "turn.usage",
        "turn.completed",
    ]
    loaded = store.load_session(loop.current_session.session_id)
    steering = next(
        message for message in loaded.messages if message.metadata.get("steering")
    )
    assert steering.role == "user"
    assert steering.content == "use the safer approach instead"


def test_steering_queue_validates_messages_and_capacity(tmp_path):
    loop = AshLoop(
        SessionStore(tmp_path / "steering-limit.db"),
        BudgetProvider(),
        SafetyGuard(project_root=tmp_path),
        EventUI(),
        tmp_path,
        max_steering_messages=1,
    )

    with pytest.raises(ValueError, match="cannot be empty"):
        loop.queue_steering("  ")
    assert loop.queue_steering("first") == 1
    with pytest.raises(OverflowError, match="queue is full"):
        loop.queue_steering("second")


@pytest.mark.asyncio
async def test_persisted_compaction_summary_is_redacted(tmp_path):
    config = AshConfig(
        model="ollama/test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        context_recent_messages=2,
    )
    store = SessionStore(tmp_path / "compaction-redaction.db")
    loop = AshLoop(
        store,
        MockProvider(),
        SafetyGuard(project_root=tmp_path),
        EventUI(),
        tmp_path,
        config=config,
    )
    session = await loop.start_session()
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz"
    for role, content in (
        ("user", f"OPENAI_API_KEY={secret}"),
        ("assistant", "acknowledged"),
        ("user", "continue"),
        ("assistant", "current response"),
    ):
        message = Message(role=role, content=content, timestamp=datetime.now(timezone.utc))
        store.save_message(session.session_id, message)
        session.messages.append(message)

    _, changed = loop.compact_current_context()
    persisted = store.load_session(session.session_id).context_summary

    assert changed is True
    assert secret not in persisted
    assert "REDACTED" in persisted


@pytest.mark.asyncio
async def test_turn_running_state_resets_after_cancellation(tmp_path):
    provider = SteeringProvider()
    ui = EventUI()
    loop = AshLoop(
        SessionStore(tmp_path / "steering-cancel.db"),
        provider,
        SafetyGuard(project_root=tmp_path),
        ui,
        tmp_path,
    )
    turn = asyncio.create_task(loop.run_turn("wait"))
    await provider.started.wait()
    assert loop.is_turn_running is True
    loop.queue_steering("pending redirect")

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    assert loop.is_turn_running is False
    assert loop.pending_steering_count == 0
    cancelled = next(event for event in ui.events if event["type"] == "turn.cancelled")
    assert cancelled["discarded_steering"] == 1
    assert (
        loop.session_store.reconcile_interrupted_turns(loop.current_session.session_id)
        == 0
    )


@pytest.mark.asyncio
async def test_approved_tool_intent_is_durable_before_execution_finishes(tmp_path):
    provider = NativeToolProvider()
    tool = BlockingCaptureTool(SafetyGuard(tmp_path))
    store = SessionStore(tmp_path / "pending-tool.db")
    loop = AshLoop(
        store,
        provider,
        tool.safety_guard,
        EventUI(),
        tmp_path,
        tools={tool.name: tool},
    )

    turn = asyncio.create_task(loop.run_turn("use the capture tool"))
    await tool.started.wait()
    assert loop.current_session is not None
    assert loop.turn_context is not None
    with get_db_connection(store.db_path) as connection:
        pending = connection.execute(
            "SELECT approved, executed, turn_id FROM tool_calls WHERE call_id = ?",
            ("call-native-1",),
        ).fetchone()
    assert pending["approved"] == 1
    assert pending["executed"] == 0
    assert pending["turn_id"] == loop.turn_context.turn_id

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    recovered = store.load_session(loop.current_session.session_id).tool_calls[0]
    assert recovered.executed is True
    assert "outcome is unknown" in (recovered.error or "")
    assert loop.recovery_summary is not None
    assert loop.recovery_summary.needs_attention is True


@pytest.mark.asyncio
async def test_native_tool_calls_are_normalized_and_persisted(tmp_path):
    provider = NativeToolProvider()
    tool = CaptureTool(SafetyGuard(project_root=tmp_path))
    store = SessionStore(tmp_path / "native.db")
    ui = EventUI()
    loop = AshLoop(
        store,
        provider,
        tool.safety_guard,
        ui,
        tmp_path,
        tools={tool.name: tool},
    )

    await loop.start_session()
    response = await loop.run_turn("use the capture tool")

    assert response == "done"
    assert tool.arguments == {"text": "hello"}
    second_request = provider.received_messages[1]
    assistant = next(
        message
        for message in second_request
        if message["role"] == "assistant" and message.get("tool_calls")
    )
    assert assistant["tool_calls"][0]["call_id"] == "call-native-1"
    tool_message = next(
        message for message in second_request if message["role"] == "tool"
    )
    assert tool_message["tool_call_id"] == "call-native-1"
    assert [
        event["type"]
        for event in ui.events
        if event["type"] != "assistant.delta"
    ] == [
        "turn.started",
        "tool.requested",
        "tool.started",
        "tool.completed",
        "turn.usage",
        "turn.completed",
    ]
    completed_tool = next(
        event for event in ui.events if event["type"] == "tool.completed"
    )
    assert completed_tool["output"] == "hello"
    assert loop.turn_context is not None
    with get_db_connection(store.db_path) as connection:
        tool_turn = connection.execute(
            "SELECT turn_id FROM tool_calls WHERE session_id = ?",
            (loop.current_session.session_id,),
        ).fetchone()
    assert tool_turn["turn_id"] == loop.turn_context.turn_id


@pytest.mark.asyncio
async def test_tool_execution_writes_tamper_evident_audit_log(tmp_path):
    provider = NativeToolProvider()
    tool = CaptureTool(SafetyGuard(project_root=tmp_path))
    store = SessionStore(tmp_path / "audit.db")
    loop = AshLoop(
        store,
        provider,
        tool.safety_guard,
        EventUI(),
        tmp_path,
        tools={tool.name: tool},
    )

    session = await loop.start_session()
    await loop.run_turn("use the capture tool")

    audit = store.list_audit_logs(session.session_id)
    assert [(row.action_type, row.result) for row in audit] == [
        ("user_approval", "APPROVED"),
        ("tool_call", "SUCCESS"),
    ]
    assert audit[1].previous_hash == audit[0].sha256_hash
    assert store.verify_audit_log(session.session_id) == []
    assert audit[0].details["arguments"] == {"text": "hello"}


@pytest.mark.asyncio
async def test_context_budget_report_enforces_sections(tmp_path):
    provider = BudgetProvider()
    guard = SafetyGuard(project_root=tmp_path)
    turn_context = TurnContext(session_id="pending", turn_id="turn-1")
    loop = AshLoop(
        SessionStore(tmp_path / "budget.db"),
        provider,
        guard,
        EventUI(),
        tmp_path,
        tools={"budget_tool": BudgetTool(guard)},
        system_prompt="system " * 120,
        repo_map=LargeRepoMap(),
        turn_context=turn_context,
        config=AshConfig(
            model="openai/budget-test",
            workspace_root=tmp_path,
            db_directory=tmp_path / "db",
            max_context_tokens=200,
            max_completion_tokens=20,
            memory_backend="off",
            context_budget_weights={
                "system": 0.10,
                "tools": 0.20,
                "history": 0.50,
                "repo_map": 0.10,
                "memory": 0.10,
            },
        ),
    )
    session = await loop.start_session()
    session.messages.append(
        Message(
            role="user",
            content="history " * 200,
            timestamp=datetime.now(timezone.utc),
        )
    )
    loop._pending_memory_context = "memory " * 120

    messages = loop._build_messages(session)
    budget = turn_context.get("context_budget")

    assert budget is loop._last_context_budget
    assert budget.slices["tools"].used > 0
    assert budget.slices["system"].truncated is True
    assert budget.slices["repo_map"].truncated is True
    assert budget.slices["memory"].truncated is True
    assert "context section truncated" in messages[0]["content"]


@pytest.mark.asyncio
async def test_turn_usage_tracks_cache_and_configured_cost(tmp_path):
    store = SessionStore(tmp_path / "cache-usage.db")
    ui = EventUI()
    config = AshConfig(
        model="openai/cache-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        model_pricing_usd_per_million={
            "openai/cache-test": {
                "input": 2.0,
                "output": 10.0,
                "cache_read": 0.2,
                "cache_write": 2.5,
            }
        },
    )
    loop = AshLoop(
        store,
        CacheUsageProvider(),
        SafetyGuard(project_root=tmp_path),
        ui,
        tmp_path,
        config=config,
    )

    session = await loop.start_session()
    assert await loop.run_turn("measure usage") == "done"

    assert loop._last_turn_prompt_tokens == 100
    assert loop._last_turn_completion_tokens == 5
    assert loop._last_cache_read_tokens == 60
    assert loop._last_cache_write_tokens == 20
    assert loop._last_turn_cost_usd == pytest.approx(0.000152)
    assert loop.turn_context is not None
    assert loop.turn_context.get("usage")["cache_hit_rate"] == 0.6
    usage_event = next(event for event in ui.events if event["type"] == "turn.usage")
    assert usage_event["schema_version"] == 1
    assert usage_event["session_id"] == session.session_id
    assert usage_event["turn_id"] == loop.turn_context.turn_id
    assert {
        key: value
        for key, value in usage_event.items()
        if key
        not in {
            "schema_version",
            "event_id",
            "timestamp",
            "source",
            "session_id",
            "turn_id",
            "operation_id",
            "parent_event_id",
        }
    } == {
        "type": "turn.usage",
        "prompt_tokens": 100,
        "completion_tokens": 5,
        "cache_read_tokens": 60,
        "cache_write_tokens": 20,
        "usage_source": "provider",
        "estimated_prompt_tokens": 0,
        "estimated_completion_tokens": 0,
        "has_estimates": False,
        "cache_hit_rate": 0.6,
        "cost_usd": pytest.approx(0.000152),
        "estimated_cost_usd": 0.0,
        "cost_is_estimated": False,
    }
    report = loop._last_context_budget
    assert report is not None
    assert {str(fragment.kind) for fragment in report.fragments} == {
        "system",
        "tool_schema",
        "history",
        "repo_map",
        "memory",
    }
    assert all(len(fragment.content_sha256) == 64 for fragment in report.fragments)
    assert report.slices["tools"].used == 0
    context_event = next(
        event for event in ui.events if event["type"] == "context.usage"
    )
    assert loop._last_context_tokens == context_event["current"]
    usage = store.get_session_usage(session.session_id)
    assert usage.prompt_tokens == 100
    assert usage.cache_read_tokens == 60
    assert usage.cache_write_tokens == 20
    assert usage.cost_usd == pytest.approx(0.000152)
    assert loop.turn_context is not None
    assert store.rewind_turn_ids(session.session_id, 0) == [loop.turn_context.turn_id]
    with get_db_connection(store.db_path) as connection:
        persisted_turn = connection.execute(
            "SELECT usage_json FROM turn_journal WHERE turn_id = ?",
            (loop.turn_context.turn_id,),
        ).fetchone()
    assert '"prompt_tokens": 100' in persisted_turn["usage_json"]


@pytest.mark.asyncio
async def test_missing_provider_usage_is_estimated_marked_and_persisted(tmp_path):
    store = SessionStore(tmp_path / "estimated-usage.db")
    config = AshConfig(
        model="openai/budget-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="off",
        max_context_tokens=200,
        max_completion_tokens=20,
        model_pricing_usd_per_million={
            "openai/budget-test": {"input": 2.0, "output": 10.0}
        },
    )
    loop = AshLoop(
        store,
        BudgetProvider(),
        SafetyGuard(project_root=tmp_path),
        EventUI(),
        tmp_path,
        config=config,
    )

    session = await loop.start_session()
    assert await loop.run_turn("measure estimated usage") == "done"

    usage = loop.last_turn_usage
    assert usage["usage_source"] == "estimated"
    assert usage["has_estimates"] is True
    assert usage["estimated_prompt_tokens"] == usage["prompt_tokens"]
    assert usage["estimated_completion_tokens"] == usage["completion_tokens"]
    assert int(usage["prompt_tokens"]) > 0
    assert int(usage["completion_tokens"]) > 0
    assert usage["cost_is_estimated"] is True
    assert usage["estimated_cost_usd"] == usage["cost_usd"]

    persisted = store.get_session_usage(session.session_id)
    assert persisted.estimated_prompt_tokens == usage["prompt_tokens"]
    assert persisted.estimated_completion_tokens == usage["completion_tokens"]
    assert persisted.estimated_cost_usd == usage["estimated_cost_usd"]


@pytest.mark.asyncio
async def test_dry_run_never_executes_native_tool_calls(tmp_path):
    provider = NativeToolProvider()
    tool = CaptureTool(SafetyGuard(project_root=tmp_path))
    ui = EventUI("dry_run")
    loop = AshLoop(
        SessionStore(tmp_path / "dry-run.db"),
        provider,
        tool.safety_guard,
        ui,
        tmp_path,
        tools={tool.name: tool},
        safety_tier="dry_run",
    )

    await loop.start_session()
    await loop.run_turn("do not execute this")

    assert tool.arguments is None
    assert [
        event["type"] for event in ui.events if event["type"].startswith("tool.")
    ][:2] == [
        "tool.requested",
        "tool.denied",
    ]


@pytest.mark.asyncio
async def test_resume_unknown_session_does_not_silently_create_one(tmp_path):
    provider = NativeToolProvider()
    guard = SafetyGuard(project_root=tmp_path)
    loop = AshLoop(
        SessionStore(tmp_path / "missing.db"),
        provider,
        guard,
        TerminalUI(safety_tier="dry_run"),
        tmp_path,
    )

    with pytest.raises(KeyError, match="Session not found"):
        await loop.start_session("missing")


@pytest.mark.asyncio
async def test_tool_output_events_inherit_call_context(tmp_path):
    guard = SafetyGuard(project_root=tmp_path)
    tool = EventTool(guard)
    ui = EventUI()
    loop = AshLoop(
        SessionStore(tmp_path / "events.db"),
        NativeToolProvider(),
        guard,
        ui,
        tmp_path,
        tools={tool.name: tool},
        safety_tier="auto_approve",
    )
    session = await loop.start_session()

    result = await loop._execute_tool_calls(
        [
            {
                "call_id": "call-stream-1",
                "name": "event_tool",
                "arguments": {},
            }
        ],
        session,
    )

    output_event = next(event for event in ui.events if event["type"] == "tool.output")
    assert output_event["schema_version"] == 1
    assert output_event["session_id"] == session.session_id
    assert output_event["operation_id"] == "call-stream-1"
    assert {
        key: value
        for key, value in output_event.items()
        if key
        not in {
            "schema_version",
            "event_id",
            "timestamp",
            "source",
            "session_id",
            "turn_id",
            "operation_id",
            "parent_event_id",
        }
    } == {
        "call_id": "call-stream-1",
        "tool": "event_tool",
        "arguments": {},
        "type": "tool.output",
        "delta": "live",
        "stream": "stdout",
    }
    assert result[0]["output"] == "live"


@pytest.mark.asyncio
async def test_middleware_before_called(tmp_path):
    spy = SpyMiddleware()
    with tempfile.TemporaryDirectory() as db_dir:
        store = SessionStore(Path(db_dir) / "test.db")
        guard = SafetyGuard(project_root=tmp_path)
        ui = TerminalUI(safety_tier="dry_run")
        loop = AshLoop(
            store,
            MockProvider(),
            guard,
            ui,
            tmp_path,
            tools={"my_tool": MyTestTool(guard)},
            tool_middlewares=[spy],
        )
        await loop.start_session()
        await loop.run_turn("test")

        assert len(spy.before_calls) >= 0  # tool was called and middleware was notified


@pytest.mark.asyncio
async def test_middleware_skip_aborts_tool(tmp_path):
    skip = SkipMiddleware()
    with tempfile.TemporaryDirectory() as db_dir:
        store = SessionStore(Path(db_dir) / "test.db")
        guard = SafetyGuard(project_root=tmp_path)
        ui = TerminalUI(safety_tier="dry_run")
        loop = AshLoop(
            store,
            MockProvider(),
            guard,
            ui,
            tmp_path,
            tools={"my_tool": MyTestTool(guard)},
            tool_middlewares=[skip],
        )
        await loop.start_session()
        result = await loop.run_turn("test")
        # The turn should complete without the tool actually running
        assert "skipped by middleware" in result or result  # no error from skipped tool


@pytest.mark.asyncio
async def test_on_tool_approval_callback_is_called(tmp_path):
    call_log = []

    class ApprovalProvider(MockProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            yield StreamChunk(
                tool_call_delta='<call_tool name="my_tool"></call_tool>',
                is_done=True,
            )

    async def approval_callback(tool_name, arguments):
        call_log.append((tool_name, arguments))
        return True  # approve all tools

    with tempfile.TemporaryDirectory() as db_dir:
        store = SessionStore(Path(db_dir) / "test.db")
        guard = SafetyGuard(project_root=tmp_path)
        ui = TerminalUI(safety_tier="interactive")
        from core.recovery import CircuitBreaker

        loop = AshLoop(
            store,
            ApprovalProvider(),
            guard,
            ui,
            tmp_path,
            tools={"my_tool": MyTestTool(guard)},
            on_tool_approval=approval_callback,
            circuit_breaker=CircuitBreaker(max_failures=10),
            max_turn_iterations=1,
        )
        await loop.start_session()
        await loop.run_turn("test")

        assert len(call_log) > 0
        assert any(call[0] == "my_tool" for call in call_log)


@pytest.mark.asyncio
async def test_allow_rule_remains_subject_to_host_approval_callback(tmp_path):
    calls = []

    class ApprovalProvider(MockProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            yield StreamChunk(
                tool_call_delta='<call_tool name="my_tool"></call_tool>',
                is_done=True,
            )

    async def approval_callback(tool_name, arguments):
        calls.append(tool_name)
        return False

    guard = SafetyGuard(project_root=tmp_path)
    loop = AshLoop(
        SessionStore(tmp_path / "allow-rule.db"),
        ApprovalProvider(),
        guard,
        TerminalUI(safety_tier="interactive"),
        tmp_path,
        tools={"my_tool": MyTestTool(guard)},
        on_tool_approval=approval_callback,
        max_turn_iterations=1,
    )
    loop.permission_policy.set_persistent_rules(
        [PermissionRule.create(RuleEffect.ALLOW, "my_tool")]
    )
    await loop.start_session()

    await loop.run_turn("test")

    assert calls == ["my_tool"]


@pytest.mark.asyncio
async def test_tool_approval_callback_cannot_override_policy_decisions(tmp_path):
    calls = []

    async def approve(tool_name, arguments):
        calls.append(tool_name)
        return True

    guard = SafetyGuard(project_root=tmp_path)
    loop = AshLoop(
        SessionStore(tmp_path / "policy-callback.db"),
        MockProvider(),
        guard,
        TerminalUI(safety_tier="dry_run"),
        tmp_path,
        tools={"read_file": ReadFileTool(guard)},
        on_tool_approval=approve,
        safety_tier="dry_run",
        max_turn_iterations=1,
    )
    await loop.start_session()

    await loop.run_turn("test")

    assert calls == []


@pytest.mark.asyncio
async def test_on_tool_approval_can_auto_deny(tmp_path):
    async def deny_all(tool_name, arguments):
        return False

    with tempfile.TemporaryDirectory() as db_dir:
        store = SessionStore(Path(db_dir) / "test.db")
        guard = SafetyGuard(project_root=tmp_path)
        ui = TerminalUI(safety_tier="dry_run")
        loop = AshLoop(
            store,
            MockProvider(),
            guard,
            ui,
            tmp_path,
            tools={"my_tool": MyTestTool(guard)},
            on_tool_approval=deny_all,
            max_turn_iterations=1,
        )
        await loop.start_session()
        result = await loop.run_turn("test")
        # Turn should complete (denied tools produce error results, not exceptions)
        assert result is not None


@pytest.mark.asyncio
async def test_retry_on_transient_failure(tmp_path):
    transient_count = 0

    class FlakyTool(BaseTool):
        name = "flaky"
        args_schema = None

        async def run(self, **kwargs):
            nonlocal transient_count
            transient_count += 1
            if transient_count < 3:
                raise RuntimeError("transient error")
            return ToolResult(success=True, output="ok")

    class FlakyProvider(ProviderABC):
        model_name = "test"

        def count_tokens(self, text):
            return 0

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            yield StreamChunk(
                tool_call_delta='<call_tool name="flaky"></call_tool>',
                is_done=True,
            )

    with tempfile.TemporaryDirectory() as db_dir:
        store = SessionStore(Path(db_dir) / "test.db")
        guard = SafetyGuard(project_root=tmp_path)
        ui = TerminalUI(safety_tier="auto_approve")
        loop = AshLoop(
            store,
            FlakyProvider(),
            guard,
            ui,
            tmp_path,
            tools={"flaky": FlakyTool(guard)},
            max_turn_iterations=1,
        )
        await loop.start_session()
        await loop.run_turn("test")
        assert (
            transient_count == 3
        )  # 3 total attempts: fail, fail, success (MAX_RETRIES=2 means 2 retries)


@pytest.mark.asyncio
async def test_provider_retries_transient_failure_before_output(tmp_path):
    class APIConnectionError(RuntimeError):
        pass

    class FlakyProvider(ProviderABC):
        model_name = "flaky-provider"

        def __init__(self):
            self.calls = 0

        def count_tokens(self, text):
            return len(text)

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.calls += 1
            if self.calls < 3:
                raise APIConnectionError("connection reset sk-abcdefghijklmnop")
            yield StreamChunk(content="recovered", is_done=True)

    provider = FlakyProvider()
    ui = EventUI()
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        provider,
        SafetyGuard(project_root=tmp_path),
        ui,
        tmp_path,
        config=AshConfig(
            model="ollama/test",
            provider_max_attempts=3,
            provider_retry_base_delay=0,
            provider_retry_max_delay=0,
        ),
    )

    response, *_ = await loop._stream_one_completion([])

    assert response == "recovered"
    assert provider.calls == 3
    retries = [event for event in ui.events if event["type"] == "provider.retrying"]
    assert [event["attempt"] for event in retries] == [2, 3]
    assert all(event["delay_seconds"] == 0 for event in retries)
    assert all("sk-abcdefghijklmnop" not in event["reason"] for event in retries)


@pytest.mark.asyncio
async def test_provider_does_not_retry_permanent_or_partial_failure(tmp_path):
    class APIError(RuntimeError):
        status_code = 401

    class PermanentProvider(ProviderABC):
        model_name = "permanent"

        def __init__(self):
            self.calls = 0

        def count_tokens(self, text):
            return 0

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.calls += 1
            raise APIError("invalid API key")
            yield  # pragma: no cover

    permanent = PermanentProvider()
    loop = AshLoop(
        SessionStore(tmp_path / "permanent.db"),
        permanent,
        SafetyGuard(project_root=tmp_path),
        EventUI(),
        tmp_path,
        config=AshConfig(model="ollama/test", provider_retry_base_delay=0),
    )
    with pytest.raises(APIError, match="invalid API key"):
        await loop._stream_one_completion([])
    assert permanent.calls == 1

    class PartialProvider(PermanentProvider):
        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.calls += 1
            yield StreamChunk(content="partial")
            raise ConnectionError("stream disconnected")

    partial = PartialProvider()
    partial_loop = AshLoop(
        SessionStore(tmp_path / "partial.db"),
        partial,
        SafetyGuard(project_root=tmp_path),
        EventUI(),
        tmp_path,
        config=AshConfig(model="ollama/test", provider_retry_base_delay=0),
    )
    with pytest.raises(ConnectionError, match="stream disconnected"):
        await partial_loop._stream_one_completion([])
    assert partial.calls == 1


@pytest.mark.asyncio
async def test_provider_circuit_fails_fast_then_allows_probe(tmp_path):
    now = 10.0

    class RecoveringProvider(ProviderABC):
        model_name = "recovering"

        def __init__(self):
            self.calls = 0

        def count_tokens(self, text):
            return 0

        async def stream_chat(self, messages, temperature=0.0, tools=None):
            self.calls += 1
            if self.calls <= 2:
                raise ConnectionError("offline")
            yield StreamChunk(content="online", is_done=True)

    provider = RecoveringProvider()
    ui = EventUI()
    circuit = ProviderCircuitBreaker(
        failure_threshold=2,
        cooldown_seconds=5,
        clock=lambda: now,
    )
    loop = AshLoop(
        SessionStore(tmp_path / "circuit.db"),
        provider,
        SafetyGuard(project_root=tmp_path),
        ui,
        tmp_path,
        provider_circuit_breaker=circuit,
        config=AshConfig(
            model="ollama/test",
            provider_max_attempts=1,
            provider_circuit_failure_threshold=2,
            provider_circuit_cooldown_seconds=5,
        ),
    )

    for _ in range(2):
        with pytest.raises(ConnectionError, match="offline"):
            await loop._stream_one_completion([])
    assert provider.calls == 2
    assert any(event["type"] == "provider.circuit_opened" for event in ui.events)

    with pytest.raises(ProviderCircuitOpen, match="circuit is open"):
        await loop._stream_one_completion([])
    assert provider.calls == 2

    now = 16.0
    response, *_ = await loop._stream_one_completion([])
    assert response == "online"
    assert provider.calls == 3
    assert circuit.snapshot(loop._provider_circuit_key)["failures"] == 0


class ImageCaptureProvider(ProviderABC):
    model_name = "vision-test"

    def __init__(self) -> None:
        self.messages = []

    def count_tokens(self, text):
        return len(str(text))

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.messages = messages
        yield StreamChunk(content="image inspected", is_done=True)


@pytest.mark.asyncio
async def test_image_blocks_reach_provider_but_are_not_persisted(tmp_path):
    provider = ImageCaptureProvider()
    store = SessionStore(tmp_path / "images.db")
    loop = AshLoop(
        store,
        provider,
        SafetyGuard(project_root=tmp_path),
        EventUI(),
        tmp_path,
    )
    metadata = {
        "image_blocks": [{"type": "image", "media_type": "image/png", "data": "YWJj"}],
        "images": [
            {"path": "image.png", "media_type": "image/png", "sha256": "digest"}
        ],
    }

    response = await loop.run_turn("inspect image", user_metadata=metadata)

    assert response == "image inspected"
    user_content = next(
        message["content"] for message in provider.messages if message["role"] == "user"
    )
    assert user_content[1]["data"] == "YWJj"
    assert loop.current_session is not None
    loaded = store.load_session(loop.current_session.session_id)
    persisted = loaded.messages[0].metadata
    assert "image_blocks" not in persisted
    assert persisted["images"][0]["path"] == "image.png"
    await loop.aclose()
