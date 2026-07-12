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
MCP_OAUTH_SCOPE = re.compile(
    r"[\x21\x23-\x5B\x5D-\x7E]+(?: [\x21\x23-\x5B\x5D-\x7E]+)*"
)


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
    auth: str = "none"
    oauth: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.transport not in {"stdio", "http", "sse", "websocket"}:
            raise ValueError(f"Unknown MCP transport: {self.transport}")
        if self.auth not in {"none", "oauth"}:
            raise ValueError(f"Unknown MCP auth mode: {self.auth}")
        if self.auth == "oauth" and self.transport not in {"http", "sse"}:
            raise ValueError("MCP OAuth requires the http or sse transport")
        if self.oauth is not None and not isinstance(self.oauth, dict):
            raise ValueError("MCP oauth configuration must be an object")
        _validate_oauth_data(self.name, self.auth, self.oauth or {})

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

    @property
    def resolved_oauth(self) -> dict[str, Any]:
        resolved = {
            key: expand_env_vars(value) if isinstance(value, str) else value
            for key, value in (self.oauth or {}).items()
        }
        configured_secret = str((self.oauth or {}).get("client_secret", ""))
        if configured_secret and resolved.get("client_secret") == configured_secret:
            raise ValueError(
                "MCP OAuth client secret environment variable is not set"
            )
        return resolved

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
            auth=data.get("auth", "none"),
            oauth=data.get("oauth", {}),
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
            "auth": config.auth,
            **({"oauth": config.oauth or {}} if config.auth == "oauth" else {}),
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
    auth = data.get("auth", "none")
    oauth = data.get("oauth", {})
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
    if auth not in {"none", "oauth"}:
        raise ValueError(f"MCP server {name!r} auth must be none or oauth")
    if not isinstance(oauth, dict):
        raise ValueError(f"MCP server {name!r} oauth must be an object")
    _validate_oauth_data(name, str(auth), oauth)
    if auth == "oauth" and transport not in {"http", "sse"}:
        raise ValueError(f"MCP server {name!r} OAuth requires http or sse")
    if transport == "stdio" and not command:
        raise ValueError(f"stdio MCP server {name!r} requires a command")
    if transport != "stdio" and not url:
        raise ValueError(f"{transport} MCP server {name!r} requires a url")


def _validate_oauth_data(name: str, auth: str, oauth: dict[str, Any]) -> None:
    if auth == "none" and oauth:
        raise ValueError(
            f"MCP server {name!r} OAuth options require auth mode oauth"
        )
    allowed_oauth_keys = {
        "client_id",
        "client_secret",
        "scope",
        "redirect_port",
        "client_name",
    }
    unknown_oauth = set(oauth) - allowed_oauth_keys
    if unknown_oauth:
        raise ValueError(
            f"MCP server {name!r} has unknown oauth keys: "
            + ", ".join(sorted(str(key) for key in unknown_oauth))
        )
    if any(
        key != "redirect_port" and not isinstance(value, str)
        for key, value in oauth.items()
    ):
        raise ValueError(f"MCP server {name!r} oauth values must be strings")
    redirect_port = oauth.get("redirect_port", 0)
    if not isinstance(redirect_port, int) or not 0 <= redirect_port <= 65535:
        raise ValueError(f"MCP server {name!r} oauth redirect_port is invalid")
    client_secret = str(oauth.get("client_secret", ""))
    if client_secret and not re.fullmatch(
        r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})",
        client_secret,
    ):
        raise ValueError(
            f"MCP server {name!r} oauth client_secret must reference an "
            "environment variable"
        )
    client_id = str(oauth.get("client_id", ""))
    if len(client_id) > 2048:
        raise ValueError(f"MCP server {name!r} oauth client_id is too long")
    if client_secret and not client_id:
        raise ValueError(
            f"MCP server {name!r} oauth client_secret requires client_id"
        )
    scope = str(oauth.get("scope", "")).strip()
    if scope and (len(scope) > 8192 or MCP_OAUTH_SCOPE.fullmatch(scope) is None):
        raise ValueError(f"MCP server {name!r} oauth scope is invalid")
    if len(str(oauth.get("client_name", ""))) > 100:
        raise ValueError(f"MCP server {name!r} oauth client_name is too long")
