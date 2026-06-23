"""Top-level CLI helpers for persisted project permission grants."""

from __future__ import annotations

import json
from pathlib import Path

from safety.grants import clear_tool_grants, load_tool_grants, set_tool_grant
from safety.trust import canonical_workspace


def permission_grants_payload(workspace: Path, grants: set[str]) -> dict:
    return {
        "workspace": canonical_workspace(workspace),
        "persistent_grants": sorted(grants),
    }


def render_permission_grants(
    workspace: Path,
    grants: set[str],
    *,
    json_output: bool = False,
) -> str:
    payload = permission_grants_payload(workspace, grants)
    if json_output:
        return json.dumps(payload, sort_keys=True)
    grants_text = ", ".join(payload["persistent_grants"]) or "(none)"
    return f"Workspace: {payload['workspace']}\nPersistent grants: {grants_text}"


def allow_permission_grant(workspace: Path, tool_name: str) -> set[str]:
    set_tool_grant(workspace, tool_name, True)
    return load_tool_grants(workspace)


def revoke_permission_grant(workspace: Path, tool_name: str) -> set[str]:
    set_tool_grant(workspace, tool_name, False)
    return load_tool_grants(workspace)


def clear_permission_grants(workspace: Path) -> set[str]:
    clear_tool_grants(workspace)
    return load_tool_grants(workspace)
