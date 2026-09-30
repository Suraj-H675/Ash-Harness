"""Persistent provenance for managed plugin installations."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from packaging.version import InvalidVersion, parse as parse_version

from ash.plugins._lifecycle_support import (
    lifecycle_error,
    require_anchored_plugin_mutation,
    sync_directory_strict,
    validate_plugin_name,
    write_all,
)
from ash.plugins.catalog import PluginCatalogError, validate_catalog_publisher
from ash.plugins.errors import PluginLifecycleError
from ash.plugins.git_source import GIT_DIGEST_PATTERN, validate_plugin_git_source
from ash.plugins.snapshot import PluginSnapshot, PluginSnapshotError
from ash.safe_io import strict_json_loads
from ash.safety.anchored_fs import AnchoredDirectory, AnchoredFilesystemError

MAX_PLUGIN_INSTALL_RECORDS_BYTES = 512 * 1024
MAX_PLUGIN_INSTALL_RECORDS = 10_000
PLUGIN_INSTALL_RECORDS_VERSION = 2
PLUGIN_INSTALL_RECORDS_FILENAME = ".ash-install-records.json"


@dataclass(frozen=True)
class PluginInstallRecord:
    name: str
    version: str
    source: str
    ref: str
    digest: str
    publisher: str | None = None
    origin: Literal["git", "catalog", "legacy-unknown"] = "legacy-unknown"


def user_plugin_root() -> Path:
    return Path.home() / ".ash" / "plugins"


def plugin_install_records_path(destination_root: Path | None = None) -> Path:
    root = (destination_root or user_plugin_root()).expanduser()
    return root / PLUGIN_INSTALL_RECORDS_FILENAME


def load_plugin_install_records(
    destination_root: Path | None = None,
) -> dict[str, PluginInstallRecord]:
    root = (destination_root or user_plugin_root()).expanduser()
    require_anchored_plugin_mutation()
    try:
        directory = AnchoredDirectory.open(root, create=False)
    except FileNotFoundError:
        return {}
    except (AnchoredFilesystemError, OSError) as exc:
        raise lifecycle_error("plugin install records", exc) from exc
    try:
        return _read_plugin_install_records_at(directory, root)
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise lifecycle_error("plugin install records", exc) from exc
    finally:
        directory.close()


def require_plugin_install_record_current(record: PluginInstallRecord) -> None:
    """Fail if one managed plugin's provenance changed since it was read."""

    root = user_plugin_root().expanduser()
    require_anchored_plugin_mutation()
    try:
        with (
            AnchoredDirectory.open(root, create=False) as directory,
            directory.lock(".ash-lifecycle.lock"),
        ):
            current = _read_plugin_install_records_at(directory, root).get(record.name)
            if current != record:
                raise PluginLifecycleError(
                    f"plugin {record.name!r} changed while update was in progress"
                )
    except FileNotFoundError as exc:
        raise PluginLifecycleError(
            f"plugin {record.name!r} changed while update was in progress"
        ) from exc
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise lifecycle_error("plugin install records", exc) from exc


def require_plugin_install_tree_current(
    record: PluginInstallRecord,
    snapshot: PluginSnapshot,
) -> None:
    """Fail if managed provenance or installed bytes differ from a trusted snapshot."""

    root = user_plugin_root().expanduser()
    require_anchored_plugin_mutation()
    try:
        with (
            AnchoredDirectory.open(root, create=False) as directory,
            directory.lock(".ash-lifecycle.lock"),
        ):
            current = _read_plugin_install_records_at(directory, root).get(record.name)
            if current != record:
                raise PluginLifecycleError(
                    f"plugin {record.name!r} changed while update was in progress"
                )
            metadata = directory.stat(record.name)
            if (
                metadata is None
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
            ):
                raise PluginLifecycleError(
                    f"installed plugin {record.name!r} differs from trusted source; "
                    "reinstall it before updating"
                )
            with directory.child(record.name, expected=metadata) as plugin_directory:
                try:
                    snapshot.verify_materialized(plugin_directory)
                except (PluginSnapshotError, AnchoredFilesystemError, OSError) as exc:
                    raise PluginLifecycleError(
                        f"installed plugin {record.name!r} differs from trusted source; "
                        "reinstall it before updating"
                    ) from exc
                if not directory.same_entry(record.name, plugin_directory.descriptor):
                    raise PluginLifecycleError(
                        f"plugin {record.name!r} changed while update was in progress"
                    )
    except FileNotFoundError as exc:
        raise PluginLifecycleError(
            f"plugin {record.name!r} changed while update was in progress"
        ) from exc
    except PluginLifecycleError:
        raise
    except (AnchoredFilesystemError, OSError) as exc:
        raise lifecycle_error("installed plugin integrity", exc) from exc


