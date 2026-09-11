"""Local plugin installation and enablement state."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ash.safe_io import strict_json_loads
from ash.plugins.anchored_fs import (
    AnchoredDirectory,
    AnchoredFilesystemError,
    AnchoredFilesystemUnavailable,
    require_anchored_mutation,
    supports_anchored_mutation,
)
from ash.plugins.snapshot import PluginSnapshot, PluginSnapshotError
from ash.plugins.manifest import (
    MAX_PLUGIN_MANIFEST_BYTES,
    PLUGIN_NAME,
    PluginManifest,
    validate_plugin_identity,
)
from ash.plugins.catalog import CatalogEntry, PluginCatalogError
from ash.plugins.registry import (
    MAX_PLUGIN_TREE_DEPTH,
    MAX_PLUGIN_TREE_ENTRIES,
)
from ash.safety.environment import resolve_host_executable
from ash.sandbox.process_utils import (
    ProcessTreeError,
    ProcessTreeUnavailable,
    ProcessTreePlan,
    prepare_process_tree,
    terminate_process_tree_sync,
)

MAX_PLUGIN_FILES = 10_000
MAX_PLUGIN_BYTES = 256 * 1024 * 1024
MAX_EXTENSION_STATE_BYTES = 256 * 1024
STATE_VERSION = 1
MAX_GIT_CLONE_BYTES = MAX_PLUGIN_BYTES
MAX_GIT_CLONE_SECONDS = 300
MAX_GIT_ERROR_BYTES = 64 * 1024
_GIT_CLONE_POLL_SECONDS = 0.05


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
    _require_anchored_plugin_mutation()
    try:
        directory = AnchoredDirectory.open(
            state_path.parent,
            create=False,
            private=False,
        )
    except FileNotFoundError:
        return ExtensionState()
    except (AnchoredFilesystemError, OSError) as exc:
        raise _lifecycle_error("extension state", exc) from exc
    try:
        return _read_extension_state_at(directory, state_path)
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise _lifecycle_error("extension state", exc) from exc
    finally:
        directory.close()


def _parse_extension_state(raw: bytes, state_path: Path) -> ExtensionState:
    try:
        payload = strict_json_loads(raw)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
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
    _require_anchored_plugin_mutation()
    state_path = path or extension_state_path()
    try:
        with (
            AnchoredDirectory.open(state_path.parent, create=True) as directory,
            directory.lock(f".{state_path.name}.lock"),
        ):
            state = _read_extension_state_at(directory, state_path)
            disabled = set(state.disabled_plugins)
            if enabled:
                disabled.discard(name)
            else:
                disabled.add(name)
            updated = ExtensionState(disabled_plugins=frozenset(disabled))
            _save_extension_state_at(directory, updated, state_path)
            return updated
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise _lifecycle_error("extension state", exc) from exc


def install_local_plugin(
    source: Path,
    *,
    destination_root: Path | None = None,
    replace: bool = False,
    validator: Callable[[Path, PluginManifest], None] | None = None,
    _validator_at: Callable[[PluginSnapshot, PluginManifest], None] | None = None,
    _source_directory: AnchoredDirectory | None = None,
    _snapshot: PluginSnapshot | None = None,
) -> InstalledPlugin:
    source_path = source.expanduser()
    _require_anchored_plugin_mutation()
    owns_source_directory = _source_directory is None
    if _source_directory is None:
        if _is_link(source_path):
            raise PluginLifecycleError(f"plugin source cannot be a link: {source_path}")
        try:
            source_metadata = os.stat(source_path, follow_symlinks=False)
        except OSError as exc:
            raise PluginLifecycleError(
                f"plugin source is not available: {source_path}"
            ) from exc
        if stat.S_ISLNK(source_metadata.st_mode):
            raise PluginLifecycleError(f"plugin source cannot be a link: {source_path}")
        if not stat.S_ISDIR(source_metadata.st_mode):
            raise PluginLifecycleError(
                f"plugin source is not a directory: {source_path}"
            )
        try:
            source_directory = AnchoredDirectory.open(
                source_path,
                create=False,
                private=False,
                expected=source_metadata,
            )
        except (AnchoredFilesystemError, OSError) as exc:
            raise _lifecycle_error("plugin source", exc) from exc
    else:
        source_directory = _source_directory

    owns_snapshot = _snapshot is None
    snapshot = _snapshot
    try:
        try:
            if snapshot is None:
                snapshot = PluginSnapshot.capture(
                    source_directory,
                    max_files=MAX_PLUGIN_FILES,
                    max_bytes=MAX_PLUGIN_BYTES,
                    max_entries=MAX_PLUGIN_TREE_ENTRIES,
                    max_depth=MAX_PLUGIN_TREE_DEPTH,
                )
            manifest = _load_manifest_snapshot(snapshot)
            _validate_manifest_snapshot(manifest, snapshot)
            _validate_plugin_name(manifest.name)
        except PluginLifecycleError:
            raise
        except (
            AnchoredFilesystemError,
            PluginSnapshotError,
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
        ) as exc:
            raise PluginLifecycleError(f"invalid plugin manifest: {exc}") from exc

        root = (destination_root or user_plugin_root()).expanduser()
        destination = root / manifest.name
        try:
            with (
                AnchoredDirectory.open(root, create=True) as root_directory,
                root_directory.lock(".ash-lifecycle.lock"),
            ):
                installed_versions = _installed_plugin_versions_at(
                    root_directory,
                    excluding=manifest.name,
                )
                dependency_errors = manifest.check_dependencies(installed_versions)
                if dependency_errors:
                    raise PluginLifecycleError("; ".join(dependency_errors))

                existing = root_directory.stat(manifest.name)
                if existing is not None:
                    if stat.S_ISLNK(existing.st_mode) or not stat.S_ISDIR(existing.st_mode):
                        raise PluginLifecycleError(
                            f"plugin destination is not a directory: {destination}"
                        )
                    if not replace:
                        raise PluginLifecycleError(
                            f"plugin {manifest.name!r} is already installed; use --replace"
                        )

                stage_name = root_directory.unique_name(".install-", ".tmp")
                stage_directory = root_directory.create_child(stage_name)
                backup_name: str | None = None
                backup_directory: AnchoredDirectory | None = None
                destination_directory: AnchoredDirectory | None = None
                destination_moved = False
                published = False
                primary: BaseException | None = None
                cleanup_errors: list[BaseException] = []
                try:
                    assert snapshot is not None
                    snapshot.write_to(stage_directory)
                    _validate_tree_at(stage_directory)
                    snapshot.verify_materialized(stage_directory)
                    staged_manifest = manifest
                    if _validator_at is not None:
                        _validator_at(snapshot, staged_manifest)
                    elif validator is not None:
                        try:
                            staged = stage_directory.validation_path()
                            validator(staged, staged_manifest)
                        except PluginLifecycleError:
                            raise
                        except Exception as exc:
                            raise PluginLifecycleError(
                                f"invalid plugin component: {exc}"
                            ) from exc
                    current = root_directory.stat(manifest.name)
                    if existing is None:
                        if current is not None:
                            raise AnchoredFilesystemError(
                                "plugin destination appeared during installation"
                            )
                    else:
                        if current is None or not _same_metadata(existing, current):
                            raise AnchoredFilesystemError(
                                "plugin destination changed during installation"
                            )
                        if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(
                            current.st_mode
                        ):
                            raise PluginLifecycleError(
                                f"plugin destination is not a directory: {destination}"
                            )
                        backup_directory = root_directory.child(
                            manifest.name,
                            expected=current,
                        )
                        backup_name = root_directory.unique_name(
                            f".{manifest.name}.backup-"
                        )
                        root_directory.rename(
                            manifest.name,
                            backup_name,
                            expected_source_descriptor=backup_directory.descriptor,
                        )
                        if not root_directory.same_entry(
                            backup_name,
                            backup_directory.descriptor,
                        ):
                            raise AnchoredFilesystemError(
                                "plugin destination changed during replacement"
                            )
                        destination_moved = True

                    # Publication is a descriptor-relative population into a
                    # directory created under the held destination root. The
                    # validated stage name is never renamed into the live slot.
                    destination_directory = root_directory.create_child(manifest.name)
                    snapshot.write_to(destination_directory)
                    _validate_tree_at(destination_directory)
                    snapshot.verify_materialized(destination_directory)
                    installed_manifest = _load_manifest_at(
                        destination_directory,
                        "plugin.json",
                    )
                    _validate_manifest_at(installed_manifest, destination_directory)
                    if installed_manifest != staged_manifest:
                        raise AnchoredFilesystemError(
                            "plugin changed while being published"
                        )
                    _verify_visible_destination(
                        root_directory,
                        destination_directory,
                        destination,
                    )
                    published = True
                    if destination_moved and backup_name is not None:
                        assert backup_directory is not None
                        root_directory.remove_tree(
                            backup_name,
                            expected_descriptor=backup_directory.descriptor,
                        )
                        backup_name = None
                    root_directory.sync()
                    _verify_visible_destination(
                        root_directory,
                        destination_directory,
                        destination,
                    )
                    return InstalledPlugin(
                        manifest.name,
                        manifest.version,
                        destination,
                    )
                except BaseException as exc:
                    primary = exc
                    if destination_directory is not None and not published:
                        try:
                            root_directory.remove_tree(
                                manifest.name,
                                expected_descriptor=destination_directory.descriptor,
                            )
                        except BaseException as cleanup:
                            primary.add_note(f"candidate cleanup failed: {cleanup}")
                    if destination_moved and not published and backup_name is not None:
                        try:
                            _restore_moved_entry(
                                root_directory,
                                manifest.name,
                                backup_name,
                                conflict_prefix=f".{manifest.name}.install-conflict-",
                                expected_descriptor=backup_directory.descriptor
                                if backup_directory is not None
                                else None,
                            )
                            if root_directory.stat(backup_name) is None:
                                backup_name = None
                        except BaseException as cleanup:
                            primary.add_note(f"replacement rollback failed: {cleanup}")
                    raise
                finally:
                    if stage_name:
                        try:
                            root_directory.remove_tree(
                                stage_name,
                                expected_descriptor=stage_directory.descriptor,
                            )
                        except BaseException as cleanup:
                            cleanup_errors.append(cleanup)
                    if published and backup_name is not None:
                        try:
                            assert backup_directory is not None
                            root_directory.remove_tree(
                                backup_name,
                                expected_descriptor=backup_directory.descriptor,
                            )
                            backup_name = None
                        except BaseException as cleanup:
                            cleanup_errors.append(cleanup)
                    # Descriptor closure is independent from tree cleanup.
                    try:
                        stage_directory.close()
                    except BaseException as cleanup:
                        cleanup_errors.append(cleanup)
                    if destination_directory is not None:
                        try:
                            destination_directory.close()
                        except BaseException as cleanup:
                            cleanup_errors.append(cleanup)
                    if backup_directory is not None:
                        try:
                            backup_directory.close()
                        except BaseException as cleanup:
                            cleanup_errors.append(cleanup)
                    if cleanup_errors:
                        if primary is not None:
                            for secondary_error in cleanup_errors:
                                primary.add_note(
                                    f"installation cleanup failed: {secondary_error}"
                                )
                        else:
                            raise PluginLifecycleError(
                                f"plugin installation cleanup failed: {cleanup_errors[0]}"
                            ) from cleanup_errors[0]
        except PluginLifecycleError:
            raise
        except (AnchoredFilesystemError, PluginSnapshotError, OSError) as exc:
            raise _lifecycle_error("plugin destination root", exc) from exc
    finally:
        if owns_snapshot and snapshot is not None:
            snapshot.close()
        if owns_source_directory:
            source_directory.close()


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
    _require_anchored_plugin_mutation()
    try:
        with (
            AnchoredDirectory.open(root, create=False) as root_directory,
            root_directory.lock(".ash-lifecycle.lock"),
        ):
            destination_metadata = root_directory.stat(name)
            if destination_metadata is None or not stat.S_ISDIR(
                destination_metadata.st_mode
            ):
                raise PluginLifecycleError(f"plugin is not installed: {name}")
            if stat.S_ISLNK(destination_metadata.st_mode):
                raise PluginLifecycleError(f"plugin is not installed: {name}")
            with root_directory.child(
                name,
                expected=destination_metadata,
            ) as plugin_directory:
                try:
                    manifest = _load_manifest_at(plugin_directory, "plugin.json")
                except (AnchoredFilesystemError, OSError, ValueError, TypeError) as exc:
                    raise PluginLifecycleError(
                        f"refusing to uninstall plugin with invalid manifest: {exc}"
                    ) from exc
                if manifest.name != name:
                    raise PluginLifecycleError(
                        f"refusing to uninstall mismatched plugin {manifest.name!r} as {name!r}"
                    )
                quarantine_name = root_directory.unique_name(f".{name}.uninstall-")
                primary: BaseException | None = None
                try:
                    root_directory.rename(
                        name,
                        quarantine_name,
                        expected_source_descriptor=plugin_directory.descriptor,
                    )
                    if not root_directory.same_entry(
                        quarantine_name,
                        plugin_directory.descriptor,
                    ):
                        raise AnchoredFilesystemError(
                            "plugin destination changed during uninstall"
                        )
                    root_directory.remove_tree(
                        quarantine_name,
                        expected_descriptor=plugin_directory.descriptor,
                    )
                    root_directory.sync()
                except BaseException as exc:
                    primary = exc
                    try:
                        _restore_moved_entry(
                            root_directory,
                            name,
                            quarantine_name,
                            conflict_prefix=f".{name}.uninstall-conflict-",
                            expected_descriptor=plugin_directory.descriptor,
                        )
                    except BaseException as cleanup:
                        primary.add_note(f"uninstall rollback failed: {cleanup}")
                    raise
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise _lifecycle_error("plugin destination root", exc) from exc
    set_plugin_enabled(name, enabled=True, path=state_path)
    return destination


def _validate_tree(root: Path) -> None:
    """Validate a path through the same anchored boundary used by lifecycle."""

    try:
        metadata = os.stat(root, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PluginLifecycleError(f"plugin root is not a directory: {root}")
        with AnchoredDirectory.open(
            root,
            create=False,
            private=False,
            expected=metadata,
        ) as directory:
            _validate_tree_at(directory)
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError, ValueError) as exc:
        raise PluginLifecycleError(str(exc)) from exc


@dataclass
class _TreeValidationCounters:
    files: int = 0
    total_bytes: int = 0
    entries: int = 0


def _validate_tree_at(directory: AnchoredDirectory) -> None:
    counters = _TreeValidationCounters()
    try:
        _validate_directory_at(directory, counters, depth=0)
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError, ValueError) as exc:
        raise PluginLifecycleError(str(exc)) from exc


def _validate_directory_at(
    directory: AnchoredDirectory,
    counters: _TreeValidationCounters,
    *,
    depth: int,
) -> None:
    if depth > MAX_PLUGIN_TREE_DEPTH:
        raise PluginLifecycleError(
            f"plugin tree exceeds depth {MAX_PLUGIN_TREE_DEPTH}"
        )
    for name in sorted(directory.list_names()):
        counters.entries += 1
        if counters.entries > MAX_PLUGIN_TREE_ENTRIES:
            raise PluginLifecycleError(
                f"plugin tree exceeds {MAX_PLUGIN_TREE_ENTRIES} entries"
            )
        metadata = directory.stat(name)
        if metadata is None:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise PluginLifecycleError(f"plugin tree contains a link: {directory.path / name}")
        if stat.S_ISDIR(metadata.st_mode):
            child = directory.child(name, expected=metadata)
            try:
                _validate_directory_at(child, counters, depth=depth + 1)
            finally:
                child.close()
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise PluginLifecycleError(
                f"plugin tree contains unsupported entry: {directory.path / name}"
            )
        descriptor = directory.open_file(
            name,
            os.O_RDONLY,
            expected=metadata,
            expected_type=stat.S_IFREG,
        )
        try:
            opened = os.fstat(descriptor)
            counters.files += 1
            counters.total_bytes += opened.st_size
        finally:
            os.close(descriptor)
        if counters.files > MAX_PLUGIN_FILES:
            raise PluginLifecycleError(
                f"plugin contains more than {MAX_PLUGIN_FILES} files"
            )
        if counters.total_bytes > MAX_PLUGIN_BYTES:
            raise PluginLifecycleError("plugin exceeds 256 MiB")


def _validate_manifest_at(
    manifest: PluginManifest,
    directory: AnchoredDirectory,
) -> None:
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
        _validate_manifest_component_at(directory, relative)


def _validate_manifest_component_at(
    directory: AnchoredDirectory,
    relative: str,
) -> None:
    candidate = Path(relative)
    if candidate.is_absolute() or not candidate.parts:
        raise ValueError(f"component path escapes plugin root: {relative}")
    components = tuple(candidate.parts)
    if any(part in {"", ".", ".."} for part in components):
        raise ValueError(f"component path escapes plugin root: {relative}")
    opened: AnchoredDirectory | None = None
    parent = directory
    try:
        for component in components[:-1]:
            metadata = parent.stat(component)
            if metadata is None or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"component path does not exist: {relative}")
            child = parent.child(component, expected=metadata)
            if opened is not None:
                opened.close()
            opened = child
            parent = child
        final_name = components[-1]
        metadata = parent.stat(final_name)
        if metadata is None or stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"component path does not exist: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            child = parent.child(final_name, expected=metadata)
            child.close()
        elif stat.S_ISREG(metadata.st_mode):
            descriptor = parent.open_file(
                final_name,
                os.O_RDONLY,
                expected=metadata,
                expected_type=stat.S_IFREG,
            )
            os.close(descriptor)
        else:
            raise ValueError(f"component path does not exist: {relative}")
    finally:
        if opened is not None:
            opened.close()


def _validate_manifest_snapshot(
    manifest: PluginManifest,
    snapshot: PluginSnapshot,
) -> None:
    """Validate manifest identity and component existence in snapshot space."""

    validate_plugin_identity(manifest)
    components: list[str] = []
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
        components.extend(item for item in values if isinstance(item, str))
    components.extend(manifest.skills)
    for relative in components:
        candidate = Path(relative)
        if candidate.is_absolute() or not candidate.parts:
            raise ValueError(f"component path escapes plugin root: {relative}")
        parts = tuple(candidate.parts)
        if any(not part or part in {".", ".."} for part in parts):
            raise ValueError(f"component path escapes plugin root: {relative}")
        if snapshot.entry(parts) is None:
            raise ValueError(f"component path does not exist: {relative}")


def install_git_plugin(
    source: str,
    *,
    ref: str,
    destination_root: Path | None = None,
    replace: bool = False,
    validator: Callable[[Path, PluginManifest], None] | None = None,
    _validator_at: Callable[[PluginSnapshot, PluginManifest], None] | None = None,
    expected: CatalogEntry | None = None,
) -> InstalledPlugin:
    _require_anchored_plugin_mutation()
    parsed = urllib.parse.urlsplit(source)
    scheme = parsed.scheme.lower()
    if scheme == "https":
        if not parsed.hostname:
            raise PluginLifecycleError("plugin Git source must use an HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise PluginLifecycleError(
                "plugin Git source URL cannot contain embedded credentials"
            )
        if parsed.query or parsed.fragment:
            raise PluginLifecycleError(
                "plugin Git source URL cannot contain a query or fragment"
            )
    elif scheme == "file":
        if parsed.hostname not in {None, "", "localhost"}:
            raise PluginLifecycleError("plugin file source must be local")
    else:
        raise PluginLifecycleError("plugin Git source must use an HTTPS URL")
    if not ref:
        raise PluginLifecycleError("plugin Git source requires an explicit --ref")
    if len(ref) > 255 or "\x00" in ref or "\n" in ref or "\r" in ref:
        raise PluginLifecycleError("plugin Git reference is invalid")
    if len(source) > 2048:
        raise PluginLifecycleError("plugin Git source URL is too long")

    workspace = Path.cwd().resolve()
    git_path = resolve_host_executable("git", workspace_root=workspace, cwd=workspace)
    if git_path is None:
        raise PluginLifecycleError("git is unavailable outside the workspace")
    try:
        process_tree_plan = prepare_process_tree(workspace_root=workspace)
    except ProcessTreeUnavailable as exc:
        raise PluginLifecycleError(
            f"plugin Git clone was not started: {exc}"
        ) from exc

    temporary_name: str | None = None
    temporary_parent: AnchoredDirectory | None = None
    temporary_directory: AnchoredDirectory | None = None
    checkout_directory: AnchoredDirectory | None = None
    snapshot: PluginSnapshot | None = None
    git_primary: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    try:
        try:
            # Acquire the environment-selected temporary parent before
            # creating any Ash-owned checkout entry.  In particular, do not
            # let a symlinked TMPDIR select a pathname outside the anchor.
            temporary_parent = AnchoredDirectory.open(
                _temporary_parent_path(),
                create=False,
                private=False,
            )
            temporary_name = temporary_parent.unique_name("ash-plugin-git-")
            temporary_directory = temporary_parent.create_child(temporary_name)
        except (AnchoredFilesystemError, OSError) as exc:
            raise PluginLifecycleError(
                f"plugin Git temporary directory is unavailable: {exc}"
            ) from exc
        checkout_directory = temporary_directory.create_child("plugin")
        checkout = checkout_directory.descriptor_path()
        with tempfile.TemporaryFile() as error_output:
            process = subprocess.Popen(
                [
                    git_path,
                    "clone",
                    "--quiet",
                    "--depth",
                    "1",
                    "--branch",
                    ref,
                    "--single-branch",
                    source,
                    str(checkout),
                ],
                stdout=subprocess.DEVNULL,
                stderr=error_output,
                pass_fds=(checkout_directory.descriptor,)
                if os.name == "posix"
                else (),
                **process_tree_plan.spawn_options,
            )
            deadline = time.monotonic() + MAX_GIT_CLONE_SECONDS
            cleanup_done = False
            try:
                while True:
                    if _tree_exceeds_bytes_at(
                        temporary_directory,
                        MAX_GIT_CLONE_BYTES,
                    ):
                        try:
                            _terminate_git_clone(process, process_tree_plan)
                        except ProcessTreeError as exc:
                            cleanup_done = True
                            raise PluginLifecycleError(
                                "plugin Git clone exceeded "
                                f"{MAX_GIT_CLONE_BYTES} bytes; process-tree "
                                f"cleanup failed: {exc}"
                            ) from exc
                        cleanup_done = True
                        raise PluginLifecycleError(
                            f"plugin Git clone exceeds {MAX_GIT_CLONE_BYTES} bytes"
                        )
                    returncode = process.poll()
                    if returncode is not None:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        try:
                            _terminate_git_clone(process, process_tree_plan)
                        except ProcessTreeError as exc:
                            cleanup_done = True
                            raise PluginLifecycleError(
                                "plugin Git clone timed out after "
                                f"{MAX_GIT_CLONE_SECONDS} seconds; process-tree "
                                f"cleanup failed: {exc}"
                            ) from exc
                        cleanup_done = True
                        raise PluginLifecycleError(
                            "plugin Git clone timed out after "
                            f"{MAX_GIT_CLONE_SECONDS} seconds"
                        )
                    time.sleep(min(_GIT_CLONE_POLL_SECONDS, remaining))
            except BaseException as primary:
                if not cleanup_done:
                    try:
                        _terminate_git_clone(process, process_tree_plan)
                    except ProcessTreeError as exc:
                        primary.add_note(f"Process-tree cleanup failed: {exc}")
                raise
            error_output.seek(0)
            detail = error_output.read(MAX_GIT_ERROR_BYTES + 1)
        if returncode:
            detail_text = detail.decode("utf-8", errors="replace").strip()
            if len(detail) > MAX_GIT_ERROR_BYTES:
                detail_text = detail_text[:MAX_GIT_ERROR_BYTES] + "…"
            raise PluginLifecycleError(
                "could not clone plugin source"
                + (f": {detail_text}" if detail_text else "")
            )
        snapshot = PluginSnapshot.capture(
            checkout_directory,
            max_files=MAX_PLUGIN_FILES,
            max_bytes=MAX_PLUGIN_BYTES,
            max_entries=MAX_PLUGIN_TREE_ENTRIES,
            max_depth=MAX_PLUGIN_TREE_DEPTH,
            exclude_names=frozenset({".git"}),
        )
        if expected is not None:
            _verify_catalog_checkout(
                checkout,
                source,
                ref,
                expected,
                git_path=git_path,
                checkout_directory=checkout_directory,
                snapshot=snapshot,
            )
        _remove_git_metadata(checkout_directory)
        return install_local_plugin(
            checkout,
            destination_root=destination_root,
            replace=replace,
            validator=validator,
            _validator_at=_validator_at,
            _source_directory=checkout_directory,
            _snapshot=snapshot,
        )
    except BaseException as exc:
        git_primary = exc
        raise
    finally:
        if temporary_directory is not None and checkout_directory is not None:
            try:
                temporary_directory.remove_tree(
                    "plugin",
                    expected_descriptor=checkout_directory.descriptor,
                )
            except BaseException as cleanup:
                cleanup_errors.append(cleanup)
        if checkout_directory is not None:
            try:
                checkout_directory.close()
            except BaseException as cleanup:
                cleanup_errors.append(cleanup)
        if (
            temporary_parent is not None
            and temporary_directory is not None
            and temporary_name is not None
        ):
            try:
                temporary_parent.remove_tree(
                    temporary_name,
                    expected_descriptor=temporary_directory.descriptor,
                )
            except BaseException as cleanup:
                cleanup_errors.append(cleanup)
        if temporary_directory is not None:
            try:
                temporary_directory.close()
            except BaseException as cleanup:
                cleanup_errors.append(cleanup)
        if temporary_parent is not None:
            try:
                temporary_parent.close()
            except BaseException as cleanup:
                cleanup_errors.append(cleanup)
        if snapshot is not None:
            try:
                snapshot.close()
            except BaseException as cleanup:
                cleanup_errors.append(cleanup)
        if cleanup_errors:
            if git_primary is not None:
                for secondary_error in cleanup_errors:
                    git_primary.add_note(
                        f"Git checkout cleanup failed: {secondary_error}"
                    )
            else:
                raise PluginLifecycleError(
                    f"Git checkout cleanup failed: {cleanup_errors[0]}"
                ) from cleanup_errors[0]


def _tree_exceeds_bytes_at(directory: AnchoredDirectory, limit: int) -> bool:
    """Bound a checkout using the held directory identity, not its pathname."""

    total = 0

    def walk(current: AnchoredDirectory) -> bool:
        nonlocal total
        for name in current.list_names():
            metadata = current.stat(name)
            if metadata is None or stat.S_ISLNK(metadata.st_mode):
                continue
            if stat.S_ISDIR(metadata.st_mode):
                child = current.child(name, expected=metadata)
                try:
                    if walk(child):
                        return True
                finally:
                    child.close()
                continue
            total += metadata.st_size
            if total > limit:
                return True
        return False

    return walk(directory)


def _terminate_git_clone(
    process: subprocess.Popen[Any],
    plan: ProcessTreePlan,
) -> None:
    terminate_process_tree_sync(process, plan=plan, timeout_seconds=1.0)


def _remove_git_metadata(directory: AnchoredDirectory) -> None:
    metadata = directory.stat(".git")
    if metadata is None:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise PluginLifecycleError("Git checkout metadata cannot be a link")
    if stat.S_ISDIR(metadata.st_mode):
        git_directory = directory.child(".git", expected=metadata)
        try:
            directory.remove_tree(
                ".git",
                expected_descriptor=git_directory.descriptor,
            )
        finally:
            git_directory.close()
        return
    if stat.S_ISREG(metadata.st_mode):
        directory.unlink(".git", expected=metadata)
        return
    raise PluginLifecycleError("Git checkout metadata is not a regular entry")


def _verify_catalog_checkout(
    checkout: Path,
    source: str,
    ref: str,
    expected: CatalogEntry,
    *,
    git_path: str,
    checkout_directory: AnchoredDirectory | None = None,
    snapshot: PluginSnapshot | None = None,
) -> None:
    if expected.source != source or expected.ref != ref:
        raise PluginCatalogError("catalog entry does not match requested plugin source")

    if checkout_directory is None:
        try:
            metadata = os.stat(checkout, follow_symlinks=False)
            with AnchoredDirectory.open(
                checkout,
                create=False,
                private=False,
                expected=metadata,
            ) as held_checkout:
                _verify_catalog_checkout(
                    checkout,
                    source,
                    ref,
                    expected,
                    git_path=git_path,
                    checkout_directory=held_checkout,
                )
                return
        except (OSError, AnchoredFilesystemError) as exc:
            raise PluginCatalogError(
                f"invalid catalog plugin checkout: {exc}"
            ) from exc

    held_checkout = checkout_directory

    def git(arguments: list[str]) -> str:
        completed = subprocess.run(
            [
                git_path,
                "-C",
                str(
                    held_checkout.descriptor_path()
                ),
                *arguments,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
            pass_fds=(held_checkout.descriptor,) if os.name == "posix" else (),
        )
        if completed.returncode:
            raise PluginCatalogError("could not verify catalog plugin revision")
        return completed.stdout.strip()

    if git(["rev-parse", "HEAD"]) != expected.digest:
        raise PluginCatalogError("catalog plugin digest does not match cloned revision")
    try:
        if snapshot is not None:
            staged_manifest = _load_manifest_snapshot(snapshot)
            _validate_manifest_snapshot(staged_manifest, snapshot)
        else:
            _validate_tree_at(held_checkout)
            staged_manifest = _load_manifest_at(held_checkout, "plugin.json")
            _validate_manifest_at(staged_manifest, held_checkout)
    except (OSError, UnicodeError, ValueError, TypeError, AnchoredFilesystemError) as exc:
        raise PluginCatalogError(f"invalid catalog plugin checkout: {exc}") from exc
    if staged_manifest.name != expected.name:
        raise PluginCatalogError("catalog plugin name does not match checkout manifest")
    if staged_manifest.version != expected.version:
        raise PluginCatalogError(
            "catalog plugin version does not match checkout manifest"
        )


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _temporary_parent_path() -> Path:
    """Return the lexical temporary parent with macOS's stable /var alias fixed.

    macOS commonly exposes the temporary directory below ``/var`` while
    ``/var`` itself is the system alias for ``/private/var``.  The anchored
    walker intentionally rejects replaceable symlink components, so normalize
    this one documented system alias before opening it.  User-controlled links
    below the alias remain visible to and rejected by ``AnchoredDirectory``.
    """

    temporary = Path(os.path.abspath(Path(tempfile.gettempdir())))
    var_alias = Path("/var")
    try:
        relative = temporary.relative_to(var_alias)
        if Path(os.path.realpath(var_alias)) == Path("/private/var"):
            return Path("/private/var").joinpath(*relative.parts)
    except (OSError, ValueError):
        pass
    return temporary


def _validate_plugin_name(name: str) -> None:
    if not PLUGIN_NAME.fullmatch(name):
        raise PluginLifecycleError("plugin name must be a path-safe identifier")


def _validate_lifecycle_path(path: Path, label: str) -> None:
    for candidate in (path, path.parent):
        if _is_link(candidate):
            raise PluginLifecycleError(f"{label} cannot traverse a link: {candidate}")


def _require_anchored_plugin_mutation() -> None:
    if not supports_anchored_mutation():
        raise PluginLifecycleError(
            "descriptor-anchored plugin lifecycle mutation is unavailable "
            "on this platform/build"
        )
    try:
        require_anchored_mutation()
    except AnchoredFilesystemUnavailable as exc:
        raise PluginLifecycleError(str(exc)) from exc
    except AnchoredFilesystemError as exc:
        raise PluginLifecycleError(str(exc)) from exc


def _lifecycle_error(label: str, exc: BaseException) -> PluginLifecycleError:
    if getattr(exc, "errno", None) == errno.ELOOP:
        return PluginLifecycleError(f"{label} cannot traverse a link")
    detail = str(exc)
    if "link" in detail.lower():
        return PluginLifecycleError(f"{label} cannot use a linked entry: {detail}")
    return PluginLifecycleError(f"cannot securely mutate {label}: {detail}")


def _verify_anchored_path(directory: AnchoredDirectory, path: Path) -> None:
    try:
        held = os.fstat(directory.descriptor)
        current = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise AnchoredFilesystemError(
            "anchored plugin validation path was displaced"
        ) from exc
    if not _same_metadata(held, current) or not stat.S_ISDIR(current.st_mode):
        raise AnchoredFilesystemError("anchored plugin validation path was displaced")


def _verify_visible_destination(
    root_directory: AnchoredDirectory,
    destination_directory: AnchoredDirectory,
    destination: Path,
) -> None:
    """Verify ordinary Path visibility before returning it to the caller."""

    _verify_anchored_path(root_directory, root_directory.path)
    try:
        visible = os.stat(destination, follow_symlinks=False)
        held = os.fstat(destination_directory.descriptor)
    except OSError as exc:
        raise AnchoredFilesystemError(
            "installed plugin destination is no longer visible at its authorized path"
        ) from exc
    if not _same_metadata(visible, held) or not stat.S_ISDIR(visible.st_mode):
        raise AnchoredFilesystemError(
            "installed plugin destination was replaced before it could be returned"
        )


def _same_metadata(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
    )


def _restore_moved_entry(
    directory: AnchoredDirectory,
    original_name: str,
    moved_name: str,
    *,
    conflict_prefix: str,
    expected_descriptor: int | None = None,
) -> None:
    moved = directory.stat(moved_name)
    if moved is None:
        return
    if expected_descriptor is not None and not directory.same_entry(moved_name, expected_descriptor):
        raise AnchoredFilesystemError("moved entry changed before rollback")
    current = directory.stat(original_name)
    if current is None:
        directory.rename(
            moved_name,
            original_name,
            expected_source_descriptor=expected_descriptor,
        )
        return
    conflict_name = directory.unique_name(conflict_prefix)
    # Preserve an unexpected occupant as residue under a conflict name; never
    # recursively delete or overwrite it during compensation.
    directory.rename(original_name, conflict_name, expected_source=current)
    try:
        directory.rename(
            moved_name,
            original_name,
            expected_source_descriptor=expected_descriptor,
        )
    except BaseException as primary:
        try:
            if directory.stat(conflict_name) is not None:
                directory.rename(conflict_name, original_name, expected_source=current)
        except BaseException as cleanup:
            primary.add_note(f"rollback conflict restoration failed: {cleanup}")
        raise


def _read_extension_state_at(
    directory: AnchoredDirectory,
    state_path: Path,
) -> ExtensionState:
    raw = directory.read_file(
        state_path.name,
        max_bytes=MAX_EXTENSION_STATE_BYTES,
    )
    if raw is None:
        return ExtensionState()
    return _parse_extension_state(raw, state_path)


def _load_manifest_snapshot(snapshot: PluginSnapshot) -> PluginManifest:
    raw = snapshot.read_bytes("plugin.json", max_bytes=MAX_PLUGIN_MANIFEST_BYTES)
    payload = strict_json_loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("plugin manifest must be a JSON object")
    return PluginManifest.from_dict(payload)


def _load_manifest_at(
    directory: AnchoredDirectory,
    name: str,
) -> PluginManifest:
    raw = directory.read_file(name, max_bytes=MAX_PLUGIN_MANIFEST_BYTES)
    if raw is None:
        raise ValueError("plugin manifest not found")
    payload = strict_json_loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("plugin manifest must be a JSON object")
    return PluginManifest.from_dict(payload)


def _installed_plugin_versions_at(
    directory: AnchoredDirectory,
    *,
    excluding: str,
) -> dict[str, str]:
    installed_versions: dict[str, str] = {}
    for name in sorted(directory.list_names()):
        if name == excluding:
            continue
        metadata = directory.stat(name)
        if metadata is None or not stat.S_ISDIR(metadata.st_mode):
            continue
        try:
            with directory.child(name) as plugin_directory:
                manifest = _load_manifest_at(plugin_directory, "plugin.json")
        except (AnchoredFilesystemError, OSError, ValueError, TypeError):
            continue
        installed_versions[manifest.name] = manifest.version
    return installed_versions


def _save_extension_state(state: ExtensionState, path: Path) -> None:
    _require_anchored_plugin_mutation()
    try:
        with (
            AnchoredDirectory.open(path.parent, create=True) as directory,
            directory.lock(f".{path.name}.lock"),
        ):
            _save_extension_state_at(directory, state, path)
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise _lifecycle_error("extension state", exc) from exc


def _save_extension_state_at(
    directory: AnchoredDirectory,
    state: ExtensionState,
    path: Path,
) -> None:
    payload: dict[str, Any] = {
        "version": STATE_VERSION,
        "disabled_plugins": sorted(state.disabled_plugins),
    }
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    existing = directory.stat(path.name)
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise PluginLifecycleError(f"extension state is not a regular file: {path}")
    temporary = directory.unique_name(f".{path.name}.", ".tmp")
    descriptor = -1
    temporary_identity: os.stat_result | None = None
    try:
        descriptor = directory.create_file(temporary, mode=0o600)
        temporary_identity = os.fstat(descriptor)
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        directory.rename(temporary, path.name, expected_source=temporary_identity)
        temporary = ""
        directory.sync()
    except BaseException as primary:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except BaseException as cleanup:
                primary.add_note(f"extension-state descriptor close failed: {cleanup}")
            descriptor = -1
        if temporary:
            try:
                directory.unlink(
                    temporary,
                    expected=temporary_identity,
                    missing_ok=True,
                )
            except BaseException as cleanup:
                primary.add_note(f"extension-state temporary cleanup failed: {cleanup}")
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise PluginLifecycleError("short write to extension state")
        view = view[written:]
