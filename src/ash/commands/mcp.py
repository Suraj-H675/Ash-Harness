"""Top-level MCP configuration rendering and parsing helpers."""

from __future__ import annotations

import json

from ash.mcp.server import MCPServerConfig
from ash.mcp.oauth import MCPOAuthTokenStore


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
                "oauth_client_configured": bool((config.oauth or {}).get("client_id")),
            }
            for name, config in sorted(servers.items())
        ]
    }


def _oauth_credential_state(config: MCPServerConfig) -> str | None:
    if config.auth != "oauth":
        return None
    try:
        store = MCPOAuthTokenStore(config.name)
        return store.credential_state(config.resolved_url)
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
        lines.append(f"{item['name']} [{item['transport']}{auth}]: {target}{suffix}")
    return "\n".join(lines)
