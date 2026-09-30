"""MCP server discovery and launch configuration."""

from __future__ import annotations

import json
import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ash.safety.anchored_fs import AnchoredDirectory, AnchoredFilesystemError
from ash.safe_io import strict_json_loads
from ash.safety.environment import resolve_host_executable
from ash.mcp.oauth import MCPOAuthError, canonical_resource_uri

MAX_MCP_CONFIG_BYTES = 256 * 1024
MAX_MCP_SERVERS = 32
MAX_MCP_ARGS = 256
MAX_MCP_ENV_ENTRIES = 256
MAX_MCP_HEADER_ENTRIES = 128
MAX_MCP_KEY_BYTES = 256
MAX_MCP_FIELD_BYTES = 8 * 1024
MAX_MCP_VALUE_BYTES = 16 * 1024
MCP_SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MCP_OAUTH_SCOPE = re.compile(
    r"[\x21\x23-\x5B\x5D-\x7E]+(?: [\x21\x23-\x5B\x5D-\x7E]+)*"
)


def _mcp_config_state_error(path: Path, exc: BaseException) -> ValueError:
    detail = str(exc)
    lowered = detail.casefold()
    parent = path.parent
    parent_is_link = parent.is_symlink() or (
        hasattr(parent, "is_junction") and parent.is_junction()
    )
    if "link" in lowered or "reparse" in lowered or parent_is_link:
        detail = f"symlink or junction in MCP config path: {detail}"
    if "exceeds" in lowered:
        return ValueError(f"MCP config exceeds 256 KiB: {path}")
    return ValueError(f"MCP config is not readable: {path}: {detail}")


def validate_mcp_server_count(configs: dict[str, Any]) -> None:
    if len(configs) > MAX_MCP_SERVERS:
        raise ValueError(
            f"MCP server count exceeds {MAX_MCP_SERVERS}: {len(configs)} configured"
        )


