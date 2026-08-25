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
from ash.mcp.client import (
    MCPClient,
    MCPProtocolError,
    MCPTaskTimeout,
)
from ash.mcp.runtime import MCPRuntime, MCPTool
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


def test_mcp_oauth_rejects_stdio_transport() -> None:
    with pytest.raises(ValueError, match="requires the http or sse transport"):
        MCPServerConfig(
            name="protected",
            command="server",
            args=[],
            env={},
            transport="stdio",
            auth="oauth",
        )


def test_mcp_config_rejects_unimplemented_websocket_transport() -> None:
    with pytest.raises(ValueError, match="Unknown MCP transport"):
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="websocket",
            url="wss://mcp.example.test/rpc",
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


COMPLEX_MCP_INPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": {
        "identifier": {"type": "string", "pattern": "^[a-z]+$"},
    },
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["safe", "fast"]},
        "target": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "path"},
                        "path": {"$ref": "#/$defs/identifier"},
                    },
                    "required": ["kind", "path"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "id"},
                        "id": {"type": "integer", "minimum": 1},
                    },
                    "required": ["kind", "id"],
                    "additionalProperties": False,
                },
            ]
        },
        "options": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {"enabled": {"type": "boolean"}},
                    "required": ["enabled"],
                    "additionalProperties": False,
                },
            ]
        },
        "tags": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "limit": {"type": "integer", "minimum": 1},
    },
    "required": ["mode", "target", "options", "tags", "limit"],
    "additionalProperties": False,
}


class StubMCPClient:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(
        self,
        name: str,
        arguments: dict,
        *,
        expected_contract: str | None = None,
        as_task: bool = False,
    ) -> dict:
        del expected_contract, as_task
        self.calls.append((name, arguments))
        return self.result


def _mcp_tool(
    tmp_path: Path,
    client: StubMCPClient,
    *,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    protocol_version: str = "2025-11-25",
) -> MCPTool:
    definition = {
        "name": "complex",
        "description": "Exercise the complete MCP schema boundary.",
        "inputSchema": input_schema or COMPLEX_MCP_INPUT_SCHEMA,
    }
    if output_schema is not None:
        definition["outputSchema"] = output_schema
    return MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="test",
        definition=definition,
        protocol_version=protocol_version,
    )


@pytest.mark.asyncio
async def test_mcp_tool_preserves_and_enforces_complete_input_schema(
    tmp_path: Path,
) -> None:
    client = StubMCPClient({"content": [{"type": "text", "text": "ok"}]})
    tool = _mcp_tool(tmp_path, client)
    exposed = tool.json_schema()

    assert exposed == COMPLEX_MCP_INPUT_SCHEMA
    exposed["properties"]["mode"]["enum"].append("mutated")
    assert tool.json_schema() == COMPLEX_MCP_INPUT_SCHEMA

    arguments = {
        "mode": "safe",
        "target": {"kind": "path", "path": "alpha"},
        "options": None,
        "tags": ["one", "two"],
        "limit": 2,
    }
    result = await tool.run(**arguments)

    assert result.success is True
    assert result.output == "ok"
    assert client.calls == [("complex", arguments)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "unknown"},
        {"target": {"kind": "path", "path": "INVALID"}},
        {"tags": ["same", "same"]},
        {"limit": "2"},
        {"extra": True},
    ],
)
async def test_mcp_tool_rejects_invalid_arguments_without_remote_call(
    tmp_path: Path,
    overrides: dict,
) -> None:
    client = StubMCPClient({"content": []})
    tool = _mcp_tool(tmp_path, client)
    arguments = {
        "mode": "safe",
        "target": {"kind": "id", "id": 4},
        "options": {"enabled": True},
        "tags": ["one"],
        "limit": 2,
        **overrides,
    }

    result = await tool.run(**arguments)

    assert result.success is False
    assert result.error is not None and "invalid MCP tool arguments" in result.error
    assert client.calls == []


def test_mcp_tool_rejects_invalid_and_unknown_schema_dialects(tmp_path: Path) -> None:
    client = StubMCPClient({"content": []})
    with pytest.raises(ValueError, match="not valid JSON Schema"):
        _mcp_tool(
            tmp_path,
            client,
            input_schema={"type": "object", "required": "not-an-array"},
        )
    with pytest.raises(ValueError, match="unsupported JSON Schema dialect"):
        _mcp_tool(
            tmp_path,
            client,
            input_schema={
                "$schema": "https://example.invalid/unknown-dialect",
                "type": "object",
            },
        )

    draft7 = _mcp_tool(
        tmp_path,
        client,
        input_schema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
    )
    assert draft7.json_schema()["$schema"].endswith("draft-07/schema#")


def test_mcp_tool_rejects_non_object_roots_and_remote_references(
    tmp_path: Path,
) -> None:
    client = StubMCPClient({"content": []})
    for schema in ({}, {"type": "string"}):
        with pytest.raises(ValueError, match="root type must be object"):
            MCPTool(
                SafetyGuard(tmp_path),
                client=client,  # type: ignore[arg-type]
                server_name="test",
                definition={"name": "invalid", "inputSchema": schema},
            )
    with pytest.raises(ValueError, match="non-local reference"):
        _mcp_tool(
            tmp_path,
            client,
            input_schema={
                "type": "object",
                "properties": {
                    "value": {"$ref": "http://169.254.169.254/latest/meta-data/"}
                },
            },
        )
    with pytest.raises(ValueError, match="root type must be object"):
        _mcp_tool(
            tmp_path,
            client,
            output_schema={"type": "array"},
        )


def test_mcp_tool_preserves_task_execution_support(tmp_path: Path) -> None:
    client = StubMCPClient({"content": []})
    required = MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="test",
        definition={
            "name": "task-required",
            "inputSchema": {"type": "object"},
            "execution": {"taskSupport": "required"},
        },
    )

    assert required.name == "mcp__test__task-required"
    assert required._task_support == "required"
    optional = MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="test",
        definition={
            "name": "ordinary-or-task",
            "inputSchema": {"type": "object"},
            "execution": {"taskSupport": "optional"},
        },
    )
    assert optional.name == "mcp__test__ordinary-or-task"
    default_forbidden = MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="test",
        definition={
            "name": "ordinary",
            "inputSchema": {"type": "object"},
            "execution": {},
        },
    )
    assert default_forbidden.name == "mcp__test__ordinary"


@pytest.mark.asyncio
async def test_mcp_schema_regex_cannot_block_runtime_or_reach_server(
    tmp_path: Path,
) -> None:
    client = StubMCPClient({"content": []})
    tool = _mcp_tool(
        tmp_path,
        client,
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string", "pattern": "^(a+)+$"}},
            "required": ["value"],
        },
    )

    result = await asyncio.wait_for(tool.run(value="a" * 32 + "!"), timeout=3)

    assert result.success is False
    assert result.error is not None
    assert "deadline" in result.error or "resource limit" in result.error
    assert client.calls == []


