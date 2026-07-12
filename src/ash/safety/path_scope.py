"""Path containment helpers for Ash safety checks."""

import os
from pathlib import Path


def normalize_project_root(project_root: str | Path) -> Path:
    """Return the canonical project root path."""

    return Path(project_root).expanduser().resolve()


def is_relative_to(path: Path, scope: Path) -> bool:
    """Return whether path is contained by scope."""

    try:
        path.relative_to(scope)
    except ValueError:
        return False
    return True


def resolve_target_path(target_path: str | Path, project_root: Path) -> Path:
    """Resolve a target path relative to the project root when needed."""

    path = Path(target_path).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def lexical_target_path(target_path: str | Path, project_root: Path) -> Path:
    """Return an absolute normalized path without resolving filesystem links."""

    path = Path(target_path).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return Path(os.path.abspath(path))


def path_has_link_component(path: Path, project_root: Path) -> Path | None:
    """Return the first existing symlink or junction below the project root."""

    try:
        relative = path.relative_to(project_root)
    except ValueError:
        return path
    current = project_root
    for part in relative.parts:
        current = current / part
        try:
            is_link = current.is_symlink()
            is_junction = hasattr(current, "is_junction") and current.is_junction()
        except OSError:
            continue
        if is_link or is_junction:
            return current
    return None


def normalize_allowed_directories(
    project_root: Path,
    allowed_directories: list[str | Path] | None = None,
) -> list[Path]:
    """Resolve allowed directories and keep every scope inside the project root."""

    directories = allowed_directories or [project_root]
    normalized: list[Path] = []

    for directory in directories:
        path = Path(directory).expanduser()
        if not path.is_absolute():
            path = project_root / path

        resolved = path.resolve()
        if not is_relative_to(resolved, project_root):
            raise ValueError(f"Allowed directory is outside project root: {directory}")
        normalized.append(resolved)

    return normalized


def path_is_in_scope(
    target_path: Path, project_root: Path, allowed_directories: list[Path]
) -> bool:
    """Return whether a resolved path is inside project root and an allowed directory."""

    return is_relative_to(target_path, project_root) and any(
        is_relative_to(target_path, allowed) for allowed in allowed_directories
    )
