"""Descriptor-anchored filesystem operations for plugin lifecycle mutations."""

from __future__ import annotations

import errno
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_FCHMOD = getattr(os, "fchmod", None)
_FLOCK = getattr(fcntl, "flock", None) if fcntl is not None else None
_LOCK_EX = getattr(fcntl, "LOCK_EX", None) if fcntl is not None else None
_LOCK_UN = getattr(fcntl, "LOCK_UN", None) if fcntl is not None else None


def _windows_backend_available() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        import msvcrt

        getattr(ctypes, "WinDLL")
        getattr(msvcrt, "open_osfhandle")
        getattr(msvcrt, "get_osfhandle")
    except (ImportError, AttributeError):
        return False
    return True


def _windows_path_is_reparse(path: Path) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    win_dll: Any = getattr(ctypes, "WinDLL")
    kernel32: Any = win_dll("kernel32", use_last_error=True)
    get_attributes: Any = kernel32.GetFileAttributesW
    get_attributes.argtypes = [wintypes.LPCWSTR]
    get_attributes.restype = wintypes.DWORD
    invalid = 0xFFFFFFFF
    reparse = 0x00000400
    attributes = int(get_attributes(str(path)))
    return attributes != invalid and bool(attributes & reparse)


def _windows_open_entry(
    path: Path,
    *,
    directory: bool,
    readable: bool = True,
    writable: bool = False,
    create_new: bool = False,
) -> int:
    """Open one Windows entry without following a reparse point."""

    if os.name != "nt":
        raise AnchoredFilesystemUnavailable("Windows anchored backend is unavailable")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    win_dll: Any = getattr(ctypes, "WinDLL")
    get_last_error: Any = getattr(ctypes, "get_last_error")
    format_error: Any = getattr(ctypes, "FormatError")
    open_osfhandle: Any = getattr(msvcrt, "open_osfhandle")
    kernel32: Any = win_dll("kernel32", use_last_error=True)
    create_file: Any = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_info: Any = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    get_info.restype = wintypes.BOOL

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

    generic_read = 0x80000000
    generic_write = 0x40000000
    file_list_directory = 0x00000001
    file_read_attributes = 0x00000080
    synchronize = 0x00100000
    share_read = 0x00000001
    share_write = 0x00000002
    share_delete = 0x00000004
    create_new_value = 1
    open_existing = 3
    attribute_normal = 0x00000080
    flag_backup_semantics = 0x02000000
    flag_open_reparse_point = 0x00200000
    attribute_reparse_point = 0x00000400
    invalid_handle = ctypes.c_void_p(-1).value

    desired_access = file_read_attributes | synchronize
    if directory:
        desired_access |= file_list_directory
    else:
        if readable:
            desired_access |= generic_read
        if writable:
            desired_access |= generic_write
    attributes = flag_open_reparse_point
    if directory:
        attributes |= flag_backup_semantics
    else:
        attributes |= attribute_normal
    handle = create_file(
        str(path),
        desired_access,
        share_read | share_write | share_delete,
        None,
        create_new_value if create_new else open_existing,
        attributes,
        None,
    )
    if handle == invalid_handle:
        error_number = int(get_last_error())
        raise OSError(error_number, str(format_error(error_number)), str(path))
    try:
        info = FileAttributeTagInfo()
        if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error_number = int(get_last_error())
            raise OSError(error_number, str(format_error(error_number)), str(path))
        if int(info.FileAttributes) & attribute_reparse_point:
            raise AnchoredFilesystemError(
                f"cannot traverse a link in the anchored plugin path: {path}"
            )
        flags = int(getattr(os, "O_BINARY", 0))
        if writable and readable:
            flags |= os.O_RDWR
        elif writable:
            flags |= os.O_WRONLY
        else:
            flags |= os.O_RDONLY
        return int(open_osfhandle(int(handle), flags))
    except BaseException:
        close_handle: Any = kernel32.CloseHandle
        close_handle(wintypes.HANDLE(handle))
        raise


