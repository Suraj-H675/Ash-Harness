"""Semantic validation for installed plugin component trees and snapshots."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ash.commands.custom_commands import (
    CommandSource,
    CustomCommandCatalog,
    parse_custom_command_bytes,
)
from ash.hooks.config import (
    MAX_HOOK_CONFIG_BYTES,
    HookConfigSource,
    load_command_hooks,
    validate_command_hooks_payload,
)
from ash.mcp.server import load_mcp_servers, parse_mcp_servers_payload
from ash.plugins.agents import (
    AgentCatalog,
    AgentSource,
    parse_agent_definition_bytes,
)
from ash.plugins.errors import PluginLifecycleError
from ash.plugins.manifest import PluginManifest
from ash.plugins.registry import DiscoveredPlugin
from ash.plugins.skills import (
    SkillCatalog,
    SkillSource,
    parse_instruction_skill_bytes,
)
from ash.plugins.snapshot import PluginSnapshot, SnapshotEntry
from ash.safe_io import strict_json_loads

def validate_plugin_contents(root: Path, manifest: PluginManifest) -> None:
    plugin = DiscoveredPlugin(manifest, root, "validation")
    skills = SkillCatalog((SkillSource(plugin.skill_paths(), manifest.name),))
    skills.discover()
    commands = CustomCommandCatalog(
        (
            CommandSource(
                plugin.command_paths(),
                source=f"plugin:{manifest.name}",
                namespace=manifest.name,
            ),
        )
    )
    commands.discover()
    agents = AgentCatalog((AgentSource(plugin.agent_paths(), manifest.name),))
    agents.discover()
    errors = [
        *skills.errors.values(),
        *commands.errors.values(),
        *agents.errors.values(),
    ]
    if errors:
        raise PluginLifecycleError(errors[0])
    try:
        load_command_hooks(
            [
                HookConfigSource(
                    path,
                    cwd=root,
                    environment=(("ASH_PLUGIN_ROOT", str(root)),),
                    trusted_root=root,
                )
                for path in plugin.hook_paths()
            ]
        )
        for path in plugin.mcp_paths():
            load_mcp_servers(
                path,
                namespace=manifest.name,
                cwd=root,
                environment={"ASH_PLUGIN_ROOT": str(root)},
            )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise PluginLifecycleError(f"invalid plugin component: {exc}") from exc


def validate_plugin_contents_at(
    snapshot: PluginSnapshot,
    manifest: PluginManifest,
) -> None:
    """Run semantic plugin validation against immutable snapshot bytes."""

    _validate_plugin_contents_snapshot(snapshot, manifest)


def _validate_plugin_contents_snapshot(
    snapshot: PluginSnapshot,
    manifest: PluginManifest,
) -> None:
    """Validate every plugin component without reopening the live filesystem."""

    try:
        skill_names: dict[str, Path] = {}
        skill_roots: list[str | dict[str, Any]] = (
            list(manifest.skills)
            if manifest.skills
            else ["SKILL.md", "skills"]
        )
        for entry in _snapshot_skill_entries(
            snapshot,
            skill_roots,
        ):
            path = _snapshot_skill_path(snapshot, entry, manifest.name)
            skill = parse_instruction_skill_bytes(
                snapshot.read_bytes(entry.relative),
                path,
                namespace=manifest.name,
            )
            previous = skill_names.get(skill.name)
            if previous is not None:
                raise PluginLifecycleError(
                    f"duplicate skill name {skill.name!r}; already provided by {previous}"
                )
            skill_names[skill.name] = path

        command_names: dict[str, Path] = {}
        command_roots: list[str | dict[str, Any]] = (
            list(manifest.commands) if manifest.commands else ["commands"]
        )
        for entry, root_relative in _snapshot_markdown_entries(snapshot, command_roots):
            path = snapshot.display_path(entry.relative)
            command = parse_custom_command_bytes(
                snapshot.read_bytes(entry.relative),
                path,
                snapshot.display_path(root_relative),
                f"plugin:{manifest.name}",
                namespace=manifest.name,
            )
            previous = command_names.get(command.name)
            if previous is not None:
                raise PluginLifecycleError(
                    f"duplicate command name {command.name!r}; already provided by {previous}"
                )
            command_names[command.name] = path

        agent_names: dict[str, Path] = {}
        agent_roots: list[str | dict[str, Any]] = (
            list(manifest.agents) if manifest.agents else ["agents"]
        )
        for entry, _root_relative in _snapshot_markdown_entries(snapshot, agent_roots):
            path = snapshot.display_path(entry.relative)
            agent = parse_agent_definition_bytes(
                snapshot.read_bytes(entry.relative),
                path,
                namespace=manifest.name,
            )
            previous = agent_names.get(agent.name)
            if previous is not None:
                raise PluginLifecycleError(
                    f"duplicate agent name {agent.name!r}; already provided by {previous}"
                )
            agent_names[agent.name] = path

        hook_roots: list[str | dict[str, Any]] = (
            list(manifest.hooks) if manifest.hooks else ["hooks/hooks.json"]
        )
        for entry in _snapshot_file_entries(snapshot, hook_roots):
            path = snapshot.display_path(entry.relative)
            payload = strict_json_loads(
                snapshot.read_bytes(entry.relative, max_bytes=MAX_HOOK_CONFIG_BYTES)
            )
            validate_command_hooks_payload(payload, path)

        mcp_roots: list[str | dict[str, Any]] = (
            list(manifest.mcp_servers) if manifest.mcp_servers else [".mcp.json"]
        )
        for entry in _snapshot_file_entries(snapshot, mcp_roots):
            path = snapshot.display_path(entry.relative)
            payload = strict_json_loads(snapshot.read_bytes(entry.relative))
            parse_mcp_servers_payload(
                payload,
                path,
                namespace=manifest.name,
                cwd=snapshot.root,
                environment={"ASH_PLUGIN_ROOT": str(snapshot.root)},
            )
    except PluginLifecycleError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PluginLifecycleError(f"invalid plugin component: {exc}") from exc


def _snapshot_file_entries(
    snapshot: PluginSnapshot,
    roots: Iterable[str | dict[str, Any]],
) -> list[SnapshotEntry]:
    entries: list[SnapshotEntry] = []
    for raw_root in roots:
        if not isinstance(raw_root, str):
            raise PluginLifecycleError("inline plugin component declarations are unsupported")
        relative = _snapshot_parts(raw_root)
        entry = snapshot.entry(relative)
        if entry is None:
            continue
        if entry.kind == "file":
            entries.append(entry)
    return entries


def _snapshot_skill_entries(
    snapshot: PluginSnapshot,
    roots: Iterable[str | dict[str, Any]],
) -> list[SnapshotEntry]:
    entries: list[SnapshotEntry] = []
    for raw_root in roots:
        if not isinstance(raw_root, str):
            raise PluginLifecycleError("inline plugin component declarations are unsupported")
        relative = _snapshot_parts(raw_root)
        entry = snapshot.entry(relative)
        if entry is None:
            continue
        if entry.kind == "file":
            if entry.relative[-1] == "SKILL.md":
                entries.append(entry)
            continue
        pending: list[tuple[tuple[str, ...], int]] = [(relative, 0)]
        while pending:
            current, depth = pending.pop()
            manifest_entry = snapshot.entry((*current, "SKILL.md"))
            if manifest_entry is not None:
                if manifest_entry.kind == "file":
                    entries.append(manifest_entry)
                continue
            for child in snapshot.children(current):
                name = child.relative[-1]
                if name.startswith(".") or name == "node_modules":
                    continue
                if child.kind == "file":
                    if name == "SKILL.md":
                        entries.append(child)
                    continue
                if depth < 32:
                    pending.append((child.relative, depth + 1))
    return entries


def _snapshot_skill_path(
    snapshot: PluginSnapshot,
    entry: SnapshotEntry,
    plugin_name: str,
) -> Path:
    """Give a root-level skill the same parent-name semantics as installation."""

    if entry.relative == ("SKILL.md",):
        return snapshot.root / plugin_name / "SKILL.md"
    return snapshot.display_path(entry.relative)


def _snapshot_markdown_entries(
    snapshot: PluginSnapshot,
    roots: Iterable[str | dict[str, Any]],
) -> list[tuple[SnapshotEntry, tuple[str, ...]]]:
    entries: list[tuple[SnapshotEntry, tuple[str, ...]]] = []
    for raw_root in roots:
        if not isinstance(raw_root, str):
            raise PluginLifecycleError("inline plugin component declarations are unsupported")
        relative = _snapshot_parts(raw_root)
        entry = snapshot.entry(relative)
        if entry is None:
            continue
        if entry.kind == "file":
            if Path(entry.relative[-1]).suffix.casefold() == ".md":
                entries.append((entry, relative[:-1]))
            continue
        pending = [relative]
        while pending:
            current = pending.pop()
            for child in snapshot.children(current):
                if child.kind == "directory":
                    pending.append(child.relative)
                elif Path(child.relative[-1]).suffix.casefold() == ".md":
                    entries.append((child, relative))
    return entries


def _snapshot_parts(relative: str) -> tuple[str, ...]:
    candidate = Path(relative)
    if candidate.is_absolute() or not candidate.parts:
        raise PluginLifecycleError(f"component path escapes plugin root: {relative}")
    parts = tuple(candidate.parts)
    if any(not part or part in {".", ".."} for part in parts):
        raise PluginLifecycleError(f"component path escapes plugin root: {relative}")
    return parts
