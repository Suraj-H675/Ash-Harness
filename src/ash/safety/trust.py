"""Persistent canonical workspace trust decisions."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from ash.plugins.anchored_fs import AnchoredDirectory, AnchoredFilesystemError


MAX_TRUST_STORE_BYTES = 1_000_000


def trust_store_path() -> Path:
    return Path.home() / ".ash" / "trusted-workspaces.json"


def canonical_workspace(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        value[key] = item
    return value


def load_trusted_workspaces() -> set[str]:
    path = trust_store_path()
    try:
        with AnchoredDirectory.open(
            path.parent,
            create=False,
            private=False,
            pin_path=True,
        ) as directory:
            return _load_from_directory(directory, path.name)
    except (
        FileNotFoundError,
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        AnchoredFilesystemError,
    ):
        return set()


def _load_from_directory(directory: AnchoredDirectory, name: str) -> set[str]:
    raw = directory.read_file(name, max_bytes=MAX_TRUST_STORE_BYTES)
    if raw is None:
        return set()
    payload = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_json_object,
    )
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return set()
    entries = payload.get("workspaces")
    if not isinstance(entries, list) or any(
        not isinstance(entry, str) for entry in entries
    ):
        return set()
    return set(entries)


def is_workspace_trusted(path: str | Path) -> bool:
    return canonical_workspace(path) in load_trusted_workspaces()


def set_workspace_trusted(path: str | Path, trusted: bool) -> bool:
    canonical = canonical_workspace(path)
    state_path = trust_store_path()
    try:
        with AnchoredDirectory.open(
            state_path.parent,
            create=True,
            private=False,
            pin_path=True,
        ) as directory:
            if os.name != "nt":
                directory.chmod(0o700)
            entries = _load_from_directory(directory, state_path.name)
            changed = canonical not in entries if trusted else canonical in entries
            if trusted:
                entries.add(canonical)
            else:
                entries.discard(canonical)
            _save(directory, state_path.name, entries)
            return changed
    except AnchoredFilesystemError as exc:
        if "link" in str(exc).casefold() or "reparse" in str(exc).casefold():
            raise ValueError(
                f"refusing to use linked workspace trust state: {state_path}"
            ) from exc
        raise ValueError(f"refusing to use workspace trust state: {state_path}: {exc}") from exc


def _save(
    directory: AnchoredDirectory,
    name: str,
    entries: set[str],
) -> None:
    payload = (
        json.dumps(
            {"version": 1, "workspaces": sorted(entries)},
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    existing = directory.stat(name)
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode):
            raise ValueError(
                f"refusing to use linked workspace trust state: {directory.path / name}"
            )
        if not stat.S_ISREG(existing.st_mode):
            raise ValueError(
                f"refusing to use non-regular workspace trust state: {directory.path / name}"
            )
    temporary_name = directory.unique_name(f".{name}.", ".tmp")
    descriptor = directory.create_file(temporary_name, mode=0o600)
    renamed = False
    completed = False
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while writing workspace trust state")
            view = view[written:]
        os.fsync(descriptor)
        directory.validation_path()
        directory.rename(
            temporary_name,
            name,
            expected_source_descriptor=descriptor,
        )
        renamed = True
        directory.validation_path()
        if os.name != "nt":
            directory.sync()
        completed = True
    finally:
        if not completed:
            cleanup_name = name if renamed else temporary_name
            try:
                directory.unlink(
                    cleanup_name,
                    missing_ok=True,
                    expected_descriptor=descriptor,
                )
            except BaseException:
                pass
        os.close(descriptor)