@pytest.mark.asyncio
async def test_mcp_legacy_protocol_uses_draft7_for_implicit_schema(
    tmp_path: Path,
) -> None:
    client = StubMCPClient({"content": [{"type": "text", "text": "ok"}]})
    tool = _mcp_tool(
        tmp_path,
        client,
        protocol_version="2025-03-26",
        input_schema={
            "type": "object",
            "properties": {
                "pair": {
                    "type": "array",
                    "items": [{"type": "string"}, {"type": "integer"}],
                    "additionalItems": False,
                }
            },
            "required": ["pair"],
        },
        output_schema={
            "type": "object",
            "required": ["ignored-for-legacy-protocol"],
        },
    )

    result = await tool.run(pair=["one", 2])

    assert result.success is True
    assert result.output == "ok"


@pytest.mark.asyncio
async def test_mcp_schema_allows_properties_named_like_schema_keywords(
    tmp_path: Path,
) -> None:
    client = StubMCPClient({"content": [{"type": "text", "text": "ok"}]})
    tool = _mcp_tool(
        tmp_path,
        client,
        input_schema={
            "type": "object",
            "properties": {
                "$ref": {"type": "string"},
                "patternProperties": {"type": "string"},
            },
            "required": ["$ref", "patternProperties"],
            "additionalProperties": False,
        },
    )

    result = await tool.run(**{"$ref": "literal", "patternProperties": "literal"})

    assert result.success is True


@pytest.mark.asyncio
async def test_mcp_tool_preserves_rich_result_and_validates_output_schema(
    tmp_path: Path,
) -> None:
    output_schema = {
        "type": "object",
        "properties": {"count": {"type": "integer", "minimum": 1}},
        "required": ["count"],
        "additionalProperties": False,
    }
    remote_result = {
        "content": [
            {"type": "text", "text": "two results"},
            {"type": "image", "mimeType": "image/png", "data": "AAAA"},
        ],
        "structuredContent": {"count": 2},
        "isError": False,
        "_meta": {"cacheKey": "stable"},
        "vendorExtension": {"trace": "abc"},
    }
    client = StubMCPClient(remote_result)
    tool = _mcp_tool(
        tmp_path,
        client,
        input_schema={"type": "object", "additionalProperties": False},
        output_schema=output_schema,
    )

    result = await tool.run()

    assert result.success is True
    assert json.loads(result.output) == remote_result


@pytest.mark.asyncio
async def test_mcp_tool_preserves_annotated_text_block_envelope(tmp_path: Path) -> None:
    remote_result = {
        "content": [
            {
                "type": "text",
                "text": "annotated",
                "annotations": {"audience": ["assistant"], "priority": 0.8},
                "_meta": {"trace": "one"},
                "vendor": "retained",
            }
        ]
    }
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is True
    assert json.loads(result.output)["content"] == remote_result["content"]


@pytest.mark.asyncio
async def test_mcp_tool_keeps_structured_only_and_application_error_envelopes(
    tmp_path: Path,
) -> None:
    structured_client = StubMCPClient(
        {"content": [], "structuredContent": {"items": [1, 2]}}
    )
    structured_tool = _mcp_tool(
        tmp_path,
        structured_client,
        input_schema={"type": "object"},
    )
    structured = await structured_tool.run()
    assert json.loads(structured.output) == {
        "content": [],
        "structuredContent": {"items": [1, 2]},
        "isError": False,
    }

    error_client = StubMCPClient(
        {
            "content": [{"type": "text", "text": "retry with another date"}],
            "structuredContent": {"code": "invalid_date"},
            "isError": True,
            "_meta": {"request": "one"},
        }
    )
    error_tool = _mcp_tool(
        tmp_path,
        error_client,
        input_schema={"type": "object"},
    )
    failed = await error_tool.run()
    assert failed.success is False
    assert failed.error == "retry with another date"
    assert json.loads(failed.output) == {
        "content": [{"type": "text", "text": "retry with another date"}],
        "structuredContent": {"code": "invalid_date"},
        "isError": True,
        "_meta": {"request": "one"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remote_result", "message"),
    [
        ({"structuredContent": {"value": 1}}, "content is required"),
        ({"content": None}, "content must be an array"),
        ({"content": [], "structuredContent": None}, "must be an object"),
        ({"content": [], "isError": None}, "isError must be a boolean"),
        ({"content": [], "_meta": None}, "_meta must be an object"),
    ],
)
async def test_mcp_tool_rejects_malformed_results_without_losing_wire_payload(
    tmp_path: Path,
    remote_result: dict,
    message: str,
) -> None:
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is False
    assert result.error is not None and message in result.error
    assert json.loads(result.output) == remote_result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        {"type": "text", "text": 7},
        {"type": "image", "data": "not base64", "mimeType": "image/png"},
        {"type": "resource", "resource": {"text": "missing uri"}},
        {"type": "unknown", "value": "extension"},
    ],
)
async def test_mcp_tool_rejects_malformed_content_blocks(
    tmp_path: Path,
    content: dict,
) -> None:
    remote_result = {"content": [content]}
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is False
    assert result.error is not None and "content[0]" in result.error
    assert json.loads(result.output) == remote_result


@pytest.mark.asyncio
async def test_mcp_tool_preserves_invalid_structured_output_for_recovery(
    tmp_path: Path,
) -> None:
    remote_result = {
        "content": [{"type": "text", "text": "server summary"}],
        "structuredContent": {"count": "two"},
    }
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
        output_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
    )

    result = await tool.run()

    assert result.success is False
    assert result.error is not None and "invalid MCP structured result" in result.error
    assert json.loads(result.output)["structuredContent"] == {"count": "two"}


@pytest.mark.asyncio
async def test_mcp_tool_requires_structured_content_for_declared_output_schema(
    tmp_path: Path,
) -> None:
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient({"content": [{"type": "text", "text": "summary"}]}),
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is False
    assert result.output == "summary"
    assert result.error is not None and "requires structuredContent" in result.error


@pytest.mark.asyncio
async def test_mcp_tool_rejects_non_json_wire_values(tmp_path: Path) -> None:
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient({"content": [], "structuredContent": {"value": float("nan")}}),
        input_schema={"type": "object"},
    )

    result = await tool.run()

    assert result.success is False
    assert result.output == ""
    assert result.error is not None and "not JSON-serializable" in result.error


