# tests/unit/test_mcp.py
import asyncio
import json
import io
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest
import httpx
from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.safety.guard import SafetyGuard
from ash.mcp.client import MCPClient
from ash.mcp.runtime import MCPRuntime
from ash.mcp.server import (
    MCPServerConfig,
    MCPServerInstance,
    MCPServerManager,
    MCPConfigSource,
    MAX_MCP_CONFIG_BYTES,
    load_mcp_servers,
    load_mcp_server_sources,
    save_mcp_servers,
    expand_env_vars,
)
from pathlib import Path
from ash.ui.headless import HeadlessUI


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


def test_load_mcp_sources_namespaces_plugin_servers(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    path = plugin / ".mcp.json"
    path.write_text(
        json.dumps(
            {"mcpServers": {"local": {"command": "server", "env": {"TOKEN": "value"}}}}
        )
    )

    servers = load_mcp_server_sources(
        [
            MCPConfigSource(
                path,
                namespace="example",
                cwd=plugin,
                environment=(("ASH_PLUGIN_ROOT", str(plugin)),),
            )
        ]
    )

    config = servers["example__local"]
    assert config.cwd == str(plugin)
    assert config.env == {"TOKEN": "value", "ASH_PLUGIN_ROOT": str(plugin)}


def test_load_mcp_sources_rejects_duplicate_names(tmp_path: Path) -> None:
    paths = []
    for name in ("first", "second"):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({"same": {"command": "server"}}))
        paths.append(MCPConfigSource(path))

    with pytest.raises(ValueError, match="duplicate MCP server"):
        load_mcp_server_sources(paths)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "must be an object"),
        ({"mcpServers": []}, "mcpServers must be an object"),
        ({"bad name": {"command": "server"}}, "invalid MCP server name"),
        ({"server": "invalid"}, "must be an object"),
        ({"server": {"args": []}}, "requires a command"),
        (
            {"server": {"command": "server", "args": [1]}},
            "args must be a list of strings",
        ),
        (
            {"server": {"transport": "http", "url": ""}},
            "requires a url",
        ),
    ],
)
def test_load_mcp_servers_rejects_invalid_config(
    tmp_path: Path, payload, message: str
) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match=message):
        load_mcp_servers(path)


def test_load_mcp_servers_rejects_oversized_config(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_bytes(b" " * (MAX_MCP_CONFIG_BYTES + 1))

    with pytest.raises(ValueError, match="exceeds 256 KiB"):
        load_mcp_servers(path)


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


def test_save_mcp_oauth_server_round_trip_and_resolves_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_OAUTH_SECRET", "runtime-secret")
    path = tmp_path / ".mcp.json"
    config = MCPServerConfig(
        name="protected",
        command="",
        args=[],
        env={},
        transport="http",
        url="https://mcp.example.test/rpc",
        auth="oauth",
        oauth={
            "client_id": "registered-client",
            "client_secret": "${MCP_OAUTH_SECRET}",
            "scope": "files:read",
            "redirect_port": 43123,
        },
    )

    save_mcp_servers({"protected": config}, path)
    loaded = load_mcp_servers(path)["protected"]

    assert loaded.auth == "oauth"
    assert loaded.oauth == config.oauth
    assert loaded.resolved_oauth["client_secret"] == "runtime-secret"
    assert "runtime-secret" not in path.read_text(encoding="utf-8")


def test_mcp_oauth_reports_missing_client_secret_environment() -> None:
    config = MCPServerConfig(
        name="protected",
        command="",
        args=[],
        env={},
        transport="http",
        url="https://mcp.example.test/rpc",
        auth="oauth",
        oauth={
            "client_id": "registered-client",
            "client_secret": "${ASH_TEST_MISSING_MCP_SECRET}",
        },
    )

    with pytest.raises(ValueError, match="environment variable is not set"):
        _ = config.resolved_oauth


def test_mcp_oauth_constructor_rejects_invalid_options_before_save() -> None:
    with pytest.raises(ValueError, match="options require auth mode oauth"):
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
            oauth={"scope": "files:read"},
        )
    with pytest.raises(ValueError, match="redirect_port is invalid"):
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
            auth="oauth",
            oauth={"redirect_port": -1},
        )


