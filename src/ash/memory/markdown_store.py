"""Plain Markdown file memory store."""

from __future__ import annotations

import os
from pathlib import Path

from ash.safe_io import read_bounded_bytes


MAX_MEMORY_CONTENT_BYTES = 8 * 1024 * 1024
MAX_MEMORY_KEYS = 10_000
MAX_MEMORY_KEY_CHARS = 128


class MarkdownMemoryStore:
    def __init__(self, memory_dir: Path) -> None:
        memory_dir = memory_dir.expanduser()
        if memory_dir.is_symlink():
            raise ValueError(f"memory directory cannot be a symlink: {memory_dir}")
        memory_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir = memory_dir.resolve()

    def save(self, key: str, content: str) -> None:
        path = self._path_for_key(key)
        raw = content.encode("utf-8")
        if len(raw) > MAX_MEMORY_CONTENT_BYTES:
            raise ValueError(
                f"memory content exceeds {MAX_MEMORY_CONTENT_BYTES} bytes"
            )
        if path.is_symlink():
            raise ValueError(f"memory file cannot be a symlink: {path}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = -1
        try:
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(raw)
        finally:
            if descriptor != -1:
                os.close(descriptor)

    def load(self, key: str) -> str | None:
        path = self._path_for_key(key)
        try:
            raw = read_bounded_bytes(
                path,
                MAX_MEMORY_CONTENT_BYTES,
                label="memory file",
            )
        except FileNotFoundError:
            return None
        return raw.decode("utf-8")

    def list_keys(self) -> list[str]:
        keys: list[str] = []
        for index, path in enumerate(self.memory_dir.iterdir(), 1):
            if index > MAX_MEMORY_KEYS:
                raise ValueError(f"memory store exceeds {MAX_MEMORY_KEYS} files")
            if path.is_symlink() or not path.is_file() or path.suffix != ".md":
                continue
            keys.append(path.stem)
        return sorted(keys)

    def _path_for_key(self, key: str) -> Path:
        if (
            not isinstance(key, str)
            or not key
            or len(key) > MAX_MEMORY_KEY_CHARS
            or key in {".", ".."}
            or "\x00" in key
            or Path(key).name != key
        ):
            raise ValueError("memory key must be one safe filename component")
        return self.memory_dir / f"{key}.md"
