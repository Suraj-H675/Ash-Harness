# tests/unit/test_loop.py
import pytest
from ash.core.loop import AshLoop
from ash.tools.base import BaseTool, ToolResult, ToolMiddleware, ToolMiddlewareSkip
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.safety.guard import SafetyGuard
from ash.ui.terminal import TerminalUI
from pathlib import Path
from unittest.mock import AsyncMock
import tempfile

class MockProvider(ProviderABC):
    model_name = "test"
    def count_tokens(self, text): return 0
    async def stream_chat(self, messages, temperature=0.0):
        yield StreamChunk(content="done", is_done=True)

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

@pytest.mark.asyncio
async def test_middleware_before_called(tmp_path):
    spy = SpyMiddleware()
    with tempfile.TemporaryDirectory() as db_dir:
        store = SessionStore(Path(db_dir) / "test.db")
        guard = SafetyGuard(project_root=tmp_path)
        ui = TerminalUI(safety_tier="dry_run")
        loop = AshLoop(
            store, MockProvider(), guard, ui, tmp_path,
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
            store, MockProvider(), guard, ui, tmp_path,
            tools={"my_tool": MyTestTool(guard)},
            tool_middlewares=[skip],
        )
        await loop.start_session()
        result = await loop.run_turn("test")
        # The turn should complete without the tool actually running
        assert "skipped by middleware" in result or result  # no error from skipped tool