"""Private, no-follow persistent history for interactive prompt input."""

from __future__ import annotations

import datetime
import os
import stat
from contextlib import contextmanager
from collections.abc import Iterable
from pathlib import Path

from prompt_toolkit.history import FileHistory

from ash.core.redaction import redact_value
from ash.safety.anchored_fs import AnchoredDirectory, AnchoredFilesystemError


_FCHMOD = getattr(os, "fchmod", None)
MAX_HISTORY_FILE_BYTES = 2 * 1024 * 1024
MAX_HISTORY_ENTRY_BYTES = 512 * 1024
_HISTORY_TRUNCATION_MARKER = "\n[history entry truncated]"

try:
    import fcntl
except ImportError:  # pragma: no cover - native Windows is not a supported host.
    fcntl = None  # type: ignore[assignment]


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def validate_history_path(path: Path) -> None:
    """Reject history files or immediate state directories that redirect via links."""

    if _is_link(path) or _is_link(path.parent):
        raise ValueError(f"refusing to use symlinked prompt history path: {path}")


def _bounded_history_text(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_HISTORY_ENTRY_BYTES:
        return value
    marker = _HISTORY_TRUNCATION_MARKER.encode("utf-8")
    budget = max(0, MAX_HISTORY_ENTRY_BYTES - len(marker))
    prefix = encoded[:budget].decode("utf-8", errors="ignore")
    return prefix + _HISTORY_TRUNCATION_MARKER


def _history_entry_bytes(value: str) -> bytes:
    chunks = [f"\n# {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n"]
    chunks.extend(f"+{line}\n" for line in value.split("\n"))
    return "".join(chunks).encode("utf-8")


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write while writing prompt history")
        view = view[written:]


def _aligned_history_tail(payload: bytes, budget: int) -> bytes:
    if budget <= 0 or not payload:
        return b""
    if len(payload) <= budget:
        return payload
    start = len(payload) - budget
    boundary = payload.find(b"\n# ", start)
    return b"" if boundary < 0 else payload[boundary:]


@contextmanager
def _history_file_lock(descriptor: int):
    if fcntl is None:
        yield
        return
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


class PrivateFileHistory(FileHistory):
    """Prompt-toolkit history with no-follow reads/writes and private POSIX mode."""

    def __init__(self, filename: Path) -> None:
        self._path = filename
        validate_history_path(filename)
        super().__init__(str(filename))
        try:
            descriptor = self._open_fd(os.O_RDONLY)
        except FileNotFoundError:
            return
        else:
            os.close(descriptor)

    def _open_fd(self, flags: int, mode: int = 0o600) -> int:
        validate_history_path(self._path)
        fchmod = _FCHMOD
        if os.name != "nt" and fchmod is None:
            raise OSError("private prompt-history permissions are unavailable")
        create = bool(flags & os.O_CREAT)
        open_flags = flags & ~os.O_CREAT
        descriptor: int | None = None
        try:
            with AnchoredDirectory.open(
                self._path.parent,
                create=create,
                private=False,
                pin_path=True,
            ) as directory:
                directory.validation_path()
                existing = directory.stat(self._path.name)
                if existing is None:
                    if not create:
                        raise FileNotFoundError(self._path)
                    descriptor = directory.create_file(self._path.name, mode=mode)
                else:
                    descriptor = directory.open_file(
                        self._path.name,
                        open_flags,
                        mode,
                        expected=existing,
                        expected_type=stat.S_IFREG,
                    )
                directory.validation_path()
        except AnchoredFilesystemError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise ValueError(
                f"refusing to use redirected prompt history path: {self._path}"
            ) from exc
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise
        assert descriptor is not None
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise ValueError(f"prompt history path is not a regular file: {self._path}")
        if os.name != "nt":
            assert fchmod is not None
            fchmod(descriptor, 0o600)
        return descriptor

    def load_history_strings(self) -> Iterable[str]:
        strings: list[str] = []
        lines: list[str] = []

        def add() -> None:
            if lines:
                strings.append("".join(lines)[:-1])

        try:
            descriptor = self._open_fd(os.O_RDONLY)
        except FileNotFoundError:
            return ()
        with os.fdopen(descriptor, "rb") as handle:
            for line_bytes in handle:
                line = line_bytes.decode("utf-8", errors="replace")
                if line.startswith("+"):
                    lines.append(line[1:])
                else:
                    add()
                    lines = []
            add()
        return reversed(strings)

    def store_string(self, string: str) -> None:
        redacted = redact_value(string)
        if not isinstance(redacted, str):
            raise ValueError("redacted prompt history entry is not text")
        entry = _history_entry_bytes(_bounded_history_text(redacted))
        descriptor = self._open_fd(os.O_RDWR | os.O_APPEND | os.O_CREAT)
        try:
            with _history_file_lock(descriptor):
                size = os.fstat(descriptor).st_size
                if size + len(entry) <= MAX_HISTORY_FILE_BYTES:
                    _write_all(descriptor, entry)
                    return

                # Legacy history may already exceed the new cap. Read only a
                # bounded tail, align it to a complete prompt-toolkit entry,
                # then retain as much recent history as fits beside the new
                # redacted entry.
                read_size = min(size, MAX_HISTORY_FILE_BYTES)
                os.lseek(descriptor, size - read_size, os.SEEK_SET)
                existing = os.read(descriptor, read_size)
                if size > read_size:
                    first_boundary = existing.find(b"\n# ")
                    existing = (
                        b"" if first_boundary < 0 else existing[first_boundary:]
                    )
                retained = _aligned_history_tail(
                    existing,
                    MAX_HISTORY_FILE_BYTES - len(entry),
                )
                os.ftruncate(descriptor, 0)
                os.lseek(descriptor, 0, os.SEEK_SET)
                _write_all(descriptor, retained + entry)
        finally:
            os.close(descriptor)
