"""Named profile administration for user-owned Ash state."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ash.profiles import (
    DEFAULT_PROFILE,
    active_profile_name,
    list_profile_names,
    profile_directory,
    profile_exists,
    set_active_profile,
    validate_profile_name,
)
from ash.safe_io import read_bounded_bytes


def _base_ash_directory() -> Path:
    return Path.home() / ".ash"


def _profile_metadata(name: str, *, base_directory: Path) -> dict[str, Any]:
    normalized = validate_profile_name(name)
    directory = profile_directory(normalized, ash_dir=base_directory)
    env_path = directory / ".env"
    config_path = directory / "ash.toml"
    model = ""
    if env_path.is_file():
        try:
            raw = read_bounded_bytes(env_path, 1024 * 1024, label="profile dotenv file")
            for line in raw.decode("utf-8").splitlines():
                key, separator, value = line.partition("=")
                if separator and key.strip() == "ASH_MODEL":
                    model = value.strip()
                    break
        except (OSError, UnicodeError, ValueError):
            model = ""
    return {
        "name": normalized,
        "active": normalized == active_profile_name(ash_dir=base_directory),
        "directory": str(directory),
        "config_file": str(config_path),
        "config_present": config_path.is_file(),
        "credentials_file": str(env_path),
        "credentials_present": env_path.is_file(),
        "model": model,
    }


def profile_catalog_payload() -> dict[str, Any]:
    """Return profile inventory without exposing credential values."""

    base_directory = _base_ash_directory()
    return {
        "active": active_profile_name(ash_dir=base_directory),
        "profiles": [
            _profile_metadata(name, base_directory=base_directory)
            for name in list_profile_names(ash_dir=base_directory)
        ],
    }


def render_profile_list(*, json_output: bool = False) -> str:
    payload = profile_catalog_payload()
    if json_output:
        return json.dumps(payload, indent=2, sort_keys=True)
    lines = [f"Active profile: {payload['active']}", ""]
    for item in payload["profiles"]:
        marker = " *" if item["active"] else ""
        model = f" model={item['model']}" if item["model"] else ""
        lines.append(
            f"{item['name']}{marker}:{model} "
            f"config={'yes' if item['config_present'] else 'no'} "
            f"credentials={'yes' if item['credentials_present'] else 'no'}"
        )
    return "\n".join(lines)


def render_profile_show(name: str, *, json_output: bool = False) -> str:
    base_directory = _base_ash_directory()
    normalized = validate_profile_name(name)
    if not profile_exists(normalized, ash_dir=base_directory):
        raise ValueError(f"profile does not exist: {normalized}")
    payload = _profile_metadata(normalized, base_directory=base_directory)
    if json_output:
        return json.dumps(payload, indent=2, sort_keys=True)
    return "\n".join(
        [
            f"Profile: {payload['name']}",
            f"Active: {'yes' if payload['active'] else 'no'}",
            f"Model: {payload['model'] or 'not configured'}",
            f"Config: {payload['config_file']}",
            f"Credentials: {payload['credentials_file']}",
        ]
    )


def add_profile(name: str) -> str:
    base_directory = _base_ash_directory()
    normalized = validate_profile_name(name)
    if normalized == DEFAULT_PROFILE:
        raise ValueError("the default profile already exists")
    directory = profile_directory(normalized, ash_dir=base_directory)
    if directory.exists():
        raise ValueError(f"profile already exists: {normalized}")
    directory.mkdir(parents=True, mode=0o700)
    return normalized


def use_profile(name: str) -> str:
    base_directory = _base_ash_directory()
    normalized = validate_profile_name(name)
    if not profile_exists(normalized, ash_dir=base_directory):
        raise ValueError(f"profile does not exist: {normalized}; run `ash profile add {normalized}`")
    return set_active_profile(normalized, ash_dir=base_directory)


def remove_profile(name: str, *, confirmed: bool) -> str:
    base_directory = _base_ash_directory()
    normalized = validate_profile_name(name)
    if normalized == DEFAULT_PROFILE:
        raise ValueError("the default profile cannot be removed")
    directory = profile_directory(normalized, ash_dir=base_directory)
    if not directory.is_dir():
        raise ValueError(f"profile does not exist: {normalized}")
    if not confirmed:
        raise ValueError("removing a profile requires --yes")
    if active_profile_name(ash_dir=base_directory) == normalized:
        set_active_profile(DEFAULT_PROFILE, ash_dir=base_directory)
    shutil.rmtree(directory)
    return normalized


__all__ = [
    "add_profile",
    "profile_catalog_payload",
    "remove_profile",
    "render_profile_list",
    "render_profile_show",
    "use_profile",
]
