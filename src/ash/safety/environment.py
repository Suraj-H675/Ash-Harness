"""Deny-by-default environment construction for child processes."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path

from ash.safety.path_scope import is_relative_to


SAFE_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "USERNAME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "PYTHONIOENCODING",
        "SystemRoot",
        "windir",
        "COMSPEC",
        "PATHEXT",
    }
)
SAFE_ENV_PREFIXES = ("LC_",)


def _environment_name_key(name: str, *, platform_name: str) -> str:
    return name.casefold() if platform_name == "nt" else name


def _build_environment_mapping(
    source: Mapping[str, str],
    allowed_names: Iterable[str],
    *,
    overrides: Mapping[str, str] | None,
    platform_name: str,
) -> dict[str, str]:
    allowed = {
        _environment_name_key(name, platform_name=platform_name)
        for name in allowed_names
    }
    safe_keys = {
        _environment_name_key(name, platform_name=platform_name)
        for name in SAFE_ENV_KEYS
    }
    safe_prefixes = tuple(
        _environment_name_key(prefix, platform_name=platform_name)
        for prefix in SAFE_ENV_PREFIXES
    )
    environment: dict[str, str] = {}
    for key, value in source.items():
        normalized = _environment_name_key(key, platform_name=platform_name)
        if (
            normalized in safe_keys
            or any(normalized.startswith(prefix) for prefix in safe_prefixes)
            or normalized in allowed
        ):
            environment[key] = value

    path_key = _environment_name_key("PATH", platform_name=platform_name)
    if not any(
        _environment_name_key(key, platform_name=platform_name) == path_key
        for key in environment
    ):
        environment["PATH"] = os.defpath

    for key, value in (overrides or {}).items():
        normalized = _environment_name_key(key, platform_name=platform_name)
        if platform_name == "nt":
            for existing in tuple(environment):
                if _environment_name_key(existing, platform_name=platform_name) == normalized:
                    environment.pop(existing)
        environment[key] = value
    return environment


def build_scrubbed_environment(
    allowed_names: Iterable[str] = (),
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return operational variables, explicit names, and explicit overrides."""

    return _build_environment_mapping(
        os.environ,
        allowed_names,
        overrides=overrides,
        platform_name=os.name,
    )


def resolve_host_executable(
    command: str,
    *,
    workspace_root: str | Path | None = None,
    cwd: str | Path | None = None,
    search_path: str | None = None,
) -> str | None:
    """Resolve an Ash-owned helper without executing workspace-controlled code.

    Internal helpers such as Git, ripgrep, and sandbox backends are selected by
    Ash rather than by the model or user.  A workspace can legitimately appear
    on ``PATH`` (including through a relative or empty PATH entry), so resolving
    those helpers with a plain ``shutil.which(name)`` would allow untrusted
    repository content to shadow the host executable.

    Search PATH in order, but skip candidates whose canonical path is inside the
    active workspace.  The returned value is always an absolute executable path
    so a later subprocess cannot be redirected by a different working directory.
    """

    if not command or "/" in command or "\\" in command or "\x00" in command:
        return None
    base = Path(cwd).expanduser().resolve() if cwd is not None else Path.cwd().resolve()
    workspace = (
        Path(workspace_root).expanduser().resolve()
        if workspace_root is not None
        else None
    )
    path_value = search_path if search_path is not None else os.environ.get("PATH", os.defpath)
    for raw_entry in path_value.split(os.pathsep):
        entry = raw_entry.strip('"') or "."
        directory = Path(entry).expanduser()
        if not directory.is_absolute():
            directory = base / directory
        try:
            directory = directory.resolve()
        except OSError:
            continue
        if workspace is not None and is_relative_to(directory, workspace):
            continue
        resolved = shutil.which(command, path=str(directory))
        if resolved is None:
            continue
        try:
            candidate = Path(resolved).resolve()
        except OSError:
            continue
        if workspace is not None and is_relative_to(candidate, workspace):
            continue
        return str(candidate)
    return None
