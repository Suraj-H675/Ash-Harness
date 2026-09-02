"""Persistent canonical workspace trust decisions."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


MAX_TRUST_STORE_BYTES = 1_000_000


def trust_store_path() -> Path:
    return Path.home() / ".ash" / "trusted-workspaces.json"


def canonical_workspace(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def load_trusted_workspaces() -> set[str]:
    path = trust_store_path()
    if not path.exists():
        return set()
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_TRUST_STORE_BYTES + 1)
        if len(raw) > MAX_TRUST_STORE_BYTES:
            return set()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set()
    entries = payload.get("workspaces", []) if isinstance(payload, dict) else []
    return {str(entry) for entry in entries if isinstance(entry, str)}


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
    path.parent.mkdir(parents=True, exist_ok=True)
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