@pytest.mark.parametrize("transport", ["stdio", "websocket"])
def test_mcp_oauth_rejects_unsupported_transports(transport: str) -> None:
    with pytest.raises(ValueError, match="requires the http or sse transport"):
        MCPServerConfig(
            name="protected",
            command="server",
            args=[],
            env={},
            transport=transport,
            auth="oauth",
        )


def test_load_mcp_oauth_rejects_plaintext_client_secret(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "protected": {
                        "transport": "http",
                        "url": "https://mcp.example.test/rpc",
                        "auth": "oauth",
                        "oauth": {"client_secret": "plaintext-secret"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must reference an environment variable"):
        load_mcp_servers(path)


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


def test_manager_scrubs_host_secrets_and_keeps_explicit_server_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    manager = MCPServerManager()
    config = MCPServerConfig(
        name="test-server",
        command="server",
        args=[],
        env={"SERVER_TOKEN": "explicit-value"},
    )
    process = Mock()

    with patch("ash.mcp.server.subprocess.Popen", return_value=process) as popen:
        manager.start_server(config)

    environment = popen.call_args.kwargs["env"]
    assert environment["SERVER_TOKEN"] == "explicit-value"
    assert "UNRELATED_SECRET" not in environment
    assert environment["PATH"]


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
import json, os, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}, "resources": {}, "prompts": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]}
    elif method == "tools/call":
        text = message["params"]["arguments"]["text"]
        if text == "__environment__":
            text = f"{os.getenv('SERVER_TOKEN', 'missing')}|{os.getenv('UNRELATED_SECRET', 'missing')}"
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    elif method == "resources/list":
        result = {"resources": [{"uri": "file:///example", "name": "example"}]}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": [{"uriTemplate": "file:///{path}", "name": "file"}]}
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


class IdleProvider(ProviderABC):
    model_name = "idle"

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        yield StreamChunk(content="idle", is_done=True)

    def count_tokens(self, text: str) -> int:
        return len(text.split())


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
        assert client.protocol_version == "2025-06-18"
        assert client.server_info == {"name": "fake", "version": "1"}
        assert client.supports_server_capability("tools") is True
        tools = await client.list_tools()
        assert tools[0]["name"] == "echo"
        result = await client.call_tool("echo", {"text": "hello"})
        assert result["content"][0]["text"] == "hello"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_async_client_scrubs_host_secrets_and_keeps_explicit_server_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={"SERVER_TOKEN": "explicit-value"},
    )
    client = MCPClient(config)
    await client.connect()
    try:
        result = await client.call_tool("echo", {"text": "__environment__"})
        assert result["content"][0]["text"] == "explicit-value|missing"
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
        listed_resources = await tools["mcp_list_resources"].run(server="fake")
        listed_templates = await tools["mcp_list_resource_templates"].run()
        listed_prompts = await tools["mcp_list_prompts"].run()
        assert "file:///example" in listed_resources.output
        assert "file:///{path}" in listed_templates.output
        assert "review" in listed_prompts.output
        resource = await tools["mcp_read_resource"].run(
            server="fake", uri="file:///example"
        )
        assert "resource text" in resource.output
        prompt = await tools["mcp_get_prompt"].run(server="fake", name="review")
        assert "review it" in prompt.output
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_applies_plugin_mcp_working_directory_and_environment(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    server = FAKE_MCP_SERVER.replace(
        'message["params"]["arguments"]["text"]',
        "__import__('os').getcwd() + '|' + __import__('os').environ['ASH_PLUGIN_ROOT']",
    )
    config = MCPServerConfig(
        name="example__fake",
        command=sys.executable,
        args=["-u", "-c", server],
        env={"ASH_PLUGIN_ROOT": str(plugin)},
        cwd=str(plugin),
    )
    runtime = MCPRuntime({"example__fake": config}, SafetyGuard(tmp_path))
    tools = await runtime.start()
    try:
        result = await tools["mcp__example__fake__echo"].run(text="ignored")
        assert result.output == f"{plugin}|{plugin}"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_loop_reloads_mcp_tools_without_restarting_session(
    tmp_path: Path,
) -> None:
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={},
    )
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"fake": config},
    )
    await loop.start_session()
    assert "mcp__fake__echo" in loop.tools

    errors = await loop.reload_mcp_servers({})

    assert errors == {}
    assert "mcp__fake__echo" not in loop.tools
    await loop.aclose()


