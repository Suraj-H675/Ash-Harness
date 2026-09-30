"""Transactional plugin publication, removal, and Git acquisition."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import time
from contextlib import nullcontext
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, parse as parse_version

from ash.plugins._lifecycle_support import (
    lifecycle_error as _lifecycle_error,
    require_anchored_plugin_mutation as _require_anchored_plugin_mutation,
    sync_directory_strict as _sync_directory_strict,
    validate_plugin_name as _validate_plugin_name,
)
from ash.plugins.errors import PluginLifecycleError
from ash.plugins.git_source import GIT_DIGEST_PATTERN, validate_plugin_git_source
from ash.plugins.install_records import (
    MAX_PLUGIN_INSTALL_RECORDS as MAX_PLUGIN_INSTALL_RECORDS,
    MAX_PLUGIN_INSTALL_RECORDS_BYTES as MAX_PLUGIN_INSTALL_RECORDS_BYTES,
    PLUGIN_INSTALL_RECORDS_FILENAME as PLUGIN_INSTALL_RECORDS_FILENAME,
    PLUGIN_INSTALL_RECORDS_VERSION as PLUGIN_INSTALL_RECORDS_VERSION,
    PluginInstallRecord as PluginInstallRecord,
    _read_plugin_install_records_at,
    load_plugin_install_records as load_plugin_install_records,
    plugin_install_records_path as plugin_install_records_path,
    require_plugin_install_record_current as require_plugin_install_record_current,
    require_plugin_install_tree_current as require_plugin_install_tree_current,
    transition_plugin_install_records_at as _transition_plugin_install_records_at,
    user_plugin_root as user_plugin_root,
    validate_plugin_install_record as _validate_plugin_install_record,
)
from ash.plugins.lifecycle_journal import (
    PluginLifecycleJournal,
    TreeIdentity,
    clear_lifecycle_journal_at,
    read_lifecycle_journal_at,
    write_lifecycle_journal_at,
)
from ash.plugins.state import (
    MAX_EXTENSION_STATE_BYTES as MAX_EXTENSION_STATE_BYTES,
    STATE_VERSION as STATE_VERSION,
    ExtensionState,
    extension_state_path as extension_state_path,
    load_extension_state as load_extension_state,
    locked_extension_state,
    set_plugin_enabled as set_plugin_enabled,
    transition_extension_state as _transition_extension_state,
    transition_extension_state_checked as _transition_extension_state_checked,
)

from ash.safe_io import strict_json_loads
from ash.safety.anchored_fs import (
    AnchoredDirectory,
    AnchoredFilesystemError,
)
from ash.plugins.snapshot import (
    MAX_PLUGIN_BYTES,
    MAX_PLUGIN_FILES,
    PluginSnapshot,
    PluginSnapshotError,
)
from ash.plugins.manifest import (
    MAX_PLUGIN_MANIFEST_BYTES,
    PluginManifest,
    validate_plugin_identity,
)
from ash.plugins.catalog import CatalogEntry, PluginCatalogError
from ash.plugins.registry import (
    MAX_PLUGIN_TREE_DEPTH,
    MAX_PLUGIN_TREE_ENTRIES,
)
from ash.plugins.validation import validate_plugin_contents_at
from ash.safety.environment import resolve_host_executable
from ash.safety.git import isolated_git_environment
from ash.sandbox.process_utils import (
    ProcessTreeError,
    ProcessTreeUnavailable,
    ProcessTreePlan,
    prepare_process_tree,
    terminate_process_tree_sync,
)

MAX_GIT_CLONE_BYTES = MAX_PLUGIN_BYTES
MAX_GIT_CLONE_SECONDS = 300
MAX_GIT_ERROR_BYTES = 64 * 1024
_GIT_CLONE_POLL_SECONDS = 0.05
_INSTALL_RECORD_UNCHANGED = object()


@dataclass(frozen=True)
class InstalledPlugin:
    name: str
    version: str
    root: Path
    install_record: PluginInstallRecord | None = None


def _validate_activation_transition(
    manifest: PluginManifest,
    manifests: Mapping[str, PluginManifest],
    state: ExtensionState,
    *,
    enabled: bool,
) -> None:
    if enabled:
        versions = {
            candidate: installed.version
            for candidate, installed in manifests.items()
            if candidate != manifest.name
            and candidate not in state.disabled_plugins
        }
        errors = manifest.check_dependencies(versions)
        if errors:
            raise PluginLifecycleError("; ".join(errors))
        return

    dependents = sorted(
        candidate
        for candidate, installed in manifests.items()
        if candidate != manifest.name
        and candidate not in state.disabled_plugins
        and any(
            dependency.get("name") == manifest.name
            for dependency in installed.dependencies
        )
    )
    if dependents:
        raise PluginLifecycleError(
            f"cannot disable {manifest.name!r}; required by: "
            + ", ".join(dependents)
        )


def recover_plugin_lifecycle(destination_root: Path | None = None) -> bool:
    """Recover one interrupted serialized plugin mutation, if present."""

    root = (destination_root or user_plugin_root()).expanduser()
    _require_anchored_plugin_mutation()
    try:
        with (
            AnchoredDirectory.open(root, create=False) as root_directory,
            root_directory.lock(".ash-lifecycle.lock"),
        ):
            return _recover_plugin_lifecycle_at(root_directory, root)
    except FileNotFoundError:
        return False
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise _lifecycle_error("plugin lifecycle recovery", exc) from exc


def _recover_plugin_lifecycle_at(
    root_directory: AnchoredDirectory,
    root: Path,
) -> bool:
    journal = read_lifecycle_journal_at(root_directory)
    if journal is None:
        return False
    if not journal.root_tree.matches(os.fstat(root_directory.descriptor)):
        raise PluginLifecycleError("plugin lifecycle root changed before crash recovery")
    if journal.phase == "committed":
        _finish_committed_lifecycle_at(root_directory, root, journal)
    else:
        _rollback_prepared_lifecycle_at(root_directory, root, journal)
    clear_lifecycle_journal_at(root_directory)
    return True


def _sync_lifecycle_root(root_directory: AnchoredDirectory) -> None:
    root_directory.sync()
    _sync_directory_strict(
        root_directory.descriptor,
        label="plugin lifecycle root",
    )


def _rollback_prepared_lifecycle_at(
    root_directory: AnchoredDirectory,
    root: Path,
    journal: PluginLifecycleJournal,
) -> None:
    if journal.operation == "install":
        _rollback_prepared_install_tree(root_directory, journal)
        _remove_journal_stage(root_directory, journal)
    else:
        _rollback_prepared_uninstall_tree(root_directory, journal)
    _restore_journal_record(root_directory, root, journal)
    _restore_journal_activation(journal)
    _sync_lifecycle_root(root_directory)


def _finish_committed_lifecycle_at(
    root_directory: AnchoredDirectory,
    root: Path,
    journal: PluginLifecycleJournal,
) -> None:
    if journal.operation == "install":
        current = root_directory.stat(journal.plugin)
        if (
            current is None
            or journal.live_tree is None
            or not journal.live_tree.matches(current)
        ):
            raise PluginLifecycleError(
                f"committed plugin {journal.plugin!r} changed before crash recovery"
            )
        _verify_journal_record(root_directory, root, journal)
        _verify_journal_activation(journal)
        _remove_journal_moved_tree(root_directory, journal)
        _remove_journal_stage(root_directory, journal)
    else:
        if root_directory.stat(journal.plugin) is not None:
            raise PluginLifecycleError(
                f"committed uninstall for {journal.plugin!r} has a live destination"
            )
        _verify_journal_record(root_directory, root, journal)
        _verify_journal_activation(journal)
        _remove_journal_moved_tree(root_directory, journal)
    _sync_lifecycle_root(root_directory)


def _rollback_prepared_install_tree(
    root_directory: AnchoredDirectory,
    journal: PluginLifecycleJournal,
) -> None:
    if journal.moved_name is not None:
        previous = _open_journal_moved_tree(root_directory, journal)
        try:
            current = root_directory.stat(journal.plugin)
            if (
                current is not None
                and journal.live_tree is not None
                and journal.live_tree.matches(current)
            ):
                with root_directory.child(
                    journal.plugin,
                    expected=current,
                ) as candidate:
                    root_directory.remove_tree(
                        journal.plugin,
                        expected_descriptor=candidate.descriptor,
                    )
            _restore_moved_entry(
                root_directory,
                journal.plugin,
                journal.moved_name,
                conflict_prefix=f".{journal.plugin}.install-conflict-",
                expected_descriptor=previous.descriptor,
            )
        finally:
            previous.close()
        return

    current = root_directory.stat(journal.plugin)
    if current is None:
        return
    if journal.live_tree is None or not journal.live_tree.matches(current):
        raise PluginLifecycleError(
            f"cannot safely recover interrupted install for {journal.plugin!r}; "
            "live tree identity is unknown"
        )
    with root_directory.child(journal.plugin, expected=current) as candidate:
        root_directory.remove_tree(
            journal.plugin,
            expected_descriptor=candidate.descriptor,
        )


def _remove_journal_stage(
    root_directory: AnchoredDirectory,
    journal: PluginLifecycleJournal,
) -> None:
    if journal.stage_name is None or journal.stage_tree is None:
        return
    metadata = root_directory.stat(journal.stage_name)
    if metadata is None:
        return
    if not journal.stage_tree.matches(metadata):
        raise PluginLifecycleError(
            f"plugin lifecycle recovery stage changed: {journal.stage_name}"
        )
    with root_directory.child(journal.stage_name, expected=metadata) as stage:
        root_directory.remove_tree(
            journal.stage_name,
            expected_descriptor=stage.descriptor,
        )


def _rollback_prepared_uninstall_tree(
    root_directory: AnchoredDirectory,
    journal: PluginLifecycleJournal,
) -> None:
    moved = root_directory.stat(journal.moved_name or "")
    if moved is None:
        current = root_directory.stat(journal.plugin)
        if (
            current is not None
            and journal.previous_tree is not None
            and journal.previous_tree.matches(current)
        ):
            return
        raise PluginLifecycleError(
            f"cannot safely recover interrupted uninstall for {journal.plugin!r}"
        )
    previous = _open_journal_moved_tree(root_directory, journal)
    try:
        _restore_moved_entry(
            root_directory,
            journal.plugin,
            journal.moved_name or "",
            conflict_prefix=f".{journal.plugin}.uninstall-conflict-",
            expected_descriptor=previous.descriptor,
        )
    finally:
        previous.close()


def _open_journal_moved_tree(
    root_directory: AnchoredDirectory,
    journal: PluginLifecycleJournal,
) -> AnchoredDirectory:
    if journal.moved_name is None or journal.previous_tree is None:
        raise PluginLifecycleError("plugin lifecycle journal lacks moved-tree identity")
    metadata = root_directory.stat(journal.moved_name)
    if metadata is None or not journal.previous_tree.matches(metadata):
        raise PluginLifecycleError(
            f"plugin lifecycle recovery tree changed: {journal.moved_name}"
        )
    return root_directory.child(journal.moved_name, expected=metadata)


def _remove_journal_moved_tree(
    root_directory: AnchoredDirectory,
    journal: PluginLifecycleJournal,
) -> None:
    if journal.moved_name is None:
        return
    metadata = root_directory.stat(journal.moved_name)
    if metadata is None:
        return
    moved = _open_journal_moved_tree(root_directory, journal)
    try:
        root_directory.remove_tree(
            journal.moved_name,
            expected_descriptor=moved.descriptor,
        )
    finally:
        moved.close()


def _restore_journal_record(
    root_directory: AnchoredDirectory,
    root: Path,
    journal: PluginLifecycleJournal,
) -> None:
    if not journal.manage_record:
        return
    records = _read_plugin_install_records_at(root_directory, root)
    current = records.get(journal.plugin)
    if current not in {journal.previous_record, journal.desired_record}:
        raise PluginLifecycleError(
            f"plugin {journal.plugin!r} provenance changed before crash recovery"
        )
    restored = dict(records)
    if journal.previous_record is None:
        restored.pop(journal.plugin, None)
    else:
        restored[journal.plugin] = journal.previous_record
    _transition_plugin_install_records_at(root_directory, records, restored)


def _verify_journal_record(
    root_directory: AnchoredDirectory,
    root: Path,
    journal: PluginLifecycleJournal,
) -> None:
    if not journal.manage_record:
        return
    current = _read_plugin_install_records_at(root_directory, root).get(journal.plugin)
    if current != journal.desired_record:
        raise PluginLifecycleError(
            f"committed plugin {journal.plugin!r} provenance changed before recovery"
        )


def _restore_journal_activation(journal: PluginLifecycleJournal) -> None:
    if journal.state_path is None or journal.previous_disabled is None:
        return
    _verify_journal_state_parent(journal)
    _transition_extension_state(
        journal.plugin,
        enabled=not journal.previous_disabled,
        path=Path(journal.state_path),
    )


def _verify_journal_activation(journal: PluginLifecycleJournal) -> None:
    if journal.state_path is None or journal.desired_disabled is None:
        return
    _verify_journal_state_parent(journal)
    state = load_extension_state(Path(journal.state_path))
    if (journal.plugin in state.disabled_plugins) != journal.desired_disabled:
        raise PluginLifecycleError(
            f"committed plugin {journal.plugin!r} activation state changed before recovery"
        )


def _state_parent_identity(path: Path) -> TreeIdentity:
    try:
        with AnchoredDirectory.open(
            path.parent,
            create=True,
            private=False,
        ) as directory:
            return TreeIdentity.from_stat(os.fstat(directory.descriptor))
    except (AnchoredFilesystemError, OSError) as exc:
        raise PluginLifecycleError(
            f"plugin activation state parent is unavailable: {path.parent}"
        ) from exc


def _verify_journal_state_parent(journal: PluginLifecycleJournal) -> None:
    if journal.state_path is None or journal.state_parent_tree is None:
        return
    parent = Path(journal.state_path).parent
    try:
        with AnchoredDirectory.open(
            parent,
            create=False,
            private=False,
        ) as directory:
            if not journal.state_parent_tree.matches(os.fstat(directory.descriptor)):
                raise PluginLifecycleError(
                    "plugin activation state parent changed before crash recovery"
                )
    except FileNotFoundError as exc:
        raise PluginLifecycleError(
            "plugin activation state parent disappeared before crash recovery"
        ) from exc
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise PluginLifecycleError(
            "plugin activation state parent could not be verified during recovery"
        ) from exc


def load_managed_plugin_for_update(
    name: str,
) -> tuple[InstalledPlugin, PluginInstallRecord]:
    """Read one managed update identity from Ash-owned provenance.

    The live plugin tree is deliberately not trusted for update eligibility. A
    changed trusted revision may be the recovery path for arbitrary local tree
    damage, including a missing or malformed manifest. Unchanged revisions
    still verify the complete installed tree against trusted source before
    reporting a no-op.
    """

    _validate_plugin_name(name)
    root = user_plugin_root().expanduser()
    destination = root / name
    _require_anchored_plugin_mutation()
    try:
        with (
            AnchoredDirectory.open(root, create=False) as root_directory,
            root_directory.lock(".ash-lifecycle.lock"),
        ):
            _recover_plugin_lifecycle_at(root_directory, root)
            record = _read_plugin_install_records_at(root_directory, root).get(name)
            if record is None:
                raise PluginLifecycleError(
                    f"plugin {name!r} is not tracked for updates; reinstall it from Git "
                    "or a signed catalog"
                )
            return InstalledPlugin(record.name, record.version, destination, record), record
    except FileNotFoundError as exc:
        raise PluginLifecycleError(
            f"plugin {name!r} is not tracked for updates; reinstall it from Git "
            "or a signed catalog"
        ) from exc
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise _lifecycle_error("managed plugin update state", exc) from exc


def set_local_plugin_enabled(
    name: str,
    *,
    enabled: bool,
    state_path: Path | None = None,
) -> InstalledPlugin:
    """Atomically validate plugin topology and change one user plugin's state."""

    _validate_plugin_name(name)
    root = user_plugin_root().expanduser()
    destination = root / name
    _require_anchored_plugin_mutation()
    try:
        with (
            AnchoredDirectory.open(root, create=False) as root_directory,
            root_directory.lock(".ash-lifecycle.lock"),
        ):
            _recover_plugin_lifecycle_at(root_directory, root)
            root_directory.prepare_durable_mutation()
            manifests = _installed_plugin_manifests_strict_at(root_directory)
            manifest = manifests.get(name)
            if manifest is None:
                raise PluginLifecycleError(f"plugin is not installed: {name}")

            if enabled:
                metadata = root_directory.stat(name)
                if (
                    metadata is None
                    or stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISDIR(metadata.st_mode)
                ):
                    raise PluginLifecycleError(f"plugin is not installed: {name}")
                with root_directory.child(name, expected=metadata) as plugin_directory:
                    snapshot = PluginSnapshot.capture(
                        plugin_directory,
                        max_files=MAX_PLUGIN_FILES,
                        max_bytes=MAX_PLUGIN_BYTES,
                        max_entries=MAX_PLUGIN_TREE_ENTRIES,
                        max_depth=MAX_PLUGIN_TREE_DEPTH,
                    )
                    try:
                        snapshot_manifest = _load_manifest_snapshot(snapshot)
                        _validate_manifest_snapshot(snapshot_manifest, snapshot)
                        if snapshot_manifest != manifest:
                            raise PluginLifecycleError(
                                f"plugin {name!r} changed while enablement was checked"
                            )
                        validate_plugin_contents_at(snapshot, snapshot_manifest)
                    finally:
                        snapshot.close()
                    if not root_directory.same_entry(name, plugin_directory.descriptor):
                        raise PluginLifecycleError(
                            f"plugin {name!r} changed while enablement was checked"
                        )

            def validate_state(state: ExtensionState) -> None:
                _validate_activation_transition(
                    manifest,
                    manifests,
                    state,
                    enabled=enabled,
                )

            _transition_extension_state_checked(
                name,
                enabled=enabled,
                path=state_path,
                validator=validate_state,
            )
            return InstalledPlugin(manifest.name, manifest.version, destination)
    except FileNotFoundError as exc:
        raise PluginLifecycleError(f"plugin is not installed: {name}") from exc
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise _lifecycle_error("plugin lifecycle", exc) from exc


