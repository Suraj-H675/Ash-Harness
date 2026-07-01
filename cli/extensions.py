"""Safe extension inventory for skills, plugins, and hooks."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from plugins.registry import PluginCatalog
from plugins.skills import SkillCatalog
from safety.trust import canonical_workspace, is_workspace_trusted

ExtensionKind = Literal["all", "skills", "plugins", "hooks"]


@dataclass(frozen=True)
class SkillSummary:
    name: str
    description: str
    path: str


@dataclass(frozen=True)
class PluginSummary:
    name: str
    version: str
    description: str
    source: str
    root: str
    skills: tuple[str, ...]


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
    plugins: tuple[PluginSummary, ...]
    hooks: tuple[HookConfigSummary, ...]
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "project_trusted": self.project_trusted,
            "skills": [asdict(item) for item in self.skills],
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

    plugin_catalog = PluginCatalog(tuple(plugin_roots))
    discovered_plugins = plugin_catalog.discover()
    skill_roots = [user_skill_root]
    if trusted:
        skill_roots.append(workspace / ".ash" / "skills")
    skill_roots.extend(plugin.root for plugin in discovered_plugins)
    skill_catalog = SkillCatalog(tuple(skill_roots))
    discovered_skills = skill_catalog.discover()

    errors = [
        f"Invalid plugin {path}: {error}"
        for path, error in sorted(plugin_catalog.errors.items())
    ]
    errors.extend(
        f"Invalid skill {path}: {error}"
        for path, error in sorted(skill_catalog.errors.items())
    )
    hooks, hook_errors = _discover_hooks(hook_paths)
    errors.extend(hook_errors)

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
        plugins=tuple(
            PluginSummary(
                name=plugin.manifest.name,
                version=plugin.manifest.version,
                description=plugin.manifest.description,
                source=plugin.source,
                root=str(plugin.root),
                skills=tuple(plugin.manifest.skills),
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
    if kind in {"all", "plugins"}:
        lines.append("Plugins:")
        lines.extend(
            f"  {plugin.name} {plugin.version} [{plugin.source}] - "
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
