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
        else:
            descriptor = os.open(target, flags, mode)
            _verify_path_identity(target, descriptor, label=label)
        _require_regular_descriptor(descriptor, target, label=label)
        if hasattr(os, "fchmod") and os.name != "nt":
            os.fchmod(descriptor, mode)
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
) -> bytes:
    """Read one bounded regular file through a held no-follow descriptor."""

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    with open_unlinked_regular_file(path, label=label) as descriptor:
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
) -> Path:
    """Atomically replace one file without following a mutable parent or leaf link."""

    if not isinstance(payload, bytes):
        raise TypeError("atomic file payload must be bytes")
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
        flags |= os.O_DIRECTORY
    flags |= _nofollow_flag()
    descriptor = os.open(target, flags)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError(f"refusing to chmod non-directory {label}: {target}")
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)
    return target


def _open_parent_directory(target: Path) -> int:
    supports_dir_fd = getattr(os, "supports_dir_fd", ())
    if (
        os.open not in supports_dir_fd
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
    ):
        return -1
    flags = os.O_RDONLY | os.O_DIRECTORY | _close_on_exec_flag() | _nofollow_flag()
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
