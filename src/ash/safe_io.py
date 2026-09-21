"""Small bounded readers for attacker-controlled or mutable local files."""

from __future__ import annotations

import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, TextIO

from ash.safety.path_scope import lexical_target_path, path_has_link_component


def _directory_open_flag() -> int:
    """Return POSIX ``O_DIRECTORY`` when available without exposing it on Windows."""

    value = getattr(os, "O_DIRECTORY", 0)
    return value if isinstance(value, int) else 0


def _fchmod_descriptor(descriptor: int, mode: int) -> None:
    """Apply descriptor permissions when the host exposes ``fchmod``."""

    fchmod = getattr(os, "fchmod", None)
    if not callable(fchmod):
        raise OSError("descriptor chmod is unavailable on this platform")
    fchmod(descriptor, mode)


def _open_windows_replaceable_regular_file(path: Path) -> int:
    """Create one Windows file handle that remains renameable while held open."""

    if os.name != "nt":
        raise OSError("Windows replaceable file open is unavailable on this platform")

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

    generic_read = 0x80000000
    generic_write = 0x40000000
    file_share_delete = 0x00000004
    create_new = 1
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    invalid_handle = ctypes.c_void_p(-1).value

    handle = create_file(
        str(path),
        generic_read | generic_write,
        file_share_delete,
        None,
        create_new,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    if handle == invalid_handle:
        error_number = int(get_last_error())
        raise OSError(error_number, str(format_error(error_number)), str(path))

    flags = os.O_RDWR | int(getattr(os, "O_BINARY", 0))
    try:
        return int(open_osfhandle(int(handle), flags))
    except BaseException:
        close_handle: Any = kernel32.CloseHandle
        close_handle(wintypes.HANDLE(handle))
        raise


def strict_json_loads(value: str | bytes | bytearray) -> Any:
    """Parse JSON while rejecting duplicate object keys and invalid constants."""

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key!r}")
            result[key] = item
        return result

    def reject_constant(raw: str) -> None:
        raise ValueError(f"invalid JSON constant: {raw}")

    return json.loads(
        value,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def validate_unlinked_path(
    path: str | Path,
    *,
    trusted_root: str | Path,
    label: str,
) -> Path:
    """Return one lexical path after rejecting link components below a trusted root."""

    root = Path(trusted_root).expanduser()
    root = Path(os.path.abspath(root))
    target = lexical_target_path(path, root)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"refusing to use {label} outside trusted root: {target}") from exc
    link = path_has_link_component(target, root)
    if link is not None:
        raise ValueError(f"refusing to use {label} through a symlink or junction: {link}")
    return target


def _validate_unlinked_target_and_parent(path: str | Path, *, label: str) -> Path:
    target = Path(os.path.abspath(Path(path).expanduser()))
    return validate_unlinked_path(
        target,
        trusted_root=target.parent.parent,
        label=label,
    )


def validate_unlinked_file_path(path: str | Path, *, label: str) -> Path:
    """Reject a linked file or immediate parent while preserving wider path aliases."""

    return _validate_unlinked_target_and_parent(path, label=label)


def validate_unlinked_directory_path(path: str | Path, *, label: str) -> Path:
    """Reject a linked directory or immediate parent without resolving wider aliases."""

    return _validate_unlinked_target_and_parent(path, label=label)


@contextmanager
def open_unlinked_regular_file(
    path: str | Path,
    *,
    label: str,
) -> Iterator[int]:
    """Open one existing regular file without re-resolving its final entry."""

    target = validate_unlinked_file_path(path, label=label)
    parent_descriptor = -1
    descriptor = -1
    try:
        parent_descriptor = _open_parent_directory(target)
        flags = os.O_RDONLY | _close_on_exec_flag() | _nofollow_flag()
        if parent_descriptor >= 0:
            descriptor = os.open(target.name, flags, dir_fd=parent_descriptor)
        else:
            descriptor = os.open(target, flags)
            _verify_path_identity(target, descriptor, label=label)
        _require_regular_descriptor(descriptor, target, label=label)
        yield descriptor
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


@contextmanager
def create_unlinked_regular_file(
    path: str | Path,
    *,
    label: str,
    mode: int = 0o600,
) -> Iterator[int]:
    """Exclusively create one regular file and remove only that inode on failure."""

    target = validate_unlinked_file_path(path, label=label)
    parent_descriptor = -1
    descriptor = -1
    completed = False
    try:
        parent_descriptor = _open_parent_directory(target)
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | _close_on_exec_flag()
            | _nofollow_flag()
        )
        if parent_descriptor >= 0:
            descriptor = os.open(target.name, flags, mode, dir_fd=parent_descriptor)
        elif os.name == "nt":
            descriptor = _open_windows_replaceable_regular_file(target)
            _verify_path_identity(target, descriptor, label=label)
        else:
            descriptor = os.open(target, flags, mode)
            _verify_path_identity(target, descriptor, label=label)
        _require_regular_descriptor(descriptor, target, label=label)
        if hasattr(os, "fchmod") and os.name != "nt":
            _fchmod_descriptor(descriptor, mode)
        yield descriptor
        completed = True
    finally:
        if not completed and descriptor >= 0:
            _unlink_same_open_file(
                target,
                descriptor,
                parent_descriptor=parent_descriptor,
            )
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def descriptor_path(descriptor: int) -> str | None:
    """Return a stable path to one held descriptor when the OS exposes one."""

    if descriptor < 0:
        raise ValueError("descriptor must be non-negative")
    if os.name != "posix":
        return None
    candidate = f"/dev/fd/{descriptor}"
    try:
        os.stat(candidate)
    except OSError:
        return None
    return candidate


