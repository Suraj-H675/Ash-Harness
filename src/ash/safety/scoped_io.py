"""Race-resistant workspace file I/O primitives."""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ash.safety.guard import SafetyGuard, SafetyViolation


class ScopedIOError(OSError):
    """A scoped filesystem operation could not be completed safely."""


class ScopedFileChanged(ScopedIOError):
    """The destination changed between validation and mutation."""


@dataclass(frozen=True)
class ScopedFileSnapshot:
    """A bounded, safely opened snapshot of one workspace file."""

    exists: bool
    content: bytes
    mode: int | None
    sha256: str


def read_scoped_bytes(
    path: str | Path,
    guard: SafetyGuard,
    *,
    max_bytes: int | None = None,
) -> tuple[Path, bytes]:
    """Read a regular file without following any workspace link component."""

    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes cannot be negative")
    target = guard.validate_mutation_path(path)
    if _supports_anchored_io():
        with _open_parent(target, guard, create=False) as (parent_fd, name):
            flags = os.O_RDONLY | _flag("O_CLOEXEC") | _flag("O_NOFOLLOW")
            try:
                fd = os.open(name, flags, dir_fd=parent_fd)
            except OSError as exc:
                raise _scoped_open_error(target, exc) from exc
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ScopedIOError(f"not a regular file: {target}")
                return target, _read_all(fd, max_bytes=max_bytes)
            finally:
                os.close(fd)
    return target, _fallback_read(target, guard, max_bytes=max_bytes)


def snapshot_scoped_file(
    path: str | Path,
    guard: SafetyGuard,
    *,
    max_bytes: int | None = None,
) -> tuple[Path, ScopedFileSnapshot]:
    """Capture one regular file without following a changed path component."""

    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes cannot be negative")
    target = guard.validate_mutation_path(path)
    if _supports_anchored_io():
        try:
            with _open_parent(target, guard, create=False) as (parent_fd, name):
                existing = _stat_entry(parent_fd, name)
                if existing is None:
                    return target, _missing_snapshot()
                if not stat.S_ISREG(existing.st_mode):
                    raise ScopedIOError(f"not a regular file: {target}")
                flags = os.O_RDONLY | _flag("O_CLOEXEC") | _flag("O_NOFOLLOW")
                try:
                    fd = os.open(name, flags, dir_fd=parent_fd)
                except FileNotFoundError:
                    raise ScopedFileChanged(f"file disappeared while opening: {target}")
                except OSError as exc:
                    raise _scoped_open_error(target, exc) from exc
                try:
                    opened = os.fstat(fd)
                    if not _same_file(existing, opened):
                        raise ScopedFileChanged(f"file changed while opening: {target}")
                    content = _read_all(fd, max_bytes=max_bytes)
                    finished = os.fstat(fd)
                    if not _same_snapshot(opened, finished):
                        raise ScopedFileChanged(f"file changed while reading: {target}")
                    return target, ScopedFileSnapshot(
                        exists=True,
                        content=content,
                        mode=finished.st_mode,
                        sha256=hashlib.sha256(content).hexdigest(),
                    )
                finally:
                    os.close(fd)
        except FileNotFoundError:
            return target, _missing_snapshot()
    return target, _fallback_snapshot(target, guard, max_bytes=max_bytes)


def list_scoped_directory(
    path: str | Path,
    guard: SafetyGuard,
) -> tuple[Path, list[tuple[str, bool]]]:
    """List names and directory flags without following workspace links."""

    target = guard.validate_mutation_path(path)
    if _supports_anchored_io():
        if target == guard.project_root:
            flags = (
                os.O_RDONLY
                | _flag("O_DIRECTORY")
                | _flag("O_CLOEXEC")
                | _flag("O_NOFOLLOW")
            )
            directory_fd = os.open(guard.project_root, flags)
        else:
            with _open_parent(target, guard, create=False) as (parent_fd, name):
                flags = (
                    os.O_RDONLY
                    | _flag("O_DIRECTORY")
                    | _flag("O_CLOEXEC")
                    | _flag("O_NOFOLLOW")
                )
                try:
                    directory_fd = os.open(name, flags, dir_fd=parent_fd)
                except OSError as exc:
                    raise _scoped_open_error(target, exc) from exc
        try:
            entries: list[tuple[str, bool]] = []
            for name in os.listdir(directory_fd):
                metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                entries.append((name, stat.S_ISDIR(metadata.st_mode)))
            return target, entries
        finally:
            os.close(directory_fd)

    before = _fallback_identity(target)
    assert before is not None
    if not stat.S_ISDIR(before.st_mode):
        raise ScopedIOError(f"not a directory: {target}")
    entries = [
        (entry.name, entry.is_dir(follow_symlinks=False))
        for entry in os.scandir(target)
    ]
    guard.validate_mutation_path(target)
    after = _fallback_identity(target)
    assert after is not None
    if not _same_snapshot(before, after):
        raise ScopedFileChanged(f"directory changed while listing: {target}")
    return target, entries


