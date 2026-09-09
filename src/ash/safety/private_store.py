"""Descriptor-anchored private storage for security-sensitive local records."""

from __future__ import annotations

import errno
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


PRIVATE_STORE_UNAVAILABLE_MESSAGE = (
    "secure MCP OAuth credential persistence is unavailable on this platform/build"
)
_UNAVAILABLE_ERRNOS = {
    errno.EACCES,
    errno.EINVAL,
    errno.EIO,
    errno.EISDIR,
    errno.ENOSYS,
    errno.ENOENT,
    errno.ENOTSUP,
    errno.EOPNOTSUPP,
    errno.EPERM,
    errno.EROFS,
}


class PrivateStoreError(RuntimeError):
    """A private-store operation could not be completed safely."""


class PrivateStoreUnavailable(PrivateStoreError):
    """The platform cannot provide the private-store security contract."""


def secure_private_store_available() -> bool:
    """Return whether the required descriptor-relative primitives are available."""

    if os.name != "posix":
        return False
    if any(
        not getattr(os, name, 0)
        for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CREAT", "O_EXCL")
    ):
        return False
    if not all(
        hasattr(os, name)
        for name in (
            "close",
            "fchmod",
            "fstat",
            "fsync",
            "mkdir",
            "open",
            "read",
            "rename",
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
        for function in (os.mkdir, os.open, os.rename, os.stat, os.unlink)
    )


class PrivateStore:
    """A small descriptor-anchored store for one private directory."""

    def __init__(
        self,
        directory: str | Path,
        *,
        trusted_root: str | Path | None = None,
    ) -> None:
        self.directory = _absolute_lexical_path(directory)
        self.trusted_root = _absolute_lexical_path(
            trusted_root if trusted_root is not None else self.directory.parent
        )
        try:
            relative = self.directory.relative_to(self.trusted_root)
        except ValueError as exc:
            raise PrivateStoreError(
                "private credential store must be below its trusted anchor"
            ) from exc
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise PrivateStoreError(
                "private credential store must contain a relative directory path"
            )
        self._anchor_components = _path_components(self.trusted_root)
        self._components = tuple(relative.parts)

    @contextmanager
    def opened(self, *, create: bool = True) -> Iterator[int]:
        """Open and retain the private store directory for one operation."""

        descriptor = self._open_store_directory(create=create)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    def read(self, name: str, *, max_bytes: int) -> bytes | None:
        """Read one bounded regular file relative to the anchored store."""

        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        _validate_name(name)
        try:
            with self.opened(create=False) as store_descriptor:
                flags = os.O_RDONLY | _close_on_exec_flag() | os.O_NOFOLLOW
                try:
                    descriptor = os.open(name, flags, dir_fd=store_descriptor)
                except FileNotFoundError:
                    return None
                except OSError as exc:
                    raise _record_open_error(exc) from exc
                try:
                    metadata = os.fstat(descriptor)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise PrivateStoreError(
                            "MCP OAuth token record is not a regular file"
                        )
                    if metadata.st_size > max_bytes:
                        raise PrivateStoreError("MCP OAuth token record exceeded 1 MB")
                    return _read_bounded(descriptor, max_bytes)
                finally:
                    os.close(descriptor)
        except FileNotFoundError:
            return None
        except PrivateStoreError:
            raise
        except OSError as exc:
            raise _operation_error(
                "unable to read the secure MCP OAuth token record",
                exc,
            ) from exc

    def write(self, name: str, payload: bytes) -> None:
        """Atomically replace one private regular file using one held directory."""

        _validate_name(name)
        if not isinstance(payload, bytes):
            raise TypeError("private-store payload must be bytes")
        try:
            with self.opened() as store_descriptor:
                _require_regular_or_missing(store_descriptor, name)
                temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
                descriptor = -1
                created = False
                try:
                    flags = (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | _close_on_exec_flag()
                        | os.O_NOFOLLOW
                    )
                    descriptor = os.open(
                        temporary_name,
                        flags,
                        0o600,
                        dir_fd=store_descriptor,
                    )
                    created = True
                    os.fchmod(descriptor, 0o600)
                    _write_all(descriptor, payload)
                    os.fsync(descriptor)
                    os.close(descriptor)
                    descriptor = -1
                    _require_regular_or_missing(store_descriptor, name)
                    os.rename(
                        temporary_name,
                        name,
                        src_dir_fd=store_descriptor,
                        dst_dir_fd=store_descriptor,
                    )
                    _sync_directory(store_descriptor)
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                    if created:
                        try:
                            os.unlink(temporary_name, dir_fd=store_descriptor)
                        except FileNotFoundError:
                            pass
        except PrivateStoreError:
            raise
        except OSError as exc:
            raise _operation_error(
                "unable to write the secure MCP OAuth token record",
                exc,
            ) from exc

    def remove(self, name: str) -> bool:
        """Unlink one anchored regular file without following its final entry."""

        _validate_name(name)
        try:
            with self.opened(create=False) as store_descriptor:
                if _require_regular_or_missing(store_descriptor, name) is None:
                    return False
                try:
                    os.unlink(name, dir_fd=store_descriptor)
                except FileNotFoundError:
                    return False
                _sync_directory(store_descriptor)
                return True
        except FileNotFoundError:
            return False
        except PrivateStoreError:
            raise
        except OSError as exc:
            raise _operation_error(
                "unable to remove the secure MCP OAuth token record",
                exc,
            ) from exc

    def _open_store_directory(self, *, create: bool) -> int:
        _require_available()
        root_descriptor = -1
        current_descriptor = -1
        try:
            root_descriptor = os.open(
                Path(self.trusted_root.anchor or os.sep),
                os.O_RDONLY | _close_on_exec_flag() | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            current_descriptor = root_descriptor
            all_components = self._anchor_components + self._components
            for index, component in enumerate(all_components):
                next_descriptor = _open_or_create_directory(
                    current_descriptor,
                    component,
                    create=create and index >= len(self._anchor_components),
                )
                if current_descriptor != root_descriptor:
                    os.close(current_descriptor)
                current_descriptor = next_descriptor
            metadata = os.fstat(current_descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise PrivateStoreError(
                    "MCP OAuth credential store is not a directory"
                )
            os.fchmod(current_descriptor, 0o700)
            result = current_descriptor
            current_descriptor = -1
            if root_descriptor >= 0 and root_descriptor != result:
                os.close(root_descriptor)
            root_descriptor = -1
            return result
        except PrivateStoreError:
            raise
        except FileNotFoundError as exc:
            if not create:
                raise
            raise _directory_error(exc) from exc
        except OSError as exc:
            raise _directory_error(exc) from exc
        finally:
            if current_descriptor >= 0:
                os.close(current_descriptor)
            if root_descriptor >= 0 and root_descriptor != current_descriptor:
                os.close(root_descriptor)


def _absolute_lexical_path(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _path_components(path: Path) -> tuple[str, ...]:
    anchor = path.anchor
    if not anchor:
        raise PrivateStoreError("private credential anchor must be absolute")
    return tuple(path.relative_to(Path(anchor)).parts)


def _close_on_exec_flag() -> int:
    return getattr(os, "O_CLOEXEC", 0)


def _require_available() -> None:
    if not secure_private_store_available():
        raise PrivateStoreUnavailable(PRIVATE_STORE_UNAVAILABLE_MESSAGE)


def _validate_name(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or "\x00" in name
        or Path(name).name != name
    ):
        raise PrivateStoreError("invalid private credential record name")


def _open_or_create_directory(
    parent_descriptor: int,
    name: str,
    *,
    create: bool,
) -> int:
    flags = os.O_RDONLY | _close_on_exec_flag() | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        else:
            _sync_directory(parent_descriptor)
        try:
            return os.open(name, flags, dir_fd=parent_descriptor)
        except OSError as exc:
            raise _directory_error(
                exc,
                parent_descriptor=parent_descriptor,
                name=name,
            ) from exc
    except OSError as exc:
        raise _directory_error(
            exc,
            parent_descriptor=parent_descriptor,
            name=name,
        ) from exc


def _directory_error(
    exc: OSError,
    *,
    parent_descriptor: int | None = None,
    name: str | None = None,
) -> PrivateStoreError:
    linked = exc.errno == errno.ELOOP
    if not linked and parent_descriptor is not None and name is not None:
        try:
            linked = stat.S_ISLNK(
                os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False).st_mode
            )
        except OSError:
            linked = False
    if linked:
        return PrivateStoreError(
            "refusing to use linked MCP OAuth credential path "
            "(symlinked MCP OAuth directory)"
        )
    if exc.errno == errno.ENOTDIR:
        return PrivateStoreError(
            "MCP OAuth credential path contains a non-directory component"
        )
    if exc.errno in _UNAVAILABLE_ERRNOS:
        return PrivateStoreUnavailable(PRIVATE_STORE_UNAVAILABLE_MESSAGE)
    return PrivateStoreError("unable to establish the secure MCP OAuth credential store")


def _record_open_error(exc: OSError) -> PrivateStoreError:
    if exc.errno == errno.ELOOP:
        return PrivateStoreError(
            "refusing to read symlinked MCP OAuth token record"
        )
    if exc.errno == errno.ENOTDIR:
        return PrivateStoreError(
            "MCP OAuth token record is not a regular file"
        )
    return _operation_error(
        "unable to read the secure MCP OAuth token record",
        exc,
    )


def _operation_error(message: str, exc: OSError) -> PrivateStoreError:
    if exc.errno in _UNAVAILABLE_ERRNOS:
        return PrivateStoreUnavailable(PRIVATE_STORE_UNAVAILABLE_MESSAGE)
    return PrivateStoreError(message)


def _require_regular_or_missing(
    store_descriptor: int,
    name: str,
) -> os.stat_result | None:
    try:
        metadata = os.stat(
            name,
            dir_fd=store_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        raise PrivateStoreError(
            "refusing to use symlinked MCP OAuth token record"
        )
    if not stat.S_ISREG(metadata.st_mode):
        raise PrivateStoreError("MCP OAuth token record is not a regular file")
    return metadata


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
            raise PrivateStoreError("MCP OAuth token record exceeded 1 MB")


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise PrivateStoreError("short write to secure MCP OAuth token record")
        view = view[written:]


def _sync_directory(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno in {
            errno.EINVAL,
            errno.EISDIR,
            errno.ENOSYS,
            errno.ENOTSUP,
            errno.EOPNOTSUPP,
        }:
            return
        raise PrivateStoreError(
            "unable to durably update the secure MCP OAuth credential store"
        ) from exc
