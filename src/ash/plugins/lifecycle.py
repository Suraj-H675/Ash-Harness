"""Local plugin installation and enablement state."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ash.plugins.manifest import PLUGIN_NAME, PluginManifest
from ash.plugins.registry import _validate_manifest

MAX_PLUGIN_FILES = 10_000
MAX_PLUGIN_BYTES = 256 * 1024 * 1024
STATE_VERSION = 1


class PluginLifecycleError(ValueError):
    """Raised when a local plugin lifecycle operation is unsafe or invalid."""


@dataclass(frozen=True)
class ExtensionState:
    disabled_plugins: frozenset[str] = frozenset()


@dataclass(frozen=True)
class InstalledPlugin:
    name: str
    version: str
    root: Path


def user_plugin_root() -> Path:
    return Path.home() / ".ash" / "plugins"


def extension_state_path() -> Path:
    return Path.home() / ".ash" / "extensions.json"


def load_extension_state(path: Path | None = None) -> ExtensionState:
    state_path = path or extension_state_path()
    if not state_path.exists():
        return ExtensionState()
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PluginLifecycleError(
            f"cannot load extension state {state_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        raise PluginLifecycleError(f"invalid extension state: {state_path}")
    disabled = payload.get("disabled_plugins", [])
    if not isinstance(disabled, list) or not all(
        isinstance(name, str) and name for name in disabled
    ):
        raise PluginLifecycleError(
            f"disabled_plugins must be a list of names: {state_path}"
        )
    return ExtensionState(disabled_plugins=frozenset(disabled))


def set_plugin_enabled(
    name: str,
    *,
    enabled: bool,
    path: Path | None = None,
) -> ExtensionState:
    _validate_plugin_name(name)
    state_path = path or extension_state_path()
    state = load_extension_state(state_path)
    disabled = set(state.disabled_plugins)
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    updated = ExtensionState(disabled_plugins=frozenset(disabled))
    _save_extension_state(updated, state_path)
    return updated


def install_local_plugin(
    source: Path,
    *,
    destination_root: Path | None = None,
    replace: bool = False,
    validator: Callable[[Path, PluginManifest], None] | None = None,
) -> InstalledPlugin:
    source = source.expanduser()
    if _is_link(source):
        raise PluginLifecycleError(f"plugin source cannot be a link: {source}")
    source = source.resolve()
    if not source.is_dir():
        raise PluginLifecycleError(f"plugin source is not a directory: {source}")
    _validate_tree(source)
    manifest_path = source / "plugin.json"
    if not manifest_path.is_file():
        raise PluginLifecycleError(f"plugin manifest not found: {manifest_path}")
    try:
        manifest = PluginManifest.load(manifest_path)
        _validate_manifest(manifest, source)
        _validate_plugin_name(manifest.name)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise PluginLifecycleError(f"invalid plugin manifest: {exc}") from exc

    root = (destination_root or user_plugin_root()).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        root.chmod(0o700)
    destination = root / manifest.name
    installed_versions: dict[str, str] = {}
    for installed_manifest in root.glob("*/plugin.json"):
        if installed_manifest.parent.name == manifest.name:
            continue
        try:
            installed = PluginManifest.load(installed_manifest)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            continue
        installed_versions[installed.name] = installed.version
    dependency_errors = manifest.check_dependencies(installed_versions)
    if dependency_errors:
        raise PluginLifecycleError("; ".join(dependency_errors))
    if destination.exists() and not replace:
        raise PluginLifecycleError(
            f"plugin {manifest.name!r} is already installed; use --replace"
        )

    temporary_root = Path(tempfile.mkdtemp(prefix=".install-", dir=root))
    staged = temporary_root / manifest.name
    backup = root / f".{manifest.name}.backup-{uuid.uuid4().hex}"
    destination_moved = False
    try:
        shutil.copytree(source, staged, symlinks=True)
        _validate_tree(staged)
        staged_manifest = PluginManifest.load(staged / "plugin.json")
        _validate_manifest(staged_manifest, staged)
        if staged_manifest != manifest:
            raise PluginLifecycleError("plugin changed while it was being installed")
        if validator is not None:
            try:
                validator(staged, staged_manifest)
            except PluginLifecycleError:
                raise
            except Exception as exc:
                raise PluginLifecycleError(f"invalid plugin component: {exc}") from exc
        if destination.exists():
            destination.replace(backup)
            destination_moved = True
        staged.replace(destination)
        if destination_moved:
            shutil.rmtree(backup)
        return InstalledPlugin(manifest.name, manifest.version, destination)
    except Exception:
        if destination_moved and backup.exists() and not destination.exists():
            backup.replace(destination)
        raise
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
        if backup.exists() and destination.exists():
            shutil.rmtree(backup, ignore_errors=True)


def uninstall_local_plugin(
    name: str,
    *,
    destination_root: Path | None = None,
    confirmed: bool = False,
    state_path: Path | None = None,
) -> Path:
    _validate_plugin_name(name)
    if not confirmed:
        raise PluginLifecycleError("uninstall requires explicit confirmation")
    root = (destination_root or user_plugin_root()).expanduser()
    destination = root / name
    if not destination.is_dir() or destination.is_symlink():
        raise PluginLifecycleError(f"plugin is not installed: {name}")
    manifest_path = destination / "plugin.json"
    try:
        manifest = PluginManifest.load(manifest_path)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise PluginLifecycleError(
            f"refusing to uninstall plugin with invalid manifest: {exc}"
        ) from exc
    if manifest.name != name:
        raise PluginLifecycleError(
            f"refusing to uninstall mismatched plugin {manifest.name!r} as {name!r}"
        )
    quarantine = root / f".{name}.uninstall-{uuid.uuid4().hex}"
    destination.replace(quarantine)
    try:
        shutil.rmtree(quarantine)
    except Exception:
        quarantine.replace(destination)
        raise
    set_plugin_enabled(name, enabled=True, path=state_path)
    return destination


def _validate_tree(root: Path) -> None:
    if _is_link(root):
        raise PluginLifecycleError(f"plugin tree contains a link: {root}")
    files = 0
    total_bytes = 0
    for path in root.rglob("*"):
        if _is_link(path):
            raise PluginLifecycleError(f"plugin tree contains a link: {path}")
        if path.is_file():
            files += 1
            total_bytes += path.stat().st_size
            if files > MAX_PLUGIN_FILES:
                raise PluginLifecycleError(
                    f"plugin contains more than {MAX_PLUGIN_FILES} files"
                )
            if total_bytes > MAX_PLUGIN_BYTES:
                raise PluginLifecycleError("plugin exceeds 256 MiB")


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _validate_plugin_name(name: str) -> None:
    if not PLUGIN_NAME.fullmatch(name):
        raise PluginLifecycleError("plugin name must be a path-safe identifier")


def _save_extension_state(state: ExtensionState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.parent.chmod(0o700)
    payload: dict[str, Any] = {
        "version": STATE_VERSION,
        "disabled_plugins": sorted(state.disabled_plugins),
    }
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
