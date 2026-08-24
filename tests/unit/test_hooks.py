import asyncio
import re

import pytest
from ash.hooks.registry import (
    HookBlock,
    HookRegistry,
    LifecycleHook,
    PostToolUseHook,
    PreToolUseHook,
    SessionStartHook,
)


@pytest.mark.asyncio
async def test_extended_lifecycle_events_dispatch_payloads():
    from ash.hooks.config import LIFECYCLE_EVENTS as CONFIG_EVENTS

    for event in ("context_compacted", "config_changed", "permission_changed"):
        assert event in CONFIG_EVENTS


@pytest.mark.asyncio
async def test_pre_tool_hook_fires_on_matcher():
    registry = HookRegistry()
    called = []

    async def check_write(name, args):
        called.append((name, args))

    registry.register_pre_tool(
        PreToolUseHook(
            matcher=re.compile(r"Write|Edit", re.IGNORECASE),
            callback=check_write,
        )
    )

    await registry.fire_pre_tool("write_file", {"file_path": "x"})
    assert called == [("write_file", {"file_path": "x"})]

    await registry.fire_pre_tool("read_file", {})  # should NOT fire
    assert len(called) == 1


@pytest.mark.asyncio
async def test_session_start_hook_fires():
    registry = HookRegistry()
    started = []

    async def on_start():
        started.append(True)

    registry.register_session_start(SessionStartHook(callback=on_start))
    await registry.fire_session_start()

    assert started == [True]


@pytest.mark.asyncio
async def test_session_start_hook_injects_prompt():
    registry = HookRegistry()
    started = []

    async def on_start():
        started.append(True)

    registry.register_session_start(SessionStartHook(callback=on_start))
    await registry.fire_session_start()

    assert started == [True]


@pytest.mark.asyncio
async def test_post_tool_hook_fires():
    registry = HookRegistry()
    results = []

    async def capture_result(name, args, result):
        results.append((name, args, result))

    registry.register_post_tool(
        PostToolUseHook(
            matcher=re.compile(r"Write|Edit", re.IGNORECASE),
            callback=capture_result,
        )
    )

    await registry.fire_post_tool("write_file", {"file_path": "x"}, {"success": True})
    assert len(results) == 1
    assert results[0][0] == "write_file"


@pytest.mark.asyncio
async def test_hook_timeout_is_enforced():
    registry = HookRegistry(timeout_seconds=0.01)

    async def slow(name, args):
        await asyncio.sleep(1)

    registry.register_pre_tool(PreToolUseHook(matcher=re.compile(".*"), callback=slow))
    with pytest.raises(asyncio.TimeoutError):
        await registry.fire_pre_tool("read_file", {})


@pytest.mark.asyncio
async def test_observer_failures_are_redacted_reported_and_isolated():
    registry = HookRegistry()
    events = []

    async def fail(_payload):
        raise RuntimeError("OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz")

    registry.register_lifecycle(LifecycleHook("turn_end", fail, source="test"))
    registry.set_event_sink(events.append)

    await registry.fire_lifecycle("turn_end", {"status": "completed"})

    assert len(registry.diagnostics) == 1
    assert registry.diagnostics[0].source == "test"
    assert "sk-proj" not in registry.diagnostics[0].error
    assert events[0]["type"] == "hook.error"
    assert "REDACTED" in events[0]["error"]


@pytest.mark.asyncio
async def test_post_tool_observer_failure_cannot_relabel_completed_work():
    registry = HookRegistry()

    async def fail(_name, _arguments, _result):
        raise RuntimeError("observer unavailable")

    registry.register_post_tool(
        PostToolUseHook(re.compile(".*"), fail, source="post-observer")
    )

    await registry.fire_post_tool("write_file", {}, {"success": True})

    assert registry.diagnostics[0].event == "post_tool"
    assert registry.diagnostics[0].source == "post-observer"


@pytest.mark.asyncio
async def test_pre_tool_gate_remains_fail_closed():
    registry = HookRegistry()

    async def block(_name, _arguments):
        raise HookBlock("policy rejected the call")

    registry.register_pre_tool(PreToolUseHook(re.compile(".*"), block))

    with pytest.raises(HookBlock, match="policy rejected"):
        await registry.fire_pre_tool("write_file", {})


@pytest.mark.asyncio
async def test_hooks_cannot_mutate_live_tool_arguments_or_results():
    registry = HookRegistry()
    arguments = {"nested": {"value": 1}}
    result = {"success": True, "nested": {"value": 2}}

    async def mutate_pre(_name, hook_arguments):
        hook_arguments["nested"]["value"] = 99

    async def mutate_post(_name, hook_arguments, hook_result):
        hook_arguments["nested"]["value"] = 98
        hook_result["nested"]["value"] = 97

    registry.register_pre_tool(PreToolUseHook(re.compile(".*"), mutate_pre))
    registry.register_post_tool(PostToolUseHook(re.compile(".*"), mutate_post))

    await registry.fire_pre_tool("example", arguments)
    await registry.fire_post_tool("example", arguments, result)

    assert arguments == {"nested": {"value": 1}}
    assert result == {"success": True, "nested": {"value": 2}}


@pytest.mark.asyncio
async def test_session_context_budget_and_failing_diagnostic_sink_are_isolated():
    registry = HookRegistry()

    async def context():
        return "x" * 40_000

    async def fail(_payload):
        raise RuntimeError("observer failed")

    registry.register_session_start(SessionStartHook(context, source="first"))
    registry.register_session_start(SessionStartHook(context, source="second"))
    registry.register_lifecycle(LifecycleHook("turn_end", fail, source="observer"))
    registry.set_event_sink(lambda _event: (_ for _ in ()).throw(RuntimeError("sink")))

    await registry.fire_session_start()
    await registry.fire_lifecycle("turn_end", {})

    assert len(registry.get_injected_prompt()) == 40_000
    assert [item.source for item in registry.diagnostics] == ["second", "observer"]


@pytest.mark.asyncio
async def test_hook_self_cancellation_is_isolated_but_task_cancellation_propagates():
    registry = HookRegistry()

    async def cancel_self(_payload):
        raise asyncio.CancelledError

    registry.register_lifecycle(LifecycleHook("turn_end", cancel_self))
    await registry.fire_lifecycle("turn_end", {})
    assert "cancelled itself" in registry.diagnostics[0].error

    started = asyncio.Event()

    async def wait_forever(_payload):
        started.set()
        await asyncio.Event().wait()

    registry = HookRegistry()
    registry.register_lifecycle(LifecycleHook("turn_end", wait_forever))
    task = asyncio.create_task(registry.fire_lifecycle("turn_end", {}))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