@dataclass(frozen=True)
class MCPServerConfig:
    """Configuration for a single MCP server."""

    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    transport: str = "stdio"  # "stdio" | "sse" | "http"
    url: str = ""  # for Streamable HTTP (including the "sse" config alias)
    headers: dict[str, str] | None = None
    cwd: str = ""
    auth: str = "none"
    oauth: dict[str, Any] | None = None
    source_root: str = ""
    cwd_identity: tuple[int, int] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    source_root_identity: tuple[int, int] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not MCP_SERVER_NAME.fullmatch(self.name):
            raise ValueError(f"invalid MCP server name: {self.name!r}")
        _validate_server_data(
            self.name,
            {
                "command": self.command,
                "args": self.args,
                "env": self.env,
                "transport": self.transport,
                "url": self.url,
                "headers": self.headers or {},
                "cwd": self.cwd,
                "auth": self.auth,
                "oauth": self.oauth or {},
            },
        )
        _validate_server_resource_bounds(
            self.name,
            command=self.command,
            args=self.args,
            env=self.env,
            url=self.url,
            headers=self.headers or {},
            cwd=self.cwd,
            source_root=self.source_root,
            oauth=self.oauth or {},
        )
        _validate_server_resource_bounds(
            self.name,
            command=expand_env_vars(self.command),
            args=[expand_env_vars(arg) for arg in self.args],
            env={key: expand_env_vars(value) for key, value in self.env.items()},
            url=expand_env_vars(self.url),
            headers={
                key: expand_env_vars(value)
                for key, value in (self.headers or {}).items()
            },
            cwd=expand_env_vars(self.cwd) if self.cwd else "",
            source_root=(
                expand_env_vars(self.source_root) if self.source_root else ""
            ),
            oauth={
                key: expand_env_vars(value) if isinstance(value, str) else value
                for key, value in (self.oauth or {}).items()
            },
        )
        if self.transport in {"http", "sse"}:
            try:
                parsed = urlparse(self.resolved_url)
                port = parsed.port
            except ValueError as exc:
                raise ValueError(
                    "MCP HTTP URLs must use http or https and include a hostname"
                ) from exc
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("MCP HTTP URLs must use http or https and include a hostname")
            if port is not None and not 0 < port <= 65535:
                raise ValueError("MCP HTTP URLs must include a valid port")
            if parsed.username or parsed.password or parsed.fragment:
                raise ValueError(
                    "MCP HTTP URLs cannot contain credentials or fragments"
                )
        if self.auth == "oauth":
            try:
                canonical_resource_uri(self.resolved_url)
            except MCPOAuthError as exc:
                raise ValueError(str(exc)) from exc
        credential_headers = {
            name.casefold()
            for name, value in (self.headers or {}).items()
            if value and name.casefold()
            in {"authorization", "proxy-authorization", "x-api-key"}
        }
        if self.transport in {"http", "sse"} and credential_headers:
            try:
                canonical_resource_uri(self.resolved_url)
            except MCPOAuthError as exc:
                raise ValueError(str(exc)) from exc
        resolved_cwd = self.resolved_cwd
        if resolved_cwd is not None:
            try:
                metadata = os.stat(resolved_cwd)
            except OSError:
                pass
            else:
                object.__setattr__(
                    self,
                    "cwd_identity",
                    (metadata.st_dev, metadata.st_ino),
                )
        resolved_source_root = self.resolved_source_root
        if resolved_source_root is not None:
            try:
                metadata = os.stat(resolved_source_root, follow_symlinks=False)
            except OSError as exc:
                raise ValueError(
                    f"MCP config source root is unavailable: {resolved_source_root}"
                ) from exc
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(
                    f"MCP config source root is not a directory: {resolved_source_root}"
                )
            object.__setattr__(
                self,
                "source_root_identity",
                (metadata.st_dev, metadata.st_ino),
            )

    @property
    def resolved_command(self) -> str:
        return _bounded_mcp_text(
            expand_env_vars(self.command),
            f"MCP server {self.name!r} command",
            MAX_MCP_FIELD_BYTES,
        )

    @property
    def resolved_args(self) -> list[str]:
        return [
            _bounded_mcp_text(
                expand_env_vars(arg),
                f"MCP server {self.name!r} argument",
                MAX_MCP_FIELD_BYTES,
            )
            for arg in self.args
        ]

    @property
    def resolved_env(self) -> dict[str, str]:
        return {
            key: _bounded_mcp_text(
                expand_env_vars(value),
                f"MCP server {self.name!r} environment value",
                MAX_MCP_VALUE_BYTES,
            )
            for key, value in self.env.items()
        }

    @property
    def resolved_url(self) -> str:
        return _bounded_mcp_text(
            expand_env_vars(self.url),
            f"MCP server {self.name!r} URL",
            MAX_MCP_FIELD_BYTES,
        )

    @property
    def resolved_headers(self) -> dict[str, str]:
        return {
            key: _bounded_mcp_text(
                expand_env_vars(value),
                f"MCP server {self.name!r} header value",
                MAX_MCP_VALUE_BYTES,
            )
            for key, value in (self.headers or {}).items()
        }

    @property
    def resolved_cwd(self) -> str | None:
        return (
            _bounded_mcp_text(
                expand_env_vars(self.cwd),
                f"MCP server {self.name!r} cwd",
                MAX_MCP_FIELD_BYTES,
            )
            if self.cwd
            else None
        )

    @property
    def resolved_source_root(self) -> str | None:
        return (
            _bounded_mcp_text(
                expand_env_vars(self.source_root),
                f"MCP server {self.name!r} source root",
                MAX_MCP_FIELD_BYTES,
            )
            if self.source_root
            else None
        )

    def ensure_source_current(self) -> None:
        """Reject a config whose trusted source directory changed after loading."""

        resolved = self.resolved_source_root
        expected = self.source_root_identity
        if resolved is None or expected is None:
            return
        try:
            metadata = os.stat(resolved, follow_symlinks=False)
        except OSError as exc:
            raise ValueError(f"MCP config source identity changed: {resolved}") from exc
        if not stat.S_ISDIR(metadata.st_mode) or (
            metadata.st_dev,
            metadata.st_ino,
        ) != expected:
            raise ValueError(f"MCP config source identity changed: {resolved}")

    @property
    def resolved_oauth(self) -> dict[str, Any]:
        resolved = {
            key: _bounded_mcp_text(
                expand_env_vars(value),
                f"MCP server {self.name!r} OAuth value",
                MAX_MCP_VALUE_BYTES,
            )
            if isinstance(value, str)
            else value
            for key, value in (self.oauth or {}).items()
        }
        configured_secret = str((self.oauth or {}).get("client_secret", ""))
        if configured_secret and resolved.get("client_secret") == configured_secret:
            raise ValueError("MCP OAuth client secret environment variable is not set")
        return resolved

    @classmethod
    def from_dict(
        cls,
        name: str,
        data: dict[str, Any],
        *,
        cwd: str = "",
        environment: dict[str, str] | None = None,
        source_root: str = "",
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
            source_root=source_root,
        )


