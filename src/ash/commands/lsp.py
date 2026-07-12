"""Operator commands for managed language servers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ash.config import AshConfig
from ash.lsp.config import lsp_command_available, load_lsp_server_configs
from ash.lsp.manager import LanguageServerManager
from ash.safety.trust import is_workspace_trusted


async def inspect_lsp(
    config: AshConfig,
    *,
    action: str,
    file_path: str = "",
    operation: str = "",
    line: int = 1,
    character: int = 1,
    query: str = "",
) -> dict[str, Any]:
    workspace = config.workspace_root.resolve()
    trusted = is_workspace_trusted(workspace)
    if not config.lsp_enabled:
        return _status_payload(workspace, trusted, False, {})
    if not trusted:
        if action != "status":
            raise ValueError(
                "managed LSP is disabled for untrusted workspaces; run `ash trust add` first"
            )
        return _status_payload(workspace, False, True, {})

    configs = load_lsp_server_configs(workspace, include_project=True)
    if action == "status":
        return _status_payload(workspace, True, True, configs)
    selected_operation = "diagnostics" if action == "diagnostics" else operation
    if not selected_operation:
        raise ValueError("an LSP query operation is required")
    manager = LanguageServerManager(workspace, configs)
    try:
        result = await manager.query(
            selected_operation,
            file_path=file_path,
            line=line,
            character=character,
            query=query,
        )
    finally:
        await manager.aclose()
    return {
        "schema_version": 1,
        "workspace": str(workspace),
        "operation": selected_operation,
        "result": result,
    }


def render_lsp(payload: dict[str, Any], *, json_output: bool) -> str:
    if json_output:
        return json.dumps(payload, indent=2, sort_keys=True)
    if "servers" in payload:
        if not payload["enabled"]:
            return "Managed LSP is disabled by configuration."
        if not payload["trusted"]:
            return "Managed LSP is disabled because the workspace is untrusted."
        servers = payload["servers"]
        if not servers:
            return "No supported language servers are installed or configured."
        lines = ["Managed language servers:"]
        for server in servers:
            extensions = ", ".join(server["extensions"])
            lines.append(
                f"  {server['name']}: {server['command']} "
                f"[{extensions}] ({server['source']}; "
                f"{'executable found' if server['executable_available'] else 'executable missing'})"
            )
        return "\n".join(lines)
    return json.dumps(payload["result"], indent=2, sort_keys=True)


def _status_payload(
    workspace: Path,
    trusted: bool,
    enabled: bool,
    configs: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workspace": str(workspace),
        "enabled": enabled,
        "trusted": trusted,
        "servers": [
            {
                "name": name,
                "command": server.command[0],
                "argument_count": len(server.command) - 1,
                "executable_available": lsp_command_available(server, workspace),
                "extensions": sorted(server.extensions),
                "source": server.source,
            }
            for name, server in sorted(configs.items())
        ],
    }
