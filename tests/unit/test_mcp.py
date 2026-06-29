# tests/unit/test_mcp.py
import json
import sys
import pytest
import httpx
from safety.guard import SafetyGuard
from mcp.client import MCPClient
from mcp.runtime import MCPRuntime
from mcp.server import (
    MCPServerConfig,
    MCPServerInstance,
    MCPServerManager,
    load_mcp_servers,
    save_mcp_servers,
    expand_env_vars,
)
from pathlib import Path


def test_load_mcp_servers_from_file(tmp_path: Path) -> None:
    mcp_file = tmp_path / ".mcp.json"
    mcp_file.write_text(
        json.dumps(
            {
                "github": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"GITHUB_TOKEN": "test"},
                }
            }
        )
    )
    servers = load_mcp_servers(mcp_file)
    assert "github" in servers
    assert servers["github"].command == "npx"
    assert servers["github"].args == ["-y", "@modelcontextprotocol/server-github"]


def test_expand_env_vars() -> None:
    import os

    os.environ["TEST_VAR"] = "hello"
    assert expand_env_vars("${TEST_VAR}/path") == "hello/path"
    assert expand_env_vars("(no var)") == "(no var)"


def test_save_mcp_servers_round_trip(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    config = MCPServerConfig(
        name="local",
        command="server",
        args=["--flag"],
        env={"TOKEN": "${TOKEN}"},
    )
    save_mcp_servers({"local": config}, path)
    loaded = load_mcp_servers(path)
    assert loaded["local"].command == "server"
    assert loaded["local"].args == ["--flag"]
    assert loaded["local"].env == {"TOKEN": "${TOKEN}"}


def test_mcp_secrets_are_resolved_only_at_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "resolved-secret")
    config = MCPServerConfig(
        name="local",
        command="${MCP_COMMAND}",
        args=["--token=${SECRET_TOKEN}"],
        env={"TOKEN": "${SECRET_TOKEN}"},
    )
    monkeypatch.setenv("MCP_COMMAND", "server")
    assert config.command == "${MCP_COMMAND}"
    assert config.resolved_command == "server"
    assert config.resolved_env == {"TOKEN": "resolved-secret"}

    path = tmp_path / ".mcp.json"
    save_mcp_servers({"local": config}, path)
    assert "resolved-secret" not in path.read_text()


def test_manager_starts_and_stops_server() -> None:
    manager = MCPServerManager()
    config = MCPServerConfig(
        name="test-server",
        command=sys.executable,
        args=["-c", "import time; time.sleep(60)"],
        env={},
        transport="stdio",
    )
    instance = manager.start_server(config)
    assert isinstance(instance, MCPServerInstance)
    assert instance.name == "test-server"
    assert instance.process.poll() is None
    manager.stop_server("test-server")
    assert manager.get_server("test-server") is None


def test_manager_stop_all() -> None:
    manager = MCPServerManager()
    for i in range(3):
        config = MCPServerConfig(
            name=f"server-{i}",
            command=sys.executable,
            args=["-c", "import time; time.sleep(60)"],
            env={},
            transport="stdio",
        )
        manager.start_server(config)
    assert len(manager.list_servers()) == 3
    manager.stop_all()
    assert len(manager.list_servers()) == 0


FAKE_MCP_SERVER = r"""
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": message["params"]["arguments"]["text"]}], "isError": False}
    elif method == "resources/list":
        result = {"resources": [{"uri": "file:///example", "name": "example"}]}
    elif method == "prompts/list":
        result = {"prompts": [{"name": "review", "description": "Review"}]}
    elif method == "resources/read":
        result = {"contents": [{"uri": "file:///example", "text": "resource text"}]}
    elif method == "prompts/get":
        result = {"messages": [{"role": "user", "content": {"type": "text", "text": "review it"}}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


@pytest.mark.asyncio
async def test_async_client_initializes_lists_and_calls_tools() -> None:
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={},
    )
    client = MCPClient(config)
    await client.connect()
    try:
        tools = await client.list_tools()
        assert tools[0]["name"] == "echo"
        result = await client.call_tool("echo", {"text": "hello"})
        assert result["content"][0]["text"] == "hello"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_runtime_registers_namespaced_tool(tmp_path: Path) -> None:
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={},
    )
    runtime = MCPRuntime({"fake": config}, SafetyGuard(tmp_path))
    tools = await runtime.start()
    try:
        tool = tools["mcp__fake__echo"]
        result = await tool.run(text="hello")
        assert result.success is True
        assert result.output == "hello"
        assert (await runtime.list_resources())[0]["uri"] == "file:///example"
        assert (await runtime.list_prompts())[0]["name"] == "review"
        resource = await tools["mcp_read_resource"].run(
            server="fake", uri="file:///example"
        )
        assert "resource text" in resource.output
        prompt = await tools["mcp_get_prompt"].run(server="fake", name="review")
        assert "review it" in prompt.output
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_streamable_http_tracks_session_and_parses_sse() -> None:
    seen_session = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            seen_session.append(request.headers.get("Mcp-Session-Id"))
            return httpx.Response(204)
        payload = json.loads(request.content)
        if "id" not in payload:
            return httpx.Response(202)
        if payload["method"] == "initialize":
            body = (
                'event: message\ndata: {"jsonrpc":"2.0","id":1,'
                '"result":{"protocolVersion":"2025-06-18","capabilities":{}}}\n\n'
            )
            return httpx.Response(
                200,
                text=body,
                headers={
                    "content-type": "text/event-stream",
                    "Mcp-Session-Id": "session-1",
                },
            )
        assert request.headers["Mcp-Session-Id"] == "session-1"
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": []}},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
        ),
        http_client=http,
    )
    await client.connect()
    assert await client.list_tools() == []
    await client.disconnect()
    assert seen_session == ["session-1"]
    await http.aclose()