def resolve_mcp_stdio_launch(
    config: MCPServerConfig,
    *,
    environment: dict[str, str],
) -> tuple[str, Path | None, tuple[int, int] | None]:
    """Resolve one stdio MCP executable and deterministic launch directory.

    Bare executable names are resolved through the configured child PATH while
    excluding the relevant workspace/source root, preventing a checkout from
    shadowing a host-installed server. Explicit paths remain an intentional
    opt-in and are resolved against cwd, then trusted source root, then the
    current directory.
    """

    if config.transport != "stdio":
        raise ValueError("MCP stdio launch resolution requires stdio transport")
    raw_command = config.resolved_command.strip()
    if not raw_command or "\x00" in raw_command:
        raise ValueError(f"MCP server {config.name!r} has an invalid command")

    configured_cwd = config.resolved_cwd
    source_root = config.resolved_source_root
    if configured_cwd is not None:
        launch_cwd = Path(configured_cwd).expanduser().resolve()
        expected_identity = config.cwd_identity
    elif source_root is not None:
        launch_cwd = Path(source_root).expanduser().resolve()
        expected_identity = config.source_root_identity
    else:
        launch_cwd = None
        expected_identity = None

    base = launch_cwd or Path.cwd().resolve()
    workspace_boundary = (
        Path(source_root).expanduser().resolve()
        if source_root is not None
        else base
    )
    command_path = Path(raw_command).expanduser()
    explicit_path = (
        command_path.is_absolute()
        or len(command_path.parts) > 1
        or raw_command != command_path.name
    )
    if explicit_path:
        candidate = command_path if command_path.is_absolute() else base / command_path
        launch_path = Path(os.path.abspath(candidate))
        try:
            canonical = launch_path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(
                f"MCP stdio executable is unavailable: {raw_command!r}"
            ) from exc
        if not canonical.is_file() or not os.access(launch_path, os.X_OK):
            raise ValueError(f"MCP stdio executable is unavailable: {raw_command!r}")
        # Keep the configured lexical launcher path after validating its
        # canonical target. Virtualenv interpreters and other launch shims may
        # deliberately derive runtime state from argv[0]/their symlink path;
        # replacing that path with the canonical target changes semantics.
        return str(launch_path), launch_cwd, expected_identity

    executable = resolve_host_executable(
        raw_command,
        workspace_root=workspace_boundary,
        cwd=base,
        search_path=environment.get("PATH"),
    )
    if executable is None:
        raise ValueError(
            f"MCP stdio executable is unavailable outside the workspace: {raw_command!r}"
        )
    return executable, launch_cwd, expected_identity


