"""Durable crash-recovery intent for one serialized plugin lifecycle mutation."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, replace
from typing import Literal

from ash.plugins._lifecycle_support import (
    sync_directory_strict,
    validate_plugin_name,
    write_all,
)
from ash.plugins.errors import PluginLifecycleError
from ash.plugins.install_records import (
    PluginInstallRecord,
    validate_plugin_install_record,
)
from ash.safe_io import strict_json_loads
from ash.safety.anchored_fs import AnchoredDirectory


LIFECYCLE_JOURNAL_FILENAME = ".ash-lifecycle-journal.json"
LIFECYCLE_JOURNAL_VERSION = 1
MAX_LIFECYCLE_JOURNAL_BYTES = 64 * 1024
_INTERNAL_TREE_NAME = re.compile(
    r"^(?:\.[A-Za-z0-9][A-Za-z0-9._-]*\."
    r"(?:backup|uninstall)-[0-9a-f]{32})$"
)
_STAGE_NAME = re.compile(r"^\.install-[0-9a-f]{32}\.tmp$")


@dataclass(frozen=True)
class TreeIdentity:
    device: int
    inode: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> "TreeIdentity":
        return cls(int(metadata.st_dev), int(metadata.st_ino))

    def matches(self, metadata: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(metadata.st_mode)
            and int(metadata.st_dev) == self.device
            and int(metadata.st_ino) == self.inode
        )


@dataclass(frozen=True)
class PluginLifecycleJournal:
    operation: Literal["install", "uninstall"]
    phase: Literal["prepared", "committed"]
    plugin: str
    root_tree: TreeIdentity
    stage_name: str | None = None
    stage_tree: TreeIdentity | None = None
    moved_name: str | None = None
    previous_tree: TreeIdentity | None = None
    live_tree: TreeIdentity | None = None
    manage_record: bool = False
    previous_record: PluginInstallRecord | None = None
    desired_record: PluginInstallRecord | None = None
    state_path: str | None = None
    state_parent_tree: TreeIdentity | None = None
    previous_disabled: bool | None = None
    desired_disabled: bool | None = None

    def with_live_tree(self, metadata: os.stat_result) -> "PluginLifecycleJournal":
        return replace(self, live_tree=TreeIdentity.from_stat(metadata))

    def committed(self) -> "PluginLifecycleJournal":
        return replace(self, phase="committed")


def read_lifecycle_journal_at(
    directory: AnchoredDirectory,
) -> PluginLifecycleJournal | None:
    raw = directory.read_file(
        LIFECYCLE_JOURNAL_FILENAME,
        max_bytes=MAX_LIFECYCLE_JOURNAL_BYTES,
    )
    if raw is None:
        return None
    try:
        payload = strict_json_loads(raw)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise PluginLifecycleError(f"invalid plugin lifecycle journal: {exc}") from exc
    return _parse_journal(payload)


def write_lifecycle_journal_at(
    directory: AnchoredDirectory,
    journal: PluginLifecycleJournal,
) -> None:
    _validate_journal(journal)
    payload = {
        "version": LIFECYCLE_JOURNAL_VERSION,
        "operation": journal.operation,
        "phase": journal.phase,
        "plugin": journal.plugin,
        "root_tree": _tree_payload(journal.root_tree),
        "stage_name": journal.stage_name,
        "stage_tree": _tree_payload(journal.stage_tree),
        "moved_name": journal.moved_name,
        "previous_tree": _tree_payload(journal.previous_tree),
        "live_tree": _tree_payload(journal.live_tree),
        "manage_record": journal.manage_record,
        "previous_record": _record_payload(journal.previous_record),
        "desired_record": _record_payload(journal.desired_record),
        "state_path": journal.state_path,
        "state_parent_tree": _tree_payload(journal.state_parent_tree),
        "previous_disabled": journal.previous_disabled,
        "desired_disabled": journal.desired_disabled,
    }
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(data) > MAX_LIFECYCLE_JOURNAL_BYTES:
        raise PluginLifecycleError("plugin lifecycle journal is too large")
    existing = directory.stat(LIFECYCLE_JOURNAL_FILENAME)
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise PluginLifecycleError("plugin lifecycle journal is not a regular file")
    temporary = directory.unique_name(".ash-lifecycle-journal-", ".tmp")
    descriptor = -1
    temporary_identity: os.stat_result | None = None
    try:
        descriptor = directory.create_file(temporary, mode=0o600)
        temporary_identity = os.fstat(descriptor)
        write_all(descriptor, data, label="plugin lifecycle journal")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        directory.rename(
            temporary,
            LIFECYCLE_JOURNAL_FILENAME,
            expected_source=temporary_identity,
        )
        temporary = ""
        directory.sync()
        sync_directory_strict(
            directory.descriptor,
            label="plugin lifecycle journal directory",
        )
    except BaseException as primary:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except BaseException as cleanup:
                primary.add_note(f"lifecycle journal descriptor close failed: {cleanup}")
            descriptor = -1
        if temporary:
            try:
                directory.unlink(
                    temporary,
                    expected=temporary_identity,
                    missing_ok=True,
                )
            except BaseException as cleanup:
                primary.add_note(f"lifecycle journal temporary cleanup failed: {cleanup}")
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def clear_lifecycle_journal_at(directory: AnchoredDirectory) -> None:
    existing = directory.stat(LIFECYCLE_JOURNAL_FILENAME)
    if existing is None:
        return
    if not stat.S_ISREG(existing.st_mode):
        raise PluginLifecycleError("plugin lifecycle journal is not a regular file")
    directory.unlink(LIFECYCLE_JOURNAL_FILENAME, expected=existing)
    directory.sync()
    sync_directory_strict(
        directory.descriptor,
        label="plugin lifecycle journal directory",
    )


def _parse_journal(payload: object) -> PluginLifecycleJournal:
    if not isinstance(payload, dict):
        raise PluginLifecycleError("invalid plugin lifecycle journal")
    expected = {
        "version",
        "operation",
        "phase",
        "plugin",
        "root_tree",
        "stage_name",
        "stage_tree",
        "moved_name",
        "previous_tree",
        "live_tree",
        "manage_record",
        "previous_record",
        "desired_record",
        "state_path",
        "state_parent_tree",
        "previous_disabled",
        "desired_disabled",
    }
    if set(payload) != expected or payload.get("version") != LIFECYCLE_JOURNAL_VERSION:
        raise PluginLifecycleError("invalid plugin lifecycle journal")
    operation = payload["operation"]
    phase = payload["phase"]
    plugin = payload["plugin"]
    stage_name = payload["stage_name"]
    moved_name = payload["moved_name"]
    manage_record = payload["manage_record"]
    state_path = payload["state_path"]
    previous_disabled = payload["previous_disabled"]
    desired_disabled = payload["desired_disabled"]
    if operation not in {"install", "uninstall"} or phase not in {
        "prepared",
        "committed",
    }:
        raise PluginLifecycleError("invalid plugin lifecycle journal")
    if not isinstance(plugin, str):
        raise PluginLifecycleError("invalid plugin lifecycle journal")
    if stage_name is not None and not isinstance(stage_name, str):
        raise PluginLifecycleError("invalid plugin lifecycle journal")
    if moved_name is not None and not isinstance(moved_name, str):
        raise PluginLifecycleError("invalid plugin lifecycle journal")
    if not isinstance(manage_record, bool):
        raise PluginLifecycleError("invalid plugin lifecycle journal")
    if state_path is not None and not isinstance(state_path, str):
        raise PluginLifecycleError("invalid plugin lifecycle journal")
    if previous_disabled is not None and not isinstance(previous_disabled, bool):
        raise PluginLifecycleError("invalid plugin lifecycle journal")
    if desired_disabled is not None and not isinstance(desired_disabled, bool):
        raise PluginLifecycleError("invalid plugin lifecycle journal")
    journal = PluginLifecycleJournal(
        operation=operation,
        phase=phase,
        plugin=plugin,
        root_tree=_parse_required_tree(payload["root_tree"]),
        stage_name=stage_name,
        stage_tree=_parse_tree(payload["stage_tree"]),
        moved_name=moved_name,
        previous_tree=_parse_tree(payload["previous_tree"]),
        live_tree=_parse_tree(payload["live_tree"]),
        manage_record=manage_record,
        previous_record=_parse_record(payload["previous_record"]),
        desired_record=_parse_record(payload["desired_record"]),
        state_path=state_path,
        state_parent_tree=_parse_tree(payload["state_parent_tree"]),
        previous_disabled=previous_disabled,
        desired_disabled=desired_disabled,
    )
    _validate_journal(journal)
    return journal


def _validate_journal(journal: PluginLifecycleJournal) -> None:
    validate_plugin_name(journal.plugin)
    if journal.operation == "install":
        if (
            journal.stage_name is None
            or journal.stage_tree is None
            or not _STAGE_NAME.fullmatch(journal.stage_name)
        ):
            raise PluginLifecycleError("invalid plugin lifecycle install stage")
    elif journal.stage_name is not None or journal.stage_tree is not None:
        raise PluginLifecycleError("uninstall lifecycle journal cannot contain a stage")
    if journal.moved_name is not None and not _INTERNAL_TREE_NAME.fullmatch(
        journal.moved_name
    ):
        raise PluginLifecycleError("invalid plugin lifecycle journal tree name")
    if journal.operation == "install" and journal.moved_name is not None:
        expected_prefix = f".{journal.plugin}.backup-"
        if not journal.moved_name.startswith(expected_prefix):
            raise PluginLifecycleError("invalid plugin lifecycle install journal")
    if journal.operation == "uninstall":
        if journal.moved_name is None or not journal.moved_name.startswith(
            f".{journal.plugin}.uninstall-"
        ):
            raise PluginLifecycleError("invalid plugin lifecycle uninstall journal")
    if journal.manage_record:
        for record in (journal.previous_record, journal.desired_record):
            if record is not None:
                validate_plugin_install_record(record)
                if record.name != journal.plugin:
                    raise PluginLifecycleError(
                        "plugin lifecycle journal record name does not match plugin"
                    )
    elif journal.previous_record is not None or journal.desired_record is not None:
        raise PluginLifecycleError("unmanaged lifecycle journal cannot contain records")
    state_managed = journal.state_path is not None
    if state_managed != (journal.previous_disabled is not None):
        raise PluginLifecycleError("invalid plugin lifecycle journal activation state")
    if state_managed != (journal.desired_disabled is not None):
        raise PluginLifecycleError("invalid plugin lifecycle journal activation state")
    if state_managed != (journal.state_parent_tree is not None):
        raise PluginLifecycleError("invalid plugin lifecycle journal activation parent")


def _tree_payload(identity: TreeIdentity | None) -> dict[str, int] | None:
    if identity is None:
        return None
    return {"device": identity.device, "inode": identity.inode}


def _parse_tree(value: object) -> TreeIdentity | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"device", "inode"}
        or not all(
            isinstance(value[key], int) and not isinstance(value[key], bool)
            for key in ("device", "inode")
        )
    ):
        raise PluginLifecycleError("invalid plugin lifecycle journal tree identity")
    return TreeIdentity(device=value["device"], inode=value["inode"])


def _parse_required_tree(value: object) -> TreeIdentity:
    identity = _parse_tree(value)
    if identity is None:
        raise PluginLifecycleError("plugin lifecycle journal requires root identity")
    return identity


def _record_payload(record: PluginInstallRecord | None) -> dict[str, object] | None:
    if record is None:
        return None
    return {
        "name": record.name,
        "version": record.version,
        "source": record.source,
        "ref": record.ref,
        "digest": record.digest,
        "publisher": record.publisher,
        "origin": record.origin,
    }


def _parse_record(value: object) -> PluginInstallRecord | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "name",
        "version",
        "source",
        "ref",
        "digest",
        "publisher",
        "origin",
    }:
        raise PluginLifecycleError("invalid plugin lifecycle journal record")
    strings = ("name", "version", "source", "ref", "digest", "origin")
    if not all(isinstance(value[key], str) for key in strings):
        raise PluginLifecycleError("invalid plugin lifecycle journal record")
    publisher = value["publisher"]
    if publisher is not None and not isinstance(publisher, str):
        raise PluginLifecycleError("invalid plugin lifecycle journal record")
    record = PluginInstallRecord(
        name=value["name"],
        version=value["version"],
        source=value["source"],
        ref=value["ref"],
        digest=value["digest"],
        publisher=publisher,
        origin=value["origin"],  # type: ignore[arg-type]
    )
    validate_plugin_install_record(record)
    return record
