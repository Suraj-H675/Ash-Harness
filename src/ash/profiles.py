"""Safe paths and active-state management for named Ash profiles."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path

from ash.plugins.anchored_fs import AnchoredDirectory, AnchoredFilesystemError
from ash.safe_io import (
    anchored_directory_exists,
    list_anchored_directory,
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


def _active_profile_root(ash_dir: Path | None) -> Path:
    return ash_dir or (Path.home() / ".ash")


def _active_profile_state_error(root: Path, exc: BaseException) -> ValueError:
    detail = str(exc)
    lowered = detail.casefold()
    root_is_link = root.is_symlink() or (
        hasattr(root, "is_junction") and root.is_junction()
    )
    if "link" in lowered or "reparse" in lowered or root_is_link:
        return ValueError(f"refusing to use symlinked Ash state directory: {root}")
    return ValueError(f"cannot use Ash state directory {root}: {detail}")


def _read_active_profile_from_directory(
    directory: AnchoredDirectory,
    root: Path,
) -> str:
    marker = root / _ACTIVE_PROFILE_FILENAME
    metadata = directory.stat(_ACTIVE_PROFILE_FILENAME)
    if metadata is None:
        return DEFAULT_PROFILE
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"symlinked active Ash profile marker: {marker}")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"active Ash profile marker is not regular: {marker}")
    raw = directory.read_file(
        _ACTIVE_PROFILE_FILENAME,
        max_bytes=MAX_ACTIVE_PROFILE_BYTES,
    )
    assert raw is not None
    return validate_profile_name(raw.decode("utf-8").strip() or DEFAULT_PROFILE)


def _write_active_profile_to_directory(
    directory: AnchoredDirectory,
    root: Path,
    normalized: str,
) -> None:
    marker = root / _ACTIVE_PROFILE_FILENAME
    existing = directory.stat(_ACTIVE_PROFILE_FILENAME)
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode):
            raise ValueError(f"symlinked active Ash profile marker: {marker}")
        if not stat.S_ISREG(existing.st_mode):
            raise ValueError(f"active Ash profile marker is not regular: {marker}")
    temporary_name = directory.unique_name(
        f".{_ACTIVE_PROFILE_FILENAME}.", ".tmp"
    )
    descriptor = directory.create_file(temporary_name, mode=0o600)
    renamed = False
    completed = False
    try:
        payload = (normalized + "\n").encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while writing active profile marker")
            view = view[written:]
        os.fsync(descriptor)
        directory.validation_path()
        directory.rename(
            temporary_name,
            _ACTIVE_PROFILE_FILENAME,
            expected_source_descriptor=descriptor,
        )
        renamed = True
        directory.validation_path()
        if os.name != "nt":
            directory.sync()
        completed = True
    finally:
        if not completed:
            cleanup_name = _ACTIVE_PROFILE_FILENAME if renamed else temporary_name
            try:
                directory.unlink(
                    cleanup_name,
                    missing_ok=True,
                    expected_descriptor=descriptor,
                )
            except BaseException:
                pass
        os.close(descriptor)


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

    root = _active_profile_root(ash_dir)
    marker = root / _ACTIVE_PROFILE_FILENAME
    try:
        with AnchoredDirectory.open(
            root,
            create=False,
            private=False,
            pin_path=True,
        ) as directory:
            directory.validation_path()
            value = _read_active_profile_from_directory(directory, root)
            directory.validation_path()
    except FileNotFoundError:
        return DEFAULT_PROFILE
    except AnchoredFilesystemError as exc:
        raise _active_profile_state_error(root, exc) from exc
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read active Ash profile marker {marker}: {exc}") from exc
    return value


def set_active_profile(name: str, *, ash_dir: Path | None = None) -> str:
    """Persist the active profile marker atomically and return its normalized name."""

    normalized = validate_profile_name(name)
    root = _active_profile_root(ash_dir)
    try:
        with AnchoredDirectory.open(
            root,
            create=True,
            private=True,
            pin_path=True,
        ) as directory:
            _write_active_profile_to_directory(directory, root, normalized)
    except AnchoredFilesystemError as exc:
        raise _active_profile_state_error(root, exc) from exc
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
