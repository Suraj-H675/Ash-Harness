"""Local plugin discovery without implicit code execution or network access."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ash.plugins.manifest import PluginManifest, validate_plugin_identity


MAX_PLUGIN_DISCOVERY_ENTRIES = 10_000
MAX_PLUGIN_TREE_ENTRIES = 100_000
MAX_PLUGIN_TREE_DEPTH = 32


@dataclass(frozen=True)
class DiscoveredPlugin:
    manifest: PluginManifest
    root: Path
    source: str
    enabled: bool = True

    @property
    def deprecation_notice(self) -> str | None:
        return self.manifest.deprecation_notice

    def skill_paths(self) -> tuple[Path, ...]:
        if self.manifest.skills:
            return tuple(self.root / relative for relative in self.manifest.skills)
        defaults = []
        root_skill = self.root / "SKILL.md"
        skill_directory = self.root / "skills"
        if root_skill.is_file():
            defaults.append(root_skill)
        if skill_directory.is_dir():
            defaults.append(skill_directory)
        return tuple(defaults)

    def command_paths(self) -> tuple[Path, ...]:
        declared = tuple(
            self.root / item for item in self.manifest.commands if isinstance(item, str)
        )
        if declared:
            return declared
        default = self.root / "commands"
        return (default,) if default.is_dir() else ()

    def hook_paths(self) -> tuple[Path, ...]:
        declared = tuple(
            self.root / item for item in self.manifest.hooks if isinstance(item, str)
        )
        if declared:
            return declared
        default = self.root / "hooks" / "hooks.json"
        return (default,) if default.is_file() else ()

    def mcp_paths(self) -> tuple[Path, ...]:
        declared = tuple(
            self.root / item
            for item in self.manifest.mcp_servers
            if isinstance(item, str)
        )
        if declared:
            return declared
        default = self.root / ".mcp.json"
        return (default,) if default.is_file() else ()

    def agent_paths(self) -> tuple[Path, ...]:
        declared = tuple(
            self.root / item for item in self.manifest.agents if isinstance(item, str)
        )
        if declared:
            return declared
        default = self.root / "agents"
        return (default,) if default.is_dir() else ()


class PluginCatalog:
    """Discover declarative plugin manifests from explicitly allowed roots."""

    def __init__(
        self,
        roots: tuple[tuple[Path, str], ...],
        *,
        disabled_plugins: frozenset[str] = frozenset(),
    ) -> None:
        self.roots = roots
        self.disabled_plugins = disabled_plugins
        self.errors: dict[str, str] = {}

    def discover(self, *, include_disabled: bool = False) -> list[DiscoveredPlugin]:
        plugins: dict[str, DiscoveredPlugin] = {}
        self.errors.clear()
        for root, source in self.roots:
            if not root.is_dir():
                continue
            try:
                manifest_paths = _plugin_manifest_paths(root)
            except (OSError, ValueError) as exc:
                self.errors[str(root)] = str(exc)
                continue
            for path in manifest_paths:
                if path.parent.name in self.disabled_plugins and not include_disabled:
                    continue
                try:
                    manifest = PluginManifest.load(path)
                    enabled = manifest.name not in self.disabled_plugins
                    if not enabled and not include_disabled:
                        continue
                    _validate_manifest(manifest, path.parent)
                except Exception as exc:  # noqa: BLE001
                    self.errors[str(path)] = str(exc)
                    continue
                existing = plugins.get(manifest.name)
                if existing is not None:
                    self.errors[str(path)] = (
                        f"duplicate plugin name {manifest.name!r}; already provided by "
                        f"{existing.root / 'plugin.json'}"
                    )
                    continue
                plugins[manifest.name] = DiscoveredPlugin(
                    manifest, path.parent, source, enabled
                )
        while True:
            versions = {
                name: plugin.manifest.version for name, plugin in plugins.items()
            }
            invalid = {
                name: errors
                for name, plugin in plugins.items()
                if (errors := plugin.manifest.check_dependencies(versions))
            }
            if not invalid:
                break
            for name, errors in invalid.items():
                plugin = plugins.pop(name)
                self.errors[str(plugin.root / "plugin.json")] = "; ".join(errors)
        return list(plugins.values())


def _validate_manifest(manifest: PluginManifest, root: Path) -> None:
    for _ in _iter_plugin_tree(root):
        pass
    validate_plugin_identity(manifest)
    for field, values in (
        ("commands", manifest.commands),
        ("agents", manifest.agents),
        ("hooks", manifest.hooks),
        ("mcpServers", manifest.mcp_servers),
    ):
        if any(isinstance(item, dict) for item in values):
            raise ValueError(
                f"inline {field} declarations are unsupported; use component paths"
            )
    path_components = [*manifest.skills]
    path_components.extend(
        item
        for collection in (
            manifest.commands,
            manifest.agents,
            manifest.hooks,
            manifest.mcp_servers,
        )
        for item in collection
        if isinstance(item, str)
    )
    for relative in path_components:
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError(f"component path escapes plugin root: {relative}") from exc
        if not candidate.exists():
            raise ValueError(f"component path does not exist: {relative}")


def _plugin_manifest_paths(root: Path) -> list[Path]:
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise ValueError(f"plugin root cannot be a link: {root}")
    children: list[Path] = []
    try:
        for child in root.iterdir():
            children.append(child)
            if len(children) > MAX_PLUGIN_DISCOVERY_ENTRIES:
                raise ValueError(
                    f"plugin discovery exceeds {MAX_PLUGIN_DISCOVERY_ENTRIES} entries"
                )
    except OSError as exc:
        raise ValueError(f"cannot read plugin root {root}: {exc}") from exc
    paths: list[Path] = []
    for child in sorted(children, key=lambda path: path.name):
        if child.is_symlink() or (
            hasattr(child, "is_junction") and child.is_junction()
        ):
            continue
        try:
            if child.is_dir():
                manifest = child / "plugin.json"
                if manifest.is_file() or manifest.is_symlink():
                    paths.append(manifest)
        except OSError:
            continue
    return paths


def _iter_plugin_tree(root: Path) -> Iterator[Path]:
    pending: list[tuple[Path, int]] = [(root, 0)]
    entries_seen = 0
    while pending:
        current, depth = pending.pop()
        if current.is_symlink() or (
            hasattr(current, "is_junction") and current.is_junction()
        ):
            raise ValueError(f"plugin tree contains a link: {current}")
        yield current
        try:
            is_directory = current.is_dir()
        except OSError as exc:
            raise ValueError(f"cannot inspect plugin path {current}: {exc}") from exc
        if not is_directory:
            continue
        children: list[Path] = []
        try:
            for child in current.iterdir():
                children.append(child)
                if len(children) > MAX_PLUGIN_TREE_ENTRIES:
                    raise ValueError(
                        f"plugin tree exceeds {MAX_PLUGIN_TREE_ENTRIES} entries"
                    )
        except OSError as exc:
            raise ValueError(f"cannot read plugin directory {current}: {exc}") from exc
        children.sort(key=lambda path: path.name)
        for child in reversed(children):
            entries_seen += 1
            if entries_seen > MAX_PLUGIN_TREE_ENTRIES:
                raise ValueError(f"plugin tree exceeds {MAX_PLUGIN_TREE_ENTRIES} entries")
            if child.is_symlink() or (
                hasattr(child, "is_junction") and child.is_junction()
            ):
                raise ValueError(f"plugin tree contains a link: {child}")
            try:
                if child.is_dir() and depth >= MAX_PLUGIN_TREE_DEPTH:
                    raise ValueError(
                        f"plugin tree exceeds depth {MAX_PLUGIN_TREE_DEPTH}"
                    )
            except OSError as exc:
                raise ValueError(f"cannot inspect plugin path {child}: {exc}") from exc
            pending.append((child, depth + 1))
