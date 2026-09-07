"""Persistent canonical workspace trust decisions."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ash.safe_io import read_bounded_bytes


MAX_TRUST_STORE_BYTES = 1_000_000


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def trust_store_path() -> Path:
    return Path.home() / ".ash" / "trusted-workspaces.json"


def _validate_trust_store_path(path: Path) -> None:
    if _is_link(path) or _is_link(path.parent):
        raise ValueError(f"refusing to use linked workspace trust state: {path}")


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
        _validate_trust_store_path(path)
    except ValueError:
        return set()
    if not path.exists():
        return set()
    try:
        raw = read_bounded_bytes(
            path,
            MAX_TRUST_STORE_BYTES,
            label="trusted workspace store",
        )
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return set()
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
    entries = load_trusted_workspaces()
    changed = canonical not in entries if trusted else canonical in entries
    if trusted:
        entries.add(canonical)
    else:
        entries.discard(canonical)
    _save(entries)
    return changed


def _save(entries: set[str]) -> None:
    path = trust_store_path()
    _validate_trust_store_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_trust_store_path(path)
    if os.name != "nt":
        path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "workspaces": sorted(entries)}, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
