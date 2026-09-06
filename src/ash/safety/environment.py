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


def build_scrubbed_environment(
    allowed_names: Iterable[str] = (),
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return operational variables, explicit names, and explicit overrides."""

    allowed = set(allowed_names)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in SAFE_ENV_KEYS
        or any(key.startswith(prefix) for prefix in SAFE_ENV_PREFIXES)
        or key in allowed
    }
    if "PATH" not in environment:
        environment["PATH"] = os.defpath
    environment.update(overrides or {})
    return environment


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
        resolved = shutil.which(str(directory / command))
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