def _windows_open_directory_path(
    path: str | Path,
    *,
    create: bool,
    expected: os.stat_result | None = None,
) -> tuple[Path, int]:
    absolute = _absolute_lexical_path(path)
    components = _path_components(absolute)
    current = Path(absolute.anchor)
    descriptor = _windows_open_entry(current, directory=True)
    try:
        for component in components:
            next_path = current / component
            if not next_path.exists():
                if not create:
                    raise FileNotFoundError(next_path)
                try:
                    os.mkdir(next_path)
                except FileExistsError as exc:
                    raise AnchoredFilesystemError(
                        "anchored directory appeared during secure creation"
                    ) from exc
            next_descriptor = _windows_open_entry(next_path, directory=True)
            os.close(descriptor)
            descriptor = next_descriptor
            current = next_path
        opened = os.fstat(descriptor)
        _require_directory_identity(opened, expected, absolute)
        return absolute, descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _windows_mutex_lock(descriptor: int):
    import ctypes
    from ctypes import wintypes

    metadata = os.fstat(descriptor)
    win_dll: Any = getattr(ctypes, "WinDLL")
    get_last_error: Any = getattr(ctypes, "get_last_error")
    format_error: Any = getattr(ctypes, "FormatError")
    kernel32: Any = win_dll("kernel32", use_last_error=True)
    create_mutex: Any = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    create_mutex.restype = wintypes.HANDLE
    wait: Any = kernel32.WaitForSingleObject
    wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait.restype = wintypes.DWORD
    release: Any = kernel32.ReleaseMutex
    release.argtypes = [wintypes.HANDLE]
    release.restype = wintypes.BOOL
    close: Any = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    name = f"Local\\AshPluginLifecycle-{metadata.st_dev:x}-{metadata.st_ino:x}"
    handle = create_mutex(None, False, name)
    if not handle:
        error_number = int(get_last_error())
        raise OSError(error_number, str(format_error(error_number)))
    acquired = False
    try:
        status = int(wait(handle, 0xFFFFFFFF))
        if status not in {0x00000000, 0x00000080}:
            raise AnchoredFilesystemError(
                "could not acquire the Windows plugin lifecycle mutex"
            )
        acquired = True
        yield
    finally:
        if acquired and not release(handle):
            error_number = int(get_last_error())
            close(handle)
            raise OSError(error_number, str(format_error(error_number)))
        close(handle)


class AnchoredFilesystemError(RuntimeError):
    """A descriptor-anchored plugin filesystem operation was unsafe or failed."""


class AnchoredFilesystemUnavailable(AnchoredFilesystemError):
    """The current platform cannot provide descriptor-anchored mutation."""


