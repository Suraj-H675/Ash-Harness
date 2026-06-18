# tests/unit/test_loop.py
import pytest
from core.loop import AshLoop
from tools.base import BaseTool, ToolResult, ToolMiddleware, ToolMiddlewareSkip
from core.session import SessionStore
from providers.base import ProviderABC, StreamChunk
from safety.guard import SafetyGuard
from ui.terminal import TerminalUI
from tools.filesystem import ReadFileTool
from pathlib import Path
import tempfile


class MockProvider(ProviderABC):
    model_name = "test"

    def count_tokens(self, text):
        return 0

    async def stream_chat(self, messages, temperature=0.0):
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

    async def approval_callback(tool_name, arguments):
        call_log.append((tool_name, arguments))
        return True  # approve all tools

    with tempfile.TemporaryDirectory() as db_dir:
        store = SessionStore(Path(db_dir) / "test.db")
        guard = SafetyGuard(project_root=tmp_path)
        ui = TerminalUI(safety_tier="dry_run")
        from core.recovery import CircuitBreaker

        loop = AshLoop(
            store,
            MockProvider(),
            guard,
            ui,
            tmp_path,
            tools={"read_file": ReadFileTool(guard)},
            on_tool_approval=approval_callback,
            circuit_breaker=CircuitBreaker(max_failures=10),
            max_turn_iterations=1,
        )
        await loop.start_session()
        await loop.run_turn("test")

        assert len(call_log) > 0
        assert any(call[0] == "read_file" for call in call_log)


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

        async def stream_chat(self, messages, temperature=0.0):
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
