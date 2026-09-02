"""Small bounded readers for attacker-controlled or mutable local files."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def read_bounded_bytes(
    path: str | Path,
    max_bytes: int,
    *,
    label: str,
) -> bytes:
    """Read one regular file without following links or exceeding ``max_bytes``."""

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    source = Path(path).expanduser()
    if source.is_symlink():
        raise ValueError(f"refusing to read symlinked {label}: {source}")

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
