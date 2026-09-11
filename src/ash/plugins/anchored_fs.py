"""Descriptor-anchored filesystem operations for plugin lifecycle mutations."""

from __future__ import annotations

import errno
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]


class AnchoredFilesystemError(RuntimeError):
    """A descriptor-anchored plugin filesystem operation was unsafe or failed."""


class AnchoredFilesystemUnavailable(AnchoredFilesystemError):
    """The current platform cannot provide descriptor-anchored mutation."""


def supports_anchored_mutation() -> bool:
    """Return whether this runtime has the required POSIX descriptor primitives."""

    if os.name != "posix" or fcntl is None:
        return False
    if not all(
        getattr(os, flag, 0)
        for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_CREAT", "O_EXCL")
    ):
        return False
    if not all(
        hasattr(os, name)
        for name in (
            "close",
            "fchmod",
            "fstat",
            "fsync",
            "listdir",
            "mkdir",
            "open",
            "read",
            "rename",
            "rmdir",
            "stat",
            "unlink",
            "write",
        )
    ):
        return False
    supports_dir_fd = getattr(os, "supports_dir_fd", ())
    supports_follow_symlinks = getattr(os, "supports_follow_symlinks", ())
    return os.stat in supports_follow_symlinks and all(
        function in supports_dir_fd
        for function in (os.mkdir, os.open, os.rename, os.rmdir, os.stat, os.unlink)
    )


def require_anchored_mutation() -> None:
    """Fail closed when the platform cannot anchor a plugin mutation."""

    if not supports_anchored_mutation():
        raise AnchoredFilesystemUnavailable(
            "descriptor-anchored plugin lifecycle mutation is unavailable "
            "on this platform/build"
        )


def supports_strict_identity_mutation() -> bool:
    """Return whether this backend can compare a name and mutate it atomically.

    The current descriptor-relative POSIX backend intentionally reports false:
    POSIX has no portable operation that binds an unlink/rmdir to a previously
    observed object identity.  Keeping this capability explicit prevents the
    ordinary anchored helpers from being mistaken for a strict same-principal
    namespace-mutation guarantee.
    """

    return False


def require_strict_identity_mutation() -> None:
    """Fail closed when strict same-principal namespace mutation is unavailable."""

    if not supports_strict_identity_mutation():
        raise AnchoredFilesystemUnavailable(
            "strict same-principal plugin filesystem mutation is unavailable "
            "on this platform/build"
        )


@dataclass(frozen=True)
class _EntryIdentity:
    """The identity and object kind observed before a descriptor was opened."""

    device: int
    inode: int
    file_type: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> _EntryIdentity:
        return cls(
            metadata.st_dev,
            metadata.st_ino,
            stat.S_IFMT(metadata.st_mode),
        )


