# tests/unit/test_hooks.py
import pytest
import re
from ash.hooks.registry import (
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