@pytest.mark.asyncio
async def test_mcp_tool_preserves_protocol_error_data_without_replay(
    tmp_path: Path,
) -> None:
    class ErrorClient:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(
            self,
            name: str,
            arguments: dict,
            *,
            expected_contract: str | None = None,
            as_task: bool = False,
        ) -> dict:
            del expected_contract
            del as_task
            self.calls += 1
            raise MCPProtocolError(
                "tools/call failed (-32602): invalid mode",
                code=-32602,
                data={"field": "mode", "expected": ["safe", "fast"]},
            )

    client = ErrorClient()
    tool = MCPTool(
        SafetyGuard(tmp_path),
        client=client,  # type: ignore[arg-type]
        server_name="test",
        definition={"name": "fails", "inputSchema": {"type": "object"}},
    )

    result = await tool.run()

    assert result.success is False
    assert client.calls == 1
    assert result.outcome == "unknown"
    assert json.loads(result.output) == {
        "error": {
            "type": "mcp_protocol_error",
            "message": "tools/call failed (-32602): invalid mode",
            "code": -32602,
            "data": {"field": "mode", "expected": ["safe", "fast"]},
        }
    }


@pytest.mark.asyncio
async def test_mcp_tool_preserves_explicit_null_protocol_error_data(
    tmp_path: Path,
) -> None:
    class ErrorClient:
        async def call_tool(
            self,
            name: str,
            arguments: dict,
            *,
            expected_contract: str | None = None,
            as_task: bool = False,
        ) -> dict:
            del expected_contract
            del as_task
            raise MCPProtocolError("explicit null", code=-32000, data=None)

    tool = MCPTool(
        SafetyGuard(tmp_path),
        client=ErrorClient(),  # type: ignore[arg-type]
        server_name="test",
        definition={"name": "fails", "inputSchema": {"type": "object"}},
    )

    result = await tool.run()

    assert json.loads(result.output)["error"]["data"] is None


@pytest.mark.asyncio
async def test_mcp_output_schema_applies_to_application_errors(tmp_path: Path) -> None:
    remote_result = {
        "content": [{"type": "text", "text": "failed"}],
        "structuredContent": {"code": 7},
        "isError": True,
    }
    tool = _mcp_tool(
        tmp_path,
        StubMCPClient(remote_result),
        input_schema={"type": "object"},
        output_schema={
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    )

    result = await tool.run()

    assert result.success is False
    assert result.error is not None and "invalid MCP structured result" in result.error
    assert json.loads(result.output) == remote_result


@pytest.mark.asyncio
async def test_mcp_client_retains_jsonrpc_error_code_and_data() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._request_stdio = AsyncMock(
        return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32602,
                "message": "invalid arguments",
                "data": {"field": "query"},
            },
        }
    )

    with pytest.raises(MCPProtocolError) as caught:
        await client.request("tools/call", {"name": "search", "arguments": {}})

    assert caught.value.code == -32602
    assert caught.value.data == {"field": "query"}


@pytest.mark.asyncio
async def test_mcp_client_rejects_boolean_error_code_and_distinguishes_data() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._request_stdio = AsyncMock(
        return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": True, "message": "invalid", "data": None},
        }
    )

    with pytest.raises(MCPProtocolError) as caught:
        await client.request("tools/call")

    assert caught.value.code is None
    assert "invalid error code" in str(caught.value)
    assert caught.value.has_data is True
    assert caught.value.data is None


def _task_client(
    responses: list[dict | Exception],
    *,
    timeout: float = 30.0,
) -> tuple[MCPClient, AsyncMock]:
    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        timeout=timeout,
    )
    client.protocol_version = "2025-11-25"
    client.server_capabilities = {"tasks": {"requests": {"tools": {"call": {}}}}}
    request = AsyncMock(side_effect=responses)
    client.request = request  # type: ignore[method-assign]
    return client, request


@pytest.mark.asyncio
async def test_mcp_required_task_tool_polls_and_fetches_result() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "one",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 0,
                }
            },
            {"task": {"taskId": "one", "status": "completed", "ttl": None}},
            {"content": [{"type": "text", "text": "done"}]},
        ]
    )
    result = await client.call_tool("long", {}, as_task=True)

    assert result["content"][0]["text"] == "done"
    assert [call.args[0] for call in request.await_args_list] == [
        "tools/call",
        "tasks/get",
        "tasks/result",
    ]
    assert request.await_args_list[0].args[1]["task"] == {}
    assert request.await_args_list[1].args[1] == {"taskId": "one"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_mcp_task_terminal_failure_is_not_fetched(status: str) -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "bad",
                    "status": status,
                    "ttl": None,
                    "statusMessage": "no",
                }
            },
        ]
    )

    with pytest.raises(MCPProtocolError, match=f"MCP tool task {status}: no"):
        await client.call_tool("long", {}, as_task=True)

    assert request.await_count == 1


@pytest.mark.asyncio
async def test_mcp_task_status_notification_wakes_without_polling() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "fast",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 100000,
                }
            },
            {"content": [{"type": "text", "text": "notified"}]},
        ]
    )

    call = asyncio.create_task(client.call_tool("long", {}, as_task=True))
    await asyncio.sleep(0.01)
    await client._handle_incoming(
        {
            "jsonrpc": "2.0",
            "method": "notifications/tasks/status",
            "params": {
                "taskId": "fast",
                "status": "completed",
                "createdAt": "2025-11-25T10:30:00Z",
                "lastUpdatedAt": "2025-11-25T10:31:00Z",
                "ttl": None,
            },
        }
    )

    result = await asyncio.wait_for(call, 1)

    assert result["content"][0]["text"] == "notified"
    assert [sent.args[0] for sent in request.await_args_list] == [
        "tools/call",
        "tasks/result",
    ]


@pytest.mark.asyncio
async def test_mcp_task_invalid_notifications_are_ignored_and_fallback_polls() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "safe",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 10,
                }
            },
            {
                "task": {
                    "taskId": "safe",
                    "status": "completed",
                    "ttl": None,
                }
            },
            {"content": [{"type": "text", "text": "polled"}]},
        ]
    )

    call = asyncio.create_task(client.call_tool("long", {}, as_task=True))
    await asyncio.sleep(0.01)
    await client._handle_incoming(
        {
            "jsonrpc": "2.0",
            "method": "notifications/tasks/status",
            "params": {"taskId": "other", "status": "completed", "ttl": None},
        }
    )
    await client._handle_incoming(
        {
            "jsonrpc": "2.0",
            "method": "notifications/tasks/status",
            "params": {"taskId": "safe", "status": "exploded", "ttl": None},
        }
    )

    result = await asyncio.wait_for(call, 1)

    assert result["content"][0]["text"] == "polled"
    assert [sent.args[0] for sent in request.await_args_list] == [
        "tools/call",
        "tasks/get",
        "tasks/result",
    ]


