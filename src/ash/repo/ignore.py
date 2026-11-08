"""Shared generated/dependency directory names excluded by code discovery."""

from __future__ import annotations

# Known generated, dependency, and metadata directories excluded by broad code discovery.
DEFAULT_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "env",
        ".tox",
        "node_modules",
        "dist",
        "build",
        ".ash",
    }
)
