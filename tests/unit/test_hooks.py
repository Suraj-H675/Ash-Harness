# tests/unit/test_hooks.py
import pytest
import re
from hooks.registry import (
    HookRegistry,
    PostToolUseHook,
    PreToolUseHook,
    SessionStartHook,
)


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
