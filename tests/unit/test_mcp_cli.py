from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ash.cli import main
from ash.commands.mcp import probe_mcp_server
from ash.mcp.client import MCPProtocolError
from ash.mcp.oauth import (
    MCPOAuthTokenStore,
    OAuthBundle,
    OAuthClient,
    OAuthDiscovery,
    OAuthTokens,
)
from ash.mcp.server import MCPServerConfig, load_mcp_servers


def _oauth_bundle(resource: str) -> OAuthBundle:
    return OAuthBundle(
        resource,
        OAuthDiscovery(
            resource,
            ("files:read",),
            "https://auth.example.test",
            "https://auth.example.test/authorize",
            "https://auth.example.test/token",
        ),
        OAuthClient("client-id"),
        OAuthTokens("access-token", "refresh-token"),
    )


def test_mcp_cli_add_persists_env_headers_and_json_hides_values(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)

    status = main(
        [
            "mcp",
            "add",
            "local",
            "--env",
            "TOKEN=${MCP_TOKEN}",
            "--header",
            "Authorization=Bearer ${MCP_TOKEN}",
            "--json",
            "--",
            "python",
            "-m",
            "server",
        ]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["servers"][0]["name"] == "local"
    assert payload["servers"][0]["env_keys"] == ["TOKEN"]
    assert payload["servers"][0]["header_keys"] == ["Authorization"]
    assert "${MCP_TOKEN}" not in json.dumps(payload)
    loaded = load_mcp_servers(tmp_path / ".mcp.json")
    assert loaded["local"].env == {"TOKEN": "${MCP_TOKEN}"}
    assert loaded["local"].headers == {"Authorization": "Bearer ${MCP_TOKEN}"}
    assert loaded["local"].command == "python"
    assert loaded["local"].args == ["-m", "server"]


def test_mcp_cli_list_json_reports_config_without_secret_values(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert (
        main(
            [
                "mcp",
                "add",
                "remote",
                "--transport",
                "http",
                "--url",
                "https://example.test/mcp",
                "--header",
                "X-Api-Key=secret",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["mcp", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["servers"][0]["transport"] == "http"
    assert payload["servers"][0]["url"] == "https://example.test/mcp"
    assert payload["servers"][0]["header_keys"] == ["X-Api-Key"]
    assert "secret" not in json.dumps(payload)


def test_mcp_cli_rejects_invalid_env_option(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)

    status = main(["mcp", "add", "bad", "--env", "TOKEN", "--", "server"])

    assert status == 2
    assert "--env must use KEY=VALUE syntax" in capsys.readouterr().err


def test_mcp_cli_add_rejects_server_name_its_loader_would_reject(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["mcp", "add", "bad name", "--", "server"]) == 2

    captured = capsys.readouterr()
    assert "invalid MCP server name" in captured.err
    assert not (tmp_path / ".mcp.json").exists()


def test_mcp_cli_classifies_malformed_config_without_traceback(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text("{", encoding="utf-8")

    assert main(["mcp", "list", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["error"]["category"] == "config"
    assert "Traceback" not in captured.out


def test_mcp_cli_config_error_escapes_terminal_controls(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "demo": {
                    "command": "server",
                    "args": [],
                    "env": {},
                    "transport": "stdio\nFORGED_ERROR\x1b[31m",
                }
            }
        ),
        encoding="utf-8",
    )

    assert main(["mcp", "list"]) == 2
    captured = capsys.readouterr()

    assert "stdio\\x0aFORGED_ERROR\\x1b[31m" in captured.err
    assert "\x1b" not in captured.err
    assert captured.err.count("\n") == 2


def test_mcp_cli_rejects_oauth_options_without_oauth_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)

    status = main(
        [
            "mcp",
            "add",
            "remote",
            "--transport",
            "http",
            "--url",
            "https://mcp.example.test/rpc",
            "--oauth-scope",
            "files:read",
        ]
    )

    assert status == 2
    assert "OAuth options require auth mode oauth" in capsys.readouterr().err
    assert not (tmp_path / ".mcp.json").exists()


def test_mcp_cli_adds_oauth_without_persisting_client_secret(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MCP_CLIENT_SECRET", "resolved-secret")

    status = main(
        [
            "mcp",
            "add",
            "protected",
            "--transport",
            "http",
            "--url",
            "https://mcp.example.test/rpc",
            "--auth",
            "oauth",
            "--oauth-client-id",
            "registered-client",
            "--oauth-client-secret-env",
            "MCP_CLIENT_SECRET",
            "--oauth-scope",
            "files:read files:write",
            "--oauth-redirect-port",
            "43123",
            "--json",
        ]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["servers"][0]["auth"] == "oauth"
    assert payload["servers"][0]["oauth_client_configured"] is True
    assert "resolved-secret" not in json.dumps(payload)
    persisted = (tmp_path / ".mcp.json").read_text(encoding="utf-8")
    assert "resolved-secret" not in persisted
    loaded = load_mcp_servers(tmp_path / ".mcp.json")["protected"]
    assert loaded.oauth == {
        "client_id": "registered-client",
        "client_secret": "${MCP_CLIENT_SECRET}",
        "redirect_port": 43123,
        "scope": "files:read files:write",
    }
    assert loaded.resolved_oauth["client_secret"] == "resolved-secret"


def test_mcp_cli_login_is_explicit_and_logout_removes_credentials(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert (
        main(
            [
                "mcp",
                "add",
                "protected",
                "--transport",
                "http",
                "--url",
                "https://mcp.example.test/rpc",
                "--auth",
                "oauth",
            ]
        )
        == 0
    )
    capsys.readouterr()

    authorize = AsyncMock(return_value=None)
    monkeypatch.setattr("ash.mcp.oauth.authorize_mcp_server", authorize)

    assert (
        main(
            [
                "mcp",
                "login",
                "protected",
                "--no-browser",
                "--scope",
                "files:read files:write",
            ]
        )
        == 0
    )
    assert "Authorized MCP server protected." in capsys.readouterr().out
    assert authorize.await_count == 1
    assert authorize.await_args.kwargs["manual_paste"] is True
    assert authorize.await_args.kwargs["requested_scope"] == "files:read files:write"
    assert authorize.await_args.kwargs["open_browser"]("unused") is False

    store = authorize.await_args.kwargs["store"]
    store.directory.mkdir(parents=True, exist_ok=True)
    store.path.write_text("credential", encoding="utf-8")
    assert main(["mcp", "logout", "protected"]) == 0
    assert "Removed OAuth credentials" in capsys.readouterr().out
    assert not store.path.exists()


def test_mcp_cli_login_rejects_non_finite_timeout(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert (
        main(
            [
                "mcp",
                "add",
                "protected",
                "--transport",
                "http",
                "--url",
                "https://mcp.example.test/rpc",
                "--auth",
                "oauth",
            ]
        )
        == 0
    )
    capsys.readouterr()
    authorize = AsyncMock(return_value=None)
    monkeypatch.setattr("ash.mcp.oauth.authorize_mcp_server", authorize)

    assert main(["mcp", "login", "protected", "--timeout", "nan"]) == 2
    assert "greater than 0 and at most 1800" in capsys.readouterr().err
    assert authorize.await_count == 0


def test_mcp_cli_status_reports_safe_oauth_credential_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert (
        main(
            [
                "mcp",
                "add",
                "protected",
                "--transport",
                "http",
                "--url",
                "https://mcp.example.test/rpc",
                "--auth",
                "oauth",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["mcp", "status"]) == 0
    output = capsys.readouterr().out
    assert "credentials=missing" in output
    assert "access-token" not in output

    store = MCPOAuthTokenStore("protected")
    store.save(_oauth_bundle("https://mcp.example.test/rpc"))
    assert main(["mcp", "status"]) == 0
    output = capsys.readouterr().out
    assert "credentials=usable" in output
    assert "access-token" not in output

    monkeypatch.setattr(
        "ash.safety.private_store.secure_private_store_available",
        lambda: False,
    )
    assert main(["mcp", "status"]) == 0
    output = capsys.readouterr().out
    assert "credentials=unavailable" in output
    assert "access-token" not in output


def test_mcp_cli_status_escapes_terminal_controls_from_config(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "demo": {
                    "command": "python\nFORGED_STATUS\x1b[31m",
                    "args": [],
                    "env": {},
                }
            }
        ),
        encoding="utf-8",
    )

    assert main(["mcp", "status"]) == 0
    output = capsys.readouterr().out

    assert output == "demo [stdio]: python\\x0aFORGED_STATUS\\x1b[31m\n"
    assert "\x1b" not in output


def test_mcp_cli_probe_live_stdio_server_reports_safe_capabilities(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    secret = "probe-secret-value"
    server = r'''
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    params = message.get("params", {})
    if method == "server/discover":
        caps = params.get("_meta", {}).get("io.modelcontextprotocol/clientCapabilities", {})
        assert "roots" in caps
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"listChanged": True, "subscribe": True},
                "prompts": {},
            },
            "_meta": {
                "io.modelcontextprotocol/serverInfo": {
                    "name": "probe-server",
                    "version": "1.2.3",
                }
            },
        }
    elif method == "tools/list":
        result = {
            "resultType": "complete",
            "tools": [
                {"name": "one", "inputSchema": {"type": "object"}},
                {"name": "two", "inputSchema": {"type": "object"}},
            ],
        }
    else:
        result = {"resultType": "complete"}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
'''
    assert (
        main(
            [
                "mcp",
                "add",
                "probe",
                "--env",
                f"PROBE_SECRET={secret}",
                "--",
                sys.executable,
                "-u",
                "-c",
                server,
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["mcp", "probe", "probe", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "name": "probe",
        "transport": "stdio",
        "protocol_version": "2026-07-28",
        "server": {"name": "probe-server", "version": "1.2.3"},
        "capabilities": {
            "tools": True,
            "resources": True,
            "prompts": True,
            "tasks": False,
            "resource_subscribe": True,
            "list_changed": {
                "tools": True,
                "resources": True,
                "prompts": False,
            },
        },
        "tools": 2,
    }
    assert secret not in json.dumps(payload)


def test_mcp_cli_probe_human_output_is_concise(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    server = r'''
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"tools": {}},
            "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "human", "version": "1"}},
        }
    elif method == "tools/list":
        result = {"resultType": "complete", "tools": []}
    else:
        result = {"resultType": "complete"}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
'''
    assert main(["mcp", "add", "human", "--", sys.executable, "-u", "-c", server]) == 0
    capsys.readouterr()

    assert main(["mcp", "probe", "human"]) == 0
    output = capsys.readouterr().out
    assert "human: connected" in output
    assert "protocol=2026-07-28" in output
    assert "tools=0" in output
    assert "resources=no" in output
    assert "prompts=no" in output


def test_mcp_cli_probe_rejects_missing_server_and_invalid_timeout(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["mcp", "probe", "missing"]) == 2
    assert "not configured" in capsys.readouterr().err

    assert main(["mcp", "probe", "missing", "--timeout", "nan"]) == 2
    assert "timeout" in capsys.readouterr().err.casefold()


def test_mcp_cli_probe_connection_failure_is_redacted(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    secret = "super-secret-probe-token"
    assert (
        main(
            [
                "mcp",
                "add",
                "broken",
                "--env",
                f"TOKEN={secret}",
                "--",
                str(tmp_path / "missing-mcp-server"),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["mcp", "probe", "broken", "--timeout", "0.1"]) == 1
    captured = capsys.readouterr()
    assert "probe failed" in captured.err.casefold()
    assert secret not in captured.err


def test_mcp_cli_probe_escapes_terminal_controls_from_server_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    server = r'''
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"tools": {}},
            "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "probe", "version": "1"}},
        }
        reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
    elif method == "tools/list":
        reply = {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {
                "code": -32001,
                "message": "boom\nFORGED_PROBE\u001b[31m",
            },
        }
    else:
        reply = {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"resultType": "complete"},
        }
    print(json.dumps(reply), flush=True)
'''
    assert main(["mcp", "add", "probe", "--", sys.executable, "-u", "-c", server]) == 0
    capsys.readouterr()

    assert main(["mcp", "probe", "probe"]) == 1
    captured = capsys.readouterr()

    assert "boom\\x0aFORGED_PROBE\\x1b[31m" in captured.err
    assert "\x1b" not in captured.err
    assert captured.err.count("\n") == 1


@pytest.mark.asyncio
async def test_mcp_probe_cancellation_disconnects_client(
    tmp_path: Path,
    monkeypatch,
) -> None:
    started = asyncio.Event()
    disconnected = asyncio.Event()

    class BlockingClient:
        protocol_version = "2026-07-28"
        server_info = {"name": "blocked", "version": "1"}
        server_capabilities = {"tools": {}}

        def __init__(self, config, *, timeout, roots) -> None:
            assert roots == (tmp_path,)

        def supports_server_capability(self, name: str) -> bool:
            return name == "tools"

        async def connect(self) -> None:
            return None

        async def list_tools(self):
            started.set()
            await asyncio.Event().wait()

        async def disconnect(self) -> None:
            disconnected.set()

    monkeypatch.setattr("ash.commands.mcp.MCPClient", BlockingClient)
    config = MCPServerConfig(name="blocked", command="server", args=[], env={})
    task = asyncio.create_task(
        probe_mcp_server(config, workspace=tmp_path, timeout=1.0)
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert disconnected.is_set()


@pytest.mark.asyncio
async def test_mcp_probe_cleanup_failure_does_not_mask_primary_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FailingClient:
        protocol_version = "2026-07-28"
        server_info = {"name": "failing", "version": "1"}
        server_capabilities = {"tools": {}}

        def __init__(self, config, *, timeout, roots) -> None:
            return None

        def supports_server_capability(self, name: str) -> bool:
            return name == "tools"

        async def connect(self) -> None:
            return None

        async def list_tools(self):
            raise MCPProtocolError("primary probe failure")

        async def disconnect(self) -> None:
            raise RuntimeError("cleanup failure")

    monkeypatch.setattr("ash.commands.mcp.MCPClient", FailingClient)
    config = MCPServerConfig(name="failing", command="server", args=[], env={})

    with pytest.raises(MCPProtocolError, match="primary probe failure") as failure:
        await probe_mcp_server(config, workspace=tmp_path, timeout=1.0)
    assert any("cleanup failure" in note for note in failure.value.__notes__)