def validate_plugin_install_record(record: PluginInstallRecord) -> None:
    validate_plugin_name(record.name)
    try:
        parse_version(record.version)
    except InvalidVersion as exc:
        raise PluginLifecycleError("plugin install record version is invalid") from exc
    try:
        validate_plugin_git_source(record.source)
    except ValueError as exc:
        raise PluginLifecycleError("plugin install record source is invalid") from exc
    if not record.ref or len(record.ref) > 255 or any(
        character in "\x00\r\n" for character in record.ref
    ):
        raise PluginLifecycleError("plugin install record ref is invalid")
    if not GIT_DIGEST_PATTERN.fullmatch(record.digest):
        raise PluginLifecycleError("plugin install record digest is invalid")
    if record.publisher is not None:
        try:
            validate_catalog_publisher(record.publisher)
        except PluginCatalogError as exc:
            raise PluginLifecycleError("plugin install record publisher is invalid") from exc
    if record.origin not in {"git", "catalog", "legacy-unknown"}:
        raise PluginLifecycleError("plugin install record origin is invalid")
    if record.origin != "catalog" and record.publisher is not None:
        raise PluginLifecycleError(
            "plugin install record publisher requires catalog origin"
        )


def _parse_plugin_install_records(
    raw: bytes,
    path: Path,
) -> dict[str, PluginInstallRecord]:
    try:
        payload = strict_json_loads(raw)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise PluginLifecycleError(
            f"cannot load plugin install records {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "plugins"}:
        raise PluginLifecycleError(f"invalid plugin install records: {path}")
    version = payload["version"]
    if version not in {1, PLUGIN_INSTALL_RECORDS_VERSION}:
        raise PluginLifecycleError(f"invalid plugin install records: {path}")
    plugins = payload["plugins"]
    if not isinstance(plugins, dict) or len(plugins) > MAX_PLUGIN_INSTALL_RECORDS:
        raise PluginLifecycleError(f"invalid plugin install records: {path}")
    records: dict[str, PluginInstallRecord] = {}
    for name, item in plugins.items():
        if not isinstance(name, str) or not isinstance(item, dict):
            raise PluginLifecycleError(f"invalid plugin install records: {path}")
        expected_keys = {"version", "source", "ref", "digest", "publisher"}
        if version == PLUGIN_INSTALL_RECORDS_VERSION:
            expected_keys.add("origin")
        if set(item) != expected_keys:
            raise PluginLifecycleError(f"invalid plugin install record for {name!r}")
        values = (item["version"], item["source"], item["ref"], item["digest"])
        if not all(isinstance(value, str) for value in values):
            raise PluginLifecycleError(f"invalid plugin install record for {name!r}")
        publisher = item["publisher"]
        if publisher is not None and not isinstance(publisher, str):
            raise PluginLifecycleError(f"invalid plugin install record for {name!r}")
        origin: Literal["git", "catalog", "legacy-unknown"]
        if version == 1:
            origin = "catalog" if publisher is not None else "legacy-unknown"
        else:
            raw_origin = item["origin"]
            if not isinstance(raw_origin, str):
                raise PluginLifecycleError(f"invalid plugin install record for {name!r}")
            if raw_origin == "git":
                origin = "git"
            elif raw_origin == "catalog":
                origin = "catalog"
            elif raw_origin == "legacy-unknown":
                origin = "legacy-unknown"
            else:
                raise PluginLifecycleError(f"invalid plugin install record for {name!r}")
        record = PluginInstallRecord(
            name=name,
            version=item["version"],
            source=item["source"],
            ref=item["ref"],
            digest=item["digest"],
            publisher=publisher,
            origin=origin,
        )
        validate_plugin_install_record(record)
        records[name] = record
    return records


def _read_plugin_install_records_at(
    directory: AnchoredDirectory,
    root: Path,
) -> dict[str, PluginInstallRecord]:
    raw = directory.read_file(
        PLUGIN_INSTALL_RECORDS_FILENAME,
        max_bytes=MAX_PLUGIN_INSTALL_RECORDS_BYTES,
    )
    if raw is None:
        return {}
    return _parse_plugin_install_records(raw, plugin_install_records_path(root))


def _save_plugin_install_records_at(
    directory: AnchoredDirectory,
    records: dict[str, PluginInstallRecord],
) -> None:
    if len(records) > MAX_PLUGIN_INSTALL_RECORDS:
        raise PluginLifecycleError(
            f"plugin install records exceed {MAX_PLUGIN_INSTALL_RECORDS} entries"
        )
    for name, record in records.items():
        if name != record.name:
            raise PluginLifecycleError("plugin install record key does not match name")
        validate_plugin_install_record(record)
    existing = directory.stat(PLUGIN_INSTALL_RECORDS_FILENAME)
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise PluginLifecycleError("plugin install records state is not a regular file")
    if not records:
        if existing is not None:
            directory.unlink(PLUGIN_INSTALL_RECORDS_FILENAME, expected=existing)
            directory.sync()
            sync_directory_strict(
                directory.descriptor,
                label="plugin install records directory",
            )
        return
    payload = {
        "version": PLUGIN_INSTALL_RECORDS_VERSION,
        "plugins": {
            name: {
                "version": record.version,
                "source": record.source,
                "ref": record.ref,
                "digest": record.digest,
                "publisher": record.publisher,
                "origin": record.origin,
            }
            for name, record in sorted(records.items())
        },
    }
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(data) > MAX_PLUGIN_INSTALL_RECORDS_BYTES:
        raise PluginLifecycleError("plugin install records state is too large")
    temporary = directory.unique_name(
        f".{PLUGIN_INSTALL_RECORDS_FILENAME}.", ".tmp"
    )
    descriptor = -1
    temporary_identity: os.stat_result | None = None
    try:
        descriptor = directory.create_file(temporary, mode=0o600)
        temporary_identity = os.fstat(descriptor)
        write_all(descriptor, data, label="plugin install records")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        directory.rename(
            temporary,
            PLUGIN_INSTALL_RECORDS_FILENAME,
            expected_source=temporary_identity,
        )
        temporary = ""
        directory.sync()
        sync_directory_strict(
            directory.descriptor,
            label="plugin install records directory",
        )
    except BaseException as primary:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except BaseException as cleanup:
                primary.add_note(f"install-record descriptor close failed: {cleanup}")
            descriptor = -1
        if temporary:
            try:
                directory.unlink(
                    temporary,
                    expected=temporary_identity,
                    missing_ok=True,
                )
            except BaseException as cleanup:
                primary.add_note(f"install-record temporary cleanup failed: {cleanup}")
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def transition_plugin_install_records_at(
    directory: AnchoredDirectory,
    before: dict[str, PluginInstallRecord],
    after: dict[str, PluginInstallRecord],
) -> None:
    try:
        _save_plugin_install_records_at(directory, after)
    except BaseException as primary:
        try:
            _save_plugin_install_records_at(directory, before)
        except BaseException as cleanup:
            primary.add_note(f"plugin install record rollback failed: {cleanup}")
        raise