def atomic_write_scoped_text(
    path: str | Path,
    content: str,
    guard: SafetyGuard,
    *,
    overwrite: bool,
    expected_sha256: str | None = None,
) -> Path:
    """Atomically write UTF-8 text while retaining a workspace directory anchor."""

    return atomic_write_scoped_bytes(
        path,
        content.encode("utf-8"),
        guard,
        overwrite=overwrite,
        expected_sha256=expected_sha256,
    )


def atomic_write_scoped_bytes(
    path: str | Path,
    payload: bytes,
    guard: SafetyGuard,
    *,
    overwrite: bool,
    expected_sha256: str | None = None,
) -> Path:
    """Atomically write bytes while retaining a workspace directory anchor."""

    target = guard.validate_mutation_path(path)
    if _supports_anchored_io():
        _anchored_atomic_write(
            target,
            payload,
            guard,
            overwrite=overwrite,
            expected_sha256=expected_sha256,
        )
    else:
        _fallback_atomic_write(
            target,
            payload,
            guard,
            overwrite=overwrite,
            expected_sha256=expected_sha256,
        )
    return target


def restore_scoped_file(
    path: str | Path,
    payload: bytes,
    guard: SafetyGuard,
    *,
    expected_sha256: str,
    mode: int | None,
    max_bytes: int | None = None,
) -> Path:
    """Restore a file with an anchored expected-state check and staged mode."""

    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes cannot be negative")
    target = guard.validate_mutation_path(path)
    if _supports_anchored_io():
        _anchored_restore(
            target,
            payload,
            guard,
            expected_sha256=expected_sha256,
            mode=mode,
            max_bytes=max_bytes,
        )
    else:
        _fallback_restore(
            target,
            payload,
            guard,
            expected_sha256=expected_sha256,
            mode=mode,
            max_bytes=max_bytes,
        )
    return target


def remove_scoped_file(
    path: str | Path,
    guard: SafetyGuard,
    *,
    expected_sha256: str,
    max_bytes: int | None = None,
) -> Path:
    """Remove a file only after an anchored expected-state check."""

    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes cannot be negative")
    target = guard.validate_mutation_path(path)
    if _supports_anchored_io():
        _anchored_remove(
            target,
            guard,
            expected_sha256=expected_sha256,
            max_bytes=max_bytes,
        )
    else:
        _fallback_remove(
            target,
            guard,
            expected_sha256=expected_sha256,
            max_bytes=max_bytes,
        )
    return target


def _supports_anchored_io() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.rename in os.supports_dir_fd
    )


@contextmanager
def _open_parent(
    target: Path,
    guard: SafetyGuard,
    *,
    create: bool,
) -> Iterator[tuple[int, str]]:
    """Walk to a parent directory from an open project-root descriptor."""

    target = guard.validate_mutation_path(target)
    try:
        relative_parent = target.parent.relative_to(guard.project_root)
    except ValueError as exc:
        raise SafetyViolation(f"path is outside project scope: {target}") from exc
    flags = (
        os.O_RDONLY | _flag("O_DIRECTORY") | _flag("O_CLOEXEC") | _flag("O_NOFOLLOW")
    )
    current_fd = os.open(guard.project_root, flags)
    try:
        for component in relative_parent.parts:
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o777, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as exc:
                raise _scoped_open_error(target.parent, exc) from exc
            os.close(current_fd)
            current_fd = next_fd
        yield current_fd, target.name
    finally:
        os.close(current_fd)


