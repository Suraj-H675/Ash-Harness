"""Discovery and typed inventory for Ash extension components."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ash.hooks.config import MAX_HOOK_CONFIG_BYTES, load_command_hooks
from ash.json_utils import strict_json_loads
from ash.mcp.server import load_mcp_servers
from ash.plugins.agents import AgentCatalog, AgentSource
from ash.plugins.errors import PluginLifecycleError
from ash.plugins.lifecycle import recover_plugin_lifecycle
from ash.plugins.state import load_extension_state
from ash.plugins.manifest import namespaced_plugin_tool_name
from ash.plugins.registry import PluginCatalog
from ash.plugins.skills import SkillCatalog, SkillSource
from ash.safe_io import read_bounded_bytes
from ash.safety.trust import canonical_workspace, is_workspace_trusted

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
    runtime_protocol: int | None
    tools: tuple[str, ...]
    enabled: bool


@dataclass(frozen=True)
class HookConfigSummary:
    path: str
    source: str
    pre_tool: int = 0
    post_tool: int = 0
    session_start: int = 0
    session_end: int = 0
    turn_start: int = 0
    turn_end: int = 0
    turn_error: int = 0
    pre_model: int = 0
    post_model: int = 0
    tool_error: int = 0


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
    extension_state_available = True
    try:
        recover_plugin_lifecycle()
        disabled_plugins = load_extension_state().disabled_plugins
    except PluginLifecycleError as exc:
        disabled_plugins = frozenset()
        extension_state_available = False
        state_errors.append(str(exc))
    plugin_catalog = PluginCatalog(
        tuple(plugin_roots), disabled_plugins=disabled_plugins
    )
    # A secure extension-state read is part of the trust decision for plugin
    # components. Do not turn an unavailable/corrupt state into an empty
    # disabled set and then activate plugin hooks, MCP, skills, or agents.
    discovered_plugins = (
        plugin_catalog.discover(include_disabled=True)
        if extension_state_available
        else []
    )
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
                runtime_protocol=(
                    plugin.manifest.runtime.protocol_version
                    if plugin.manifest.runtime is not None
                    else None
                ),
                tools=tuple(
                    namespaced_plugin_tool_name(plugin.manifest.name, tool.name)
                    for tool in plugin.manifest.tools
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

def _discover_hooks(
    paths: list[tuple[Path, str]],
) -> tuple[list[HookConfigSummary], list[str]]:
    hooks: list[HookConfigSummary] = []
    errors: list[str] = []
    for path, source in paths:
        if not path.is_file():
            continue
        try:
            raw = read_bounded_bytes(
                path,
                MAX_HOOK_CONFIG_BYTES,
                label="hook config",
            )
            payload = strict_json_loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("hook config must be an object")
            summary = HookConfigSummary(
                path=str(path),
                source=source,
                pre_tool=_count_hook_entries(payload, "pre_tool"),
                post_tool=_count_hook_entries(payload, "post_tool"),
                session_start=_count_hook_entries(payload, "session_start"),
                session_end=_count_hook_entries(payload, "session_end"),
                turn_start=_count_hook_entries(payload, "turn_start"),
                turn_end=_count_hook_entries(payload, "turn_end"),
                turn_error=_count_hook_entries(payload, "turn_error"),
                pre_model=_count_hook_entries(payload, "pre_model"),
                post_model=_count_hook_entries(payload, "post_model"),
                tool_error=_count_hook_entries(payload, "tool_error"),
            )
            load_command_hooks([path])
            hooks.append(summary)
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
