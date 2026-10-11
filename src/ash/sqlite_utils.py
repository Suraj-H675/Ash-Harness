"""Race-resistant SQLite database path binding for Ash-owned local state."""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from ash.safety.anchored_fs import AnchoredDirectory, AnchoredFilesystemError


class SQLitePathError(RuntimeError):
    """A SQLite path could not be bound to one stable local file identity."""


def _restrict_existing_file(
    directory: AnchoredDirectory,
    name: str,
    expected: os.stat_result,
    *,
    label: str,
    mode: int = 0o600,
) -> None:
    """Restrict one pinned entry without opening another SQLite file descriptor."""

    current = directory.stat(name)
    if (
        current is None
        or not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise SQLitePathError(f"{label} storage identity changed: {directory.path / name}")
    # Closing any extra descriptor for a SQLite file releases this process's
    # POSIX locks, including locks owned by another live SQLite connection.
    if stat.S_IMODE(current.st_mode) != mode:
        try:
            os.chmod(name, mode, dir_fd=directory.descriptor, follow_symlinks=False)
        except (OSError, ValueError, NotImplementedError) as exc:
            raise SQLitePathError(
                f"cannot secure {label} {directory.path / name}: {exc}"
            ) from exc
    secured = directory.stat(name)
    if (
        secured is None
        or not stat.S_ISREG(secured.st_mode)
        or (secured.st_dev, secured.st_ino) != (expected.st_dev, expected.st_ino)
        or stat.S_IMODE(secured.st_mode) != mode
    ):
        raise SQLitePathError(f"{label} storage changed: {directory.path / name}")


def sqlite_wal_is_safe(
    version: tuple[int, ...] | None = None,
) -> bool:
    """Return whether the SQLite runtime contains the WAL-reset corruption fix."""

    raw = tuple(version or sqlite3.sqlite_version_info)
    padded = (raw + (0, 0, 0))[:3]
    if padded >= (3, 51, 3):
        return True
    major_minor = padded[:2]
    return (
        major_minor == (3, 50)
        and padded >= (3, 50, 7)
        or major_minor == (3, 44)
        and padded >= (3, 44, 6)
    )


def preferred_sqlite_journal_mode(
    version: tuple[int, ...] | None = None,
) -> str:
    """Choose WAL only when the runtime includes SQLite's WAL-reset fix."""

    return "WAL" if sqlite_wal_is_safe(version) else "DELETE"


def configure_sqlite_journal_mode(connection: sqlite3.Connection) -> str:
    """Apply Ash's corruption-safe journal mode and return the selected mode."""

    expected = preferred_sqlite_journal_mode()
    current_row = connection.execute("PRAGMA journal_mode").fetchone()
    current = str(current_row[0]).upper() if current_row else ""
    if current == expected:
        return expected
    row = connection.execute(f"PRAGMA journal_mode={expected}").fetchone()
    actual = str(row[0]).upper() if row else ""
    if actual != expected:
        raise sqlite3.OperationalError(
            f"SQLite refused journal mode {expected}; active mode is {actual or 'unknown'}"
        )
    return expected


def _canonicalize_platform_alias_prefix(path: Path) -> Path:
    """Resolve only stable OS-owned root aliases before anchored traversal."""

    if sys.platform != "darwin" or not path.is_absolute() or len(path.parts) < 2:
        return path
    alias = Path(path.anchor) / path.parts[1]
    if alias not in {Path("/var"), Path("/tmp"), Path("/etc")}:
        return path
    resolved_alias = Path(os.path.realpath(alias))
    if not resolved_alias.is_absolute():
        return path
    return resolved_alias.joinpath(*path.parts[2:])


@dataclass(frozen=True)
class PinnedSQLiteDatabase:
    """One Ash-owned SQLite path pinned to parent and file inode identities."""

    path: Path
    parent_device: int
    parent_inode: int
    file_device: int
    file_inode: int
    initial_size: int

    @classmethod
    def prepare(
        cls,
        path: str | Path,
        *,
        label: str,
        create: bool = True,
    ) -> "PinnedSQLiteDatabase":
        database = Path(os.path.abspath(Path(path).expanduser()))
        database = _canonicalize_platform_alias_prefix(database)
        if not database.name:
            raise SQLitePathError(f"{label} path must name a file")
        try:
            with AnchoredDirectory.open(
                database.parent,
                create=create,
                private=False,
                pin_path=True,
            ) as directory:
                parent = os.fstat(directory.descriptor)
                metadata = directory.stat(database.name)
                created = False
                descriptor = -1
                if metadata is None:
                    if not create:
                        raise SQLitePathError(f"{label} does not exist: {database}")
                    descriptor = directory.create_file(database.name, mode=0o600)
                    created = True
                    metadata = os.fstat(descriptor)
                if stat.S_ISLNK(metadata.st_mode):
                    raise SQLitePathError(
                        f"{label} path contains a symlink or junction: {database}"
                    )
                if not stat.S_ISREG(metadata.st_mode):
                    raise SQLitePathError(f"{label} is not a regular file: {database}")
                if created:
                    try:
                        os.fchmod(descriptor, 0o600)
                        opened = os.fstat(descriptor)
                        if (opened.st_dev, opened.st_ino) != (
                            metadata.st_dev, metadata.st_ino
                        ):
                            raise SQLitePathError(
                                f"{label} changed while it was being created: {database}"
                            )
                    finally:
                        os.close(descriptor)
                else:
                    _restrict_existing_file(
                        directory, database.name, metadata, label=label,
                    )
                directory.validation_path()
                if created:
                    directory.sync()
                return cls(
                    path=database,
                    parent_device=int(parent.st_dev),
                    parent_inode=int(parent.st_ino),
                    file_device=int(metadata.st_dev),
                    file_inode=int(metadata.st_ino),
                    initial_size=int(metadata.st_size),
                )
        except SQLitePathError:
            raise
        except AnchoredFilesystemError as exc:
            if "link" in str(exc).casefold():
                raise SQLitePathError(
                    f"{label} path contains a symlink or junction: {database}"
                ) from exc
            raise SQLitePathError(f"cannot bind {label} {database}: {exc}") from exc
        except FileNotFoundError as exc:
            if not create:
                raise SQLitePathError(f"{label} does not exist: {database}") from exc
            raise SQLitePathError(f"cannot bind {label} {database}: {exc}") from exc
        except OSError as exc:
            raise SQLitePathError(f"cannot bind {label} {database}: {exc}") from exc

    def verify(self, *, label: str) -> None:
        """Fail if the visible path no longer identifies the pinned database."""

        with self.parent_directory(label=label) as directory:
            metadata = directory.stat(self.path.name)
            if metadata is not None and stat.S_ISLNK(metadata.st_mode):
                raise SQLitePathError(
                    f"{label} path contains a symlink or junction: {self.path}"
                )
            if metadata is None or not stat.S_ISREG(metadata.st_mode):
                raise SQLitePathError(f"{label} file identity changed")
            if (
                int(metadata.st_dev) != self.file_device
                or int(metadata.st_ino) != self.file_inode
            ):
                raise SQLitePathError(f"{label} file identity changed")
            directory.validation_path()

    @contextmanager
    def parent_directory(self, *, label: str) -> Iterator[AnchoredDirectory]:
        """Open and verify the exact parent directory captured by this pin."""

        try:
            with AnchoredDirectory.open(
                self.path.parent,
                create=False,
                private=False,
                pin_path=True,
            ) as directory:
                parent = os.fstat(directory.descriptor)
                if (
                    int(parent.st_dev) != self.parent_device
                    or int(parent.st_ino) != self.parent_inode
                ):
                    raise SQLitePathError(f"{label} parent identity changed")
                directory.validation_path()
                yield directory
                directory.validation_path()
        except SQLitePathError:
            raise
        except AnchoredFilesystemError as exc:
            if "link" in str(exc).casefold():
                raise SQLitePathError(
                    f"{label} path contains a symlink or junction: {self.path}"
                ) from exc
            raise SQLitePathError(f"cannot verify {label} {self.path}: {exc}") from exc
        except OSError as exc:
            raise SQLitePathError(f"cannot verify {label} {self.path}: {exc}") from exc

    def connect(
        self,
        *,
        label: str,
        check_same_thread: bool = False,
        timeout: float = 5.0,
        factory: Any = sqlite3.Connection,
    ) -> sqlite3.Connection:
        """Open the pinned file without allowing SQLite to create another path."""

        self.verify(label=label)
        uri = self.read_write_uri()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                uri,
                uri=True,
                check_same_thread=check_same_thread,
                timeout=timeout,
                factory=factory,
            )
            # sqlite3.connect() has opened the visible pathname. Re-check before
            # the caller can issue PRAGMAs/schema writes so a parent/file swap
            # cannot redirect initialization to another database.
            self.verify(label=label)
            return connection
        except BaseException:
            if connection is not None:
                connection.close()
            raise

    def read_write_uri(self) -> str:
        """Return a SQLite URI that can open, but never create, the pinned path."""

        return "file:" + quote(str(self.path), safe="/") + "?mode=rw"

    def restrict_permissions(
        self,
        *,
        label: str,
        suffixes: tuple[str, ...] = ("", "-wal", "-shm"),
        mode: int = 0o600,
    ) -> None:
        """Apply private permissions to the pinned DB and present SQLite sidecars."""

        if os.name == "nt":
            return
        with self.parent_directory(label=label) as directory:
            for suffix in suffixes:
                name = f"{self.path.name}{suffix}"
                metadata = directory.stat(name)
                if metadata is None:
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    raise SQLitePathError(
                        f"{label} storage contains a symlink or junction: "
                        f"{directory.path / name}"
                    )
                if not stat.S_ISREG(metadata.st_mode):
                    raise SQLitePathError(
                        f"{label} storage is not a regular file: {directory.path / name}"
                    )
                _restrict_existing_file(
                    directory, name, metadata, label=label, mode=mode,
                )
