from __future__ import annotations

import io
import sys

import pytest

from ash.core.loop import AshLoop
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
