"""Safe paths and active-state management for named Ash profiles."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path


DEFAULT_PROFILE = "default"
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ACTIVE_PROFILE_FILENAME = "active-profile"
MAX_ACTIVE_PROFILE_BYTES = 256


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

    return (ash_dir or (Path.home() / ".ash")) / "profiles"


def profile_directory(name: str, *, ash_dir: Path | None = None) -> Path:
    """Return the isolated state directory for a validated profile."""

    normalized = validate_profile_name(name)
    root = ash_dir or (Path.home() / ".ash")
    if normalized == DEFAULT_PROFILE:
        return root
    return profiles_directory(root) / normalized


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

    marker = (ash_dir or (Path.home() / ".ash")) / _ACTIVE_PROFILE_FILENAME
    if not marker.is_file():
        return DEFAULT_PROFILE
    try:
        with marker.open("rb") as handle:
            raw = handle.read(MAX_ACTIVE_PROFILE_BYTES + 1)
        if len(raw) > MAX_ACTIVE_PROFILE_BYTES:
            raise ValueError(
                f"active Ash profile marker exceeds {MAX_ACTIVE_PROFILE_BYTES} bytes"
            )
        value = raw.decode("utf-8").strip()
    except OSError as exc:
        raise ValueError(f"cannot read active Ash profile marker {marker}: {exc}") from exc
    return validate_profile_name(value or DEFAULT_PROFILE)


def set_active_profile(name: str, *, ash_dir: Path | None = None) -> str:
    """Persist the active profile marker atomically and return its normalized name."""

    normalized = validate_profile_name(name)
    root = ash_dir or (Path.home() / ".ash")
    root.mkdir(parents=True, exist_ok=True)
    marker = root / _ACTIVE_PROFILE_FILENAME
    fd, temporary = tempfile.mkstemp(dir=root, prefix=f".{marker.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(normalized + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
        if os.name != "nt":
            marker.chmod(0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return normalized


def list_profile_names(*, ash_dir: Path | None = None) -> tuple[str, ...]:
    """Return the default profile plus valid named profile directories."""

    root = profiles_directory(ash_dir)
    names = [DEFAULT_PROFILE]
    if root.is_dir():
        for path in root.iterdir():
            if not path.is_dir():
                continue
            try:
                names.append(validate_profile_name(path.name))
            except ValueError:
                continue
    return tuple(sorted(set(names), key=lambda value: (value != DEFAULT_PROFILE, value)))


def profile_exists(name: str, *, ash_dir: Path | None = None) -> bool:
    """Return whether a profile is usable; the default profile always exists."""

    normalized = validate_profile_name(name)
    return normalized == DEFAULT_PROFILE or profile_directory(
        normalized, ash_dir=ash_dir
    ).is_dir()


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
