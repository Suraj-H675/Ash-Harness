"""Selective local-state reset command."""

from __future__ import annotations

import shutil
from pathlib import Path


def reset_local_state(
    *, config: bool, sessions: bool, cache: bool, confirmed: bool
) -> list[Path]:
    if not confirmed:
        raise ValueError("reset requires explicit confirmation")
    root = Path.home() / ".ash"
    targets: list[Path] = []
    if config:
        targets.extend(
            root / name
            for name in (
                ".env",
                "ash.toml",
                "trusted-workspaces.json",
                "permission-grants.json",
            )
        )
    if sessions:
        targets.append(root / "db")
    if cache:
        targets.extend((root / "cache", root / "chroma", root / "history"))
    removed: list[Path] = []
    for target in targets:
        if target.is_symlink() or target.is_file():
            target.unlink(missing_ok=True)
            removed.append(target)
        elif target.is_dir():
            shutil.rmtree(target)
            removed.append(target)
    return removed
