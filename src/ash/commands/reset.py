"""Selective local-state reset command."""

from __future__ import annotations

from pathlib import Path

from ash.safe_io import remove_anchored_path


def reset_local_state(
    *, config: bool, sessions: bool, cache: bool, confirmed: bool
) -> list[Path]:
    if not confirmed:
        raise ValueError("reset requires explicit confirmation")
    root = Path.home() / ".ash"
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise ValueError(f"refusing to reset through symlinked Ash state directory: {root}")
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
        if remove_anchored_path(
            target,
            trusted_root=root.parent,
            label="Ash reset state",
        ):
            removed.append(target)
    return removed
