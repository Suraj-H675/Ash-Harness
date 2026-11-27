"""Fail-closed source-tree snapshots for isolated coding evaluations."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


UNSAFE_ENTRIES = frozenset({"SYMLINK", "SPECIAL", "UNREADABLE"})

# Version 1 disqualifies observed stale verification, without claiming that
# absence of an observed edit proves the final source was tested.
VERIFICATION_ORDER_VERSION = 1


def verification_freshness(observed_late_edit: bool | None) -> str:
    """Classify only what the trace proves, never infer source-hash freshness."""
    return "observed_stale" if observed_late_edit is True else "unknown"


def content_identity(fingerprint: str | None) -> str | None:
    """Ignore permission bits when proving that required source bytes changed."""

    if fingerprint is not None and fingerprint.startswith("FILE:"):
        return fingerprint.rsplit(":", 1)[-1]
    return fingerprint


def snapshot_workspace_files(
    root: Path, *, include_git: bool = False, include_directories: bool = False,
) -> dict[str, str]:
    """Capture regular-file content and mode, plus every non-directory hazard.

    File descriptors are opened without following links and checked again
    after open; nonblocking opens cannot hang on a FIFO substituted at read time.
    An unreadable or unsupported entry must not disappear from scoring.
    """

    entries: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if not include_git and relative.parts[0] == ".git":
            continue
        name = relative.as_posix()
        try:
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                if include_directories:
                    entries[name] = f"DIR:{stat.S_IMODE(mode):04o}"
                continue
            if stat.S_ISLNK(mode):
                entries[name] = "SYMLINK"
                continue
            if not stat.S_ISREG(mode):
                entries[name] = "SPECIAL"
                continue
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(path, flags), "rb") as source:
                opened = os.fstat(source.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    entries[name] = "SPECIAL"
                    continue
                digest = hashlib.sha256()
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            entries[name] = f"FILE:{stat.S_IMODE(opened.st_mode):04o}:{digest.hexdigest()}"
        except OSError:
            entries[name] = "UNREADABLE"
    return entries
