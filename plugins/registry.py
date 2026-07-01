"""Local plugin discovery without implicit code execution or network access."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from plugins.manifest import PluginManifest


@dataclass(frozen=True)
class DiscoveredPlugin:
    manifest: PluginManifest
    root: Path
    source: str
    enabled: bool = True

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
            for path in sorted(root.glob("*/plugin.json")):
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
        return list(plugins.values())


def _validate_manifest(manifest: PluginManifest, root: Path) -> None:
    if not manifest.name or any(part in manifest.name for part in ("/", "\\", "..")):
        raise ValueError("plugin name must be a non-empty path-safe identifier")
    dependency_errors = manifest.check_dependencies()
    if dependency_errors:
        raise ValueError("; ".join(dependency_errors))
    for relative in manifest.skills:
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError(f"skill path escapes plugin root: {relative}") from exc
        if not candidate.exists():
            raise ValueError(f"skill path does not exist: {relative}")
