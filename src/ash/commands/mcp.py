"""Top-level MCP configuration rendering and parsing helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ash.mcp.client import MCPClient
from ash.mcp.diagnostics import safe_mcp_diagnostic
from ash.mcp.server import MCPServerConfig
from ash.mcp.oauth import MCPOAuthTokenStore
from ash.ui.safe_text import terminal_safe_text


def parse_key_value_options(values: list[str] | None, *, label: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"{label} must use KEY=VALUE syntax")
        key, item = value.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"{label} key must not be empty")
        parsed[key] = item
    return parsed


def mcp_servers_payload(servers: dict[str, MCPServerConfig]) -> dict:
    return {
        "servers": [
            {
                "name": name,
                "transport": config.transport,
                "command": config.command,
                "args": list(config.args),
                "url": config.url,
                "env_keys": sorted(config.env),
                "header_keys": sorted(config.headers or {}),
                "auth": config.auth,
                "oauth_client_configured": bool(
                    (config.oauth or {}).get("client_id")
                    or (config.oauth or {}).get("client_metadata_url")
                ),
            }
            for name, config in sorted(servers.items())
        ]
    }


def _oauth_credential_state(config: MCPServerConfig) -> str | None:
    if config.auth != "oauth":
        return None
    try:
        store = MCPOAuthTokenStore(config.name)
        issuer = str((config.oauth or {}).get("issuer", ""))
        return store.credential_state(config.resolved_url, issuer=issuer)
    except ValueError as exc:
        return f"invalid configuration: {exc}"


def render_mcp_servers(
    servers: dict[str, MCPServerConfig],
    *,
    json_output: bool = False,
) -> str:
    payload = mcp_servers_payload(servers)
    if json_output:
        return json.dumps(payload, sort_keys=True)
    if not servers:
        return "No MCP servers configured."
    oauth_states = {
        name: _oauth_credential_state(config)
        for name, config in sorted(servers.items())
    }
    lines: list[str] = []
    for item in payload["servers"]:
        target = (
            item["url"]
            if item["transport"] != "stdio"
            else " ".join([item["command"], *item["args"]]).strip()
        )
        extras = []
        if item["env_keys"]:
            extras.append("env=" + ",".join(item["env_keys"]))
        if item["header_keys"]:
            extras.append("headers=" + ",".join(item["header_keys"]))
        state = oauth_states.get(item["name"])
        if item["auth"] == "oauth" and state is not None:
            extras.append(f"credentials={state}")
        auth = " oauth" if item["auth"] == "oauth" else ""
        suffix = f" ({'; '.join(extras)})" if extras else ""
        lines.append(
            terminal_safe_text(
                f"{item['name']} [{item['transport']}{auth}]: {target}{suffix}",
                single_line=True,
            )
        )
    return "\n".join(lines)


async def probe_mcp_server(
    config: MCPServerConfig,
    *,
    workspace: Path,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Connect to one configured MCP server and return bounded capability metadata."""

    client = MCPClient(config, timeout=timeout, roots=(workspace,))
    try:
        await client.connect()
        tools = (
            await client.list_tools()
            if client.supports_server_capability("tools")
            else []
        )
        capabilities = client.server_capabilities
        resources = capabilities.get("resources")
        prompts = capabilities.get("prompts")
        tool_capability = capabilities.get("tools")
        server_info = client.server_info if isinstance(client.server_info, dict) else {}
        payload = {
            "name": config.name,
            "transport": config.transport,
            "protocol_version": safe_mcp_diagnostic(client.protocol_version),
            "server": {
                "name": safe_mcp_diagnostic(server_info.get("name", "unknown")),
                "version": safe_mcp_diagnostic(server_info.get("version", "unknown")),
            },
            "capabilities": {
                "tools": isinstance(tool_capability, dict),
                "resources": isinstance(resources, dict),
                "prompts": isinstance(prompts, dict),
                "tasks": client.supports_server_capability("tasks"),
                "resource_subscribe": (
                    isinstance(resources, dict) and resources.get("subscribe") is True
                ),
                "list_changed": {
                    "tools": (
                        isinstance(tool_capability, dict)
                        and tool_capability.get("listChanged") is True
                    ),
                    "resources": (
                        isinstance(resources, dict)
                        and resources.get("listChanged") is True
                    ),
                    "prompts": (
                        isinstance(prompts, dict)
                        and prompts.get("listChanged") is True
                    ),
                },
            },
            "tools": len(tools),
        }
    except BaseException as primary:
        try:
            await client.disconnect()
        except BaseException as cleanup_error:
            primary.add_note(
                "MCP probe disconnect cleanup failed: "
                + safe_mcp_diagnostic(cleanup_error)
            )
        raise
    await client.disconnect()
    return payload


def render_mcp_probe(payload: dict[str, Any], *, json_output: bool = False) -> str:
    if json_output:
        return json.dumps(payload, sort_keys=True)
    capabilities = payload["capabilities"]
    server = payload["server"]
    server_identity = terminal_safe_text(
        f"{server['name']} {server['version']}", single_line=True
    )
    return (
        f"{payload['name']}: connected "
        f"protocol={payload['protocol_version']} server={server_identity} "
        f"tools={payload['tools']} "
        f"resources={'yes' if capabilities['resources'] else 'no'} "
        f"prompts={'yes' if capabilities['prompts'] else 'no'} "
        f"tasks={'yes' if capabilities['tasks'] else 'no'}"
    )