def supports_anchored_mutation() -> bool:
    """Return whether this runtime has the required POSIX descriptor primitives."""

    if os.name == "nt":
        return _windows_backend_available()
    if os.name != "posix" or fcntl is None:
        return False
    if not _O_DIRECTORY or not _O_NOFOLLOW or not os.O_CREAT or not os.O_EXCL:
        return False
    if _FCHMOD is None or _FLOCK is None or _LOCK_EX is None or _LOCK_UN is None:
        return False
    if not all(
        hasattr(os, name)
        for name in (
            "close",
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
        if os.name == "nt":
            absolute, descriptor = _windows_open_directory_path(
                path,
                create=create,
                expected=expected,
            )
            return cls(absolute, descriptor)
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
                _fchmod(current_descriptor, 0o700)
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
        """Return the held directory's visible path for legacy validators.

        A descriptor pseudo-path is not portable as a directory pathname:
        macOS exposes ``/dev/fd`` entries, but child lookups such as
        ``/dev/fd/12/component`` do not provide the Linux ``/proc`` behavior
        that the old fallback relied on.  Legacy validators are therefore
        given the ordinary path only while it still identifies the held
        directory.  If that name has been displaced, failing closed is safer
        than handing the validator a path that may not address the descriptor.
        """

        return self._visible_path()

    def descriptor_path(self) -> Path:
        """Return a pathname usable by an external process while held.

        External tools such as Git need a normal directory pathname.  Keep
        the descriptor as the authority for Ash's own operations, and only
        expose the visible pathname after confirming that it still identifies
        the held directory.  Descriptor pseudo-paths are intentionally not
        used because their child-path semantics differ across POSIX systems.
        """

        try:
            return self._visible_path()
        except OSError as exc:
            raise AnchoredFilesystemUnavailable(
                "the anchored directory has no usable visible pathname"
            ) from exc

    def _visible_path(self) -> Path:
        held = os.fstat(self.descriptor)
        if os.name == "nt" and _windows_path_is_reparse(self.path):
            raise AnchoredFilesystemError(
                f"anchored directory path became a reparse point: {self.path}"
            )
        try:
            current = os.stat(self.path, follow_symlinks=False)
        except OSError:
            raise
        if not stat.S_ISDIR(current.st_mode) or not _same_identity(held, current):
            raise AnchoredFilesystemError(
                f"anchored directory path no longer identifies the held directory: "
                f"{self.path}"
            )
        return self.path

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
        if os.name == "nt":
            self._visible_path()
            descriptor = _windows_open_entry(self.path / name, directory=True)
            try:
                opened = os.fstat(descriptor)
                _require_directory_identity(opened, expected, self.path / name)
                self._visible_path()
                return AnchoredDirectory(self.path / name, descriptor)
            except BaseException:
                os.close(descriptor)
                raise
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
        if os.name == "nt":
            self._visible_path()
            target = self.path / name
            os.mkdir(target)
            created_windows: os.stat_result | None = None
            try:
                created_windows = os.stat(target, follow_symlinks=False)
                child = self.child(name, expected=created_windows)
                self._visible_path()
                return child
            except BaseException as primary:
                if created_windows is not None:
                    try:
                        current = os.stat(target, follow_symlinks=False)
                        if _same_identity(created_windows, current):
                            os.rmdir(target)
                    except BaseException as cleanup:
                        primary.add_note(f"created-directory cleanup failed: {cleanup}")
                raise
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
        if os.name == "nt":
            self._visible_path()
            names = os.listdir(self.path)
            self._visible_path()
            return names
        return list(os.listdir(self.descriptor))

    def stat(self, name: str) -> os.stat_result | None:
        _validate_name(name)
        if os.name == "nt":
            self._visible_path()
            target = self.path / name
            try:
                metadata = os.stat(target, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if _windows_path_is_reparse(target):
                raise AnchoredFilesystemError(
                    f"cannot use a linked entry: {target}"
                )
            self._visible_path()
            return metadata
        try:
            return os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def same_entry(self, name: str, descriptor: int) -> bool:
        """Return whether ``name`` still refers to the held filesystem object."""

        _validate_name(name)
        if os.name == "nt":
            current = self.stat(name)
            if current is None:
                return False
            expected = os.fstat(descriptor)
            return _same_identity(current, expected)
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
        if os.name == "nt":
            observed = expected
            is_exclusive_create = bool(flags & os.O_CREAT and flags & os.O_EXCL)
            if observed is None and not is_exclusive_create:
                observed = self.stat(name)
                if observed is None:
                    raise FileNotFoundError(name)
            writable = bool(flags & (os.O_WRONLY | os.O_RDWR))
            readable = not bool(flags & os.O_WRONLY) or bool(flags & os.O_RDWR)
            descriptor = _windows_open_entry(
                self.path / name,
                directory=False,
                readable=readable,
                writable=writable,
                create_new=is_exclusive_create,
            )
            try:
                opened = os.fstat(descriptor)
                if observed is not None and not _same_identity(observed, opened):
                    raise AnchoredFilesystemError(
                        f"anchored entry changed while opening: {self.path / name}"
                    )
                if expected_type is not None and stat.S_IFMT(opened.st_mode) != expected_type:
                    raise AnchoredFilesystemError(
                        f"unexpected anchored entry type: {self.path / name}"
                    )
                self._visible_path()
                return descriptor
            except BaseException:
                os.close(descriptor)
                raise
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
                flags | _nofollow_flag() | _close_on_exec_flag(),
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

    def chmod(self, mode: int) -> None:
        """Apply a mode to the held directory through the anchored descriptor."""

        if os.name == "nt":
            return
        _fchmod(self.descriptor, mode)

    def create_file(self, name: str, *, mode: int = 0o600) -> int:
        descriptor = self.open_file(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
            expected_type=stat.S_IFREG,
        )
        try:
            if os.name != "nt":
                _fchmod(descriptor, mode)
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
        if os.name == "nt":
            with _windows_mutex_lock(self.descriptor):
                yield
            return
        if _FLOCK is None or _LOCK_EX is None or _LOCK_UN is None:
            raise AnchoredFilesystemUnavailable(
                "descriptor-anchored plugin lifecycle locking is unavailable"
            )
        descriptor = self.descriptor
        try:
            _FLOCK(descriptor, _LOCK_EX)
        except OSError as exc:
            raise AnchoredFilesystemError(
                "could not acquire the anchored plugin lifecycle lock"
            ) from exc
        try:
            yield
        finally:
            try:
                _FLOCK(descriptor, _LOCK_UN)
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
        if os.name == "nt":
            self._visible_path()
            destination_metadata = self.stat(destination)
            if destination_metadata is not None and _windows_path_is_reparse(
                self.path / destination
            ):
                raise AnchoredFilesystemError(
                    f"refusing to replace linked entry: {self.path / destination}"
                )
            os.replace(self.path / source, self.path / destination)
            self._visible_path()
            if expected is not None:
                moved = self.stat(destination)
                if moved is None or not _same_identity(moved, expected):
                    raise AnchoredFilesystemError(
                        f"anchored entry changed during rename: {self.path / destination}"
                    )
            return
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
        if os.name == "nt":
            self._visible_path()
            os.unlink(self.path / name)
            self._visible_path()
            return
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
        if os.name == "nt":
            self._visible_path()
            os.rmdir(self.path / name)
            self._visible_path()
            return
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
                if os.name == "nt":
                    self._visible_path()
                    os.rmdir(self.path / name)
                    self._visible_path()
                    continue
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
            if os.name == "nt":
                os.rmdir(self.path / name)
                return
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
                child_destination.chmod(stat.S_IMODE(metadata.st_mode))
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
    return os.O_RDONLY | _directory_flag() | _nofollow_flag() | _close_on_exec_flag()


def _directory_flag() -> int:
    if not _O_DIRECTORY:
        raise AnchoredFilesystemUnavailable(
            "descriptor-anchored directory opens are unavailable"
        )
    return _O_DIRECTORY


def _nofollow_flag() -> int:
    if not _O_NOFOLLOW:
        raise AnchoredFilesystemUnavailable(
            "descriptor-anchored no-follow opens are unavailable"
        )
    return _O_NOFOLLOW


def _fchmod(descriptor: int, mode: int) -> None:
    if _FCHMOD is None:
        raise AnchoredFilesystemUnavailable(
            "descriptor-anchored permission changes are unavailable"
        )
    _FCHMOD(descriptor, mode)


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
