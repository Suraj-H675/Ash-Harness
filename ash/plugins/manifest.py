"""Plugin manifest schema and validation."""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.version import parse


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
    dependencies: list[dict[str, str]] = field(default_factory=list)
    # e.g. [{"name": "other-plugin", "version": ">=1.0.0"}]

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
            dependencies=data.get("dependencies", []),
        )

    def check_dependencies(self) -> list[str]:
        """Validate all declared dependencies are installed.

        Returns a list of error messages for unmet dependencies.
        Returns an empty list if all dependencies are satisfied.
        """
        errors: list[str] = []
        for dep in self.dependencies:
            name = dep.get("name", "")
            version_spec = dep.get("version", "")
            if not name:
                continue
            # Try to import the plugin package
            try:
                spec = importlib.util.find_spec(name)
                if spec is None:
                    errors.append(f"Missing dependency: {name} ({version_spec})")
                    continue
                # If version spec is given (e.g. ">=1.0.0"), check it
                if version_spec:
                    try:
                        installed = importlib.metadata.version(name)
                        if not SpecifierSet(version_spec).contains(parse(installed)):
                            errors.append(
                                f"{name} {installed} does not satisfy {version_spec}"
                            )
                    except Exception:
                        # If version checking fails, skip
                        pass
            except Exception:
                errors.append(f"Missing dependency: {name} ({version_spec})")
        return errors

    @classmethod
    def load(cls, path: Path) -> PluginManifest:
        import json

        with path.open() as f:
            data = json.load(f)
        return cls.from_dict(data)