def install_local_plugin(
    source: Path,
    *,
    destination_root: Path | None = None,
    replace: bool = False,
    enabled: bool | None = None,
    state_path: Path | None = None,
    validator: Callable[[Path, PluginManifest], None] | None = None,
    _validator_at: Callable[[PluginSnapshot, PluginManifest], None] | None = None,
    _topology_validator: (
        Callable[[PluginManifest, Mapping[str, PluginManifest]], None] | None
    ) = None,
    _source_directory: AnchoredDirectory | None = None,
    _snapshot: PluginSnapshot | None = None,
    _state_validator: (
        Callable[
            [PluginManifest, ExtensionState, Mapping[str, PluginManifest]],
            None,
        ]
        | None
    ) = None,
    _install_record: PluginInstallRecord | None | object = _INSTALL_RECORD_UNCHANGED,
    _expected_install_record: PluginInstallRecord | object = _INSTALL_RECORD_UNCHANGED,
) -> InstalledPlugin:
    if enabled is not None and destination_root is not None:
        raise PluginLifecycleError(
            "plugin activation state is only available for the user plugin root"
        )
    if enabled is not None and _state_validator is not None:
        raise PluginLifecycleError(
            "plugin activation and publication-state validation cannot be combined"
        )
    source_path = source.expanduser()
    if destination_root is None and _install_record is _INSTALL_RECORD_UNCHANGED:
        _install_record = None
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
            if isinstance(_install_record, PluginInstallRecord):
                _validate_plugin_install_record(_install_record)
                if (
                    _install_record.name != manifest.name
                    or _install_record.version != manifest.version
                ):
                    raise PluginLifecycleError(
                        "plugin install record does not match plugin manifest"
                    )
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
                _recover_plugin_lifecycle_at(root_directory, root)
                root_directory.prepare_durable_mutation()
                install_records_before: dict[str, PluginInstallRecord] | None = None
                if (
                    _install_record is not _INSTALL_RECORD_UNCHANGED
                    or _expected_install_record is not _INSTALL_RECORD_UNCHANGED
                ):
                    install_records_before = _read_plugin_install_records_at(
                        root_directory,
                        root,
                    )
                if isinstance(_expected_install_record, PluginInstallRecord):
                    assert install_records_before is not None
                    if (
                        install_records_before.get(_expected_install_record.name)
                        != _expected_install_record
                    ):
                        raise PluginLifecycleError(
                            f"plugin {_expected_install_record.name!r} changed while "
                            "update was in progress"
                        )
                existing = root_directory.stat(manifest.name)
                current_version: str | None = None
                if existing is not None:
                    if stat.S_ISLNK(existing.st_mode) or not stat.S_ISDIR(existing.st_mode):
                        raise PluginLifecycleError(
                            f"plugin destination is not a directory: {destination}"
                        )
                    if not replace:
                        raise PluginLifecycleError(
                            f"plugin {manifest.name!r} is already installed; use --replace"
                        )
                    try:
                        with root_directory.child(
                            manifest.name,
                            expected=existing,
                        ) as current_directory:
                            current_manifest = _load_manifest_at(
                                current_directory,
                                "plugin.json",
                            )
                            _validate_manifest_at(current_manifest, current_directory)
                    except (
                        AnchoredFilesystemError,
                        OSError,
                        ValueError,
                        TypeError,
                    ):
                        current_manifest = None
                    if (
                        current_manifest is not None
                        and current_manifest.name == manifest.name
                    ):
                        current_version = current_manifest.version

                installed_manifests = _installed_plugin_manifests_at(
                    root_directory,
                    excluding=manifest.name,
                )
                activation_manifests = dict(installed_manifests)
                activation_manifests[manifest.name] = manifest
                installed_versions = {
                    name: installed.version
                    for name, installed in installed_manifests.items()
                }
                dependency_errors = manifest.check_dependencies(installed_versions)
                if dependency_errors:
                    raise PluginLifecycleError("; ".join(dependency_errors))
                reverse_dependency_errors = _new_reverse_dependency_errors(
                    installed_manifests,
                    target=manifest.name,
                    before_version=current_version,
                    after_version=manifest.version,
                )
                if reverse_dependency_errors:
                    raise PluginLifecycleError("; ".join(reverse_dependency_errors))
                if _topology_validator is not None:
                    _topology_validator(
                        manifest,
                        _installed_plugin_manifests_strict_at(
                            root_directory,
                            excluding=manifest.name,
                        ),
                    )

                stage_name = root_directory.unique_name(".install-", ".tmp")
                stage_directory = root_directory.create_child(stage_name)
                backup_name: str | None = None
                backup_directory: AnchoredDirectory | None = None
                destination_directory: AnchoredDirectory | None = None
                destination_moved = False
                published = False
                lifecycle_journal: PluginLifecycleJournal | None = None
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
                    backup_name = (
                        root_directory.unique_name(f".{manifest.name}.backup-")
                        if existing is not None
                        else None
                    )
                    state_journal_path: Path | None = None
                    previous_disabled: bool | None = None
                    desired_disabled: bool | None = None
                    if enabled is not None:
                        state_journal_path = Path(
                            state_path or extension_state_path()
                        ).expanduser().absolute()
                        state_parent_tree = _state_parent_identity(state_journal_path)
                        state_before = load_extension_state(state_journal_path)
                        previous_disabled = (
                            manifest.name in state_before.disabled_plugins
                        )
                        desired_disabled = not enabled
                    previous_record = (
                        install_records_before.get(manifest.name)
                        if install_records_before is not None
                        else None
                    )
                    desired_record = (
                        _install_record
                        if isinstance(_install_record, PluginInstallRecord)
                        else None
                    )
                    lifecycle_journal = PluginLifecycleJournal(
                        operation="install",
                        phase="prepared",
                        plugin=manifest.name,
                        root_tree=TreeIdentity.from_stat(
                            os.fstat(root_directory.descriptor)
                        ),
                        stage_name=stage_name,
                        stage_tree=TreeIdentity.from_stat(
                            os.fstat(stage_directory.descriptor)
                        ),
                        moved_name=backup_name,
                        previous_tree=(
                            TreeIdentity.from_stat(existing)
                            if existing is not None
                            else None
                        ),
                        manage_record=install_records_before is not None,
                        previous_record=previous_record,
                        desired_record=desired_record,
                        state_path=(
                            str(state_journal_path)
                            if state_journal_path is not None
                            else None
                        ),
                        state_parent_tree=(
                            state_parent_tree if enabled is not None else None
                        ),
                        previous_disabled=previous_disabled,
                        desired_disabled=desired_disabled,
                    )
                    write_lifecycle_journal_at(root_directory, lifecycle_journal)
                    state_guard = (
                        locked_extension_state(path=state_path)
                        if _state_validator is not None
                        else nullcontext(None)
                    )
                    with state_guard as publication_state:
                        if _state_validator is not None:
                            assert publication_state is not None
                            _state_validator(
                                staged_manifest,
                                publication_state,
                                _installed_plugin_manifests_strict_at(
                                    root_directory,
                                    excluding=staged_manifest.name,
                                ),
                            )
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
                            assert backup_name is not None
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
                        lifecycle_journal = lifecycle_journal.with_live_tree(
                            os.fstat(destination_directory.descriptor)
                        )
                        write_lifecycle_journal_at(
                            root_directory,
                            lifecycle_journal,
                        )
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
                        if install_records_before is not None:
                            install_records_after = dict(install_records_before)
                            if _install_record is None:
                                install_records_after.pop(manifest.name, None)
                            else:
                                assert isinstance(_install_record, PluginInstallRecord)
                                install_records_after[manifest.name] = _install_record
                            _transition_plugin_install_records_at(
                                root_directory,
                                install_records_before,
                                install_records_after,
                            )
                        if enabled is not None:
                            assert previous_disabled is not None

                            def validate_activation_state(
                                state: ExtensionState,
                            ) -> None:
                                if (
                                    manifest.name in state.disabled_plugins
                                ) != previous_disabled:
                                    raise PluginLifecycleError(
                                        f"plugin {manifest.name!r} activation state "
                                        "changed while installation was in progress"
                                    )
                                _validate_activation_transition(
                                    manifest,
                                    activation_manifests,
                                    state,
                                    enabled=enabled,
                                )

                            _transition_extension_state_checked(
                                manifest.name,
                                enabled=enabled,
                                path=state_path,
                                validator=validate_activation_state,
                            )
                        # Keep dependency/activation state stable through the
                        # actual commit checkpoint.  Once this succeeds, later
                        # state changes apply to an already committed plugin.
                        _sync_lifecycle_root(root_directory)
                        _verify_visible_destination(
                            root_directory,
                            destination_directory,
                            destination,
                        )
                        lifecycle_journal = lifecycle_journal.committed()
                        write_lifecycle_journal_at(
                            root_directory,
                            lifecycle_journal,
                        )
                        published = True
                    if destination_moved and backup_name is not None:
                        assert backup_directory is not None
                        root_directory.remove_tree(
                            backup_name,
                            expected_descriptor=backup_directory.descriptor,
                        )
                        backup_name = None
                    # Backup deletion is post-commit cleanup.  Failure here may
                    # be reported, but must not roll provenance back underneath
                    # an already committed live plugin tree.
                    _sync_lifecycle_root(root_directory)
                    _verify_visible_destination(
                        root_directory,
                        destination_directory,
                        destination,
                    )
                    root_directory.remove_tree(
                        stage_name,
                        expected_descriptor=stage_directory.descriptor,
                    )
                    stage_name = ""
                    _sync_lifecycle_root(root_directory)
                    clear_lifecycle_journal_at(root_directory)
                    lifecycle_journal = None
                    return InstalledPlugin(
                        manifest.name,
                        manifest.version,
                        destination,
                        (
                            _install_record
                            if isinstance(_install_record, PluginInstallRecord)
                            else None
                        ),
                    )
                except BaseException as exc:
                    primary = exc
                    if lifecycle_journal is not None and not published:
                        try:
                            _rollback_prepared_lifecycle_at(
                                root_directory,
                                root,
                                lifecycle_journal,
                            )
                            clear_lifecycle_journal_at(root_directory)
                            lifecycle_journal = None
                            backup_name = None
                        except BaseException as cleanup:
                            primary.add_note(
                                f"durable plugin installation rollback failed: {cleanup}"
                            )
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
    _preflight: (
        Callable[[PluginManifest, Mapping[str, PluginManifest]], None] | None
    ) = None,
) -> Path:
    _validate_plugin_name(name)
    if not confirmed:
        raise PluginLifecycleError("uninstall requires explicit confirmation")
    root = (destination_root or user_plugin_root()).expanduser()
    destination = root / name
    manage_install_record = destination_root is None
    manage_activation_state = destination_root is None or state_path is not None
    _require_anchored_plugin_mutation()
    try:
        with (
            AnchoredDirectory.open(root, create=False) as root_directory,
            root_directory.lock(".ash-lifecycle.lock"),
        ):
            _recover_plugin_lifecycle_at(root_directory, root)
            root_directory.prepare_durable_mutation()
            install_records_before: dict[str, PluginInstallRecord] | None = None
            install_records_after: dict[str, PluginInstallRecord] | None = None
            if manage_install_record:
                install_records_before = _read_plugin_install_records_at(
                    root_directory,
                    root,
                )
                install_records_after = dict(install_records_before)
                install_records_after.pop(name, None)
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
                if _preflight is not None:
                    _preflight(
                        manifest,
                        _installed_plugin_manifests_strict_at(root_directory),
                    )
                quarantine_name = root_directory.unique_name(f".{name}.uninstall-")
                state_journal_path: Path | None = None
                previous_disabled: bool | None = None
                desired_disabled: bool | None = None
                if manage_activation_state:
                    state_journal_path = Path(
                        state_path or extension_state_path()
                    ).expanduser().absolute()
                    state_parent_tree = _state_parent_identity(state_journal_path)
                    state_before = load_extension_state(state_journal_path)
                    previous_disabled = name in state_before.disabled_plugins
                    desired_disabled = False
                lifecycle_journal = PluginLifecycleJournal(
                    operation="uninstall",
                    phase="prepared",
                    plugin=name,
                    root_tree=TreeIdentity.from_stat(
                        os.fstat(root_directory.descriptor)
                    ),
                    moved_name=quarantine_name,
                    previous_tree=TreeIdentity.from_stat(destination_metadata),
                    manage_record=manage_install_record,
                    previous_record=(
                        install_records_before.get(name)
                        if install_records_before is not None
                        else None
                    ),
                    desired_record=None,
                    state_path=(
                        str(state_journal_path)
                        if state_journal_path is not None
                        else None
                    ),
                    state_parent_tree=(
                        state_parent_tree if manage_activation_state else None
                    ),
                    previous_disabled=previous_disabled,
                    desired_disabled=desired_disabled,
                )
                write_lifecycle_journal_at(root_directory, lifecycle_journal)
                primary: BaseException | None = None
                published = False
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
                    if (
                        install_records_before is not None
                        and install_records_after is not None
                    ):
                        _transition_plugin_install_records_at(
                            root_directory,
                            install_records_before,
                            install_records_after,
                        )
                    if manage_activation_state:
                        assert previous_disabled is not None

                        def validate_activation_state(state: ExtensionState) -> None:
                            if (name in state.disabled_plugins) != previous_disabled:
                                raise PluginLifecycleError(
                                    f"plugin {name!r} activation state changed while "
                                    "uninstall was in progress"
                                )

                        _transition_extension_state_checked(
                            name,
                            enabled=True,
                            path=state_path,
                            validator=validate_activation_state,
                        )
                    _sync_lifecycle_root(root_directory)
                    lifecycle_journal = lifecycle_journal.committed()
                    write_lifecycle_journal_at(root_directory, lifecycle_journal)
                    published = True
                    root_directory.remove_tree(
                        quarantine_name,
                        expected_descriptor=plugin_directory.descriptor,
                    )
                    _sync_lifecycle_root(root_directory)
                    clear_lifecycle_journal_at(root_directory)
                except BaseException as exc:
                    primary = exc
                    if not published:
                        try:
                            _rollback_prepared_lifecycle_at(
                                root_directory,
                                root,
                                lifecycle_journal,
                            )
                            clear_lifecycle_journal_at(root_directory)
                            quarantine_name = ""
                        except BaseException as cleanup:
                            primary.add_note(
                                f"durable plugin uninstall rollback failed: {cleanup}"
                            )
                    raise
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise _lifecycle_error("plugin destination root", exc) from exc
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
    enabled: bool | None = None,
    state_path: Path | None = None,
    validator: Callable[[Path, PluginManifest], None] | None = None,
    _validator_at: Callable[[PluginSnapshot, PluginManifest], None] | None = None,
    expected: CatalogEntry | None = None,
    _skip_if_digest: str | None = None,
    _unchanged: InstalledPlugin | None = None,
    _expected_previous_record: PluginInstallRecord | None = None,
    _topology_validator: (
        Callable[[PluginManifest, Mapping[str, PluginManifest]], None] | None
    ) = None,
    _state_validator: (
        Callable[
            [PluginManifest, ExtensionState, Mapping[str, PluginManifest]],
            None,
        ]
        | None
    ) = None,
) -> InstalledPlugin:
    _require_anchored_plugin_mutation()
    try:
        validate_plugin_git_source(source)
    except ValueError as exc:
        raise PluginLifecycleError(str(exc)) from exc
    if not ref:
        raise PluginLifecycleError("plugin Git source requires an explicit --ref")
    if len(ref) > 255 or "\x00" in ref or "\n" in ref or "\r" in ref:
        raise PluginLifecycleError("plugin Git reference is invalid")
    if _skip_if_digest is not None:
        if not GIT_DIGEST_PATTERN.fullmatch(_skip_if_digest) or _unchanged is None:
            raise PluginLifecycleError("invalid tracked plugin update digest")
        if expected is not None and expected.digest != _skip_if_digest:
            raise PluginLifecycleError(
                "tracked plugin digest does not match signed catalog digest"
            )

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
    git_home_directory: AnchoredDirectory | None = None
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
        git_home_directory = temporary_directory.create_child("git-home")
        git_environment = isolated_git_environment(
            git_home_directory.descriptor_path()
        )
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
                env=git_environment,
                pass_fds=(checkout_directory.descriptor,),
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
        resolved_digest = _resolve_git_revision(
            checkout_directory,
            git_path,
            environment=git_environment,
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
                source,
                ref,
                expected,
                git_path=git_path,
                git_environment=git_environment,
                checkout_directory=checkout_directory,
                snapshot=snapshot,
            )
        if _skip_if_digest == resolved_digest:
            assert _unchanged is not None
            if _expected_previous_record is not None:
                require_plugin_install_tree_current(
                    _expected_previous_record,
                    snapshot,
                )
            return _unchanged
        record_manifest = _load_manifest_snapshot(snapshot)
        _validate_manifest_snapshot(record_manifest, snapshot)
        install_record: PluginInstallRecord | object = _INSTALL_RECORD_UNCHANGED
        if destination_root is None:
            install_record = PluginInstallRecord(
                name=record_manifest.name,
                version=record_manifest.version,
                source=source,
                ref=ref,
                digest=resolved_digest,
                publisher=expected.publisher if expected is not None else None,
                origin="catalog" if expected is not None else "git",
            )
        _remove_git_metadata(checkout_directory)
        return install_local_plugin(
            checkout,
            destination_root=destination_root,
            replace=replace,
            enabled=enabled,
            state_path=state_path,
            validator=validator,
            _validator_at=_validator_at,
            _topology_validator=_topology_validator,
            _source_directory=checkout_directory,
            _snapshot=snapshot,
            _state_validator=_state_validator,
            _install_record=install_record,
            _expected_install_record=(
                _expected_previous_record
                if _expected_previous_record is not None
                else _INSTALL_RECORD_UNCHANGED
            ),
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
        if git_home_directory is not None:
            try:
                git_home_directory.close()
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
                try:
                    child = current.child(name, expected=metadata)
                except FileNotFoundError:
                    # Git mutates its checkout while the clone is still
                    # running.  An entry that vanished after ``stat`` no
                    # longer contributes to the live size budget.
                    continue
                try:
                    try:
                        if walk(child):
                            return True
                    except FileNotFoundError:
                        # The held child can itself be removed between the
                        # parent lookup and the descriptor-relative traversal.
                        continue
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


def _resolve_git_revision(
    directory: AnchoredDirectory,
    git_path: str,
    *,
    environment: Mapping[str, str],
) -> str:
    completed = subprocess.run(
        [
            git_path,
            "-C",
            str(directory.descriptor_path()),
            "rev-parse",
            "HEAD",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
        env=dict(environment),
        pass_fds=(directory.descriptor,),
    )
    digest = completed.stdout.strip().casefold()
    if completed.returncode or not GIT_DIGEST_PATTERN.fullmatch(digest):
        raise PluginLifecycleError("could not resolve plugin Git revision")
    return digest


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
    source: str,
    ref: str,
    expected: CatalogEntry,
    *,
    git_path: str,
    git_environment: Mapping[str, str],
    checkout_directory: AnchoredDirectory,
    snapshot: PluginSnapshot,
) -> None:
    if expected.source != source or expected.ref != ref:
        raise PluginCatalogError("catalog entry does not match requested plugin source")
    held_checkout = checkout_directory
    verification_environment = dict(git_environment)

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
            env=verification_environment,
            pass_fds=(held_checkout.descriptor,),
        )
        if completed.returncode:
            raise PluginCatalogError("could not verify catalog plugin revision")
        return completed.stdout.strip()

    if git(["rev-parse", "HEAD"]) != expected.digest:
        raise PluginCatalogError("catalog plugin digest does not match cloned revision")
    try:
        staged_manifest = _load_manifest_snapshot(snapshot)
        _validate_manifest_snapshot(staged_manifest, snapshot)
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


