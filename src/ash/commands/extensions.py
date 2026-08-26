"""Safe extension inventory for skills, plugins, and hooks."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from ash.mcp.server import load_mcp_servers
from ash.plugins.agents import AgentCatalog, AgentSource
from ash.commands.custom_commands import CommandSource, CustomCommandCatalog
from ash.hooks.config import (
    MAX_HOOK_CONFIG_BYTES,
    HookConfigSource,
    load_command_hooks,
)
from ash.plugins.catalog import (
    CatalogEntry,
    default_catalog_path,
    parse_and_verify_catalog,
    trusted_catalog_keys_path,
)
from ash.plugins.manifest import PluginManifest, namespaced_plugin_tool_name
from ash.plugins.lifecycle import (
    PluginLifecycleError,
    install_git_plugin,
    install_local_plugin,
    load_extension_state,
    set_plugin_enabled,
    uninstall_local_plugin,
    user_plugin_root,
)
from ash.plugins.registry import PluginCatalog
from ash.plugins.registry import DiscoveredPlugin, _validate_manifest
from ash.plugins.skills import SkillCatalog, SkillSource
from ash.safety.trust import canonical_workspace, is_workspace_trusted

ExtensionKind = Literal["all", "skills", "agents", "plugins", "hooks"]
PluginAction = Literal["install", "enable", "disable", "uninstall"]
ExtensionAction = Literal[
    "all",
    "skills",
    "agents",
    "plugins",
    "hooks",
    "search",
    "install",
    "enable",
    "disable",
    "uninstall",
]


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
            + (
                f" (runtime v{plugin.runtime_protocol}: {', '.join(plugin.tools)})"
                if plugin.runtime_protocol is not None
                else ""
            )
            for plugin in inventory.plugins
        )
        if not inventory.plugins:
            lines.append("  (none)")
    if kind in {"all", "hooks"}:
        lines.append("Hooks:")
        for hook in inventory.hooks:
            counts = {
                name: getattr(hook, name)
                for name in (
                    "pre_tool",
                    "post_tool",
                    "session_start",
                    "session_end",
                    "turn_start",
                    "turn_end",
                    "turn_error",
                    "pre_model",
                    "post_model",
                    "tool_error",
                )
            }
            active = ", ".join(
                f"{name}={count}" for name, count in counts.items() if count
            )
            lines.append(f"  {hook.path} [{hook.source}]: {active or 'empty'}")
        if not inventory.hooks:
            lines.append("  (none)")
    if inventory.errors:
        lines.append("Errors:")
        lines.extend(f"  {error}" for error in inventory.errors)
    return "\n".join(lines)


def _verified_catalog(catalog_path: Path | None = None):
    catalog_file = catalog_path or default_catalog_path()
    if catalog_file is None:
        raise PluginLifecycleError(
            "plugin catalog is not configured; pass --catalog or set ASH_PLUGIN_CATALOG"
        )
    try:
        return parse_and_verify_catalog(
            catalog_file,
            trusted_keys_path=trusted_catalog_keys_path(),
        )
    except ValueError as exc:
        raise PluginLifecycleError(str(exc)) from exc


def search_catalog_plugins(
    query: str = "",
    *,
    catalog_path: Path | None = None,
) -> tuple[int, tuple[CatalogEntry, ...]]:
    verified = _verified_catalog(catalog_path)
    normalized = query.strip().casefold()
    entries = sorted(verified.entries.values(), key=lambda entry: entry.name.casefold())
    if normalized:
        entries = [
            entry
            for entry in entries
            if normalized
            in " ".join(
                (
                    entry.name,
                    entry.version,
                    entry.source,
                    entry.ref,
                )
            ).casefold()
        ]
    return verified.sequence, tuple(entries)


def render_catalog_search(
    sequence: int,
    entries: tuple[CatalogEntry, ...],
    *,
    json_output: bool = False,
) -> str:
    payload = {
        "sequence": sequence,
        "plugins": [
            {
                "name": entry.name,
                "version": entry.version,
                "source": entry.source,
                "ref": entry.ref,
                "digest": entry.digest,
            }
            for entry in entries
        ],
    }
    if json_output:
        return json.dumps(payload, sort_keys=True)
    lines = [f"Catalog sequence: {sequence}", "Plugins:"]
    lines.extend(
        f"  {entry.name} {entry.version} [{entry.source}@{entry.ref}]"
        for entry in entries
    )
    if not entries:
        lines.append("  (none)")
    return "\n".join(lines)


def catalog_entry_for_name(
    name: str,
    *,
    catalog_path: Path | None = None,
) -> CatalogEntry:
    _, entries = search_catalog_plugins(name, catalog_path=catalog_path)
    matches = [entry for entry in entries if entry.name.casefold() == name.casefold()]
    if len(matches) != 1:
        raise PluginLifecycleError(f"unknown catalog plugin: {name}")
    if not matches[0].source.lower().startswith(("https://", "file://")):
        raise PluginLifecycleError(
            f"catalog plugin {name!r} does not use an HTTPS Git source"
        )
    return matches[0]


def manage_local_plugin(
    action: PluginAction,
    target: str,
    *,
    replace: bool = False,
    confirmed: bool = False,
    git_ref: str | None = None,
    catalog_path: Path | None = None,
) -> dict[str, Any]:
    if action == "install":
        state = load_extension_state()

        def validate_install(root: Path, manifest: PluginManifest) -> None:
            _require_enabled_dependencies(manifest, state.disabled_plugins)
            _validate_plugin_contents(root, manifest)

        if target.startswith(("https://", "http://")):
            expected = None
            if catalog_path is not None or default_catalog_path() is not None:
                verified_catalog = _verified_catalog(catalog_path)
                expected = verified_catalog.entries.get(target)
            installed = install_git_plugin(
                target,
                ref=git_ref or "",
                replace=replace,
                validator=validate_install,
                expected=expected,
            )
        elif git_ref is not None:
            raise PluginLifecycleError("--ref cannot override a catalog-pinned ref")
        elif Path(target).is_dir() or target.startswith((".", "/", "~")):
            installed = install_local_plugin(
                Path(target).expanduser(), replace=replace, validator=validate_install
            )
        else:
            expected = catalog_entry_for_name(target, catalog_path=catalog_path)
            installed = install_git_plugin(
                expected.source,
                ref=expected.ref,
                replace=replace,
                validator=validate_install,
                expected=expected,
            )
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
        state = load_extension_state()
        _require_enabled_dependencies(plugin.manifest, state.disabled_plugins)
        _validate_plugin_contents(plugin.root, plugin.manifest)
        set_plugin_enabled(target, enabled=True)
        enabled = True
    elif action == "disable":
        dependents = _plugin_dependents(target, enabled_only=True)
        if dependents:
            raise PluginLifecycleError(
                f"cannot disable {target!r}; required by: {', '.join(dependents)}"
            )
        set_plugin_enabled(target, enabled=False)
        enabled = False
    else:
        dependents = _plugin_dependents(target, enabled_only=False)
        if dependents:
            raise PluginLifecycleError(
                f"cannot uninstall {target!r}; required by: {', '.join(dependents)}"
            )
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
    expected = user_plugin_root() / name / "plugin.json"
    if not expected.is_file():
        raise PluginLifecycleError(f"plugin is not installed in user scope: {name}")
    try:
        manifest = PluginManifest.load(expected)
        _validate_manifest(manifest, expected.parent)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise PluginLifecycleError(
            f"installed plugin {name!r} is invalid: {exc}"
        ) from exc
    if manifest.name != name:
        raise PluginLifecycleError(
            f"installed plugin directory {name!r} contains manifest {manifest.name!r}"
        )
    return DiscoveredPlugin(
        manifest,
        expected.parent,
        "user",
        name not in load_extension_state().disabled_plugins,
    )


def _installed_user_manifests() -> dict[str, PluginManifest]:
    manifests: dict[str, PluginManifest] = {}
    root = user_plugin_root()
    if not root.is_dir():
        return manifests
    for path in sorted(root.glob("*/plugin.json")):
        try:
            manifest = PluginManifest.load(path)
            _validate_manifest(manifest, path.parent)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise PluginLifecycleError(
                f"installed plugin {path} is invalid: {exc}"
            ) from exc
        manifests[manifest.name] = manifest
    return manifests


def _require_enabled_dependencies(
    manifest: PluginManifest, disabled_plugins: frozenset[str]
) -> None:
    versions = {
        name: installed.version
        for name, installed in _installed_user_manifests().items()
        if name not in disabled_plugins and name != manifest.name
    }
    errors = manifest.check_dependencies(versions)
    if errors:
        raise PluginLifecycleError("; ".join(errors))


def _plugin_dependents(name: str, *, enabled_only: bool) -> list[str]:
    disabled = load_extension_state().disabled_plugins
    return sorted(
        candidate
        for candidate, manifest in _installed_user_manifests().items()
        if candidate != name
        and (not enabled_only or candidate not in disabled)
        and any(dependency.get("name") == name for dependency in manifest.dependencies)
    )


def _validate_plugin_contents(root: Path, manifest: PluginManifest) -> None:
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


def _discover_hooks(
    paths: list[tuple[Path, str]],
) -> tuple[list[HookConfigSummary], list[str]]:
    hooks: list[HookConfigSummary] = []
    errors: list[str] = []
    for path, source in paths:
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > MAX_HOOK_CONFIG_BYTES:
                raise ValueError("hook config exceeds 1 MiB")
            payload = json.loads(path.read_text(encoding="utf-8"))
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