def _anchored_atomic_write(
    target: Path,
    payload: bytes,
    guard: SafetyGuard,
    *,
    overwrite: bool,
    expected_sha256: str | None,
) -> None:
    with _open_parent(target, guard, create=True) as (parent_fd, name):
        existing = _stat_entry(parent_fd, name)
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ScopedIOError(f"destination is not a regular file: {target}")
        if expected_sha256 is not None:
            actual = _hash_entry(parent_fd, name, target)
            if actual != expected_sha256.casefold():
                raise ScopedFileChanged(
                    f"file changed before write: expected {expected_sha256}, got {actual}"
                )
        temp_name = f".{name}.{secrets.token_hex(8)}.tmp"
        temp_fd = -1
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | _flag("O_CLOEXEC")
                | _flag("O_NOFOLLOW")
            )
            temp_fd = os.open(temp_name, flags, 0o666, dir_fd=parent_fd)
            if existing is not None and hasattr(os, "fchmod"):
                os.fchmod(temp_fd, stat.S_IMODE(existing.st_mode) & 0o777)
            _write_all(temp_fd, payload)
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = -1
            if expected_sha256 is not None:
                current = _stat_entry(parent_fd, name)
                if not _same_optional_snapshot(existing, current):
                    raise ScopedFileChanged(
                        f"destination changed before replace: {target}"
                    )
            if overwrite:
                os.rename(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            else:
                try:
                    os.link(
                        temp_name,
                        name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    raise
                os.unlink(temp_name, dir_fd=parent_fd)
                temp_name = ""
            os.fsync(parent_fd)
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            if temp_name:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass


def _anchored_restore(
    target: Path,
    payload: bytes,
    guard: SafetyGuard,
    *,
    expected_sha256: str,
    mode: int | None,
    max_bytes: int | None,
) -> None:
    with _open_parent(target, guard, create=True) as (parent_fd, name):
        existing = _stat_entry(parent_fd, name)
        _require_expected(
            _entry_digest(parent_fd, name, target, max_bytes=max_bytes),
            expected_sha256,
            target,
        )
        temp_name = f".{name}.{secrets.token_hex(8)}.tmp"
        temp_fd = -1
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | _flag("O_CLOEXEC")
                | _flag("O_NOFOLLOW")
            )
            temp_fd = os.open(temp_name, flags, 0o666, dir_fd=parent_fd)
            effective_mode = mode
            if effective_mode is None and existing is not None:
                effective_mode = existing.st_mode
            _set_staged_mode(
                temp_fd,
                target.parent / temp_name,
                effective_mode,
            )
            _write_all(temp_fd, payload)
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = -1
            _require_expected(
                _entry_digest(parent_fd, name, target, max_bytes=max_bytes),
                expected_sha256,
                target,
            )
            os.rename(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            if temp_name:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass


def _anchored_remove(
    target: Path,
    guard: SafetyGuard,
    *,
    expected_sha256: str,
    max_bytes: int | None,
) -> None:
    try:
        with _open_parent(target, guard, create=False) as (parent_fd, name):
            _require_expected(
                _entry_digest(parent_fd, name, target, max_bytes=max_bytes),
                expected_sha256,
                target,
            )
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
    except FileNotFoundError:
        if expected_sha256.casefold() != "missing":
            raise ScopedFileChanged(f"file disappeared before removal: {target}")


def _entry_digest(
    parent_fd: int,
    name: str,
    target: Path,
    *,
    max_bytes: int | None,
) -> str:
    existing = _stat_entry(parent_fd, name)
    if existing is None:
        return "missing"
    return _hash_entry(parent_fd, name, target, max_bytes=max_bytes)


def _require_expected(actual: str, expected: str, target: Path) -> None:
    if actual != expected.casefold():
        raise ScopedFileChanged(
            f"file changed before checkpoint restore: expected {expected}, got {actual}: "
            f"{target}"
        )


def _fallback_read(
    target: Path, guard: SafetyGuard, *, max_bytes: int | None = None
) -> bytes:
    before = _fallback_identity(target)
    assert before is not None
    with target.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or not _same_file(before, opened):
            raise ScopedFileChanged(f"file changed while opening: {target}")
        data = handle.read() if max_bytes is None else handle.read(max_bytes + 1)
        if max_bytes is not None and len(data) > max_bytes:
            raise ScopedIOError(f"file exceeds {max_bytes} bytes: {target}")
    guard.validate_mutation_path(target)
    after = _fallback_identity(target)
    assert after is not None
    if not _same_snapshot(opened, after):
        raise ScopedFileChanged(f"file changed while reading: {target}")
    return data


def _missing_snapshot() -> ScopedFileSnapshot:
    return ScopedFileSnapshot(exists=False, content=b"", mode=None, sha256="missing")


def _fallback_snapshot(
    target: Path,
    guard: SafetyGuard,
    *,
    max_bytes: int | None,
) -> ScopedFileSnapshot:
    existing = _fallback_identity(target, missing_ok=True)
    if existing is None:
        return _missing_snapshot()
    if not stat.S_ISREG(existing.st_mode):
        raise ScopedIOError(f"not a regular file: {target}")
    content = _fallback_read(target, guard, max_bytes=max_bytes)
    return ScopedFileSnapshot(
        exists=True,
        content=content,
        mode=existing.st_mode,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _fallback_atomic_write(
    target: Path,
    payload: bytes,
    guard: SafetyGuard,
    *,
    overwrite: bool,
    expected_sha256: str | None,
) -> None:
    guard.validate_mutation_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = _fallback_identity(target, missing_ok=True)
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise ScopedIOError(f"destination is not a regular file: {target}")
    if expected_sha256 is not None:
        if existing is None:
            raise ScopedFileChanged(f"file disappeared before write: {target}")
        actual = hashlib.sha256(_fallback_read(target, guard)).hexdigest()
        if actual != expected_sha256.casefold():
            raise ScopedFileChanged(
                f"file changed before write: expected {expected_sha256}, got {actual}"
            )
    temp = target.parent / f".{target.name}.{secrets.token_hex(8)}.tmp"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        try:
            if existing is not None and hasattr(os, "fchmod"):
                os.fchmod(fd, stat.S_IMODE(existing.st_mode) & 0o777)
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        guard.validate_mutation_path(target)
        current = _fallback_identity(target, missing_ok=True)
        if not _same_optional_snapshot(existing, current):
            raise ScopedFileChanged(f"destination changed before replace: {target}")
        if overwrite:
            os.replace(temp, target)
        else:
            os.link(temp, target, follow_symlinks=False)
            temp.unlink()
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _fallback_restore(
    target: Path,
    payload: bytes,
    guard: SafetyGuard,
    *,
    expected_sha256: str,
    mode: int | None,
    max_bytes: int | None,
) -> None:
    guard.validate_mutation_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = _fallback_identity(target, missing_ok=True)
    _require_expected(
        _fallback_entry_digest(target, guard, max_bytes=max_bytes),
        expected_sha256,
        target,
    )
    temp = target.parent / f".{target.name}.{secrets.token_hex(8)}.tmp"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            effective_mode = mode
            if effective_mode is None and existing is not None:
                effective_mode = existing.st_mode
            _set_staged_mode(fd, temp, effective_mode)
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        guard.validate_mutation_path(target)
        _require_expected(
            _fallback_entry_digest(target, guard, max_bytes=max_bytes),
            expected_sha256,
            target,
        )
        os.replace(temp, target)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _fallback_remove(
    target: Path,
    guard: SafetyGuard,
    *,
    expected_sha256: str,
    max_bytes: int | None,
) -> None:
    guard.validate_mutation_path(target)
    _require_expected(
        _fallback_entry_digest(target, guard, max_bytes=max_bytes),
        expected_sha256,
        target,
    )
    if _fallback_identity(target, missing_ok=True) is not None:
        target.unlink()
    guard.validate_mutation_path(target)


def _fallback_entry_digest(
    target: Path,
    guard: SafetyGuard,
    *,
    max_bytes: int | None,
) -> str:
    existing = _fallback_identity(target, missing_ok=True)
    if existing is None:
        return "missing"
    if not stat.S_ISREG(existing.st_mode):
        raise ScopedIOError(f"destination is not a regular file: {target}")
    return hashlib.sha256(
        _fallback_read(target, guard, max_bytes=max_bytes)
    ).hexdigest()


def _stat_entry(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        result = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(result.st_mode):
        raise ScopedIOError(f"destination is a symlink: {name}")
    return result


def _hash_entry(
    parent_fd: int,
    name: str,
    target: Path,
    *,
    max_bytes: int | None = None,
) -> str:
    flags = os.O_RDONLY | _flag("O_CLOEXEC") | _flag("O_NOFOLLOW")
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise _scoped_open_error(target, exc) from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ScopedIOError(f"not a regular file: {target}")
        return hashlib.sha256(_read_all(fd, max_bytes=max_bytes)).hexdigest()
    finally:
        os.close(fd)


def _read_all(fd: int, *, max_bytes: int | None = None) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := os.read(
        fd,
        min(1024 * 1024, max_bytes + 1 - total)
        if max_bytes is not None
        else 1024 * 1024,
    ):
        chunks.append(chunk)
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise ScopedIOError(f"file exceeds {max_bytes} bytes")
    return b"".join(chunks)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise ScopedIOError("short write")
        view = view[written:]


def _set_staged_mode(fd: int, path: Path, mode: int | None) -> None:
    if mode is None:
        return
    normalized = stat.S_IMODE(mode)
    if hasattr(os, "fchmod"):
        os.fchmod(fd, normalized)
    else:
        os.chmod(path, normalized)


def _fallback_identity(
    path: Path, *, missing_ok: bool = False
) -> os.stat_result | None:
    try:
        result = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    if stat.S_ISLNK(result.st_mode):
        raise ScopedIOError(f"path is a symlink: {path}")
    return result


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _same_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return _same_file(left, right) and (
        left.st_size,
        left.st_mtime_ns,
    ) == (
        right.st_size,
        right.st_mtime_ns,
    )


def _same_optional_snapshot(
    left: os.stat_result | None,
    right: os.stat_result | None,
) -> bool:
    if left is None or right is None:
        return left is right
    return _same_snapshot(left, right)


def _flag(name: str) -> int:
    return int(getattr(os, name, 0))


def _scoped_open_error(path: Path, error: OSError) -> ScopedIOError:
    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
        return ScopedIOError(f"path contains a symlink or non-directory: {path}")
    return ScopedIOError(f"cannot safely open {path}: {error}")