def verify_open_file_identity(
    path: str | Path,
    descriptor: int,
    *,
    label: str,
) -> Path:
    """Require that a visible file path still names the held regular-file inode."""

    target = validate_unlinked_file_path(path, label=label)
    _require_regular_descriptor(descriptor, target, label=label)
    parent_descriptor = -1
    try:
        parent_descriptor = _open_parent_directory(target)
        if parent_descriptor >= 0:
            observed = os.stat(
                target.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        else:
            observed = os.lstat(target)
        opened = os.fstat(descriptor)
        if stat.S_ISLNK(observed.st_mode) or (
            observed.st_dev,
            observed.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"refusing to use {label} that changed after opening: {target}")
        return target
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def replace_open_file(
    source: str | Path,
    destination: str | Path,
    descriptor: int,
    *,
    label: str,
) -> Path:
    """Replace a same-directory destination with the inode held by ``descriptor``."""

    source_path = validate_unlinked_file_path(source, label=label)
    destination_path = validate_unlinked_file_path(destination, label=label)
    if source_path.parent != destination_path.parent:
        raise ValueError(f"refusing to move {label} across directories")
    _require_regular_descriptor(descriptor, source_path, label=label)
    parent_descriptor = -1
    try:
        parent_descriptor = _open_parent_directory(source_path)
        supports_dir_fd = getattr(os, "supports_dir_fd", ())
        if parent_descriptor >= 0 and os.rename in supports_dir_fd:
            observed = os.stat(
                source_path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            opened = os.fstat(descriptor)
            if stat.S_ISLNK(observed.st_mode) or (
                observed.st_dev,
                observed.st_ino,
            ) != (opened.st_dev, opened.st_ino):
                raise ValueError(
                    f"refusing to replace {label} that changed after opening: {source_path}"
                )
            os.rename(
                source_path.name,
                destination_path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            try:
                os.fsync(parent_descriptor)
            except OSError:
                pass
            return destination_path
        verify_open_file_identity(source_path, descriptor, label=label)
        os.replace(source_path, destination_path)
        return destination_path
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def read_bounded_open_file(
    path: str | Path,
    max_bytes: int,
    *,
    label: str,
    trusted_root: str | Path | None = None,
) -> bytes:
    """Read one bounded regular file through a held no-follow descriptor."""

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    if trusted_root is not None and _supports_anchored_path_io():
        with _open_anchored_parent(
            path,
            trusted_root=trusted_root,
            label=label,
        ) as (parent_descriptor, name, target):
            flags = os.O_RDONLY | _close_on_exec_flag() | _nofollow_flag()
            try:
                descriptor = os.open(name, flags, dir_fd=parent_descriptor)
            except OSError as exc:
                try:
                    observed = os.stat(
                        name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except OSError:
                    raise exc
                if stat.S_ISLNK(observed.st_mode):
                    raise ValueError(f"refusing to read symlinked {label}: {target}") from exc
                raise
            try:
                return _read_bounded_descriptor(
                    descriptor,
                    max_bytes,
                    path=target,
                    label=label,
                )
            finally:
                os.close(descriptor)
    with open_unlinked_regular_file(path, label=label) as descriptor:
        return _read_bounded_descriptor(
            descriptor,
            max_bytes,
            path=Path(path),
            label=label,
        )


@contextmanager
def open_anchored_regular_file(
    path: str | Path,
    *,
    trusted_root: str | Path,
    label: str,
    writable: bool = False,
) -> Iterator[int]:
    """Open one regular file below ``trusted_root`` without following links."""

    if not _supports_anchored_path_io():
        validate_unlinked_path(path, trusted_root=trusted_root, label=label)
        target = validate_unlinked_file_path(path, label=label)
        flags = (os.O_RDWR if writable else os.O_RDONLY) | _close_on_exec_flag()
        flags |= _nofollow_flag()
        descriptor = os.open(target, flags)
        try:
            _require_regular_descriptor(descriptor, target, label=label)
            yield descriptor
        finally:
            os.close(descriptor)
        return
    with _open_anchored_parent(
        path,
        trusted_root=trusted_root,
        label=label,
    ) as (parent_descriptor, name, target):
        flags = (os.O_RDWR if writable else os.O_RDONLY) | _close_on_exec_flag()
        flags |= _nofollow_flag()
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except OSError as exc:
            try:
                observed = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except OSError:
                raise exc
            if stat.S_ISLNK(observed.st_mode):
                raise ValueError(f"refusing to open symlinked {label}: {target}") from exc
            raise
        try:
            _require_regular_descriptor(descriptor, target, label=label)
            yield descriptor
        finally:
            os.close(descriptor)


@contextmanager
def create_anchored_regular_file(
    path: str | Path,
    *,
    trusted_root: str | Path,
    label: str,
    mode: int = 0o600,
) -> Iterator[int]:
    """Exclusively create one regular file below ``trusted_root``."""

    if not _supports_anchored_path_io():
        validate_unlinked_path(path, trusted_root=trusted_root, label=label)
        with create_unlinked_regular_file(path, label=label, mode=mode) as descriptor:
            yield descriptor
        return
    with _open_anchored_parent(
        path,
        trusted_root=trusted_root,
        label=label,
    ) as (parent_descriptor, name, target):
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | _close_on_exec_flag()
            | _nofollow_flag()
        )
        try:
            descriptor = os.open(name, flags, mode, dir_fd=parent_descriptor)
        except FileExistsError as exc:
            observed = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(observed.st_mode):
                raise ValueError(
                    f"refusing to use {label} through a symlink or junction: {target}"
                ) from exc
            raise
        completed = False
        try:
            _require_regular_descriptor(descriptor, target, label=label)
            if hasattr(os, "fchmod") and os.name != "nt":
                _fchmod_descriptor(descriptor, mode)
            yield descriptor
            completed = True
        finally:
            if not completed:
                _unlink_same_directory_entry(parent_descriptor, name, descriptor)
            os.close(descriptor)


def unlink_anchored_open_file(
    path: str | Path,
    descriptor: int,
    *,
    trusted_root: str | Path,
    label: str,
) -> None:
    """Unlink ``path`` only while it still names the already-open descriptor."""

    if not _supports_anchored_path_io():
        validate_unlinked_path(path, trusted_root=trusted_root, label=label)
        unlink_open_file(path, descriptor, label=label)
        return
    with _open_anchored_parent(
        path,
        trusted_root=trusted_root,
        label=label,
    ) as (parent_descriptor, name, target):
        opened = os.fstat(descriptor)
        observed = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(observed.st_mode) or (
            observed.st_dev,
            observed.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise ValueError(
                f"refusing to unlink {label} that changed after opening: {target}"
            )
        os.unlink(name, dir_fd=parent_descriptor)
        try:
            os.fsync(parent_descriptor)
        except OSError:
            pass


def require_anchored_path_absent(
    path: str | Path,
    *,
    trusted_root: str | Path,
    label: str,
) -> None:
    """Require that one anchored path is still absent."""

    root, target, _ = _anchored_path_parts(
        path,
        trusted_root=trusted_root,
        label=label,
    )
    if not _supports_anchored_path_io():
        validate_unlinked_path(target.parent, trusted_root=root, label=label)
        try:
            os.lstat(target)
        except FileNotFoundError:
            return
        raise ValueError(f"refusing to use {label} that appeared after snapshot: {target}")
    with _open_anchored_parent(
        target,
        trusted_root=root,
        label=label,
    ) as (parent_descriptor, name, _target):
        try:
            os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise ValueError(f"refusing to use {label} that appeared after snapshot: {target}")


def replace_anchored_open_file(
    source: str | Path,
    destination: str | Path,
    descriptor: int,
    *,
    trusted_root: str | Path,
    label: str,
    expected_destination_descriptor: int | None = None,
    require_destination_absent: bool = False,
) -> Path:
    """Atomically replace one anchored sibling with the inode held by ``descriptor``."""

    root, source_path, source_parts = _anchored_path_parts(
        source,
        trusted_root=trusted_root,
        label=label,
    )
    _root, destination_path, destination_parts = _anchored_path_parts(
        destination,
        trusted_root=root,
        label=label,
    )
    if source_parts[:-1] != destination_parts[:-1]:
        raise ValueError(f"refusing to move {label} across directories")
    if expected_destination_descriptor is not None and require_destination_absent:
        raise ValueError("destination cannot be both expected and required absent")
    if not _supports_anchored_path_io():
        return replace_open_file(source_path, destination_path, descriptor, label=label)

    with _open_anchored_parent(
        source_path,
        trusted_root=root,
        label=label,
    ) as (parent_descriptor, source_name, target):
        opened = os.fstat(descriptor)
        observed = os.stat(
            source_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(observed.st_mode) or (
            observed.st_dev,
            observed.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise ValueError(
                f"refusing to replace {label} that changed after opening: {target}"
            )
        destination_name = destination_parts[-1]
        try:
            destination_metadata = os.stat(
                destination_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            destination_metadata = None
        if expected_destination_descriptor is not None:
            expected_destination = os.fstat(expected_destination_descriptor)
            if destination_metadata is None or stat.S_ISLNK(
                destination_metadata.st_mode
            ) or (
                destination_metadata.st_dev,
                destination_metadata.st_ino,
            ) != (
                expected_destination.st_dev,
                expected_destination.st_ino,
            ):
                raise ValueError(
                    f"refusing to replace {label} destination that changed after "
                    f"snapshot: {destination_path}"
                )
        elif require_destination_absent and destination_metadata is not None:
            raise ValueError(
                f"refusing to replace {label} destination that appeared after "
                f"snapshot: {destination_path}"
            )
        if destination_metadata is not None and not stat.S_ISREG(
            destination_metadata.st_mode
        ):
            raise ValueError(
                f"refusing to replace non-regular {label}: {destination_path}"
            )
        os.rename(
            source_name,
            destination_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        try:
            os.fsync(parent_descriptor)
        except OSError:
            pass
        current = os.stat(
            destination_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(
                f"refusing to use {label} that changed during replacement: "
                f"{destination_path}"
            )
        return destination_path


def _read_bounded_descriptor(
    descriptor: int,
    max_bytes: int,
    *,
    path: Path,
    label: str,
) -> bytes:
    _require_regular_descriptor(descriptor, path, label=label)
    metadata = os.fstat(descriptor)
    if metadata.st_size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes: {path}")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"{label} exceeds {max_bytes} bytes: {path}")
    return b"".join(chunks)


def atomic_write_unlinked_bytes(
    path: str | Path,
    payload: bytes,
    *,
    label: str,
    mode: int = 0o600,
    trusted_root: str | Path | None = None,
) -> Path:
    """Atomically replace one file without following a mutable parent or leaf link."""

    if not isinstance(payload, bytes):
        raise TypeError("atomic file payload must be bytes")
    if trusted_root is not None and _supports_anchored_path_io():
        target, _identity = _atomic_write_anchored_bytes(
            path,
            payload,
            trusted_root=trusted_root,
            label=label,
            mode=mode,
        )
        return target
    if trusted_root is not None:
        validate_unlinked_path(path, trusted_root=trusted_root, label=label)
    target = validate_unlinked_file_path(path, label=label)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(16)}.tmp")
    with create_unlinked_regular_file(
        temporary,
        label=f"{label} temporary",
        mode=mode,
    ) as descriptor:
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(f"short write while writing {label}")
            view = view[written:]
        os.fsync(descriptor)
        return replace_open_file(
            temporary,
            target,
            descriptor,
            label=label,
        )


def atomic_write_unlinked_bytes_with_identity(
    path: str | Path,
    payload: bytes,
    *,
    trusted_root: str | Path,
    label: str,
    mode: int = 0o600,
) -> tuple[Path, tuple[int, int]]:
    """Atomically write bytes and return the committed file's device/inode identity."""

    if not isinstance(payload, bytes):
        raise TypeError("atomic file payload must be bytes")
    if _supports_anchored_path_io():
        return _atomic_write_anchored_bytes(
            path,
            payload,
            trusted_root=trusted_root,
            label=label,
            mode=mode,
        )
    target = atomic_write_unlinked_bytes(
        path,
        payload,
        label=label,
        mode=mode,
        trusted_root=trusted_root,
    )
    with open_unlinked_regular_file(target, label=label) as descriptor:
        opened = os.fstat(descriptor)
        return target, (opened.st_dev, opened.st_ino)


def _atomic_write_anchored_bytes(
    path: str | Path,
    payload: bytes,
    *,
    trusted_root: str | Path,
    label: str,
    mode: int,
) -> tuple[Path, tuple[int, int]]:
    with _open_anchored_parent(
        path,
        trusted_root=trusted_root,
        label=label,
    ) as (parent_descriptor, name, target):
        temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | _close_on_exec_flag()
            | _nofollow_flag()
        )
        descriptor = os.open(temporary_name, flags, mode, dir_fd=parent_descriptor)
        renamed = False
        try:
            _require_regular_descriptor(descriptor, target, label=label)
            if hasattr(os, "fchmod") and os.name != "nt":
                _fchmod_descriptor(descriptor, mode)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(f"short write while writing {label}")
                view = view[written:]
            os.fsync(descriptor)

            temporary_metadata = os.stat(
                temporary_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            opened = os.fstat(descriptor)
            if (
                stat.S_ISLNK(temporary_metadata.st_mode)
                or (temporary_metadata.st_dev, temporary_metadata.st_ino)
                != (opened.st_dev, opened.st_ino)
            ):
                raise ValueError(
                    f"refusing to replace {label} after temporary file changed: {target}"
                )
            try:
                destination_metadata = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                destination_metadata = None
            if destination_metadata is not None and not stat.S_ISREG(
                destination_metadata.st_mode
            ):
                raise ValueError(f"refusing to replace non-regular {label}: {target}")

            os.rename(
                temporary_name,
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            renamed = True
            try:
                os.fsync(parent_descriptor)
            except OSError:
                pass
            _verify_anchored_file_identity(
                target,
                descriptor,
                trusted_root=trusted_root,
                label=label,
            )
            return target, (opened.st_dev, opened.st_ino)
        finally:
            if not renamed:
                _unlink_same_directory_entry(
                    parent_descriptor,
                    temporary_name,
                    descriptor,
                )
            os.close(descriptor)


def ensure_anchored_directory(
    path: str | Path,
    *,
    trusted_root: str | Path,
    label: str,
    mode: int = 0o700,
) -> Path:
    """Create/open one directory tree below a trusted root without following links."""

    root, target, parts = _anchored_path_parts(
        path,
        trusted_root=trusted_root,
        label=label,
    )
    if not _supports_anchored_path_io():
        target.mkdir(parents=True, exist_ok=True)
        validate_unlinked_path(target, trusted_root=root, label=label)
        if os.name != "nt":
            target.chmod(mode)
        return target

    flags = os.O_RDONLY | _directory_open_flag() | _close_on_exec_flag() | _nofollow_flag()
    descriptor = os.open(root, flags)
    try:
        for component in parts:
            try:
                next_descriptor = _open_anchored_directory_component(
                    descriptor,
                    component,
                    flags=flags,
                    target=target,
                    label=label,
                )
            except FileNotFoundError:
                os.mkdir(component, mode, dir_fd=descriptor)
                next_descriptor = _open_anchored_directory_component(
                    descriptor,
                    component,
                    flags=flags,
                    target=target,
                    label=label,
                )
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError(f"refusing to use non-directory {label}: {target}")
        if hasattr(os, "fchmod") and os.name != "nt":
            _fchmod_descriptor(descriptor, mode)
        _verify_anchored_directory_identity(
            target,
            descriptor,
            trusted_root=root,
            label=label,
        )
        return target
    finally:
        os.close(descriptor)


def anchored_directory_exists(
    path: str | Path,
    *,
    trusted_root: str | Path,
    label: str,
) -> bool:
    """Return whether one directory exists below a trusted root without following links."""

    root, target, _ = _anchored_path_parts(
        path,
        trusted_root=trusted_root,
        label=label,
    )
    if not _supports_anchored_path_io():
        try:
            validate_unlinked_path(target, trusted_root=root, label=label)
        except ValueError:
            return False
        return target.is_dir() and not target.is_symlink()
    try:
        with _open_anchored_parent(
            target,
            trusted_root=root,
            label=label,
        ) as (parent_descriptor, name, _target):
            try:
                observed = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return False
            return stat.S_ISDIR(observed.st_mode) and not stat.S_ISLNK(observed.st_mode)
    except FileNotFoundError:
        return False


def anchored_regular_file_exists(
    path: str | Path,
    *,
    trusted_root: str | Path,
    label: str,
) -> bool:
    """Return whether one regular file exists below a trusted root without following links."""

    root, target, _ = _anchored_path_parts(
        path,
        trusted_root=trusted_root,
        label=label,
    )
    if not _supports_anchored_path_io():
        try:
            validate_unlinked_path(target, trusted_root=root, label=label)
        except ValueError:
            return False
        return target.is_file() and not target.is_symlink()
    try:
        with _open_anchored_parent(
            target,
            trusted_root=root,
            label=label,
        ) as (parent_descriptor, name, _target):
            try:
                observed = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return False
            return stat.S_ISREG(observed.st_mode) and not stat.S_ISLNK(observed.st_mode)
    except FileNotFoundError:
        return False


def list_anchored_directory(
    path: str | Path,
    *,
    trusted_root: str | Path,
    label: str,
    max_entries: int,
) -> list[tuple[str, bool]]:
    """List one directory through a held descriptor, returning name/directory pairs."""

    if max_entries < 0:
        raise ValueError("max_entries must be non-negative")
    root, target, _ = _anchored_path_parts(
        path,
        trusted_root=trusted_root,
        label=label,
    )
    if not _supports_anchored_path_io():
        validate_unlinked_path(target, trusted_root=root, label=label)
        entries: list[tuple[str, bool]] = []
        for index, entry in enumerate(os.scandir(target), 1):
            if index > max_entries:
                raise ValueError(f"{label} exceeds {max_entries} entries")
            entries.append((entry.name, entry.is_dir(follow_symlinks=False)))
        return entries
    with _open_anchored_parent(
        target,
        trusted_root=root,
        label=label,
    ) as (parent_descriptor, name, _target):
        flags = os.O_RDONLY | _directory_open_flag() | _close_on_exec_flag() | _nofollow_flag()
        directory_descriptor = _open_anchored_directory_component(
            parent_descriptor,
            name,
            flags=flags,
            target=target,
            label=label,
        )
        try:
            entries = []
            for index, entry_name in enumerate(os.listdir(directory_descriptor), 1):
                if index > max_entries:
                    raise ValueError(f"{label} exceeds {max_entries} entries")
                observed = os.stat(
                    entry_name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                entries.append((entry_name, stat.S_ISDIR(observed.st_mode)))
            return entries
        finally:
            os.close(directory_descriptor)


def create_anchored_directory(
    path: str | Path,
    *,
    trusted_root: str | Path,
    label: str,
    mode: int = 0o700,
) -> Path:
    """Exclusively create one directory below a trusted root without following links."""

    root, target, _ = _anchored_path_parts(
        path,
        trusted_root=trusted_root,
        label=label,
    )
    if not _supports_anchored_path_io():
        validate_unlinked_path(target.parent, trusted_root=root, label=label)
        target.mkdir(mode=mode)
        validate_unlinked_path(target, trusted_root=root, label=label)
        return target
    with _open_anchored_parent(
        target,
        trusted_root=root,
        label=label,
    ) as (parent_descriptor, name, _target):
        os.mkdir(name, mode, dir_fd=parent_descriptor)
        flags = os.O_RDONLY | _directory_open_flag() | _close_on_exec_flag() | _nofollow_flag()
        descriptor = _open_anchored_directory_component(
            parent_descriptor,
            name,
            flags=flags,
            target=target,
            label=label,
        )
        try:
            if hasattr(os, "fchmod") and os.name != "nt":
                _fchmod_descriptor(descriptor, mode)
            opened = os.fstat(descriptor)
            observed = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (observed.st_dev, observed.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError(f"refusing to use {label} that changed after creation: {target}")
            return target
        finally:
            os.close(descriptor)


def remove_anchored_directory_tree(
    path: str | Path,
    *,
    trusted_root: str | Path,
    label: str,
) -> None:
    """Recursively remove one directory through held descriptors without following links."""

    root, target, _ = _anchored_path_parts(
        path,
        trusted_root=trusted_root,
        label=label,
    )
    if not _supports_anchored_path_io() or os.rmdir not in getattr(os, "supports_dir_fd", ()):
        validate_unlinked_path(target, trusted_root=root, label=label)
        import shutil

        shutil.rmtree(target)
        return
    with _open_anchored_parent(
        target,
        trusted_root=root,
        label=label,
    ) as (parent_descriptor, name, _target):
        flags = os.O_RDONLY | _directory_open_flag() | _close_on_exec_flag() | _nofollow_flag()
        descriptor = _open_anchored_directory_component(
            parent_descriptor,
            name,
            flags=flags,
            target=target,
            label=label,
        )
        try:
            _remove_directory_contents(descriptor, target=target, label=label)
            opened = os.fstat(descriptor)
            observed = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (observed.st_dev, observed.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError(f"refusing to remove {label} that changed during deletion: {target}")
            os.rmdir(name, dir_fd=parent_descriptor)
            try:
                os.fsync(parent_descriptor)
            except OSError:
                pass
        finally:
            os.close(descriptor)


def remove_anchored_path(
    path: str | Path,
    *,
    trusted_root: str | Path,
    label: str,
) -> bool:
    """Remove one file, symlink, or directory below a trusted root without following links."""

    root, target, _ = _anchored_path_parts(
        path,
        trusted_root=trusted_root,
        label=label,
    )
    if not _supports_anchored_path_io():
        validate_unlinked_path(target.parent, trusted_root=root, label=label)
        try:
            observed = os.lstat(target)
        except FileNotFoundError:
            return False
        if stat.S_ISDIR(observed.st_mode) and not stat.S_ISLNK(observed.st_mode):
            import shutil

            shutil.rmtree(target)
        else:
            target.unlink()
        return True

    try:
        with _open_anchored_parent(
            target,
            trusted_root=root,
            label=label,
        ) as (parent_descriptor, name, _target):
            try:
                observed = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return False
            if stat.S_ISDIR(observed.st_mode) and not stat.S_ISLNK(observed.st_mode):
                remove_anchored_directory_tree(
                    target,
                    trusted_root=root,
                    label=label,
                )
                return True
            os.unlink(name, dir_fd=parent_descriptor)
            try:
                os.fsync(parent_descriptor)
            except OSError:
                pass
            return True
    except FileNotFoundError:
        return False


def _remove_directory_contents(descriptor: int, *, target: Path, label: str) -> None:
    flags = os.O_RDONLY | _directory_open_flag() | _close_on_exec_flag() | _nofollow_flag()
    for entry_name in os.listdir(descriptor):
        observed = os.stat(entry_name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(observed.st_mode):
            child_descriptor = _open_anchored_directory_component(
                descriptor,
                entry_name,
                flags=flags,
                target=target / entry_name,
                label=label,
            )
            try:
                _remove_directory_contents(
                    child_descriptor,
                    target=target / entry_name,
                    label=label,
                )
                opened = os.fstat(child_descriptor)
                current = os.stat(
                    entry_name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError(
                        f"refusing to remove {label} entry that changed during deletion: "
                        f"{target / entry_name}"
                    )
                os.rmdir(entry_name, dir_fd=descriptor)
            finally:
                os.close(child_descriptor)
            continue
        os.unlink(entry_name, dir_fd=descriptor)


def unlink_open_file(
    path: str | Path,
    descriptor: int,
    *,
    label: str,
) -> None:
    """Unlink a visible path only when it still names ``descriptor``."""

    target = validate_unlinked_file_path(path, label=label)
    _require_regular_descriptor(descriptor, target, label=label)
    parent_descriptor = -1
    try:
        parent_descriptor = _open_parent_directory(target)
        opened = os.fstat(descriptor)
        if parent_descriptor >= 0:
            observed = os.stat(
                target.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(observed.st_mode) or (
                observed.st_dev,
                observed.st_ino,
            ) != (opened.st_dev, opened.st_ino):
                raise ValueError(
                    f"refusing to unlink {label} that changed after opening: {target}"
                )
            os.unlink(target.name, dir_fd=parent_descriptor)
            try:
                os.fsync(parent_descriptor)
            except OSError:
                pass
            return
        _verify_path_identity(target, descriptor, label=label)
        target.unlink()
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def chmod_unlinked_directory(
    path: str | Path,
    mode: int,
    *,
    label: str,
) -> Path:
    """Apply permissions to one directory without following a replaced leaf link."""

    target = validate_unlinked_directory_path(path, label=label)
    if os.name == "nt":
        return target
    flags = os.O_RDONLY | _close_on_exec_flag()
    if hasattr(os, "O_DIRECTORY"):
        flags |= _directory_open_flag()
    flags |= _nofollow_flag()
    descriptor = os.open(target, flags)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError(f"refusing to chmod non-directory {label}: {target}")
        _fchmod_descriptor(descriptor, mode)
    finally:
        os.close(descriptor)
    return target


def _supports_anchored_path_io() -> bool:
    supports_dir_fd = getattr(os, "supports_dir_fd", ())
    return bool(
        os.name == "posix"
        and _directory_open_flag() != 0
        and hasattr(os, "O_NOFOLLOW")
        and os.open in supports_dir_fd
        and os.mkdir in supports_dir_fd
        and os.rename in supports_dir_fd
        and os.stat in supports_dir_fd
        and os.unlink in supports_dir_fd
    )


def _anchored_path_parts(
    path: str | Path,
    *,
    trusted_root: str | Path,
    label: str,
) -> tuple[Path, Path, tuple[str, ...]]:
    root = Path(os.path.abspath(Path(trusted_root).expanduser()))
    target = lexical_target_path(path, root)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"refusing to use {label} outside trusted root: {target}") from exc
    if not relative.parts:
        raise ValueError(f"refusing to use trusted root itself as {label}: {target}")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"refusing to use invalid {label} path: {target}")
    return root, target, tuple(relative.parts)


@contextmanager
def _open_anchored_parent(
    path: str | Path,
    *,
    trusted_root: str | Path,
    label: str,
) -> Iterator[tuple[int, str, Path]]:
    root, target, parts = _anchored_path_parts(
        path,
        trusted_root=trusted_root,
        label=label,
    )
    flags = os.O_RDONLY | _directory_open_flag() | _close_on_exec_flag() | _nofollow_flag()
    descriptor = os.open(root, flags)
    try:
        for component in parts[:-1]:
            next_descriptor = _open_anchored_directory_component(
                descriptor,
                component,
                flags=flags,
                target=target,
                label=label,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor, parts[-1], target
    finally:
        os.close(descriptor)


def _verify_anchored_file_identity(
    path: Path,
    descriptor: int,
    *,
    trusted_root: str | Path,
    label: str,
) -> None:
    with _open_anchored_parent(
        path,
        trusted_root=trusted_root,
        label=label,
    ) as (parent_descriptor, name, target):
        observed = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if stat.S_ISLNK(observed.st_mode) or (
            observed.st_dev,
            observed.st_ino,
        ) != (opened.st_dev, opened.st_ino):
            raise ValueError(
                f"refusing to use {label} that changed after writing: {target}"
            )


def _verify_anchored_directory_identity(
    path: Path,
    descriptor: int,
    *,
    trusted_root: str | Path,
    label: str,
) -> None:
    root, target, parts = _anchored_path_parts(
        path,
        trusted_root=trusted_root,
        label=label,
    )
    flags = os.O_RDONLY | _directory_open_flag() | _close_on_exec_flag() | _nofollow_flag()
    visible_descriptor = os.open(root, flags)
    try:
        for component in parts:
            next_descriptor = _open_anchored_directory_component(
                visible_descriptor,
                component,
                flags=flags,
                target=target,
                label=label,
            )
            os.close(visible_descriptor)
            visible_descriptor = next_descriptor
        visible = os.fstat(visible_descriptor)
        opened = os.fstat(descriptor)
        if (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(
                f"refusing to use {label} that changed after opening: {target}"
            )
    finally:
        os.close(visible_descriptor)


def _unlink_same_directory_entry(
    parent_descriptor: int,
    name: str,
    descriptor: int,
) -> None:
    try:
        observed = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if (observed.st_dev, observed.st_ino) == (opened.st_dev, opened.st_ino):
            os.unlink(name, dir_fd=parent_descriptor)
    except FileNotFoundError:
        pass


def _open_anchored_directory_component(
    parent_descriptor: int,
    component: str,
    *,
    flags: int,
    target: Path,
    label: str,
) -> int:
    try:
        return os.open(component, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        try:
            metadata = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            raise exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(
                f"refusing to use {label} through a symlink or junction: {target}"
            ) from exc
        raise


def _open_parent_directory(target: Path) -> int:
    supports_dir_fd = getattr(os, "supports_dir_fd", ())
    if (
        os.open not in supports_dir_fd
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
    ):
        return -1
    flags = os.O_RDONLY | _directory_open_flag() | _close_on_exec_flag() | _nofollow_flag()
    return os.open(target.parent, flags)


def _close_on_exec_flag() -> int:
    return getattr(os, "O_CLOEXEC", 0)


def _nofollow_flag() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _require_regular_descriptor(descriptor: int, path: Path, *, label: str) -> None:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise ValueError(f"refusing to use non-regular {label}: {path}")


def _verify_path_identity(path: Path, descriptor: int, *, label: str) -> None:
    observed = os.lstat(path)
    if stat.S_ISLNK(observed.st_mode):
        raise ValueError(f"refusing to use symlinked {label}: {path}")
    opened = os.fstat(descriptor)
    if (observed.st_dev, observed.st_ino) != (opened.st_dev, opened.st_ino):
        raise ValueError(f"refusing to use {label} that changed while opening: {path}")


def _unlink_same_open_file(
    path: Path,
    descriptor: int,
    *,
    parent_descriptor: int,
) -> None:
    try:
        opened = os.fstat(descriptor)
        if parent_descriptor >= 0:
            observed = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (observed.st_dev, observed.st_ino) == (opened.st_dev, opened.st_ino):
                os.unlink(path.name, dir_fd=parent_descriptor)
            return
        observed = os.lstat(path)
        if (observed.st_dev, observed.st_ino) == (opened.st_dev, opened.st_ino):
            path.unlink()
    except FileNotFoundError:
        pass


def read_bounded_bytes(
    path: str | Path,
    max_bytes: int,
    *,
    label: str,
    trusted_root: str | Path | None = None,
) -> bytes:
    """Read one regular file without following links or exceeding ``max_bytes``."""

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    source = Path(path).expanduser()
    if source.is_symlink():
        raise ValueError(f"refusing to read symlinked {label}: {source}")
    if trusted_root is not None:
        root = Path(trusted_root).expanduser().resolve()
        lexical = lexical_target_path(source, root)
        try:
            lexical.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"refusing to read {label} outside trusted root: {source}") from exc
        link = path_has_link_component(lexical, root)
        if link is not None:
            raise ValueError(
                f"refusing to read {label} through a symlink or junction: {link}"
            )
        source = lexical

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK

    descriptor = -1
    try:
        descriptor = os.open(source, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"refusing to read non-regular {label}: {source}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            contents = handle.read(max_bytes + 1)
    finally:
        if descriptor != -1:
            os.close(descriptor)

    if len(contents) > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes: {source}")
    return contents


def read_bounded_text(stream: TextIO, max_bytes: int, *, label: str) -> str:
    """Read UTF-8 text from a stream without retaining more than its byte limit."""

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    binary_stream: Any | None = getattr(stream, "buffer", None)
    if binary_stream is not None:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = binary_stream.read(max_bytes + 1 - total)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise ValueError(f"{label} stream returned non-bytes data")
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"{label} exceeds {max_bytes} bytes")
        raw = b"".join(chunks)
    else:
        chunks_text: list[str] = []
        total = 0
        while True:
            chunk = stream.read(max_bytes + 1 - total)
            if not chunk:
                break
            if not isinstance(chunk, str):
                raise ValueError(f"{label} stream returned non-text data")
            chunks_text.append(chunk)
            total += len(chunk.encode("utf-8"))
            if total > max_bytes:
                raise ValueError(f"{label} exceeds {max_bytes} bytes")
        return "".join(chunks_text)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8") from exc
