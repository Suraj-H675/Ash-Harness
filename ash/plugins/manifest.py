"""Plugin manifest schema and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PluginManifest:
    name: str
    version: str
    description: str = ""
    commands: list[dict[str, Any]] = field(default_factory=list)
    agents: list[dict[str, Any]] = field(default_factory=list)
    hooks: list[dict[str, Any]] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginManifest:
        return cls(
            name=data["name"],
            version=data.get("version", "0.0.0"),
            description=data.get("description", ""),
            commands=data.get("commands", []),
            agents=data.get("agents", []),
            hooks=data.get("hooks", []),
            mcp_servers=data.get("mcpServers", []),
            skills=data.get("skills", []),
        )

    @classmethod
    def load(cls, path: Path) -> PluginManifest:
        import json

        with path.open() as f:
            data = json.load(f)
        return cls.from_dict(data)