def mcp_server_fingerprint(
    config: MCPServerConfig,
    server_info: dict[str, Any] | None = None,
) -> str:
    """Return a non-secret digest binding a durable task to one MCP server."""

    payload = {
        "name": config.name,
        "transport": config.transport,
        "command": config.resolved_command,
        "args": config.resolved_args,
        "env": config.resolved_env,
        "url": config.resolved_url,
        "headers": config.resolved_headers,
        "cwd": config.resolved_cwd,
        "cwd_identity": list(config.cwd_identity) if config.cwd_identity else None,
        "auth": config.auth,
        "oauth": config.resolved_oauth,
        "server_info": server_info or {},
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class MCPConfigSource:
    path: Path
    namespace: str = ""
    cwd: Path | None = None
    environment: tuple[tuple[str, str], ...] = ()
    source_root: Path | None = None


def load_mcp_servers(
    config_path: Path | None = None,
    *,
    namespace: str = "",
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    source_root: Path | None = None,
) -> dict[str, MCPServerConfig]:
    """Load MCP server definitions from .mcp.json."""
    if config_path is None:
        config_path = Path(".mcp.json")
    try:
        with AnchoredDirectory.open(
            config_path.parent,
            create=False,
            private=False,
            pin_path=True,
        ) as directory:
            directory.validation_path()
            metadata = directory.stat(config_path.name)
            if metadata is None:
                return {}
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"symlinked MCP config: {config_path}")
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"MCP config is not readable: {config_path}")
            raw_bytes = directory.read_file(
                config_path.name,
                max_bytes=MAX_MCP_CONFIG_BYTES,
            )
            assert raw_bytes is not None
            directory.validation_path()
    except FileNotFoundError:
        return {}
    except AnchoredFilesystemError as exc:
        raise _mcp_config_state_error(config_path, exc) from exc
    raw: Any = strict_json_loads(raw_bytes)
    return parse_mcp_servers_payload(
        raw,
        config_path,
        namespace=namespace,
        cwd=cwd,
        environment=environment,
        source_root=source_root,
    )


def parse_mcp_servers_payload(
    raw: Any,
    config_path: Path,
    *,
    namespace: str = "",
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    source_root: Path | None = None,
) -> dict[str, MCPServerConfig]:
    """Validate MCP server data that was already read from immutable bytes."""

    if not isinstance(raw, dict):
        raise ValueError(f"MCP config must be an object: {config_path}")
    if "mcpServers" in raw:
        wrapped = raw["mcpServers"]
        if not isinstance(wrapped, dict):
            raise ValueError(f"mcpServers must be an object: {config_path}")
        raw = wrapped
    validate_mcp_server_count(raw)

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
            source_root=str(source_root) if source_root is not None else "",
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
            source_root=source.source_root,
        )
        duplicates = sorted(merged.keys() & loaded.keys())
        if duplicates:
            raise ValueError("duplicate MCP server name(s): " + ", ".join(duplicates))
        if len(merged) + len(loaded) > MAX_MCP_SERVERS:
            raise ValueError(
                f"MCP server count exceeds {MAX_MCP_SERVERS}: "
                f"{len(merged) + len(loaded)} configured"
            )
        merged.update(loaded)
    return merged


