"""MCP server discovery and management."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
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
    headers: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.transport not in {"stdio", "http", "sse", "websocket"}:
            raise ValueError(f"Unknown MCP transport: {self.transport}")

    @property
    def resolved_command(self) -> str:
        return expand_env_vars(self.command)

    @property
    def resolved_args(self) -> list[str]:
        return [expand_env_vars(str(arg)) for arg in self.args]

    @property
    def resolved_env(self) -> dict[str, str]:
        return {key: expand_env_vars(value) for key, value in self.env.items()}

    @property
    def resolved_url(self) -> str:
        return expand_env_vars(self.url)

    @property
    def resolved_headers(self) -> dict[str, str]:
        return {
            key: expand_env_vars(value)
            for key, value in (self.headers or {}).items()
        }

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> MCPServerConfig:
        return cls(
            name=name,
            command=data.get("command", ""),
            args=data.get("args", []),
            env=data.get("env", {}),
            transport=data.get("transport", "stdio"),
            url=data.get("url", ""),
            headers=data.get("headers", {}),
        )


@dataclass
class MCPServerInstance:
    """A running MCP server subprocess."""

    name: str
    config: MCPServerConfig
    process: subprocess.Popen[bytes] | None
    transport: str = "stdio"


class MCPServerManager:
    """Manages the lifecycle of MCP server subprocesses."""

    def __init__(self) -> None:
        self._servers: dict[str, MCPServerInstance] = {}

    def start_server(self, config: MCPServerConfig) -> MCPServerInstance:
        """Start an MCP server as a subprocess."""
        if config.transport in ("sse", "http", "websocket"):
            # Network transports: server is already running externally.
            # Just store the config — no subprocess to manage.
            instance = MCPServerInstance(
                name=config.name,
                config=config,
                process=None,
                transport=config.transport,
            )
            self._servers[config.name] = instance
            return instance

        if config.transport != "stdio":
            raise ValueError(f"Unknown MCP transport: {config.transport}")

        env = {**os.environ, **config.resolved_env}

        # Spawn subprocess. stderr=DEVNULL avoids deadlock when the subprocess
        # writes to stderr — we never read it, so PIPE would fill and block.
        proc = subprocess.Popen(
            [config.resolved_command] + config.resolved_args,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        instance = MCPServerInstance(
            name=config.name,
            config=config,
            process=proc,
            transport=config.transport,
        )
        try:
            self._servers[config.name] = instance
        except Exception:
            # Defensive: if dict insertion fails, reap the subprocess immediately.
            proc.terminate()
            proc.wait(timeout=5)
            raise

        return instance

    def stop_server(self, name: str) -> None:
        """Stop a running MCP server."""
        instance = self._servers.get(name)
        if instance is None:
            return
        proc = instance.process
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()  # Reap the zombie — wait() after kill() is mandatory
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
        if isinstance(data, dict) and ("command" in data or "url" in data):
            servers[name] = MCPServerConfig.from_dict(name, data)
    return servers


def save_mcp_servers(
    servers: dict[str, MCPServerConfig],
    config_path: Path | None = None,
) -> Path:
    """Atomically persist MCP server definitions."""

    path = config_path or Path(".mcp.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        name: {
            "command": config.command,
            "args": config.args,
            "env": config.env,
            "transport": config.transport,
            "url": config.url,
            "headers": config.headers or {},
        }
        for name, config in sorted(servers.items())
    }
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def expand_env_vars(value: str) -> str:
    """Expand ${VAR} and $VAR in strings."""
    return os.path.expandvars(value)