def _validate_lifecycle_path(path: Path, label: str) -> None:
    for candidate in (path, path.parent):
        if _is_link(candidate):
            raise PluginLifecycleError(f"{label} cannot traverse a link: {candidate}")


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


def _installed_plugin_manifests_at(
    directory: AnchoredDirectory,
    *,
    excluding: str,
) -> dict[str, PluginManifest]:
    installed_manifests: dict[str, PluginManifest] = {}
    for name in sorted(directory.list_names()):
        if name == excluding:
            continue
        metadata = directory.stat(name)
        if metadata is None or not stat.S_ISDIR(metadata.st_mode):
            continue
        try:
            with directory.child(name, expected=metadata) as plugin_directory:
                manifest = _load_manifest_at(plugin_directory, "plugin.json")
                _validate_manifest_at(manifest, plugin_directory)
        except (AnchoredFilesystemError, OSError, ValueError, TypeError):
            continue
        if manifest.name != name:
            continue
        installed_manifests[name] = manifest
    return installed_manifests


def _installed_plugin_manifests_strict_at(
    directory: AnchoredDirectory,
    *,
    excluding: str | None = None,
) -> dict[str, PluginManifest]:
    """Load the visible installed plugin graph from one held root descriptor."""

    installed_manifests: dict[str, PluginManifest] = {}
    for name in sorted(directory.list_names()):
        if name == excluding:
            continue
        if name.startswith("."):
            continue
        metadata = directory.stat(name)
        if metadata is None or stat.S_ISLNK(metadata.st_mode):
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            continue
        try:
            with directory.child(name, expected=metadata) as plugin_directory:
                manifest = _load_manifest_at(plugin_directory, "plugin.json")
                _validate_manifest_at(manifest, plugin_directory)
        except (AnchoredFilesystemError, OSError, ValueError, TypeError) as exc:
            raise PluginLifecycleError(
                f"installed plugin {name!r} is invalid: {exc}"
            ) from exc
        if manifest.name != name:
            raise PluginLifecycleError(
                f"installed plugin directory {name!r} contains manifest "
                f"{manifest.name!r}"
            )
        installed_manifests[name] = manifest
    return installed_manifests


def _dependency_version_satisfied(version: str | None, version_spec: str) -> bool:
    if version is None:
        return False
    if not version_spec:
        return True
    try:
        return SpecifierSet(version_spec).contains(parse_version(version))
    except (InvalidSpecifier, InvalidVersion):
        return False


def _new_reverse_dependency_errors(
    installed_manifests: Mapping[str, PluginManifest],
    *,
    target: str,
    before_version: str | None,
    after_version: str,
) -> list[str]:
    errors: list[str] = []
    for dependent_name, dependent_manifest in installed_manifests.items():
        for dependency in dependent_manifest.dependencies:
            if dependency.get("name") != target:
                continue
            version_spec = dependency.get("version", "")
            if not _dependency_version_satisfied(before_version, version_spec):
                continue
            if _dependency_version_satisfied(after_version, version_spec):
                continue
            requirement = version_spec or "any version"
            errors.append(
                f"{dependent_name} requires {target} {requirement}; "
                f"candidate {after_version} would break it"
            )
    return errors