@pytest.mark.asyncio
async def test_streamable_http_tracks_session_and_parses_sse() -> None:
    seen_session = []
    seen_protocol = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            seen_session.append(request.headers.get("Mcp-Session-Id"))
            seen_protocol.append(request.headers.get("MCP-Protocol-Version"))
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
        assert request.headers["MCP-Protocol-Version"] == "2025-06-18"
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
    assert seen_protocol == ["2025-06-18"]
    await http.aclose()


INTERACTIVE_MCP_SERVER = r"""
import json, sys
pending_tools_id = None
root_uri = ""
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {"listChanged": True}, "logging": {}},
            "serverInfo": {"name": "interactive", "version": "1"},
            "instructions": "server guidance",
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif message.get("method") == "notifications/initialized":
        print(json.dumps({"jsonrpc": "2.0", "id": "roots-1", "method": "roots/list", "params": {}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "ready"}}), flush=True)
    elif message.get("method") == "tools/list":
        if root_uri:
            result = {"tools": [{"name": "root", "description": root_uri, "inputSchema": {"type": "object"}}]}
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
        else:
            pending_tools_id = message["id"]
    elif message.get("id") == "roots-1" and "result" in message:
        root_uri = message["result"]["roots"][0]["uri"]
        if pending_tools_id is not None:
            result = {"tools": [{"name": "root", "description": root_uri, "inputSchema": {"type": "object"}}]}
            print(json.dumps({"jsonrpc": "2.0", "id": pending_tools_id, "result": result}), flush=True)
            pending_tools_id = None
"""


@pytest.mark.asyncio
async def test_stdio_dispatches_server_requests_and_notifications(
    tmp_path: Path,
) -> None:
    notifications: list[tuple[str, dict]] = []
    client = MCPClient(
        MCPServerConfig(
            name="interactive",
            command=sys.executable,
            args=["-u", "-c", INTERACTIVE_MCP_SERVER],
            env={},
        ),
        roots=(tmp_path,),
        notification_handler=lambda method, params: notifications.append(
            (method, params)
        ),
    )

    await client.connect()
    try:
        instructions = client.server_instructions
        tools = await client.list_tools()
        for _ in range(20):
            if notifications:
                break
            await asyncio.sleep(0.01)
    finally:
        await client.disconnect()

    assert instructions == "server guidance"
    assert tools[0]["description"] == tmp_path.resolve().as_uri()
    assert notifications == [
        ("notifications/message", {"level": "info", "data": "ready"})
    ]


@pytest.mark.asyncio
async def test_http_tool_listing_follows_pagination() -> None:
    cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if "id" not in payload:
            return httpx.Response(202)
        cursor = payload.get("params", {}).get("cursor")
        cursors.append(cursor)
        result = (
            {"tools": [{"name": "first"}], "nextCursor": "page-2"}
            if cursor is None
            else {"tools": [{"name": "second"}]}
        )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
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
    try:
        tools = await client.list_tools()
    finally:
        await client.disconnect()
        await http.aclose()

    assert [tool["name"] for tool in tools] == ["first", "second"]
    assert cursors == [None, "page-2"]


@pytest.mark.asyncio
async def test_request_timeout_sends_cancellation_notification() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._request_stdio = AsyncMock(side_effect=asyncio.TimeoutError())
    client.notify = AsyncMock()

    with pytest.raises(asyncio.TimeoutError):
        await client.request("tools/call", {"name": "slow"})

    client.notify.assert_awaited_once_with(
        "notifications/cancelled",
        {"requestId": 1, "reason": "tools/call timed out"},
    )
