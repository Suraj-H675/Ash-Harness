"""Safe paths and active-state management for named Ash profiles."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from ash.safe_io import (
    anchored_directory_exists,
    atomic_write_unlinked_bytes,
    ensure_anchored_directory,
    list_anchored_directory,
    read_bounded_open_file,
)


DEFAULT_PROFILE = "default"
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ACTIVE_PROFILE_FILENAME = "active-profile"
MAX_ACTIVE_PROFILE_BYTES = 256
MAX_PROFILE_ENTRIES = 10_000


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _state_root(ash_dir: Path | None = None) -> Path:
    root = ash_dir or (Path.home() / ".ash")
    if _is_link(root):
        raise ValueError(f"refusing to use symlinked Ash state directory: {root}")
    return root


def _profiles_root(ash_dir: Path | None = None) -> Path:
    root = _state_root(ash_dir) / "profiles"
    if _is_link(root):
        raise ValueError(f"refusing to use symlinked profiles directory: {root}")
    return root


def validate_profile_name(name: str) -> str:
    """Normalize a profile name and reject path traversal or shell-like input."""

    normalized = name.strip()
    if normalized.casefold() == DEFAULT_PROFILE:
        return DEFAULT_PROFILE
    if not _PROFILE_NAME.fullmatch(normalized):
        raise ValueError(
            "profile name must start with a letter or digit and contain only "
            "letters, digits, '.', '-', or '_'"
        )
    return normalized.casefold()


def profiles_directory(ash_dir: Path | None = None) -> Path:
    """Return the user-owned directory containing named profile directories."""

    return _profiles_root(ash_dir)


def profile_directory(name: str, *, ash_dir: Path | None = None) -> Path:
    """Return the isolated state directory for a validated profile."""

    normalized = validate_profile_name(name)
    root = _state_root(ash_dir)
    if normalized == DEFAULT_PROFILE:
        return root
    return _profiles_root(root) / normalized


def active_profile_name(
    *,
    environ: Mapping[str, str] | None = None,
    ash_dir: Path | None = None,
) -> str:
    """Resolve the explicit environment override, then the persisted selection."""

    environment = os.environ if environ is None else environ
    if "ASH_PROFILE" in environment:
        value = str(environment["ASH_PROFILE"]).strip()
        return validate_profile_name(value or DEFAULT_PROFILE)

    root = _state_root(ash_dir)
    marker = root / _ACTIVE_PROFILE_FILENAME
    try:
        raw = read_bounded_open_file(
            marker,
            MAX_ACTIVE_PROFILE_BYTES,
            label="active Ash profile marker",
            trusted_root=root.parent,
        )
        value = raw.decode("utf-8").strip()
    except FileNotFoundError:
        return DEFAULT_PROFILE
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read active Ash profile marker {marker}: {exc}") from exc
    return validate_profile_name(value or DEFAULT_PROFILE)


def set_active_profile(name: str, *, ash_dir: Path | None = None) -> str:
    """Persist the active profile marker atomically and return its normalized name."""

    normalized = validate_profile_name(name)
    root = _state_root(ash_dir)
    ensure_anchored_directory(
        root,
        trusted_root=root.parent,
        label="Ash state directory",
        mode=0o700,
    )
    marker = root / _ACTIVE_PROFILE_FILENAME
    atomic_write_unlinked_bytes(
        marker,
        (normalized + "\n").encode("utf-8"),
        label="active Ash profile marker",
        mode=0o600,
        trusted_root=root.parent,
    )
    return normalized


def list_profile_names(*, ash_dir: Path | None = None) -> tuple[str, ...]:
    """Return the default profile plus valid named profile directories."""

    state_root = _state_root(ash_dir)
    root = _profiles_root(state_root)
    names = [DEFAULT_PROFILE]
    try:
        entries = list_anchored_directory(
            root,
            trusted_root=state_root.parent,
            label="profile directory",
            max_entries=MAX_PROFILE_ENTRIES,
        )
    except FileNotFoundError:
        entries = []
    for entry_name, is_directory in entries:
        if not is_directory:
            continue
        try:
            names.append(validate_profile_name(entry_name))
        except ValueError:
            continue
    return tuple(sorted(set(names), key=lambda value: (value != DEFAULT_PROFILE, value)))


def profile_exists(name: str, *, ash_dir: Path | None = None) -> bool:
    """Return whether a profile is usable; the default profile always exists."""

    normalized = validate_profile_name(name)
    if normalized == DEFAULT_PROFILE:
        return True
    state_root = _state_root(ash_dir)
    directory = _profiles_root(state_root) / normalized
    return anchored_directory_exists(
        directory,
        trusted_root=state_root.parent,
        label="profile directory",
    )


__all__ = [
    "DEFAULT_PROFILE",
    "active_profile_name",
    "list_profile_names",
    "profile_directory",
    "profile_exists",
    "profiles_directory",
    "set_active_profile",
    "validate_profile_name",
]
