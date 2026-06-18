"""Plugin manifest schema and validation."""

from __future__ import annotations

import importlib
import importlib.metadata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, parse


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

        Uses importlib.metadata.version() (case-insensitive) to detect packages,
        avoiding the case-sensitivity mismatch of find_spec().
        """
        errors: list[str] = []
        for dep in self.dependencies:
            name = dep.get("name", "")
            version_spec = dep.get("version", "")
            if not name:
                continue

            # Detect package via importlib.metadata (case-insensitive, unlike find_spec).
            # PackageNotFoundError is raised if the package is not installed.
            try:
                installed_version = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                errors.append(f"Missing dependency: {name} ({version_spec})")
                continue

            # If version spec is given, validate it.
            if version_spec:
                try:
                    spec_set = SpecifierSet(version_spec)
                    installed = parse(installed_version)
                    if not spec_set.contains(installed):
                        errors.append(
                            f"{name} {installed_version} does not satisfy {version_spec}"
                        )
                except packaging.specifiers.InvalidSpecifier:
                    errors.append(
                        f"Invalid version specifier '{version_spec}' for {name}"
                    )
                except packaging.version.InvalidVersion:
                    # Installed package has malformed metadata version string.
                    errors.append(
                        f"{name} has invalid version string '{installed_version}'"
                    )

        return errors

    @classmethod
    def load(cls, path: Path) -> PluginManifest:
        import json

        with path.open() as f:
            data = json.load(f)
        return cls.from_dict(data)