@pytest.mark.asyncio
async def test_mcp_task_status_notification_can_update_before_terminal() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "ordered",
                    "status": "input_required",
                    "ttl": None,
                    "pollInterval": 100000,
                    "statusMessage": "need input",
                }
            },
            {
                "task": {
                    "taskId": "ordered",
                    "status": "completed",
                    "ttl": None,
                }
            },
            {"content": [{"type": "text", "text": "resumed"}]},
        ]
    )

    call = asyncio.create_task(client.call_tool("long", {}, as_task=True))
    await asyncio.sleep(0.01)
    await client._handle_incoming(
        {
            "jsonrpc": "2.0",
            "method": "notifications/tasks/status",
            "params": {
                "taskId": "ordered",
                "status": "working",
                "createdAt": "2025-11-25T10:30:00Z",
                "lastUpdatedAt": "2025-11-25T10:31:00Z",
                "ttl": None,
                "pollInterval": 10,
            },
        }
    )

    result = await asyncio.wait_for(call, 1)

    assert result["content"][0]["text"] == "resumed"
    assert [sent.args[0] for sent in request.await_args_list] == [
        "tools/call",
        "tasks/get",
        "tasks/result",
    ]

    assert request.await_args_list[1].args[1] == {"taskId": "ordered"}


@pytest.mark.asyncio
async def test_mcp_task_timeout_cancels_remote_task() -> None:
    client, request = _task_client([], timeout=0.01)
    responses = iter(
        [
            {
                "task": {
                    "taskId": "slow",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 0,
                }
            },
        ]
    )

    async def request_side_effect(method, params, **_):
        del params
        if method == "tools/call":
            return next(responses)
        return {"task": {"taskId": "slow", "status": "working", "ttl": None}}

    request.side_effect = request_side_effect
    client._task_poll_delay = lambda task: 1.0  # type: ignore[method-assign]

    with pytest.raises(MCPTaskTimeout):
        await client.call_tool("slow", {}, as_task=True)

    assert request.await_args_list[-1].args[:2] == ("tasks/cancel", {"taskId": "slow"})


@pytest.mark.asyncio
async def test_mcp_task_cancellation_sends_tasks_cancel() -> None:
    client, request = _task_client(
        [
            {
                "task": {
                    "taskId": "stop",
                    "status": "working",
                    "ttl": None,
                    "pollInterval": 100000,
                }
            },
        ]
    )

    async def cancel_side_effect(method, params, **_):
        if method != "tools/call":
            return {"task": {"taskId": "stop", "status": "cancelled", "ttl": None}}
        return {
            "task": {
                "taskId": "stop",
                "status": "working",
                "ttl": None,
                "pollInterval": 100000,
            }
        }

    request.side_effect = cancel_side_effect
    call = asyncio.create_task(client.call_tool("stop", {}, as_task=True))
    await asyncio.sleep(0)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    await asyncio.sleep(0)
    assert request.await_args_list[-1].args[:2] == ("tasks/cancel", {"taskId": "stop"})


@pytest.mark.asyncio
async def test_mcp_runtime_isolates_invalid_tool_schema(
    tmp_path: Path, monkeypatch
) -> None:
    class CatalogClient:
        server_capabilities = {"tools": {}}

        def __init__(self, config, *, roots=()) -> None:
            self.config = config

        async def connect(self) -> None:
            return None

        def supports_server_capability(self, name: str) -> bool:
            return name == "tools"

        async def list_tools(self) -> list[dict]:
            return [
                {
                    "name": "broken",
                    "inputSchema": {"type": "object", "required": "invalid"},
                },
                {
                    "name": "task-only",
                    "inputSchema": {"type": "object"},
                    "execution": {"taskSupport": "required"},
                },
                {"name": "healthy", "inputSchema": {"type": "object"}},
            ]

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", CatalogClient)
    config = MCPServerConfig(name="catalog", command="unused", args=[], env={})
    runtime = MCPRuntime({"catalog": config}, SafetyGuard(tmp_path))

    tools = await runtime.start()
    try:
        assert "mcp__catalog__healthy" in tools
        assert "mcp__catalog__broken" not in tools
        assert "mcp__catalog__task-only" in tools
        assert "not valid JSON Schema" in runtime.errors["catalog:tool:broken"]
        assert "catalog:tool:task-only" not in runtime.errors
    finally:
        await runtime.close()


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
async def test_stdio_client_accepts_bounded_rich_results_above_64_kib() -> None:
    config = MCPServerConfig(
        name="fake",
        command=sys.executable,
        args=["-u", "-c", FAKE_MCP_SERVER],
        env={},
    )
    client = MCPClient(config)
    await client.connect()
    try:
        text = "x" * 70_000
        result = await client.call_tool("echo", {"text": text})
        assert result["content"] == [{"type": "text", "text": text}]
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
async def test_loop_applies_live_mcp_tool_refresh(tmp_path: Path) -> None:
    config = MCPServerConfig(
        name="dynamic",
        command=sys.executable,
        args=["-u", "-c", DYNAMIC_MCP_SERVER],
        env={},
    )
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
        mcp_configs={"dynamic": config},
    )
    await loop.start_session()
    try:
        await loop._mcp_runtime.wait_for_refreshes()
        assert "mcp__dynamic__old" not in loop.tools
        assert "mcp__dynamic__new" in loop.tools
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_reload_keeps_in_flight_mcp_snapshot_alive_until_turn_end(
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
    old_tool = loop.tools["mcp__fake__echo"]
    loop._turn_running = True
    try:
        assert await loop.reload_mcp_servers({}) == {}
        assert "mcp__fake__echo" not in loop.tools
        result = await old_tool.run(text="in flight")
        assert result.success is True
        assert result.output == "in flight"
        assert loop._retired_mcp_runtimes
    finally:
        loop._turn_running = False
        await loop._close_retired_mcp_runtimes()
        await loop.aclose()


@pytest.mark.asyncio
async def test_failed_mcp_reload_preserves_working_runtime(tmp_path: Path) -> None:
    working = MCPServerConfig(
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
        mcp_configs={"fake": working},
    )
    await loop.start_session()
    broken = MCPServerConfig(
        name="broken",
        command=str(tmp_path / "missing-server"),
        args=[],
        env={},
    )
    try:
        errors = await loop.reload_mcp_servers({"broken": broken})
        assert "broken" in errors
        assert "mcp__fake__echo" in loop.tools
        result = await loop.tools["mcp__fake__echo"].run(text="still works")
        assert result.success is True
        assert result.output == "still works"
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_loop_shutdown_serializes_with_in_progress_mcp_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_entered = asyncio.Event()
    release_start = asyncio.Event()
    instances = []

    class PausedRuntime:
        def __init__(self, configs, safety_guard, **kwargs) -> None:
            del safety_guard, kwargs
            self.configs = configs
            self.clients = {"paused": object()}
            self.errors = {}
            self.close_calls = 0
            instances.append(self)

        async def start(self) -> dict:
            start_entered.set()
            await release_start.wait()
            return {}

        def server_tools_snapshot(self) -> dict:
            return {}

        def activate_notifications(self) -> None:
            return None

        async def close(self) -> None:
            self.close_calls += 1
            self.clients.clear()

    monkeypatch.setattr("ash.mcp.runtime.MCPRuntime", PausedRuntime)
    loop = AshLoop(
        session_store=SessionStore(tmp_path / "sessions.db"),
        provider=IdleProvider(),
        safety_guard=SafetyGuard(tmp_path),
        ui=HeadlessUI(output_format="text", stream=io.StringIO()),
        project_root=tmp_path,
    )
    await loop.start_session()
    config = MCPServerConfig(name="paused", command="unused", args=[], env={})
    reload_task = asyncio.create_task(loop.reload_mcp_servers({"paused": config}))
    await asyncio.wait_for(start_entered.wait(), timeout=1)
    close_task = asyncio.create_task(loop.aclose())
    await asyncio.sleep(0)
    assert close_task.done() is False

    release_start.set()
    assert await reload_task == {}
    await asyncio.wait_for(close_task, timeout=1)

    assert len(instances) == 1
    assert instances[0].close_calls == 1
    assert loop._mcp_runtime is None
    assert loop._closed is True
    with pytest.raises(RuntimeError, match="after loop shutdown"):
        await loop.reload_mcp_servers({})


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


@pytest.mark.asyncio
async def test_http_recovers_expired_session_without_replaying_tool_call() -> None:
    trace: list[tuple[str, str | None, int | None]] = []
    initialize_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(405)
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        trace.append((method, session, payload.get("id")))
        if method == "initialize":
            initialize_count += 1
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": "must-not-replace-session-2"},
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": [{"type": "text", "text": "ok"}]},
            },
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
        with pytest.raises(MCPProtocolError, match="operation was not replayed"):
            await client.call_tool("echo", {})
        assert client._http_session_id == "session-2"
    finally:
        await client.disconnect()
        await http.aclose()

    calls = [item for item in trace if item[0] == "tools/call"]
    assert [(method, session) for method, session, _ in trace] == [
        ("initialize", None),
        ("notifications/initialized", "session-1"),
        ("tools/call", "session-1"),
        ("initialize", None),
        ("notifications/initialized", "session-2"),
    ]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_http_concurrent_expiry_uses_one_recovery_handshake() -> None:
    initialize_count = 0
    old_calls = 0
    both_old_calls = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count, old_calls
        if request.method == "DELETE":
            return httpx.Response(405)
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if session == "session-1":
            old_calls += 1
            if old_calls == 2:
                both_old_calls.set()
            await asyncio.wait_for(both_old_calls.wait(), timeout=1)
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
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
        with pytest.raises(MCPProtocolError, match="operation was not replayed"):
            await asyncio.gather(
                client.call_tool("first", {}), client.call_tool("second", {})
            )
        assert initialize_count == 2
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_http_session_404_recovers_without_replaying_tool_attempt() -> None:
    initialize_count = 0
    tool_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count, tool_attempts
        if request.method == "DELETE":
            return httpx.Response(405)
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            initialize_count += 1
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        tool_attempts += 1
        return httpx.Response(404)

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
        with pytest.raises(MCPProtocolError, match="operation was not replayed"):
            await client.call_tool("write", {})
        assert tool_attempts == 1
        assert initialize_count == 2
        assert client._http_session_id == "session-2"
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_http_rejects_invalid_initialize_session_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": b"not-visible-\xff"},
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                },
            },
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
    with pytest.raises(MCPProtocolError, match="visible ASCII"):
        await client.connect()
    await http.aclose()


