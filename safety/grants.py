"""Atomic project-scoped persistent tool grants."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from safety.trust import canonical_workspace


def grants_path() -> Path:
    return Path.home() / ".ash" / "permission-grants.json"


def load_tool_grants(workspace: Path) -> set[str]:
    path = grants_path()
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    values = payload.get("workspaces", {}).get(canonical_workspace(workspace), [])
    return {str(value) for value in values if isinstance(value, str)}


def set_tool_grant(workspace: Path, tool_name: str, allowed: bool) -> None:
    if not tool_name or any(character.isspace() for character in tool_name):
        raise ValueError("tool name must be a non-empty identifier")
    path = grants_path()
    payload: dict = {"version": 1, "workspaces": {}}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, json.JSONDecodeError):
            pass
    workspaces = payload.setdefault("workspaces", {})
    key = canonical_workspace(workspace)
    grants = {str(value) for value in workspaces.get(key, [])}
    grants.add(tool_name) if allowed else grants.discard(tool_name)
    if grants:
        workspaces[key] = sorted(grants)
    else:
        workspaces.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
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
