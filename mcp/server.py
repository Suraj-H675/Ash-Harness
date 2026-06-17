"""MCP server discovery and management."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


@dataclass
class MCPServerInstance:
    """A running MCP server subprocess."""

    name: str
    config: MCPServerConfig
    process: subprocess.Popen[bytes]
    transport: str = "stdio"


class MCPServerManager:
    """Manages the lifecycle of MCP server subprocesses."""

    def __init__(self) -> None:
        self._servers: dict[str, MCPServerInstance] = {}

    def start_server(self, config: MCPServerConfig) -> MCPServerInstance:
        """Start an MCP server as a subprocess."""
        env = {**os.environ, **config.env}

        if config.transport == "stdio":
            proc = subprocess.Popen(
                [config.command] + config.args,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        elif config.transport in ("sse", "http", "websocket"):
            # For network transports, the server is already running;
            # just store the config for the client to connect to.
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(86400)"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        else:
            raise ValueError(f"Unknown MCP transport: {config.transport}")

        instance = MCPServerInstance(
            name=config.name,
            config=config,
            process=proc,
            transport=config.transport,
        )
        self._servers[config.name] = instance
        return instance

    def stop_server(self, name: str) -> None:
        """Stop a running MCP server."""
        instance = self._servers.get(name)
        if instance is None:
            return
        if instance.process is not None and instance.process.poll() is None:
            instance.process.terminate()
            try:
                instance.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                instance.process.kill()
        del self._servers[name]

    def stop_all(self) -> None:
        """Stop all running MCP servers."""
        for name in list(self._servers.keys()):
            self.stop_server(name)

    def get_server(self, name: str) -> MCPServerInstance | None:
        return self._servers.get(name)

    def list_servers(self) -> list[MCPServerInstance]:
        return list(self._servers.values())


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