@pytest.mark.asyncio
async def test_http_malformed_sse_never_replays_tool_call() -> None:
    tool_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tool_attempts
        if request.method == "DELETE":
            return httpx.Response(405)
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
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
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        tool_attempts += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="event: message\ndata: {not-json}\n\n",
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
        with pytest.raises(MCPProtocolError, match="SSE event contained invalid JSON"):
            await client.call_tool("write", {})
        assert tool_attempts == 1
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_concurrent_http_connect_initializes_once() -> None:
    initialize_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(405)
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            initialize_count += 1
            await asyncio.sleep(0)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                    },
                },
            )
        return httpx.Response(202)

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
    await asyncio.gather(client.connect(), client.connect())
    try:
        assert initialize_count == 1
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_paginated_list_restarts_after_session_recovery() -> None:
    initialize_count = 0
    cursors: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(405)
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        cursor = payload.get("params", {}).get("cursor")
        cursors.append((session or "", cursor))
        if session == "session-1" and cursor == "page-2":
            return httpx.Response(404)
        prefix = "old" if session == "session-1" else "new"
        result = (
            {
                "tools": [{"name": f"{prefix}-first"}],
                "nextCursor": "page-2",
            }
            if cursor is None
            else {"tools": [{"name": f"{prefix}-second"}]}
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
        assert [tool["name"] for tool in tools] == ["new-first", "new-second"]
        assert cursors == [
            ("session-1", None),
            ("session-1", "page-2"),
            ("session-2", None),
            ("session-2", "page-2"),
        ]
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_list_rejects_non_object_catalog_entries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
            }
        elif payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        else:
            result = {"tools": [{"name": "valid"}, "invalid"]}
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
        with pytest.raises(MCPProtocolError, match="non-object tools entry"):
            await client.list_tools()
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_failed_initialize_deletes_pending_server_session() -> None:
    deleted_sessions: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted_sessions.append(request.headers.get("Mcp-Session-Id"))
            return httpx.Response(204)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": "allocated-session"},
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"protocolVersion": "unsupported", "capabilities": {}},
            },
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
    with pytest.raises(MCPProtocolError, match="unsupported protocol version"):
        await client.connect()
    assert deleted_sessions == ["allocated-session"]
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", [None, False, []])
async def test_initialize_rejects_non_object_capabilities(capability: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": capability},
                },
            },
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
    with pytest.raises(
        MCPProtocolError, match="capabilities must contain objects: tools"
    ):
        await client.connect()
    await http.aclose()


@pytest.mark.asyncio
async def test_failed_replacement_initialize_deletes_allocated_session() -> None:
    initialize_count = 0
    deleted_sessions: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            deleted_sessions.append(request.headers.get("Mcp-Session-Id"))
            return httpx.Response(204)
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            capabilities = {"tools": None} if initialize_count == 2 else {"tools": {}}
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": capabilities,
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": []},
            },
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
        with pytest.raises(
            MCPProtocolError, match="capabilities must contain objects: tools"
        ):
            await client.call_tool("echo", {})
        assert deleted_sessions == ["session-2"]

        assert await client.call_tool("echo", {}) == {"content": []}
        assert initialize_count == 3
    finally:
        await client.disconnect()
        await http.aclose()
    assert deleted_sessions == ["session-2", "session-3"]


