"""MCP server discovery and management."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MCPServerConfig:
    """Configuration for a single MCP server."""

    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    transport: str = "stdio"  # "stdio" | "sse" | "http" | "websocket"
    url: str = ""  # for SSE/HTTP/WebSocket

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", expand_env_vars(self.command))
        object.__setattr__(self, "args", [expand_env_vars(str(a)) for a in self.args])
        object.__setattr__(
            self, "env", {k: expand_env_vars(v) for k, v in self.env.items()}
        )
        object.__setattr__(self, "transport", self.transport)
        object.__setattr__(self, "url", expand_env_vars(self.url))

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> MCPServerConfig:
        return cls(
            name=name,
            command=data["command"],
            args=data.get("args", []),
            env=data.get("env", {}),
            transport=data.get("transport", "stdio"),
            url=data.get("url", ""),
        )


def load_mcp_servers(config_path: Path | None = None) -> dict[str, MCPServerConfig]:
    """Load MCP server definitions from .mcp.json."""
    if config_path is None:
        config_path = Path(".mcp.json")
    if not config_path.exists():
        return {}

    with config_path.open() as f:
        raw = json.load(f)

    servers = {}
    for name, data in raw.items():
        if isinstance(data, dict) and "command" in data:
            servers[name] = MCPServerConfig.from_dict(name, data)
    return servers


def expand_env_vars(value: str) -> str:
    """Expand ${VAR} and $VAR in strings."""
    return os.path.expandvars(value)