def save_mcp_servers(
    servers: dict[str, MCPServerConfig],
    config_path: Path | None = None,
) -> Path:
    """Atomically persist MCP server definitions."""

    path = config_path or Path(".mcp.json")
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
    serialized = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with AnchoredDirectory.open(
            path.parent,
            create=True,
            private=False,
            pin_path=True,
        ) as directory:
            existing = directory.stat(path.name)
            if existing is not None:
                if stat.S_ISLNK(existing.st_mode):
                    raise ValueError(f"symlinked MCP config: {path}")
                if not stat.S_ISREG(existing.st_mode):
                    raise ValueError(f"MCP config is not writable: {path}")
            temporary_name = directory.unique_name(f".{path.name}.", ".tmp")
            descriptor = directory.create_file(temporary_name, mode=0o600)
            renamed = False
            completed = False
            try:
                view = memoryview(serialized)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short write while writing MCP config")
                    view = view[written:]
                os.fsync(descriptor)
                directory.validation_path()
                directory.rename(
                    temporary_name,
                    path.name,
                    expected_source_descriptor=descriptor,
                )
                renamed = True
                directory.validation_path()
                if os.name != "nt":
                    directory.sync()
                completed = True
            finally:
                if not completed:
                    cleanup_name = path.name if renamed else temporary_name
                    try:
                        directory.unlink(
                            cleanup_name,
                            missing_ok=True,
                            expected_descriptor=descriptor,
                        )
                    except BaseException:
                        pass
                os.close(descriptor)
    except AnchoredFilesystemError as exc:
        raise _mcp_config_state_error(path, exc) from exc
    return path


def expand_env_vars(value: str) -> str:
    """Expand ${VAR} and $VAR in strings."""
    return os.path.expandvars(value)


def _bounded_mcp_text(value: str, label: str, maximum: int) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8 text") from exc
    if len(encoded) > maximum:
        raise ValueError(f"{label} exceeds {maximum} bytes")
    return value


def _validate_server_resource_bounds(
    name: str,
    *,
    command: str,
    args: list[str],
    env: dict[str, str],
    url: str,
    headers: dict[str, str],
    cwd: str,
    source_root: str,
    oauth: dict[str, Any],
) -> None:
    if len(args) > MAX_MCP_ARGS:
        raise ValueError(
            f"MCP server {name!r} has more than {MAX_MCP_ARGS} arguments"
        )
    if len(env) > MAX_MCP_ENV_ENTRIES:
        raise ValueError(
            f"MCP server {name!r} has more than {MAX_MCP_ENV_ENTRIES} environment entries"
        )
    if len(headers) > MAX_MCP_HEADER_ENTRIES:
        raise ValueError(
            f"MCP server {name!r} has more than {MAX_MCP_HEADER_ENTRIES} headers"
        )

    _bounded_mcp_text(command, f"MCP server {name!r} command", MAX_MCP_FIELD_BYTES)
    _bounded_mcp_text(url, f"MCP server {name!r} URL", MAX_MCP_FIELD_BYTES)
    _bounded_mcp_text(cwd, f"MCP server {name!r} cwd", MAX_MCP_FIELD_BYTES)
    _bounded_mcp_text(
        source_root,
        f"MCP server {name!r} source root",
        MAX_MCP_FIELD_BYTES,
    )
    for argument in args:
        _bounded_mcp_text(
            argument,
            f"MCP server {name!r} argument",
            MAX_MCP_FIELD_BYTES,
        )
    for key, value in env.items():
        _bounded_mcp_text(
            key,
            f"MCP server {name!r} environment key",
            MAX_MCP_KEY_BYTES,
        )
        _bounded_mcp_text(
            value,
            f"MCP server {name!r} environment value",
            MAX_MCP_VALUE_BYTES,
        )
    for key, value in headers.items():
        _bounded_mcp_text(
            key,
            f"MCP server {name!r} header name",
            MAX_MCP_KEY_BYTES,
        )
        _bounded_mcp_text(
            value,
            f"MCP server {name!r} header value",
            MAX_MCP_VALUE_BYTES,
        )
    for key, value in oauth.items():
        _bounded_mcp_text(
            str(key),
            f"MCP server {name!r} OAuth key",
            MAX_MCP_KEY_BYTES,
        )
        if isinstance(value, str):
            _bounded_mcp_text(
                value,
                f"MCP server {name!r} OAuth value",
                MAX_MCP_VALUE_BYTES,
            )


