"""Deny-by-default environment construction for child processes."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping


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
        "SYSTEMROOT",
        "WINDIR",
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