@pytest.mark.asyncio
async def test_cancelled_session_recovery_restores_readiness() -> None:
    initialize_count = 0
    replacement_initialize_started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(204)
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            if initialize_count == 2:
                replacement_initialize_started.set()
                await asyncio.Event().wait()
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": []},
            },
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
    task = asyncio.create_task(client.call_tool("echo", {}))
    try:
        await asyncio.wait_for(replacement_initialize_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert client._session_ready.is_set()

        assert await client.call_tool("echo", {}) == {"content": []}
        assert initialize_count == 3
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_factory", "message"),
    [
        (
            lambda request_id: httpx.Response(
                200,
                json=[{"jsonrpc": "2.0", "id": request_id, "result": {}}],
            ),
            "must contain one JSON-RPC object",
        ),
        (
            lambda request_id: httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": True, "result": {}},
            ),
            "id must be a string or integer",
        ),
        (
            lambda request_id: httpx.Response(
                200,
                text='{"jsonrpc":"2.0","id":2,"result":{}}',
                headers={"content-type": "text/plain"},
            ),
            "must use application/json or text/event-stream",
        ),
        (
            lambda request_id: httpx.Response(
                200,
                text='{"jsonrpc":"2.0","id":2,"result":{}}',
                headers={"content-type": "application/jsonp"},
            ),
            "must use application/json or text/event-stream",
        ),
        (
            lambda request_id: httpx.Response(
                200,
                text='data: {"jsonrpc":"2.0","id":2,"result":{}}\n\n',
                headers={"content-type": "text/event-stream-evil"},
            ),
            "must use application/json or text/event-stream",
        ),
    ],
)
async def test_http_rejects_invalid_jsonrpc_envelopes(
    response_factory, message: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
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
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        return response_factory(payload["id"])

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
        with pytest.raises(MCPProtocolError, match=message):
            await client.call_tool("echo", {})
    finally:
        await client.disconnect()
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
        _allow_session_recovery=False,
    )


@pytest.mark.asyncio
async def test_stdio_send_failure_cleans_pending_future() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._process = Mock(stdin=object())
    client._send_message = AsyncMock(side_effect=BrokenPipeError("closed"))

    with pytest.raises(BrokenPipeError):
        await client._request_stdio(9, {"jsonrpc": "2.0", "id": 9})

    assert client._pending == {}


@pytest.mark.asyncio
async def test_stdio_revalidates_tool_contract_inside_write_lock() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    stdin = Mock()
    client._process = Mock(stdin=stdin)
    client._session_generation = 1
    catalog_valid = True
    client.tool_contract_validator = lambda name, fingerprint, generation: catalog_valid
    await client._write_lock.acquire()
    task = asyncio.create_task(
        client.call_tool("echo", {}, expected_contract="fingerprint")
    )
    try:
        await asyncio.sleep(0)
        catalog_valid = False
        client._write_lock.release()
        with pytest.raises(MCPProtocolError, match="active verified server contract"):
            await asyncio.wait_for(task, timeout=1)
        stdin.write.assert_not_called()
    finally:
        if client._write_lock.locked():
            client._write_lock.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stdio_reader_fails_pending_request_on_framing_error() -> None:
    class BrokenReader:
        async def readline(self) -> bytes:
            raise ValueError("line exceeds configured limit")

    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._process = Mock(stdout=BrokenReader())
    future = asyncio.get_running_loop().create_future()
    client._pending[3] = future

    await client._read_stdio()

    with pytest.raises(MCPProtocolError, match="invalid stdio framing"):
        await future
    assert client._pending == {}


@pytest.mark.asyncio
async def test_cancelled_stdio_connect_cleans_process_and_reader_tasks() -> None:
    client = MCPClient(
        MCPServerConfig(
            name="blocked",
            command=sys.executable,
            args=["-u", "-c", "import time; time.sleep(60)"],
            env={},
        )
    )
    task = asyncio.create_task(client.connect())
    for _ in range(100):
        if client._process is not None and client._reader_task is not None:
            break
        await asyncio.sleep(0.01)
    process = client._process
    assert process is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert client._process is None
    assert client._reader_task is None
    assert client._stderr_task is None
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_cancelled_runtime_start_cleans_initialized_clients(
    tmp_path: Path,
) -> None:
    blocked_server = r"""
import json, sys, time
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    elif method == "tools/list":
        time.sleep(60)
"""
    runtime = MCPRuntime(
        {
            "blocked": MCPServerConfig(
                name="blocked",
                command=sys.executable,
                args=["-u", "-c", blocked_server],
                env={},
            )
        },
        SafetyGuard(tmp_path),
    )
    task = asyncio.create_task(runtime.start())
    client = None
    for _ in range(200):
        client = runtime.clients.get("blocked")
        if client is not None and client._initialized and client._pending:
            break
        await asyncio.sleep(0.01)
    assert client is not None and client._process is not None
    process = client._process

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)

    assert runtime.clients == {}
    assert client._process is None
    assert client._reader_task is None
    assert client._stderr_task is None
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_initialize_timeout_does_not_send_cancellation_notification() -> None:
    client = MCPClient(MCPServerConfig(name="fake", command="fake", args=[], env={}))
    client._request_stdio = AsyncMock(side_effect=asyncio.TimeoutError())
    client.notify = AsyncMock()

    with pytest.raises(asyncio.TimeoutError):
        await client.request("initialize")

    client.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_sessionless_http_timeout_sends_cancellation_without_reinitialize() -> (
    None
):
    trace: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload["method"]
        trace.append(method)
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
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "notifications/cancelled":
            assert request.headers.get("Mcp-Session-Id") is None
            return httpx.Response(202)
        raise httpx.ReadTimeout("timed out", request=request)

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
        with pytest.raises(httpx.ReadTimeout):
            await client.call_tool("slow", {})
        assert trace == [
            "initialize",
            "notifications/initialized",
            "tools/call",
            "notifications/cancelled",
        ]
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_server_cancellation_stops_incoming_request_without_response() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handle_request(method: str, params: dict) -> dict:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return {}

    client = MCPClient(
        MCPServerConfig(name="fake", command="fake", args=[], env={}),
        server_request_handler=handle_request,
    )
    client._send_message = AsyncMock()
    client._dispatch_incoming(
        {"jsonrpc": "2.0", "id": "server-1", "method": "custom", "params": {}}
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    client._dispatch_incoming(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "server-1", "reason": "no longer needed"},
        }
    )
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await asyncio.sleep(0)

    client._send_message.assert_not_awaited()


