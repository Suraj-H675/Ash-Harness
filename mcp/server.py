"""MCP server discovery and management."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from safety.environment import build_scrubbed_environment

MAX_MCP_CONFIG_BYTES = 256 * 1024
MCP_SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
    cwd: str = ""

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
            key: expand_env_vars(value) for key, value in (self.headers or {}).items()
        }

    @property
    def resolved_cwd(self) -> str | None:
        return expand_env_vars(self.cwd) if self.cwd else None

    @classmethod
    def from_dict(
        cls,
        name: str,
        data: dict[str, Any],
        *,
        cwd: str = "",
        environment: dict[str, str] | None = None,
    ) -> MCPServerConfig:
        _validate_server_data(name, data)
        env = dict(data.get("env", {}))
        env.update(environment or {})
        return cls(
            name=name,
            command=data.get("command", ""),
            args=data.get("args", []),
            env=env,
            transport=data.get("transport", "stdio"),
            url=data.get("url", ""),
            headers=data.get("headers", {}),
            cwd=cwd or str(data.get("cwd", "")),
        )


@dataclass(frozen=True)
class MCPConfigSource:
    path: Path
    namespace: str = ""
    cwd: Path | None = None
    environment: tuple[tuple[str, str], ...] = ()


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

        env = build_scrubbed_environment(overrides=config.resolved_env)

        # Spawn subprocess. stderr=DEVNULL avoids deadlock when the subprocess
        # writes to stderr — we never read it, so PIPE would fill and block.
        proc = subprocess.Popen(
            [config.resolved_command] + config.resolved_args,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=config.resolved_cwd,
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


def load_mcp_servers(
    config_path: Path | None = None,
    *,
    namespace: str = "",
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, MCPServerConfig]:
    """Load MCP server definitions from .mcp.json."""
    if config_path is None:
        config_path = Path(".mcp.json")
    if not config_path.exists():
        return {}
    if config_path.stat().st_size > MAX_MCP_CONFIG_BYTES:
        raise ValueError(f"MCP config exceeds 256 KiB: {config_path}")

    with config_path.open() as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"MCP config must be an object: {config_path}")
    if "mcpServers" in raw:
        wrapped = raw["mcpServers"]
        if not isinstance(wrapped, dict):
            raise ValueError(f"mcpServers must be an object: {config_path}")
        raw = wrapped

    servers = {}
    for name, data in raw.items():
        if not isinstance(name, str) or not MCP_SERVER_NAME.fullmatch(name):
            raise ValueError(f"invalid MCP server name: {name!r}")
        if not isinstance(data, dict):
            raise ValueError(f"MCP server {name!r} must be an object")
        resolved_name = f"{namespace}__{name}" if namespace else name
        servers[resolved_name] = MCPServerConfig.from_dict(
            resolved_name,
            data,
            cwd=str(cwd) if cwd is not None else "",
            environment=environment,
        )
    return servers


def load_mcp_server_sources(
    sources: list[MCPConfigSource],
) -> dict[str, MCPServerConfig]:
    merged: dict[str, MCPServerConfig] = {}
    for source in sources:
        loaded = load_mcp_servers(
            source.path,
            namespace=source.namespace,
            cwd=source.cwd,
            environment=dict(source.environment),
        )
        duplicates = sorted(merged.keys() & loaded.keys())
        if duplicates:
            raise ValueError("duplicate MCP server name(s): " + ", ".join(duplicates))
        merged.update(loaded)
    return merged


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
            **({"cwd": config.cwd} if config.cwd else {}),
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


def _validate_server_data(name: str, data: dict[str, Any]) -> None:
    transport = data.get("transport", "stdio")
    if transport not in {"stdio", "http", "sse", "websocket"}:
        raise ValueError(f"Unknown MCP transport: {transport}")
    command = data.get("command", "")
    url = data.get("url", "")
    args = data.get("args", [])
    env = data.get("env", {})
    headers = data.get("headers", {})
    cwd = data.get("cwd", "")
    if not isinstance(command, str) or not isinstance(url, str):
        raise ValueError(f"MCP server {name!r} command and url must be strings")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError(f"MCP server {name!r} args must be a list of strings")
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        raise ValueError(f"MCP server {name!r} env must contain string values")
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in headers.items()
    ):
        raise ValueError(f"MCP server {name!r} headers must contain string values")
    if not isinstance(cwd, str):
        raise ValueError(f"MCP server {name!r} cwd must be a string")
    if transport == "stdio" and not command:
        raise ValueError(f"stdio MCP server {name!r} requires a command")
    if transport != "stdio" and not url:
        raise ValueError(f"{transport} MCP server {name!r} requires a url")
