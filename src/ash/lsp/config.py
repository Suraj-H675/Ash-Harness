"""Validated LSP server discovery and trusted configuration."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ash.safe_io import read_bounded_bytes


MAX_LSP_CONFIG_BYTES = 256 * 1024
MAX_LSP_SERVERS = 32
MAX_LSP_VALUES = 128
LSP_SERVER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
LSP_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class LSPServerConfig:
    name: str
    command: tuple[str, ...]
    extensions: dict[str, str]
    root_markers: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    initialization_options: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    disabled: bool = False
    source: str = "builtin"


BUILTIN_SERVERS: tuple[LSPServerConfig, ...] = (
    LSPServerConfig(
        "basedpyright",
        ("basedpyright-langserver", "--stdio"),
        {".py": "python", ".pyi": "python"},
        ("pyproject.toml", "setup.cfg", "setup.py", "requirements.txt", ".git"),
    ),
    LSPServerConfig(
        "pyright",
        ("pyright-langserver", "--stdio"),
        {".py": "python", ".pyi": "python"},
        ("pyproject.toml", "setup.cfg", "setup.py", "requirements.txt", ".git"),
    ),
    LSPServerConfig(
        "typescript",
        ("typescript-language-server", "--stdio"),
        {
            ".ts": "typescript",
            ".tsx": "typescriptreact",
            ".js": "javascript",
            ".jsx": "javascriptreact",
            ".mjs": "javascript",
            ".cjs": "javascript",
            ".mts": "typescript",
            ".cts": "typescript",
        },
        (
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "bun.lock",
            "bun.lockb",
            ".git",
        ),
    ),
    LSPServerConfig(
        "gopls",
        ("gopls",),
        {".go": "go"},
        ("go.work", "go.mod", ".git"),
    ),
    LSPServerConfig(
        "rust-analyzer",
        ("rust-analyzer",),
        {".rs": "rust"},
        ("Cargo.toml", ".git"),
    ),
    LSPServerConfig(
        "clangd",
        ("clangd",),
        {
            ".c": "c",
            ".h": "c",
            ".cc": "cpp",
            ".cpp": "cpp",
            ".cxx": "cpp",
            ".hpp": "cpp",
            ".hh": "cpp",
        },
        ("compile_commands.json", "compile_flags.txt", ".clangd", ".git"),
    ),
    LSPServerConfig(
        "lua",
        ("lua-language-server",),
        {".lua": "lua"},
        (".luarc.json", ".luarc.jsonc", ".git"),
    ),
)


def load_lsp_server_configs(
    workspace: Path,
    *,
    include_project: bool,
    detect_builtins: bool = True,
) -> dict[str, LSPServerConfig]:
    """Merge installed built-ins, user config, and trusted project config."""

    servers: dict[str, LSPServerConfig] = {}
    if detect_builtins:
        for config in BUILTIN_SERVERS:
            executable = _resolve_executable(
                config.command[0], workspace, allow_workspace=include_project
            )
            if executable is None:
                continue
            servers[config.name] = replace(
                config, command=(executable, *config.command[1:])
            )

    paths = [Path.home() / ".ash" / "lsp.json"]
    if include_project:
        paths.append(workspace / ".ash" / "lsp.json")
    for path in paths:
        if not path.is_file():
            continue
        for name, raw in _read_config(path).items():
            existing = servers.get(name)
            parsed = _parse_server(name, raw, path, existing)
            if parsed.disabled:
                servers.pop(name, None)
                continue
            servers[name] = parsed
    if "basedpyright" in servers and "pyright" in servers:
        based = servers["basedpyright"]
        pyright = servers["pyright"]
        if based.source == "builtin" and pyright.source != "builtin":
            servers.pop("basedpyright")
        else:
            servers.pop("pyright")
    if len(servers) > MAX_LSP_SERVERS:
        raise ValueError(f"LSP configuration exceeds {MAX_LSP_SERVERS} servers")
    return servers


def lsp_command_available(config: LSPServerConfig, workspace: Path) -> bool:
    """Return whether a configured executable resolves without running it."""

    command = Path(config.command[0]).expanduser()
    if (
        command.is_absolute()
        or len(command.parts) > 1
        or config.command[0] != command.name
    ):
        candidate = command if command.is_absolute() else workspace / command
        return candidate.is_file() and (
            os.name == "nt" or os.access(candidate, os.X_OK)
        )
    return shutil.which(config.command[0]) is not None


def _read_config(path: Path) -> dict[str, Any]:
    try:
        raw = read_bounded_bytes(
            path,
            MAX_LSP_CONFIG_BYTES,
            label="LSP config",
        )
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid LSP config {path}: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"servers"}:
        raise ValueError(f"LSP config {path} must contain only a servers object")
    servers = payload["servers"]
    if not isinstance(servers, dict) or len(servers) > MAX_LSP_SERVERS:
        raise ValueError(
            f"LSP config {path} servers must be an object of at most 32 entries"
        )
    return servers


def _parse_server(
    name: str,
    raw: Any,
    path: Path,
    existing: LSPServerConfig | None,
) -> LSPServerConfig:
    if not isinstance(name, str) or not LSP_SERVER_NAME.fullmatch(name):
        raise ValueError(f"invalid LSP server name in {path}: {name!r}")
    if not isinstance(raw, dict):
        raise ValueError(f"LSP server {name!r} in {path} must be an object")
    allowed = {
        "command",
        "extensions",
        "root_markers",
        "env",
        "initialization_options",
        "settings",
        "disabled",
    }
    if not set(raw) <= allowed:
        raise ValueError(f"LSP server {name!r} in {path} has unknown fields")
    disabled = raw.get("disabled", False)
    if not isinstance(disabled, bool):
        raise ValueError(f"LSP server {name!r} disabled must be boolean")
    if disabled:
        return replace(
            existing or LSPServerConfig(name, ("disabled",), {}, source=str(path)),
            disabled=True,
            source=str(path),
        )

    command = raw.get("command", list(existing.command) if existing else None)
    extensions = raw.get("extensions", existing.extensions if existing else None)
    root_markers = raw.get(
        "root_markers", list(existing.root_markers) if existing else []
    )
    env = raw.get("env", existing.env if existing else {})
    initialization = raw.get(
        "initialization_options",
        existing.initialization_options if existing else {},
    )
    settings = raw.get("settings", existing.settings if existing else {})
    _validate_string_list(command, f"LSP server {name!r} command", 64, minimum=1)
    if not command or not str(command[0]).strip():
        raise ValueError(f"LSP server {name!r} command cannot be empty")
    if not isinstance(extensions, dict) or not extensions:
        raise ValueError(f"LSP server {name!r} extensions must be a non-empty object")
    if len(extensions) > MAX_LSP_VALUES or not all(
        isinstance(key, str)
        and key.startswith(".")
        and len(key) <= 32
        and isinstance(value, str)
        and 0 < len(value) <= 64
        for key, value in extensions.items()
    ):
        raise ValueError(f"LSP server {name!r} extensions are invalid")
    normalized_extensions: dict[str, str] = {}
    for extension, language_id in extensions.items():
        normalized = extension.casefold()
        if normalized in normalized_extensions:
            raise ValueError(
                f"LSP server {name!r} has duplicate extension {normalized!r}"
            )
        normalized_extensions[normalized] = language_id
    _validate_string_list(
        root_markers, f"LSP server {name!r} root_markers", 64, minimum=0
    )
    if any(
        not marker or Path(marker).is_absolute() or ".." in Path(marker).parts
        for marker in root_markers
    ):
        raise ValueError(
            f"LSP server {name!r} root_markers must be relative workspace paths"
        )
    if (
        not isinstance(env, dict)
        or len(env) > MAX_LSP_VALUES
        or not all(
            isinstance(key, str)
            and LSP_ENV_NAME.fullmatch(key)
            and isinstance(value, str)
            and len(value.encode("utf-8")) <= 65_536
            for key, value in env.items()
        )
    ):
        raise ValueError(f"LSP server {name!r} env is invalid")
    if not isinstance(initialization, dict) or not isinstance(settings, dict):
        raise ValueError(f"LSP server {name!r} options and settings must be objects")
    try:
        initialization_bytes = json.dumps(
            initialization, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        settings_bytes = json.dumps(
            settings, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"LSP server {name!r} options and settings must be JSON values"
        ) from exc
    if len(initialization_bytes) > 128 * 1024 or len(settings_bytes) > 128 * 1024:
        raise ValueError(f"LSP server {name!r} options exceed 128 KiB")
    return LSPServerConfig(
        name=name,
        command=tuple(command),
        extensions=normalized_extensions,
        root_markers=tuple(root_markers),
        env=dict(env),
        initialization_options=dict(initialization),
        settings=dict(settings),
        source=str(path),
    )


def _validate_string_list(
    value: Any, label: str, maximum: int, *, minimum: int
) -> None:
    if (
        not isinstance(value, list | tuple)
        or not minimum <= len(value) <= maximum
        or not all(
            isinstance(item, str) and len(item.encode("utf-8")) <= 4096
            for item in value
        )
    ):
        raise ValueError(f"{label} must contain {minimum}..{maximum} bounded strings")


def _resolve_executable(
    command: str, workspace: Path, *, allow_workspace: bool
) -> str | None:
    suffix = ".cmd" if os.name == "nt" else ""
    local = workspace / "node_modules" / ".bin" / f"{command}{suffix}"
    if (
        allow_workspace
        and local.is_file()
        and (os.name == "nt" or os.access(local, os.X_OK))
    ):
        return str(local.resolve())
    return shutil.which(command)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        value[key] = item
    return value
