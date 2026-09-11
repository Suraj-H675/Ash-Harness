"""Immutable, bounded snapshots of plugin trees.

The live plugin source is only authoritative while this module is capturing
it.  Once capture completes, validation and publication can use the private
snapshot backing store without reopening a mutable pathname.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ash.plugins.anchored_fs import AnchoredDirectory


class PluginSnapshotError(ValueError):
    """The bounded plugin snapshot could not be captured or consumed."""


@dataclass(frozen=True)
class SnapshotEntry:
    """Immutable metadata and backing-file range for one snapshot entry."""

    relative: tuple[str, ...]
    kind: str
    mode: int
    size: int
    digest: bytes
    offset: int | None = None


class PluginSnapshot:
    """A private disk-backed, read-only view of one captured plugin tree."""

    _DISPLAY_ROOT = Path("/__ash_plugin_snapshot__")

    def __init__(
        self,
        storage: object,
        entries: tuple[SnapshotEntry, ...],
    ) -> None:
        self._storage = storage
        self._entries = entries
        self._by_path = {entry.relative: entry for entry in entries}
        self._lock = threading.RLock()
        self._closed = False

    @classmethod
    def capture(
        cls,
        source: AnchoredDirectory,
        *,
        max_files: int,
        max_bytes: int,
        max_entries: int,
        max_depth: int,
        exclude_names: frozenset[str] = frozenset(),
    ) -> PluginSnapshot:
        """Capture a held source directory into a private temporary file."""

        if min(max_files, max_bytes, max_entries, max_depth) < 0:
            raise ValueError("snapshot bounds must be non-negative")
        storage = tempfile.TemporaryFile(prefix="ash-plugin-snapshot-")
        entries: list[SnapshotEntry] = []
        counters = _SnapshotCounters()
        try:
            root_metadata = os.fstat(source.descriptor)
            if not stat.S_ISDIR(root_metadata.st_mode):
                raise PluginSnapshotError("plugin snapshot root is not a directory")
            _capture_directory(
                source,
                (),
                storage,
                entries,
                counters,
                max_files=max_files,
                max_bytes=max_bytes,
                max_entries=max_entries,
                max_depth=max_depth,
                depth=0,
                exclude_names=exclude_names,
            )
            storage.flush()
            return cls(storage, tuple(entries))
        except BaseException:
            storage.close()
            raise

    @property
    def entries(self) -> tuple[SnapshotEntry, ...]:
        self._ensure_open()
        return self._entries

    @property
    def root(self) -> Path:
        return self._DISPLAY_ROOT

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._storage, "close")
        close()

    def __enter__(self) -> PluginSnapshot:
        self._ensure_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def entry(self, relative: str | tuple[str, ...]) -> SnapshotEntry | None:
        self._ensure_open()
        return self._by_path.get(_relative_parts(relative))

    def require_entry(self, relative: str | tuple[str, ...]) -> SnapshotEntry:
        entry = self.entry(relative)
        if entry is None:
            raise PluginSnapshotError(
                f"snapshot entry does not exist: {self.display_path(relative)}"
            )
        return entry

    def display_path(self, relative: str | tuple[str, ...]) -> Path:
        parts = _relative_parts(relative)
        return self._DISPLAY_ROOT.joinpath(*parts)

    def children(self, relative: str | tuple[str, ...]) -> tuple[SnapshotEntry, ...]:
        parent = _relative_parts(relative)
        self._ensure_open()
        return tuple(
            entry
            for entry in self._entries
            if len(entry.relative) == len(parent) + 1
            and entry.relative[: len(parent)] == parent
        )

    def descendants(self, relative: str | tuple[str, ...]) -> tuple[SnapshotEntry, ...]:
        parent = _relative_parts(relative)
        self._ensure_open()
        return tuple(
            entry
            for entry in self._entries
            if len(entry.relative) > len(parent)
            and entry.relative[: len(parent)] == parent
        )

    def read_bytes(
        self,
        relative: str | tuple[str, ...],
        *,
        max_bytes: int | None = None,
    ) -> bytes:
        entry = self.require_entry(relative)
        if entry.kind != "file" or entry.offset is None:
            raise PluginSnapshotError(f"snapshot entry is not a file: {self.display_path(relative)}")
        if max_bytes is not None and entry.size > max_bytes:
            raise PluginSnapshotError(
                f"snapshot file exceeds {max_bytes} bytes: {self.display_path(relative)}"
            )
        return b"".join(self.iter_bytes(entry))

    def iter_bytes(
        self,
        entry_or_relative: SnapshotEntry | str | tuple[str, ...],
        *,
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        entry = (
            entry_or_relative
            if isinstance(entry_or_relative, SnapshotEntry)
            else self.require_entry(entry_or_relative)
        )
        if entry.kind != "file" or entry.offset is None:
            raise PluginSnapshotError(
                f"snapshot entry is not a file: {self.display_path(entry.relative)}"
            )
        self._ensure_open()
        remaining = entry.size
        digest = hashlib.sha256()
        with self._lock:
            storage = self._storage
            seek = getattr(storage, "seek")
            read = getattr(storage, "read")
            seek(entry.offset)
            while remaining:
                chunk = read(min(chunk_size, remaining))
                if not isinstance(chunk, bytes) or not chunk:
                    raise PluginSnapshotError(
                        f"snapshot backing store ended early: {self.display_path(entry.relative)}"
                    )
                remaining -= len(chunk)
                digest.update(chunk)
                yield chunk
            if digest.digest() != entry.digest:
                raise PluginSnapshotError(
                    f"snapshot backing store changed: {self.display_path(entry.relative)}"
                )

    def write_to(self, destination: AnchoredDirectory) -> None:
        """Materialize only snapshot bytes into a held destination directory."""

        self._ensure_open()
        root = self.require_entry(())
        if root.kind != "directory":
            raise PluginSnapshotError("plugin snapshot root is not a directory")
        os.fchmod(destination.descriptor, root.mode)
        _write_directory(self, destination, ())

    def verify_materialized(self, directory: AnchoredDirectory) -> None:
        """Verify a materialized tree against the immutable snapshot."""

        self._ensure_open()
        _verify_directory(self, directory, ())

    def _ensure_open(self) -> None:
        if self._closed:
            raise PluginSnapshotError("plugin snapshot is closed")


@dataclass
class _SnapshotCounters:
    files: int = 0
    total_bytes: int = 0
    entries: int = 0


def _capture_directory(
    directory: AnchoredDirectory,
    relative: tuple[str, ...],
    storage: object,
    entries: list[SnapshotEntry],
    counters: _SnapshotCounters,
    *,
    max_files: int,
    max_bytes: int,
    max_entries: int,
    max_depth: int,
    depth: int,
    exclude_names: frozenset[str],
) -> None:
    if depth > max_depth:
        raise PluginSnapshotError(f"plugin tree exceeds depth {max_depth}")
    metadata = os.fstat(directory.descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise PluginSnapshotError("plugin snapshot entry is not a directory")
    entries.append(
        SnapshotEntry(
            relative=relative,
            kind="directory",
            mode=stat.S_IMODE(metadata.st_mode),
            size=0,
            digest=b"",
        )
    )
    for name in sorted(directory.list_names()):
        if not relative and name in exclude_names:
            continue
        child_metadata = directory.stat(name)
        if child_metadata is None:
            continue
        counters.entries += 1
        if counters.entries > max_entries:
            raise PluginSnapshotError(f"plugin tree exceeds {max_entries} entries")
        child_relative = (*relative, name)
        if stat.S_ISLNK(child_metadata.st_mode):
            raise PluginSnapshotError(
                f"plugin tree contains a link: {directory.path / name}"
            )
        if stat.S_ISDIR(child_metadata.st_mode):
            child = directory.child(name, expected=child_metadata)
            try:
                _capture_directory(
                    child,
                    child_relative,
                    storage,
                    entries,
                    counters,
                    max_files=max_files,
                    max_bytes=max_bytes,
                    max_entries=max_entries,
                    max_depth=max_depth,
                    depth=depth + 1,
                    exclude_names=exclude_names,
                )
            finally:
                child.close()
            continue
        if not stat.S_ISREG(child_metadata.st_mode):
            raise PluginSnapshotError(
                f"plugin tree contains unsupported entry: {directory.path / name}"
            )
        counters.files += 1
        if counters.files > max_files:
            raise PluginSnapshotError(f"plugin contains more than {max_files} files")
        if child_metadata.st_size > max_bytes - counters.total_bytes:
            raise PluginSnapshotError(f"plugin exceeds {max_bytes} bytes")
        descriptor = directory.open_file(
            name,
            os.O_RDONLY,
            expected=child_metadata,
            expected_type=stat.S_IFREG,
        )
        offset = getattr(storage, "tell")()
        digest = hashlib.sha256()
        copied = 0
        try:
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                counters.total_bytes += len(chunk)
                if counters.total_bytes > max_bytes:
                    raise PluginSnapshotError(f"plugin exceeds {max_bytes} bytes")
                digest.update(chunk)
                getattr(storage, "write")(chunk)
            if copied != child_metadata.st_size:
                raise PluginSnapshotError(
                    f"plugin source changed while snapshotting: {directory.path / name}"
                )
        finally:
            os.close(descriptor)
        entries.append(
            SnapshotEntry(
                relative=child_relative,
                kind="file",
                mode=stat.S_IMODE(child_metadata.st_mode),
                size=copied,
                digest=digest.digest(),
                offset=offset,
            )
        )


def _write_directory(
    snapshot: PluginSnapshot,
    destination: AnchoredDirectory,
    relative: tuple[str, ...],
) -> None:
    for entry in snapshot.children(relative):
        name = entry.relative[-1]
        if entry.kind == "directory":
            child = destination.create_child(name)
            try:
                os.fchmod(child.descriptor, entry.mode)
                _write_directory(snapshot, child, entry.relative)
            finally:
                child.close()
            continue
        descriptor = destination.create_file(name, mode=entry.mode)
        try:
            for chunk in snapshot.iter_bytes(entry):
                _write_all(descriptor, chunk)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _verify_directory(
    snapshot: PluginSnapshot,
    directory: AnchoredDirectory,
    relative: tuple[str, ...],
) -> None:
    directory_metadata = os.fstat(directory.descriptor)
    expected_directory = snapshot.require_entry(relative)
    if (
        expected_directory.kind != "directory"
        or not stat.S_ISDIR(directory_metadata.st_mode)
        or stat.S_IMODE(directory_metadata.st_mode) != expected_directory.mode
    ):
        raise PluginSnapshotError(
            f"materialized plugin directory metadata differs at "
            f"{snapshot.display_path(relative)}"
        )
    expected = {entry.relative[-1]: entry for entry in snapshot.children(relative)}
    actual = set(directory.list_names())
    if actual != set(expected):
        raise PluginSnapshotError(
            f"materialized plugin differs from snapshot at {snapshot.display_path(relative)}"
        )
    for name, entry in expected.items():
        metadata = directory.stat(name)
        if metadata is None:
            raise PluginSnapshotError("materialized plugin entry disappeared")
        if entry.kind == "directory":
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != entry.mode
            ):
                raise PluginSnapshotError("materialized plugin directory changed type")
            child = directory.child(name, expected=metadata)
            try:
                _verify_directory(snapshot, child, entry.relative)
            finally:
                child.close()
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != entry.size
            or stat.S_IMODE(metadata.st_mode) != entry.mode
        ):
            raise PluginSnapshotError("materialized plugin file changed")
        descriptor = directory.open_file(
            name,
            os.O_RDONLY,
            expected=metadata,
            expected_type=stat.S_IFREG,
        )
        digest = hashlib.sha256()
        copied = 0
        try:
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                digest.update(chunk)
        finally:
            os.close(descriptor)
        if copied != entry.size or digest.digest() != entry.digest:
            raise PluginSnapshotError("materialized plugin bytes differ from snapshot")


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise PluginSnapshotError("could not write plugin snapshot")
        view = view[written:]


def _relative_parts(relative: str | tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(relative, tuple):
        parts = relative
    else:
        if not relative:
            return ()
        candidate = PurePosixPath(relative)
        if candidate.is_absolute():
            raise ValueError(f"snapshot path must be relative: {relative}")
        parts = candidate.parts
    if any(not part or part in {".", ".."} or "/" in part for part in parts):
        raise ValueError(f"snapshot path must be canonical and relative: {relative}")
    return tuple(parts)
