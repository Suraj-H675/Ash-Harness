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


class PluginCatalog:
    """Discover declarative plugin manifests from explicitly allowed roots."""

    def __init__(self, roots: tuple[tuple[Path, str], ...]) -> None:
        self.roots = roots
        self.errors: dict[str, str] = {}

    def discover(self) -> list[DiscoveredPlugin]:
        plugins: dict[str, DiscoveredPlugin] = {}
        self.errors.clear()
        for root, source in self.roots:
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*/plugin.json")):
                try:
                    manifest = PluginManifest.load(path)
                    _validate_manifest(manifest, path.parent)
                except Exception as exc:  # noqa: BLE001
                    self.errors[str(path)] = str(exc)
                    continue
                plugins.setdefault(
                    manifest.name,
                    DiscoveredPlugin(manifest, path.parent, source),
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