class AnchoredDirectory:
    """A directory held open while all sensitive child operations use its fd."""

    def __init__(self, path: Path, descriptor: int) -> None:
        self.path = path
        self._descriptor = descriptor

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        create: bool,
        private: bool = True,
        expected: os.stat_result | None = None,
    ) -> AnchoredDirectory:
        """Open a directory path without following replaceable components.

        Each existing component is lstat-like inspected before it is opened and
        the resulting descriptor is checked with ``fstat``.  ``expected`` is
        used by callers that observed the final directory before entering this
        function (for example a freshly-created temporary root).
        """

        require_anchored_mutation()
        absolute = _absolute_lexical_path(path)
        components = _path_components(absolute)
        root_descriptor = -1
        current_descriptor = -1
        try:
            root_descriptor = os.open(
                Path(absolute.anchor or os.sep),
                _directory_flags(),
            )
            current_descriptor = root_descriptor
            for component in components:
                next_descriptor = _open_or_create_directory(
                    current_descriptor,
                    component,
                    create=create,
                )
                if current_descriptor != root_descriptor:
                    os.close(current_descriptor)
                current_descriptor = next_descriptor
            opened = os.fstat(current_descriptor)
            _require_directory_identity(opened, expected, absolute)
            if private:
                os.fchmod(current_descriptor, 0o700)
            descriptor = current_descriptor
            current_descriptor = -1
            if root_descriptor != descriptor:
                os.close(root_descriptor)
            root_descriptor = -1
            return cls(absolute, descriptor)
        finally:
            if current_descriptor >= 0:
                os.close(current_descriptor)
            if root_descriptor >= 0 and root_descriptor != current_descriptor:
                os.close(root_descriptor)

    @property
    def descriptor(self) -> int:
        if self._descriptor < 0:
            raise AnchoredFilesystemError("anchored directory is closed")
        return self._descriptor

    def close(self) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1

    def validation_path(self) -> Path:
        """Return a stable path view of this held directory for legacy validators."""

        held = os.fstat(self.descriptor)
        for prefix in ("/proc/self/fd", "/dev/fd"):
            descriptor_path = f"{prefix}/{self.descriptor}"
            try:
                target = os.readlink(descriptor_path)
                candidate = Path(target)
                current = os.stat(candidate, follow_symlinks=False)
            except (OSError, ValueError):
                continue
            if _same_identity(held, current) and stat.S_ISDIR(current.st_mode):
                return candidate
        # A held descriptor remains usable after its visible name is removed.
        # This view is only for internal validation; public InstalledPlugin.root
        # remains the ordinary caller-visible Path.
        for prefix in ("/proc/self/fd", "/dev/fd"):
            descriptor_view = Path(f"{prefix}/{self.descriptor}")
            if descriptor_view.exists():
                return descriptor_view
        raise AnchoredFilesystemError(
            "no stable path view is available for anchored plugin validation"
        )

    def descriptor_path(self) -> Path:
        """Return the procfs path for this descriptor when available."""

        for prefix in ("/proc/self/fd", "/dev/fd"):
            candidate = Path(f"{prefix}/{self.descriptor}")
            if candidate.exists():
                return candidate
        raise AnchoredFilesystemUnavailable(
            "descriptor-relative filesystem path is unavailable"
        )

    def __enter__(self) -> AnchoredDirectory:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def child(
        self,
        name: str,
        *,
        create: bool = False,
        expected: os.stat_result | None = None,
    ) -> AnchoredDirectory:
        _validate_name(name)
        descriptor = _open_or_create_directory(
            self.descriptor,
            name,
            create=create,
            expected=expected,
        )
        return AnchoredDirectory(self.path / name, descriptor)

    def create_child(self, name: str) -> AnchoredDirectory:
        """Create and then open a directory, verifying the created identity."""

        _validate_name(name)
        os.mkdir(name, 0o700, dir_fd=self.descriptor)
        created: os.stat_result | None = None
        try:
            created = os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(created.st_mode):
                raise AnchoredFilesystemError(
                    f"created anchored entry is not a directory: {self.path / name}"
                )
            return self.child(name, expected=created)
        except BaseException as primary:
            if created is not None:
                try:
                    self._rmdir_if_same(name, created)
                except BaseException as cleanup:
                    primary.add_note(f"created-directory cleanup failed: {cleanup}")
            raise

    def create_child_strict(self, name: str) -> AnchoredDirectory:
        """Create a child only when strict identity binding is available."""

        require_strict_identity_mutation()
        return self.create_child(name)

    def list_names(self) -> list[str]:
        return list(os.listdir(self.descriptor))

    def stat(self, name: str) -> os.stat_result | None:
        _validate_name(name)
        try:
            return os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def same_entry(self, name: str, descriptor: int) -> bool:
        """Return whether ``name`` still refers to the held filesystem object."""

        _validate_name(name)
        try:
            current = os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
            expected = os.fstat(descriptor)
        except FileNotFoundError:
            return False
        return _same_identity(current, expected)

    def open_file(
        self,
        name: str,
        flags: int,
        mode: int = 0o600,
        *,
        expected: os.stat_result | None = None,
        expected_type: int | None = None,
    ) -> int:
        """Open a file relative to this directory and verify its identity."""

        _validate_name(name)
        observed = expected
        is_exclusive_create = bool(flags & os.O_CREAT and flags & os.O_EXCL)
        if observed is None and not is_exclusive_create:
            observed = self.stat(name)
            if observed is None:
                raise FileNotFoundError(name)
        if observed is not None and stat.S_ISLNK(observed.st_mode):
            raise AnchoredFilesystemError(
                f"cannot open a linked entry: {self.path / name}"
            )
        descriptor = -1
        try:
            descriptor = os.open(
                name,
                flags | os.O_NOFOLLOW | _close_on_exec_flag(),
                mode,
                dir_fd=self.descriptor,
            )
            opened = os.fstat(descriptor)
            if observed is not None and not _same_identity(observed, opened):
                raise AnchoredFilesystemError(
                    f"anchored entry changed while opening: {self.path / name}"
                )
            if expected_type is not None and stat.S_IFMT(opened.st_mode) != expected_type:
                raise AnchoredFilesystemError(
                    f"unexpected anchored entry type: {self.path / name}"
                )
            return descriptor
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            if _is_link_entry(self.descriptor, name, exc):
                raise AnchoredFilesystemError(
                    f"cannot open a linked entry: {self.path / name}"
                ) from exc
            raise
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            raise

    def create_file(self, name: str, *, mode: int = 0o600) -> int:
        descriptor = self.open_file(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
            expected_type=stat.S_IFREG,
        )
        try:
            os.fchmod(descriptor, mode)
            return descriptor
        except BaseException:
            try:
                self._unlink_descriptor_if_same(name, descriptor)
            finally:
                os.close(descriptor)
            raise

    @contextmanager
    def lock(self, name: str):
        """Hold an exclusive lock for mutations rooted in this directory."""

        _validate_name(name)
        if fcntl is None:
            raise AnchoredFilesystemUnavailable(
                "descriptor-anchored plugin lifecycle locking is unavailable"
            )
        descriptor = self.descriptor
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as exc:
            raise AnchoredFilesystemError(
                "could not acquire the anchored plugin lifecycle lock"
            ) from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError as exc:
                raise AnchoredFilesystemError(
                    "could not release the anchored plugin lifecycle lock"
                ) from exc

    def rename(
        self,
        source: str,
        destination: str,
        *,
        expected_source: os.stat_result | None = None,
        expected_source_descriptor: int | None = None,
    ) -> None:
        _validate_name(source)
        _validate_name(destination)
        expected = expected_source
        if expected_source_descriptor is not None:
            expected = os.fstat(expected_source_descriptor)
        if expected is not None:
            current = self.stat(source)
            if current is None or not _same_identity(current, expected):
                raise AnchoredFilesystemError(
                    f"anchored entry changed before rename: {self.path / source}"
                )
        os.rename(
            source,
            destination,
            src_dir_fd=self.descriptor,
            dst_dir_fd=self.descriptor,
        )

    def unlink(
        self,
        name: str,
        *,
        missing_ok: bool = False,
        expected: os.stat_result | None = None,
        expected_descriptor: int | None = None,
    ) -> None:
        """Unlink only the entry identity supplied by the caller.

        POSIX has no portable unlink-by-open-directory-entry primitive.  The
        identity check is therefore deliberately performed immediately before
        the anchored unlink; if the observed identity has changed, cleanup
        fails and leaves the replacement in place.
        """

        _validate_name(name)
        expected_metadata = expected
        if expected_descriptor is not None:
            expected_metadata = os.fstat(expected_descriptor)
        current = self.stat(name)
        if current is None:
            if not missing_ok:
                raise FileNotFoundError(name)
            return
        if expected_metadata is not None and not _same_identity(
            current,
            expected_metadata,
        ):
            raise AnchoredFilesystemError(
                f"anchored entry changed before unlink: {self.path / name}"
            )
        if stat.S_ISDIR(current.st_mode):
            raise AnchoredFilesystemError(
                f"cannot unlink anchored directory: {self.path / name}"
            )
        if stat.S_ISLNK(current.st_mode):
            raise AnchoredFilesystemError(
                f"refusing to unlink linked entry: {self.path / name}"
            )
        os.unlink(name, dir_fd=self.descriptor)

    def unlink_strict(
        self,
        name: str,
        *,
        missing_ok: bool = False,
        expected: os.stat_result | None = None,
        expected_descriptor: int | None = None,
    ) -> None:
        """Unlink only through a backend with strict identity semantics."""

        require_strict_identity_mutation()
        self.unlink(
            name,
            missing_ok=missing_ok,
            expected=expected,
            expected_descriptor=expected_descriptor,
        )

    def remove_tree(
        self,
        name: str,
        *,
        expected_descriptor: int | None = None,
    ) -> None:
        """Remove a tree without following replaced entries."""

        metadata = self.stat(name)
        if metadata is None:
            return
        if expected_descriptor is not None and not self.same_entry(
            name,
            expected_descriptor,
        ):
            raise AnchoredFilesystemError("anchored tree entry changed before cleanup")
        if stat.S_ISLNK(metadata.st_mode):
            raise AnchoredFilesystemError(
                f"refusing to clean linked entry: {self.path / name}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            self.unlink(
                name,
                expected=metadata,
                expected_descriptor=expected_descriptor,
            )
            return
        if expected_descriptor is None:
            child = self.child(name, expected=metadata)
        else:
            child = AnchoredDirectory(self.path / name, os.dup(expected_descriptor))
        try:
            child._remove_tree_contents()
        finally:
            child.close()
        current = self.stat(name)
        if current is None:
            return
        if not _same_identity(metadata, current):
            raise AnchoredFilesystemError("anchored tree entry changed during cleanup")
        try:
            os.rmdir(name, dir_fd=self.descriptor)
        except FileNotFoundError:
            return

    def remove_tree_strict(
        self,
        name: str,
        *,
        expected_descriptor: int | None = None,
    ) -> None:
        """Remove a tree only through a strict identity-capable backend."""

        require_strict_identity_mutation()
        self.remove_tree(name, expected_descriptor=expected_descriptor)

    def _remove_tree_contents(self) -> None:
        for name in self.list_names():
            metadata = self.stat(name)
            if metadata is None:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise AnchoredFilesystemError(
                    f"refusing to clean linked entry: {self.path / name}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                child = self.child(name, expected=metadata)
                try:
                    child._remove_tree_contents()
                finally:
                    child.close()
                current = self.stat(name)
                if current is None:
                    continue
                if not _same_identity(metadata, current):
                    raise AnchoredFilesystemError(
                        "anchored tree entry changed during cleanup"
                    )
                try:
                    os.rmdir(name, dir_fd=self.descriptor)
                except FileNotFoundError:
                    continue
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise AnchoredFilesystemError(
                    f"unsupported entry during anchored cleanup: {self.path / name}"
                )
            self.unlink(name, expected=metadata)

    def _rmdir_if_same(self, name: str, expected: os.stat_result) -> None:
        current = self.stat(name)
        if current is None:
            return
        if _same_identity(current, expected) and stat.S_ISDIR(current.st_mode):
            os.rmdir(name, dir_fd=self.descriptor)

    def _unlink_descriptor_if_same(self, name: str, descriptor: int) -> None:
        try:
            self.unlink(name, expected_descriptor=descriptor, missing_ok=True)
        except (AnchoredFilesystemError, OSError):
            pass

    def unique_name(self, prefix: str, suffix: str = "") -> str:
        for _ in range(32):
            candidate = f"{prefix}{secrets.token_hex(16)}{suffix}"
            if self.stat(candidate) is None:
                return candidate
        raise AnchoredFilesystemError("could not allocate a unique staged name")

    def read_file(self, name: str, *, max_bytes: int) -> bytes | None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        metadata = self.stat(name)
        if metadata is None:
            return None
        if not stat.S_ISREG(metadata.st_mode):
            raise AnchoredFilesystemError(f"not a regular file: {self.path / name}")
        descriptor = self.open_file(
            name,
            os.O_RDONLY,
            expected=metadata,
            expected_type=stat.S_IFREG,
        )
        try:
            opened = os.fstat(descriptor)
            if opened.st_size > max_bytes:
                raise AnchoredFilesystemError(
                    f"file exceeds {max_bytes} bytes: {self.path / name}"
                )
            return _read_bounded(descriptor, max_bytes)
        finally:
            os.close(descriptor)

    def sync(self) -> None:
        try:
            os.fsync(self.descriptor)
        except OSError as exc:
            if exc.errno in {
                errno.EINVAL,
                errno.EISDIR,
                errno.ENOSYS,
                errno.ENOTSUP,
                errno.EOPNOTSUPP,
            }:
                return
            raise AnchoredFilesystemError(
                "could not durably update the anchored plugin directory"
            ) from exc


def copy_tree(
    source: AnchoredDirectory | str | Path,
    destination: AnchoredDirectory,
    *,
    max_files: int,
    max_bytes: int,
    max_entries: int,
    max_depth: int,
) -> None:
    """Copy a link-free source tree from a held descriptor when supplied."""

    if isinstance(source, AnchoredDirectory):
        counters = _CopyCounters()
        _copy_directory(
            source,
            destination,
            counters,
            max_files=max_files,
            max_bytes=max_bytes,
            max_entries=max_entries,
            max_depth=max_depth,
            depth=0,
        )
        return
    with AnchoredDirectory.open(source, create=False, private=False) as source_directory:
        copy_tree(
            source_directory,
            destination,
            max_files=max_files,
            max_bytes=max_bytes,
            max_entries=max_entries,
            max_depth=max_depth,
        )


@dataclass
class _CopyCounters:
    files: int = 0
    total_bytes: int = 0
    entries: int = 0


def _copy_directory(
    source: AnchoredDirectory,
    destination: AnchoredDirectory,
    counters: _CopyCounters,
    *,
    max_files: int,
    max_bytes: int,
    max_entries: int,
    max_depth: int,
    depth: int,
) -> None:
    if depth > max_depth:
        raise AnchoredFilesystemError(f"plugin tree exceeds depth {max_depth}")
    for name in sorted(source.list_names()):
        counters.entries += 1
        if counters.entries > max_entries:
            raise AnchoredFilesystemError(f"plugin tree exceeds {max_entries} entries")
        metadata = source.stat(name)
        if metadata is None:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise AnchoredFilesystemError(
                f"plugin tree contains a link: {source.path / name}"
            )
        if stat.S_ISDIR(metadata.st_mode):
            child_source = source.child(name, expected=metadata)
            child_destination: AnchoredDirectory | None = None
            try:
                child_destination = destination.create_child(name)
                _copy_directory(
                    child_source,
                    child_destination,
                    counters,
                    max_files=max_files,
                    max_bytes=max_bytes,
                    max_entries=max_entries,
                    max_depth=max_depth,
                    depth=depth + 1,
                )
                os.fchmod(
                    child_destination.descriptor,
                    stat.S_IMODE(metadata.st_mode),
                )
            finally:
                child_source.close()
                if child_destination is not None:
                    child_destination.close()
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise AnchoredFilesystemError(
                f"plugin tree contains unsupported entry: {source.path / name}"
            )
        counters.files += 1
        if counters.files > max_files:
            raise AnchoredFilesystemError(
                f"plugin contains more than {max_files} files"
            )
        if metadata.st_size > max_bytes - counters.total_bytes:
            raise AnchoredFilesystemError(f"plugin exceeds {max_bytes} bytes")
        source_descriptor = source.open_file(
            name,
            os.O_RDONLY,
            expected=metadata,
            expected_type=stat.S_IFREG,
        )
        destination_descriptor = -1
        try:
            destination_descriptor = destination.create_file(
                name,
                mode=stat.S_IMODE(metadata.st_mode),
            )
            copied = 0
            while True:
                chunk = os.read(source_descriptor, 1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                counters.total_bytes += len(chunk)
                if counters.total_bytes > max_bytes:
                    raise AnchoredFilesystemError(f"plugin exceeds {max_bytes} bytes")
                _write_all(destination_descriptor, chunk)
            os.fsync(destination_descriptor)
            if copied != metadata.st_size:
                raise AnchoredFilesystemError(
                    f"plugin source changed while copying: {source.path / name}"
                )
        except BaseException as primary:
            if destination_descriptor >= 0:
                try:
                    destination.unlink(
                        name,
                        expected_descriptor=destination_descriptor,
                        missing_ok=True,
                    )
                except BaseException as cleanup:
                    primary.add_note(f"copied-file cleanup failed: {cleanup}")
            raise
        finally:
            os.close(source_descriptor)
            if destination_descriptor >= 0:
                os.close(destination_descriptor)


def _open_or_create_directory(
    parent_descriptor: int,
    name: str,
    *,
    create: bool,
    expected: os.stat_result | None = None,
) -> int:
    _validate_name(name)
    observed = expected
    if observed is None:
        try:
            observed = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            observed = None
    if observed is None:
        if not create:
            raise FileNotFoundError(name)
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            raise AnchoredFilesystemError(
                "anchored directory appeared during secure creation"
            )
        else:
            observed = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
    return _open_existing_directory(parent_descriptor, name, observed)


def _open_existing_directory(
    parent_descriptor: int,
    name: str,
    expected: os.stat_result,
) -> int:
    if stat.S_ISLNK(expected.st_mode):
        raise AnchoredFilesystemError("cannot traverse a link in the anchored plugin path")
    if not stat.S_ISDIR(expected.st_mode):
        raise NotADirectoryError(name)
    descriptor = -1
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        _require_directory_identity(opened, expected, Path(name))
        return descriptor
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if _is_link_entry(parent_descriptor, name, exc):
            raise AnchoredFilesystemError(
                "cannot traverse a link in the anchored plugin path"
            ) from exc
        raise
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def _is_link_entry(parent_descriptor: int, name: str, error: OSError) -> bool:
    if error.errno not in {errno.ELOOP, errno.ENOTDIR}:
        return False
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode)


def _require_directory_identity(
    opened: os.stat_result,
    expected: os.stat_result | None,
    path: Path,
) -> None:
    if not stat.S_ISDIR(opened.st_mode):
        raise AnchoredFilesystemError(f"anchored entry is not a directory: {path}")
    if expected is not None and not _same_identity(expected, opened):
        raise AnchoredFilesystemError(f"anchored directory changed while opening: {path}")


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
    )


def _absolute_lexical_path(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _path_components(path: Path) -> tuple[str, ...]:
    anchor = path.anchor
    if not anchor:
        raise AnchoredFilesystemError("anchored plugin path must be absolute")
    components = tuple(path.relative_to(Path(anchor)).parts)
    if not components:
        raise AnchoredFilesystemError(
            "anchored plugin path cannot be the filesystem root"
        )
    if any(component in {"", ".", ".."} for component in components):
        raise AnchoredFilesystemError(
            "anchored plugin path contains invalid components"
        )
    return components


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _close_on_exec_flag()


def _close_on_exec_flag() -> int:
    return getattr(os, "O_CLOEXEC", 0)


def _validate_name(name: str) -> None:
    if not name or name in {".", ".."} or "\x00" in name or Path(name).name != name:
        raise AnchoredFilesystemError(f"invalid anchored entry name: {name!r}")


def _read_bounded(descriptor: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise AnchoredFilesystemError(f"file exceeds {max_bytes} bytes")


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise AnchoredFilesystemError("short write to anchored plugin file")
        view = view[written:]