DYNAMIC_MCP_SERVER = r"""
import json, sys
state = "old"
list_count = 0
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
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
        if list_count == 1:
            state = "new"
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}), flush=True)
    elif method == "tools/call":
        name = message["params"]["name"]
        result = {"content": [{"type": "text", "text": name}]}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


@pytest.mark.asyncio
async def test_runtime_applies_startup_tool_list_change_atomically(
    tmp_path: Path,
) -> None:
    live_tools: dict[str, object] = {}
    replacements: list[tuple[set[str], set[str]]] = []

    async def replace(server: str, previous: dict, replacement: dict) -> None:
        assert server == "dynamic"
        replacements.append((set(previous), set(replacement)))
        for name in previous:
            live_tools.pop(name, None)
        live_tools.update(replacement)

    events: list[dict] = []
    runtime = MCPRuntime(
        {
            "dynamic": MCPServerConfig(
                name="dynamic",
                command=sys.executable,
                args=["-u", "-c", DYNAMIC_MCP_SERVER],
                env={},
            )
        },
        SafetyGuard(tmp_path),
        tool_change_handler=replace,
        event_sink=events.append,
    )
    live_tools.update(await runtime.start())
    try:
        await runtime.wait_for_refreshes()
        assert "mcp__dynamic__old" not in live_tools
        assert "mcp__dynamic__new" in live_tools
        result = await live_tools["mcp__dynamic__new"].run(text="hello")
        assert result.success is True
        assert result.output == "new"
        assert replacements == [({"mcp__dynamic__old"}, {"mcp__dynamic__new"})]
        assert any(
            event.get("type") == "mcp.catalog.changed"
            and event.get("added") == ["mcp__dynamic__new"]
            and event.get("removed") == ["mcp__dynamic__old"]
            for event in events
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_failed_dynamic_catalog_preserves_last_good_tools(
    tmp_path: Path,
) -> None:
    broken_server = DYNAMIC_MCP_SERVER.replace(
        '"required": ["text"]',
        '"required": ["text"] if state == "old" else "invalid"',
    )
    replacements: list[dict] = []

    async def replace(server: str, previous: dict, replacement: dict) -> None:
        replacements.append(replacement)

    runtime = MCPRuntime(
        {
            "dynamic": MCPServerConfig(
                name="dynamic",
                command=sys.executable,
                args=["-u", "-c", broken_server],
                env={},
            )
        },
        SafetyGuard(tmp_path),
        tool_change_handler=replace,
    )
    tools = await runtime.start()
    try:
        await runtime.wait_for_refreshes()
        assert "mcp__dynamic__old" in tools
        assert replacements == []
        assert "invalid tool catalog" in runtime.errors["dynamic:tools/refresh"]
        quarantined = await tools["mcp__dynamic__old"].run(text="blocked")
        assert quarantined.success is False
        assert "no longer matches the active verified" in quarantined.error
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_tool_list_change_quarantines_calls_until_refresh_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    class PausedRefreshClient:
        server_capabilities = {"tools": {"listChanged": True}}
        protocol_version = "2025-11-25"
        session_generation = 1

        def __init__(self, config, *, roots=()) -> None:
            self.notification_handler = None
            self.session_reinitialized_handler = None
            self.tool_contract_validator = None
            self.list_calls = 0
            self.tool_calls = 0

        async def connect(self) -> None:
            return None

        def supports_server_capability(self, name: str) -> bool:
            return name == "tools"

        async def list_tools(self) -> list[dict]:
            self.list_calls += 1
            if self.list_calls == 2:
                refresh_started.set()
                await release_refresh.wait()
            return [{"name": "echo", "inputSchema": {"type": "object"}}]

        async def call_tool(
            self,
            name: str,
            arguments: dict,
            *,
            expected_contract: str | None = None,
            as_task: bool = False,
        ) -> dict:
            del name, arguments, expected_contract
            del as_task
            self.tool_calls += 1
            return {"content": []}

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", PausedRefreshClient)
    runtime = MCPRuntime(
        {"paused": MCPServerConfig(name="paused", command="unused", args=[], env={})},
        SafetyGuard(tmp_path),
    )
    tools = await runtime.start()
    client = runtime.clients["paused"]
    try:
        await client.notification_handler("notifications/tools/list_changed", {})
        await asyncio.wait_for(refresh_started.wait(), timeout=1)

        quarantined = await tools["mcp__paused__echo"].run()
        assert quarantined.success is False
        assert "no longer matches the active verified" in quarantined.error
        assert client.tool_calls == 0

        release_refresh.set()
        await runtime.wait_for_refreshes()
        restored = await tools["mcp__paused__echo"].run()
        assert restored.success is True
        assert client.tool_calls == 1
    finally:
        release_refresh.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_resource_and_prompt_list_changes_emit_live_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CatalogClient:
        server_capabilities = {
            "resources": {"listChanged": True},
            "prompts": {"listChanged": True},
        }
        protocol_version = "2025-11-25"

        def __init__(self, config, *, roots=()) -> None:
            self.config = config
            self.notification_handler = None

        async def connect(self) -> None:
            return None

        def supports_server_capability(self, name: str) -> bool:
            return name in self.server_capabilities

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", CatalogClient)
    events: list[dict] = []
    runtime = MCPRuntime(
        {"catalog": MCPServerConfig(name="catalog", command="unused", args=[], env={})},
        SafetyGuard(tmp_path),
        event_sink=events.append,
    )
    await runtime.start()
    client = runtime.clients["catalog"]
    assert client.notification_handler is not None
    try:
        await client.notification_handler("notifications/resources/list_changed", {})
        await client.notification_handler("notifications/prompts/list_changed", {})
        assert [(event["capability"], event["revision"]) for event in events] == [
            ("resources", 1),
            ("prompts", 2),
        ]
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("contract_mode", ["unchanged", "renamed", "removed"])
async def test_runtime_reconciles_catalog_without_http_tool_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contract_mode: str,
) -> None:
    initialize_count = 0
    tool_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count, tool_attempts
        if request.method == "DELETE":
            return httpx.Response(405)
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            capabilities = (
                {}
                if contract_mode == "removed" and initialize_count == 2
                else {"tools": {}}
            )
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": capabilities,
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            assert not (contract_mode == "removed" and session == "session-2")
            name = (
                "replacement"
                if contract_mode == "renamed" and session == "session-2"
                else "echo"
            )
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": name,
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                ]
            }
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
            )
        tool_attempts += 1
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": [{"type": "text", "text": "ok"}]},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_client = MCPClient

    def client_factory(config, **kwargs):
        return real_client(config, http_client=http, **kwargs)

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", client_factory)
    live: dict[str, object] = {}

    async def replace(server: str, previous: dict, replacement: dict) -> None:
        for name in previous:
            live.pop(name, None)
        live.update(replacement)

    runtime = MCPRuntime(
        {
            "remote": MCPServerConfig(
                name="remote",
                command="",
                args=[],
                env={},
                transport="http",
                url="https://mcp.example.test/rpc",
            )
        },
        SafetyGuard(tmp_path),
        tool_change_handler=replace,
    )
    live.update(await runtime.start())
    old_tool = live["mcp__remote__echo"]
    try:
        result = await old_tool.run(text="hello")
        assert result.success is False
        assert "operation was not replayed" in result.error
        assert tool_attempts == 1
        if contract_mode != "unchanged":
            assert "mcp__remote__echo" not in live
            assert ("mcp__remote__replacement" in live) is (contract_mode == "renamed")
            stale_result = await old_tool.run(text="again")
            assert stale_result.success is False
            assert "no longer matches the active verified" in stale_result.error
            assert tool_attempts == 1
        else:
            assert "mcp__remote__echo" in live
        assert initialize_count == 2
    finally:
        await runtime.close()
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_supports_tools", [True, False])
async def test_runtime_start_recovers_session_expiry_during_initial_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_supports_tools: bool,
) -> None:
    initialize_count = 0
    list_sessions: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count
        if request.method == "DELETE":
            return httpx.Response(405)
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            capabilities = (
                {"tools": {}}
                if initialize_count == 1 or replacement_supports_tools
                else {}
            )
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": capabilities,
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        assert method == "tools/list"
        list_sessions.append(session)
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "tools": [{"name": "echo", "inputSchema": {"type": "object"}}]
                },
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_client = MCPClient

    def client_factory(config, **kwargs):
        return real_client(config, http_client=http, **kwargs)

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", client_factory)
    runtime = MCPRuntime(
        {
            "remote": MCPServerConfig(
                name="remote",
                command="",
                args=[],
                env={},
                transport="http",
                url="https://mcp.example.test/rpc",
            )
        },
        SafetyGuard(tmp_path),
    )
    try:
        tools = await asyncio.wait_for(runtime.start(), timeout=1)
        assert ("mcp__remote__echo" in tools) is replacement_supports_tools
        assert initialize_count == 2
        expected_sessions = (
            ["session-1", "session-2"] if replacement_supports_tools else ["session-1"]
        )
        assert list_sessions == expected_sessions
        assert runtime.errors == {}
    finally:
        await runtime.close()
        await http.aclose()


@pytest.mark.asyncio
async def test_runtime_blocks_concurrent_stale_call_until_catalog_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize_count = 0
    tool_attempts = 0
    replacement_list_started = asyncio.Event()
    release_replacement_list = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_count, tool_attempts
        if request.method == "DELETE":
            return httpx.Response(405)
        payload = json.loads(request.content)
        method = payload["method"]
        session = request.headers.get("Mcp-Session-Id")
        if method == "initialize":
            initialize_count += 1
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": f"session-{initialize_count}"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            if session == "session-2":
                replacement_list_started.set()
                await asyncio.wait_for(release_replacement_list.wait(), timeout=1)
            name = "replacement" if session == "session-2" else "echo"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "tools": [
                            {
                                "name": name,
                                "inputSchema": {"type": "object"},
                            }
                        ]
                    },
                },
            )
        tool_attempts += 1
        if session == "session-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    real_client = MCPClient

    def client_factory(config, **kwargs):
        return real_client(config, http_client=http, **kwargs)

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", client_factory)
    runtime = MCPRuntime(
        {
            "remote": MCPServerConfig(
                name="remote",
                command="",
                args=[],
                env={},
                transport="http",
                url="https://mcp.example.test/rpc",
            )
        },
        SafetyGuard(tmp_path),
    )
    tools = await runtime.start()
    old_tool = tools["mcp__remote__echo"]
    first = asyncio.create_task(old_tool.run())
    try:
        await asyncio.wait_for(replacement_list_started.wait(), timeout=1)
        second = asyncio.create_task(old_tool.run())
        await asyncio.sleep(0.05)
        assert second.done() is False
        assert tool_attempts == 1

        release_replacement_list.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result.success is False
        assert "operation was not replayed" in first_result.error
        assert second_result.success is False
        assert "no longer matches the active verified" in second_result.error
        assert tool_attempts == 1
    finally:
        release_replacement_list.set()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        await runtime.close()
        await http.aclose()


@pytest.mark.asyncio
async def test_output_schema_only_refresh_is_reported_as_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ChangingClient:
        server_capabilities = {"tools": {"listChanged": True}}
        protocol_version = "2025-11-25"

        def __init__(self, config, *, roots=()) -> None:
            self.notification_handler = None
            self.session_reinitialized_handler = None
            self.calls = 0

        async def connect(self) -> None:
            return None

        def supports_server_capability(self, name: str) -> bool:
            return name == "tools"

        async def list_tools(self) -> list[dict]:
            self.calls += 1
            return [
                {
                    "name": "same",
                    "description": "same",
                    "inputSchema": {"type": "object"},
                    "outputSchema": {
                        "type": "object",
                        "properties": {"version": {"const": self.calls}},
                    },
                }
            ]

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr("ash.mcp.runtime.MCPClient", ChangingClient)
    events: list[dict] = []
    runtime = MCPRuntime(
        {
            "changing": MCPServerConfig(
                name="changing", command="unused", args=[], env={}
            )
        },
        SafetyGuard(tmp_path),
        event_sink=events.append,
    )
    await runtime.start()
    client = runtime.clients["changing"]
    try:
        await client.notification_handler("notifications/tools/list_changed", {})
        await runtime.wait_for_refreshes()
        tool_events = [
            event
            for event in events
            if event.get("type") == "mcp.catalog.changed"
            and event.get("capability") == "tools"
        ]
        assert tool_events[-1]["changed"] == ["mcp__changing__same"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_refresh_storm_is_bounded_and_reported(tmp_path: Path) -> None:
    storm_server = DYNAMIC_MCP_SERVER.replace("if list_count == 1:", "if True:")
    events: list[dict] = []
    runtime = MCPRuntime(
        {
            "storm": MCPServerConfig(
                name="storm",
                command=sys.executable,
                args=["-u", "-c", storm_server],
                env={},
            )
        },
        SafetyGuard(tmp_path),
        event_sink=events.append,
    )
    await runtime.start()
    try:
        await asyncio.wait_for(runtime.wait_for_refreshes(), timeout=2)
        assert "refresh storm" in runtime.errors["storm:tools/refresh"]
        assert any(
            event.get("type") == "mcp.catalog.refresh_suppressed" for event in events
        )
    finally:
        await runtime.close()
