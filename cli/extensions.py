"""Safe extension inventory for skills, plugins, and hooks."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from mcp.server import load_mcp_servers
from plugins.agents import AgentCatalog, AgentSource
from plugins.lifecycle import (
    PluginLifecycleError,
    install_local_plugin,
    load_extension_state,
    set_plugin_enabled,
    uninstall_local_plugin,
    user_plugin_root,
)
from plugins.registry import PluginCatalog
from plugins.skills import SkillCatalog, SkillSource
from safety.trust import canonical_workspace, is_workspace_trusted

ExtensionKind = Literal["all", "skills", "agents", "plugins", "hooks"]
PluginAction = Literal["install", "enable", "disable", "uninstall"]


@dataclass(frozen=True)
class SkillSummary:
    name: str
    description: str
    path: str


@dataclass(frozen=True)
class AgentSummary:
    name: str
    description: str
    base_role: str
    path: str


@dataclass(frozen=True)
class PluginSummary:
    name: str
    version: str
    description: str
    source: str
    root: str
    skills: tuple[str, ...]
    commands: tuple[str, ...]
    hooks: tuple[str, ...]
    mcp_servers: tuple[str, ...]
    agents: tuple[str, ...]
    enabled: bool


@dataclass(frozen=True)
class HookConfigSummary:
    path: str
    source: str
    pre_tool: int = 0
    post_tool: int = 0
    session_start: int = 0


@dataclass(frozen=True)
class ExtensionInventory:
    workspace: str
    project_trusted: bool
    skills: tuple[SkillSummary, ...]
    agents: tuple[AgentSummary, ...]
    plugins: tuple[PluginSummary, ...]
    hooks: tuple[HookConfigSummary, ...]
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "project_trusted": self.project_trusted,
            "skills": [asdict(item) for item in self.skills],
            "agents": [asdict(item) for item in self.agents],
            "plugins": [asdict(item) for item in self.plugins],
            "hooks": [asdict(item) for item in self.hooks],
            "errors": list(self.errors),
        }


def discover_extensions(workspace: Path) -> ExtensionInventory:
    trusted = is_workspace_trusted(workspace)
    user_skill_root = Path.home() / ".ash" / "skills"
    plugin_roots = [(Path.home() / ".ash" / "plugins", "user")]
    hook_paths = [(Path.home() / ".ash" / "hooks.json", "user")]
    if trusted:
        plugin_roots.append((workspace / ".ash" / "plugins", "project"))
        hook_paths.append((workspace / ".ash" / "hooks.json", "project"))

    state_errors: list[str] = []
    try:
        disabled_plugins = load_extension_state().disabled_plugins
    except PluginLifecycleError as exc:
        disabled_plugins = frozenset()
        state_errors.append(str(exc))
    plugin_catalog = PluginCatalog(
        tuple(plugin_roots), disabled_plugins=disabled_plugins
    )
    discovered_plugins = plugin_catalog.discover(include_disabled=True)
    hook_paths.extend(
        (path, f"plugin:{plugin.manifest.name}")
        for plugin in discovered_plugins
        if plugin.enabled
        for path in plugin.hook_paths()
    )
    skill_roots: list[Path | SkillSource] = [user_skill_root]
    if trusted:
        skill_roots.append(workspace / ".ash" / "skills")
    skill_roots.extend(
        SkillSource(
            paths=plugin.skill_paths(),
            namespace=plugin.manifest.name,
        )
        for plugin in discovered_plugins
        if plugin.enabled
    )
    skill_catalog = SkillCatalog(tuple(skill_roots))
    discovered_skills = skill_catalog.discover()
    agent_sources: list[Path | AgentSource] = [Path.home() / ".ash" / "agents"]
    if trusted:
        agent_sources.append(workspace / ".ash" / "agents")
    agent_sources.extend(
        AgentSource(
            paths=plugin.agent_paths(),
            namespace=plugin.manifest.name,
        )
        for plugin in discovered_plugins
        if plugin.enabled
    )
    agent_catalog = AgentCatalog(tuple(agent_sources))
    discovered_agents = agent_catalog.discover()

    errors = state_errors + [
        f"Invalid plugin {path}: {error}"
        for path, error in sorted(plugin_catalog.errors.items())
    ]
    errors.extend(
        f"Invalid skill {path}: {error}"
        for path, error in sorted(skill_catalog.errors.items())
    )
    errors.extend(
        f"Invalid agent {path}: {error}"
        for path, error in sorted(agent_catalog.errors.items())
    )
    hooks, hook_errors = _discover_hooks(hook_paths)
    errors.extend(hook_errors)
    for plugin in discovered_plugins:
        if not plugin.enabled:
            continue
        for path in plugin.mcp_paths():
            try:
                load_mcp_servers(
                    path,
                    namespace=plugin.manifest.name,
                    cwd=plugin.root,
                    environment={"ASH_PLUGIN_ROOT": str(plugin.root)},
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"Invalid plugin MCP config {path}: {exc}")

    return ExtensionInventory(
        workspace=canonical_workspace(workspace),
        project_trusted=trusted,
        skills=tuple(
            SkillSummary(
                name=skill.name,
                description=skill.description,
                path=str(skill.path),
            )
            for skill in sorted(discovered_skills, key=lambda item: item.name)
        ),
        agents=tuple(
            AgentSummary(
                name=agent.name,
                description=agent.description,
                base_role=agent.base_role,
                path=str(agent.path),
            )
            for agent in sorted(discovered_agents, key=lambda item: item.name)
        ),
        plugins=tuple(
            PluginSummary(
                name=plugin.manifest.name,
                version=plugin.manifest.version,
                description=plugin.manifest.description,
                source=plugin.source,
                root=str(plugin.root),
                skills=tuple(plugin.manifest.skills),
                commands=tuple(
                    item for item in plugin.manifest.commands if isinstance(item, str)
                ),
                hooks=tuple(
                    item for item in plugin.manifest.hooks if isinstance(item, str)
                ),
                mcp_servers=tuple(
                    item
                    for item in plugin.manifest.mcp_servers
                    if isinstance(item, str)
                ),
                agents=tuple(
                    item for item in plugin.manifest.agents if isinstance(item, str)
                ),
                enabled=plugin.enabled,
            )
            for plugin in sorted(
                discovered_plugins, key=lambda item: item.manifest.name
            )
        ),
        hooks=tuple(hooks),
        errors=tuple(errors),
    )


def render_extension_inventory(
    inventory: ExtensionInventory,
    *,
    kind: ExtensionKind = "all",
    json_output: bool = False,
) -> str:
    payload = inventory.as_dict()
    if kind != "all":
        payload = {
            "workspace": payload["workspace"],
            "project_trusted": payload["project_trusted"],
            kind: payload[kind],
            "errors": payload["errors"],
        }
    if json_output:
        return json.dumps(payload, sort_keys=True)

    lines = [
        f"Workspace: {inventory.workspace}",
        f"Project extensions: {'trusted' if inventory.project_trusted else 'untrusted'}",
    ]
    if kind in {"all", "skills"}:
        lines.append("Skills:")
        lines.extend(
            f"  {skill.name}: {skill.description} ({skill.path})"
            for skill in inventory.skills
        )
        if not inventory.skills:
            lines.append("  (none)")
    if kind in {"all", "agents"}:
        lines.append("Agents:")
        lines.extend(
            f"  {agent.name} [{agent.base_role}]: {agent.description} ({agent.path})"
            for agent in inventory.agents
        )
        if not inventory.agents:
            lines.append("  (none)")
    if kind in {"all", "plugins"}:
        lines.append("Plugins:")
        lines.extend(
            f"  {plugin.name} {plugin.version} [{plugin.source}; "
            f"{'enabled' if plugin.enabled else 'disabled'}] - "
            f"{plugin.description or '(no description)'}"
            for plugin in inventory.plugins
        )
        if not inventory.plugins:
            lines.append("  (none)")
    if kind in {"all", "hooks"}:
        lines.append("Hooks:")
        lines.extend(
            f"  {hook.path} [{hook.source}]: pre_tool={hook.pre_tool}, "
            f"post_tool={hook.post_tool}, session_start={hook.session_start}"
            for hook in inventory.hooks
        )
        if not inventory.hooks:
            lines.append("  (none)")
    if inventory.errors:
        lines.append("Errors:")
        lines.extend(f"  {error}" for error in inventory.errors)
    return "\n".join(lines)


def manage_local_plugin(
    action: PluginAction,
    target: str,
    *,
    replace: bool = False,
    confirmed: bool = False,
) -> dict[str, Any]:
    if action == "install":
        load_extension_state()
        installed = install_local_plugin(Path(target), replace=replace)
        set_plugin_enabled(installed.name, enabled=True)
        return {
            "action": action,
            "name": installed.name,
            "version": installed.version,
            "root": str(installed.root),
            "enabled": True,
        }

    plugin = _installed_user_plugin(target)
    if action == "enable":
        set_plugin_enabled(target, enabled=True)
        enabled = True
    elif action == "disable":
        set_plugin_enabled(target, enabled=False)
        enabled = False
    else:
        uninstall_local_plugin(target, confirmed=confirmed)
        return {
            "action": action,
            "name": target,
            "version": plugin.manifest.version,
            "root": str(plugin.root),
            "removed": True,
        }
    return {
        "action": action,
        "name": target,
        "version": plugin.manifest.version,
        "root": str(plugin.root),
        "enabled": enabled,
    }


def render_plugin_action(result: dict[str, Any], *, json_output: bool) -> str:
    if json_output:
        return json.dumps(result, sort_keys=True)
    action = str(result["action"])
    name = str(result["name"])
    if action == "install":
        return f"Installed and enabled {name} {result['version']} at {result['root']}"
    if action == "uninstall":
        return f"Uninstalled {name} from {result['root']}"
    return f"{action.capitalize()}d {name}"


def _installed_user_plugin(name: str):
    state = load_extension_state()
    catalog = PluginCatalog(
        ((user_plugin_root(), "user"),),
        disabled_plugins=state.disabled_plugins,
    )
    plugins = catalog.discover(include_disabled=True)
    matched = next((plugin for plugin in plugins if plugin.manifest.name == name), None)
    if matched is not None:
        return matched
    expected = user_plugin_root() / name / "plugin.json"
    detail = catalog.errors.get(str(expected))
    if detail:
        raise PluginLifecycleError(f"installed plugin {name!r} is invalid: {detail}")
    raise PluginLifecycleError(f"plugin is not installed in user scope: {name}")


def _discover_hooks(
    paths: list[tuple[Path, str]],
) -> tuple[list[HookConfigSummary], list[str]]:
    hooks: list[HookConfigSummary] = []
    errors: list[str] = []
    for path, source in paths:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("hook config must be an object")
            hooks.append(
                HookConfigSummary(
                    path=str(path),
                    source=source,
                    pre_tool=_count_hook_entries(payload, "pre_tool"),
                    post_tool=_count_hook_entries(payload, "post_tool"),
                    session_start=_count_hook_entries(payload, "session_start"),
                )
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"Invalid hook config {path}: {exc}")
    return hooks, errors


def _count_hook_entries(payload: dict[str, Any], key: str) -> int:
    entries = payload.get(key, [])
    if not isinstance(entries, list):
        raise ValueError(f"{key} hooks must be a list")
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), list):
            raise ValueError(f"{key} hook entries must contain command arrays")
    return len(entries)
