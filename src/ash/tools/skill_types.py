"""Shared declarative contracts for executable skill discovery."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillIndexEntry:
    """Lightweight skill description used to populate the system prompt."""

    name: str
    description: str
    source: str  # "python" or "markdown"
    path: str
    trigger: str = ""