def _validate_server_data(name: str, data: dict[str, Any]) -> None:
    transport = data.get("transport", "stdio")
    if transport not in {"stdio", "http", "sse"}:
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
    folded_headers: dict[str, str] = {}
    for key in headers:
        folded = key.casefold()
        if folded in folded_headers:
            raise ValueError(
                f"MCP server {name!r} has duplicate header names: "
                f"{folded_headers[folded]!r} and {key!r}"
            )
        folded_headers[folded] = key
    if not isinstance(cwd, str):
        raise ValueError(f"MCP server {name!r} cwd must be a string")
    if auth not in {"none", "oauth"}:
        raise ValueError(f"MCP server {name!r} auth must be none or oauth")
    if not isinstance(oauth, dict):
        raise ValueError(f"MCP server {name!r} oauth must be an object")
    _validate_oauth_data(name, str(auth), oauth)
    if auth == "oauth" and transport not in {"http", "sse"}:
        raise ValueError(
            f"MCP server {name!r} OAuth requires the http or sse transport"
        )
    if transport == "stdio" and not command:
        raise ValueError(f"stdio MCP server {name!r} requires a command")
    if transport != "stdio" and not url:
        raise ValueError(f"{transport} MCP server {name!r} requires a url")


def _validate_oauth_data(name: str, auth: str, oauth: dict[str, Any]) -> None:
    if auth == "none" and oauth:
        raise ValueError(f"MCP server {name!r} OAuth options require auth mode oauth")
    allowed_oauth_keys = {
        "client_id",
        "client_secret",
        "client_metadata_url",
        "issuer",
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
    if (
        not isinstance(redirect_port, int)
        or isinstance(redirect_port, bool)
        or not 0 <= redirect_port <= 65535
    ):
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
        raise ValueError(f"MCP server {name!r} oauth client_secret requires client_id")
    client_metadata_url = str(oauth.get("client_metadata_url", ""))
    if client_metadata_url:
        try:
            parsed_metadata = urlparse(client_metadata_url)
            metadata_port = parsed_metadata.port
        except ValueError as exc:
            raise ValueError(
                f"MCP server {name!r} oauth client metadata URL is invalid"
            ) from exc
        if (
            client_metadata_url != client_metadata_url.strip()
            or len(client_metadata_url) > 4096
            or parsed_metadata.scheme != "https"
            or not parsed_metadata.hostname
            or parsed_metadata.username
            or parsed_metadata.password
            or parsed_metadata.fragment
            or parsed_metadata.path in {"", "/"}
            or (metadata_port is not None and not 0 < metadata_port <= 65535)
        ):
            raise ValueError(
                f"MCP server {name!r} oauth client metadata URL must be an HTTPS "
                "URL with a non-root path and no credentials or fragment"
            )
    if client_id and client_metadata_url:
        raise ValueError(
            f"MCP server {name!r} oauth client_id and client_metadata_url are mutually exclusive"
        )
    issuer = str(oauth.get("issuer", ""))
    if issuer:
        if issuer != issuer.strip() or len(issuer) > 4096:
            raise ValueError(f"MCP server {name!r} oauth issuer is invalid")
        try:
            parsed_issuer = urlparse(issuer)
            issuer_port = parsed_issuer.port
        except ValueError as exc:
            raise ValueError(f"MCP server {name!r} oauth issuer is invalid") from exc
        if (
            parsed_issuer.scheme != "https"
            or not parsed_issuer.hostname
            or parsed_issuer.username
            or parsed_issuer.password
            or parsed_issuer.fragment
            or (issuer_port is not None and not 0 < issuer_port <= 65535)
        ):
            raise ValueError(
                f"MCP server {name!r} oauth issuer must be an HTTPS URL "
                "without credentials or fragments"
            )
    scope = str(oauth.get("scope", "")).strip()
    if scope and (len(scope) > 8192 or MCP_OAUTH_SCOPE.fullmatch(scope) is None):
        raise ValueError(f"MCP server {name!r} oauth scope is invalid")
    if len(str(oauth.get("client_name", ""))) > 100:
        raise ValueError(f"MCP server {name!r} oauth client_name is too long")
