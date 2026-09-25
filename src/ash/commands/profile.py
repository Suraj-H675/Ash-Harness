"""Named profile administration for user-owned Ash state."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from ash.plugins.anchored_fs import AnchoredDirectory, AnchoredFilesystemError
from ash.profiles import (
    DEFAULT_PROFILE,
    _read_active_profile_from_directory,
    _write_active_profile_to_directory,
    active_profile_name,
    list_profile_names,
    profile_directory,
    profile_exists,
    set_active_profile,
    validate_profile_name,
)
from ash.safe_io import (
    anchored_regular_file_exists,
    read_bounded_open_file,
)
from ash.ui.safe_text import terminal_safe_text


def _base_ash_directory() -> Path:
    return Path.home() / ".ash"


def _open_profiles_directory(
    base: AnchoredDirectory,
    base_directory: Path,
    *,
    create: bool,
) -> AnchoredDirectory:
    metadata = base.stat("profiles")
    if metadata is None:
        if not create:
            raise FileNotFoundError(base_directory / "profiles")
        created = base.create_child("profiles")
        try:
            metadata = os.fstat(created.descriptor)
        finally:
            created.close()
    elif stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"symlinked profiles directory: {base_directory / 'profiles'}")
    elif not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(
            f"profiles directory is not a directory: {base_directory / 'profiles'}"
        )
    return AnchoredDirectory.open(
        base_directory / "profiles",
        create=False,
        private=False,
        expected=metadata,
        pin_path=True,
    )


def _profile_metadata(name: str, *, base_directory: Path) -> dict[str, Any]:
    normalized = validate_profile_name(name)
    directory = profile_directory(normalized, ash_dir=base_directory)
    env_path = directory / ".env"
    config_path = directory / "ash.toml"
    model = ""
    trusted_root = base_directory.parent
    try:
        raw = read_bounded_open_file(
            env_path,
            1024 * 1024,
            label="profile dotenv file",
            trusted_root=trusted_root,
        )
        for line in raw.decode("utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "ASH_MODEL":
                model = value.strip()
                break
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        model = ""
    return {
        "name": normalized,
        "active": normalized == active_profile_name(ash_dir=base_directory),
        "directory": str(directory),
        "config_file": str(config_path),
        "config_present": anchored_regular_file_exists(
            config_path,
            trusted_root=trusted_root,
            label="profile config file",
        ),
        "credentials_file": str(env_path),
        "credentials_present": anchored_regular_file_exists(
            env_path,
            trusted_root=trusted_root,
            label="profile credential file",
        ),
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
        model = (
            " model=" + terminal_safe_text(str(item["model"]), single_line=True)
            if item["model"]
            else ""
        )
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
    display_model = terminal_safe_text(
        str(payload["model"] or "not configured"), single_line=True
    )
    return "\n".join(
        [
            f"Profile: {payload['name']}",
            f"Active: {'yes' if payload['active'] else 'no'}",
            f"Model: {display_model}",
            f"Config: {payload['config_file']}",
            f"Credentials: {payload['credentials_file']}",
        ]
    )


def add_profile(name: str) -> str:
    base_directory = _base_ash_directory()
    normalized = validate_profile_name(name)
    if normalized == DEFAULT_PROFILE:
        raise ValueError("the default profile already exists")
    try:
        with AnchoredDirectory.open(
            base_directory,
            create=True,
            private=True,
            pin_path=True,
        ) as base:
            with _open_profiles_directory(
                base,
                base_directory,
                create=True,
            ) as profiles:
                profiles.chmod(0o700)
                profiles.validation_path()
                if profiles.stat(normalized) is not None:
                    raise ValueError(f"profile already exists: {normalized}")
                child = profiles.create_child(normalized)
                try:
                    child.chmod(0o700)
                    child.validation_path()
                finally:
                    child.close()
                profiles.validation_path()
                base.validation_path()
    except FileExistsError as exc:
        raise ValueError(f"profile already exists: {normalized}") from exc
    except AnchoredFilesystemError as exc:
        detail = str(exc)
        lowered = detail.casefold()
        profiles_path = base_directory / "profiles"
        profiles_is_link = profiles_path.is_symlink() or (
            hasattr(profiles_path, "is_junction") and profiles_path.is_junction()
        )
        if "link" in lowered or "reparse" in lowered or profiles_is_link:
            raise ValueError(f"symlinked profiles directory: {profiles_path}") from exc
        raise ValueError(f"cannot create profile {normalized}: {exc}") from exc
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
    try:
        with AnchoredDirectory.open(
            base_directory,
            create=False,
            private=False,
            pin_path=True,
        ) as base:
            with _open_profiles_directory(
                base,
                base_directory,
                create=False,
            ) as profiles:
                profiles.validation_path()
                metadata = profiles.stat(normalized)
                if metadata is None:
                    raise ValueError(f"profile does not exist: {normalized}")
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise ValueError(f"profile does not exist: {normalized}")
                if not confirmed:
                    raise ValueError("removing a profile requires --yes")

                if "ASH_PROFILE" in os.environ:
                    active = validate_profile_name(
                        str(os.environ["ASH_PROFILE"]).strip() or DEFAULT_PROFILE
                    )
                else:
                    active = _read_active_profile_from_directory(base, base_directory)
                if active == normalized:
                    _write_active_profile_to_directory(
                        base,
                        base_directory,
                        DEFAULT_PROFILE,
                    )

                base.validation_path()
                profiles.validation_path()
                target = profiles.child(normalized, expected=metadata)
                try:
                    if not profiles.same_entry(normalized, target.descriptor):
                        raise ValueError(f"profile changed before removal: {normalized}")
                    profiles.remove_tree(
                        normalized,
                        expected_descriptor=target.descriptor,
                    )
                finally:
                    target.close()
                profiles.validation_path()
                base.validation_path()
    except FileNotFoundError as exc:
        raise ValueError(f"profile does not exist: {normalized}") from exc
    except AnchoredFilesystemError as exc:
        detail = str(exc)
        lowered = detail.casefold()
        profiles_path = base_directory / "profiles"
        profiles_is_link = profiles_path.is_symlink() or (
            hasattr(profiles_path, "is_junction") and profiles_path.is_junction()
        )
        if "link" in lowered or "reparse" in lowered or profiles_is_link:
            raise ValueError(f"symlinked profiles directory: {profiles_path}") from exc
        raise ValueError(f"cannot remove profile {normalized}: {exc}") from exc
    return normalized


__all__ = [
    "add_profile",
    "profile_catalog_payload",
    "remove_profile",
    "render_profile_list",
    "render_profile_show",
    "use_profile",
]
