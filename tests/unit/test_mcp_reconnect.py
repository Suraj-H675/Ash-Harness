from __future__ import annotations

import asyncio
import io
from pathlib import Path
import sys

import pytest

from ash.core.loop import AshLoop
from ash.mcp.client import MCPClient
from ash.mcp.runtime import MCPRuntime
from ash.core.session import SessionStore
from ash.mcp.server import MCPServerConfig
from ash.providers.base import ProviderABC, StreamChunk
from ash.safety.guard import SafetyGuard
from ash.ui.headless import HeadlessUI


FAKE_SERVER = r"""
import json, sys
import os
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        response = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "legacy"}}
        print(json.dumps(response), flush=True)
        continue
    if method != "initialize":
        continue
    result = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "serverInfo": {"name": "fake", "version": "1"},
    }
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
print(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}), flush=True)
"""


DYNAMIC_REPLACEMENT_SERVER = r"""
import json, os, sys
mode = os.environ.get("ASH_DYNAMIC_MODE", "initial")
state = "old" if mode == "initial" else "new"
list_count = 0
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "legacy"}}), flush=True)
        continue
    if method == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "dynamic", "version": "1"},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "tools/list":
        list_count += 1
        schema = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }
        result = {"tools": [{"name": state, "description": state, "inputSchema": schema}]}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
        if mode == "replacement" and list_count == 1:
            state = "updated"
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}), flush=True)
    elif method == "tools/call":
        name = message["params"]["name"]
        result = {"content": [{"type": "text", "text": name}]}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


class IdleProvider(ProviderABC):
    model_name = "idle"

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        yield StreamChunk(content="idle", is_done=True)

    def count_tokens(self, text: str) -> int:
        return len(text.split())


def _config(name: str) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        command=sys.executable,
        args=["-u", "-c", FAKE_SERVER],
        env={},
        transport="stdio",
    )


def _dynamic_config(name: str, mode: str) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        command=sys.executable,
        args=["-u", "-c", DYNAMIC_REPLACEMENT_SERVER],
        env={"ASH_DYNAMIC_MODE": mode},
        transport="stdio",
    )


@pytest.mark.asyncio
async def test_reconnect_replaces_only_target_server(tmp_path) -> None:
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"one": _config("one"), "two": _config("two")},
    )
    await loop.start_session()
    try:
        old_one = loop._mcp_runtime.clients["one"]
        old_two = loop._mcp_runtime.clients["two"]
        errors = await loop.reconnect_mcp_server("one")

        assert errors == {}
        assert loop._mcp_runtime.clients["two"] is old_two
        assert loop._mcp_runtime.clients["one"] is not old_one
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_reconnect_unknown_server_fails_without_reload(tmp_path) -> None:
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"known": _config("known")},
    )
    await loop.start_session()
    try:
        runtime = loop._mcp_runtime
        with pytest.raises(ValueError, match="unknown MCP server: absent"):
            await loop.reconnect_mcp_server("absent")

        assert loop._mcp_runtime is runtime
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_targeted_reconnect_keeps_new_runtime_client_owned_and_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[MCPClient] = []

    class SpyClient(MCPClient):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.disconnect_calls = 0
            created.append(self)

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            await super().disconnect()

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", SpyClient)
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={
            "one": _dynamic_config("one", "initial"),
            "two": _config("two"),
        },
    )
    await loop.start_session()
    runtime = loop._mcp_runtime
    assert runtime is not None
    old_client = runtime.clients["one"]
    loop._mcp_configs["one"] = _dynamic_config("one", "replacement")
    loop._turn_running = True
    try:
        assert await loop.reconnect_mcp_server("one") == {}
        new_client = runtime.clients["one"]
        assert new_client is not old_client
        assert old_client.disconnect_calls == 0
        assert new_client.disconnect_calls == 0
        assert new_client._process is not None
        assert new_client.session_reinitialized_handler is not None

        reinitialized = await new_client.session_reinitialized_handler(
            new_client.session_generation,
            "resources/list",
            {},
        )
        assert reinitialized is True
        assert runtime.clients["one"] is new_client

        await runtime.wait_for_refreshes()
        result = await loop.tools["mcp__one__updated"].run(text="ignored")
        assert result.success is True
        assert result.output == "updated"
    finally:
        loop._turn_running = False
        await loop._close_retired_mcp_runtimes()
        assert old_client.disconnect_calls == 1
        await loop.aclose()
        assert new_client.disconnect_calls == 1

    assert len(created) == 3


@pytest.mark.asyncio
async def test_failed_targeted_reconnect_preserves_old_server_and_cleans_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[MCPClient] = []

    class SpyClient(MCPClient):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.disconnect_calls = 0
            created.append(self)

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            await super().disconnect()

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", SpyClient)
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"one": _dynamic_config("one", "initial")},
    )
    await loop.start_session()
    runtime = loop._mcp_runtime
    assert runtime is not None
    old_client = runtime.clients["one"]
    old_tool = loop.tools["mcp__one__old"]
    loop._mcp_configs["one"] = MCPServerConfig(
        name="one",
        command=str(tmp_path / "missing-mcp-server"),
        args=[],
        env={},
        transport="stdio",
    )
    try:
        with pytest.raises(OSError):
            await loop.reconnect_mcp_server("one")

        assert loop._mcp_runtime is runtime
        assert runtime.clients["one"] is old_client
        assert old_client.disconnect_calls == 0
        assert created[-1].disconnect_calls == 1
        result = await old_tool.run(text="still works")
        assert result.success is True
        assert result.output == "old"
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_cancelled_targeted_reconnect_preserves_old_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate_started = asyncio.Event()
    release_candidate = asyncio.Event()
    created: list[MCPClient] = []

    class SpyClient(MCPClient):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.disconnect_calls = 0
            created.append(self)

        async def connect(self) -> None:
            await super().connect()
            if self.config.env.get("ASH_DYNAMIC_MODE") == "replacement":
                candidate_started.set()

        async def list_tools(self) -> list[dict]:
            if self.config.env.get("ASH_DYNAMIC_MODE") == "replacement":
                await release_candidate.wait()
            return await super().list_tools()

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            await super().disconnect()

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", SpyClient)
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"one": _dynamic_config("one", "initial")},
    )
    await loop.start_session()
    runtime = loop._mcp_runtime
    assert runtime is not None
    old_client = runtime.clients["one"]
    old_tool = loop.tools["mcp__one__old"]
    loop._mcp_configs["one"] = _dynamic_config("one", "replacement")
    replacement_task = asyncio.create_task(loop.reconnect_mcp_server("one"))
    try:
        await asyncio.wait_for(candidate_started.wait(), timeout=2)
        replacement_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await replacement_task

        assert runtime.clients["one"] is old_client
        assert old_client.disconnect_calls == 0
        assert created[-1].disconnect_calls == 1
        result = await old_tool.run(text="still works")
        assert result.success is True
        assert result.output == "old"
    finally:
        release_candidate.set()
        if not replacement_task.done():
            replacement_task.cancel()
            await replacement_task
        await loop.aclose()


@pytest.mark.asyncio
async def test_targeted_reconnect_cancellation_finishes_retired_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disconnect_started = asyncio.Event()
    release_disconnect = asyncio.Event()

    class SpyClient(MCPClient):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.disconnect_calls = 0

        async def disconnect(self) -> None:
            if self.config.env.get("ASH_DYNAMIC_MODE") == "initial":
                disconnect_started.set()
                await release_disconnect.wait()
            self.disconnect_calls += 1
            await super().disconnect()

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", SpyClient)
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"one": _dynamic_config("one", "initial")},
    )
    await loop.start_session()
    runtime = loop._mcp_runtime
    assert runtime is not None
    old_client = runtime.clients["one"]
    loop._mcp_configs["one"] = _dynamic_config("one", "replacement")
    replacement_task = asyncio.create_task(loop.reconnect_mcp_server("one"))
    try:
        await asyncio.wait_for(disconnect_started.wait(), timeout=2)
        replacement_task.cancel()
        release_disconnect.set()
        with pytest.raises(asyncio.CancelledError):
            await replacement_task

        assert old_client.disconnect_calls == 1
        assert old_client._process is None
        assert not runtime._retired_clients
    finally:
        release_disconnect.set()
        if not replacement_task.done():
            replacement_task.cancel()
            await replacement_task
        await loop.aclose()


@pytest.mark.asyncio
async def test_retired_client_cleanup_is_serialized_and_disconnects_once(
    tmp_path: Path,
) -> None:
    disconnect_started = asyncio.Event()
    release_disconnect = asyncio.Event()

    class RetiredClient:
        def __init__(self) -> None:
            self.disconnect_calls = 0

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            disconnect_started.set()
            await release_disconnect.wait()

    runtime = MCPRuntime({}, SafetyGuard(tmp_path))
    client = RetiredClient()
    runtime._retired_clients.add(client)  # type: ignore[arg-type]

    first = asyncio.create_task(runtime.close_retired_clients())
    await asyncio.wait_for(disconnect_started.wait(), timeout=2)
    second = asyncio.create_task(runtime.close_retired_clients())
    release_disconnect.set()

    await asyncio.gather(first, second)

    assert client.disconnect_calls == 1
    assert not runtime._retired_clients


@pytest.mark.asyncio
async def test_retired_client_cleanup_retries_and_retains_failed_clients(
    tmp_path: Path,
) -> None:
    class FlakyRetiredClient:
        def __init__(self) -> None:
            self.disconnect_calls = 0

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            if self.disconnect_calls == 1:
                raise RuntimeError("synthetic disconnect failure")

    runtime = MCPRuntime({}, SafetyGuard(tmp_path))
    flaky = FlakyRetiredClient()
    runtime._retired_clients.add(flaky)  # type: ignore[arg-type]

    await runtime.close_retired_clients()

    assert flaky.disconnect_calls == 2
    assert not runtime._retired_clients

    persistent = FlakyRetiredClient()

    async def always_fail() -> None:
        persistent.disconnect_calls += 1
        raise RuntimeError("synthetic persistent disconnect failure")

    persistent.disconnect = always_fail  # type: ignore[method-assign]
    runtime._retired_clients.add(persistent)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="failed to disconnect 1 retired"):
        await runtime.close_retired_clients()
    assert persistent in runtime._retired_clients


@pytest.mark.asyncio
async def test_runtime_close_surfaces_failed_active_client_cleanup(
    tmp_path: Path,
) -> None:
    class FailingActiveClient:
        def __init__(self) -> None:
            self.disconnect_calls = 0

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            raise RuntimeError("synthetic active disconnect failure")

    runtime = MCPRuntime({}, SafetyGuard(tmp_path))
    client = FailingActiveClient()
    runtime.clients["one"] = client  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="failed to disconnect 1 retired"):
        await runtime.close()

    assert client.disconnect_calls == 2
    assert not runtime.clients
    assert any(item is client for item in runtime._retired_clients)


@pytest.mark.asyncio
async def test_targeted_reconnect_waits_for_catalog_refresh_before_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    candidate_created = asyncio.Event()
    created: list[MCPClient] = []

    def definition(name: str) -> dict:
        return {
            "name": name,
            "description": name,
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        }

    class RaceClient(MCPClient):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.list_calls = 0
            self.disconnect_calls = 0
            created.append(self)
            if self.config.env.get("ASH_DYNAMIC_MODE") == "replacement":
                candidate_created.set()

        async def list_tools(self) -> list[dict]:
            self.list_calls += 1
            if self.config.env.get("ASH_DYNAMIC_MODE") == "initial" and self.list_calls == 2:
                refresh_started.set()
                await release_refresh.wait()
                return [definition("refreshed")]
            return await super().list_tools()

        async def disconnect(self) -> None:
            self.disconnect_calls += 1
            await super().disconnect()

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", RaceClient)
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"one": _dynamic_config("one", "initial")},
    )
    await loop.start_session()
    runtime = loop._mcp_runtime
    assert runtime is not None
    loop._mcp_configs["one"] = _dynamic_config("one", "replacement")
    old_client = runtime.clients["one"]
    await old_client.notification_handler("notifications/tools/list_changed", {})
    await asyncio.wait_for(refresh_started.wait(), timeout=2)

    replacement_task = asyncio.create_task(loop.reconnect_mcp_server("one"))
    try:
        await asyncio.wait_for(candidate_created.wait(), timeout=2)
        release_refresh.set()
        await runtime.wait_for_refreshes()
        assert await replacement_task == {}
        assert "mcp__one__updated" in loop.tools
        assert "mcp__one__refreshed" not in loop.tools
        assert not any("ownership changed unexpectedly" in error for error in runtime.errors.values())
    finally:
        release_refresh.set()
        if not replacement_task.done():
            replacement_task.cancel()
            await replacement_task
        await loop.aclose()


@pytest.mark.asyncio
async def test_mcp_reload_log_redacts_bounded_server_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "synthetic logging marker"

    class LogSpy:
        def __init__(self) -> None:
            self.warning_calls: list[tuple[object, ...]] = []

        def warning(self, *args: object) -> None:
            self.warning_calls.append(args)

    async def fake_start(self: MCPRuntime) -> dict:
        self._started = True
        self.errors[f'password="{marker}"'] = (
            f'upstream password="{marker}" ' + "x" * 700
        )
        return {}

    monkeypatch.setattr(MCPRuntime, "start", fake_start)
    log_spy = LogSpy()
    monkeypatch.setattr("ash.core.loop._log", log_spy)
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"broken": _config("broken")},
    )
    try:
        await loop.start_session()
    finally:
        await loop.aclose()

    assert len(log_spy.warning_calls) == 1
    logged_name = log_spy.warning_calls[0][1]
    assert isinstance(logged_name, str)
    assert marker not in logged_name
    assert 'password="[REDACTED]"' in logged_name
    logged_error = log_spy.warning_calls[0][2]
    assert isinstance(logged_error, str)
    assert marker not in logged_error
    assert 'password="[REDACTED]"' in logged_error
    assert len(logged_error) == 512
