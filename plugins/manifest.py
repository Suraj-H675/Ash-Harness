"""Plugin manifest schema and validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, parse


CURRENT_PLUGIN_MANIFEST_SCHEMA_VERSION = 1
MAX_PLUGIN_MANIFEST_BYTES = 128 * 1024
PLUGIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass
class PluginManifest:
    name: str
    version: str
    schema_version: int = CURRENT_PLUGIN_MANIFEST_SCHEMA_VERSION
    description: str = ""
    commands: list[str | dict[str, Any]] = field(default_factory=list)
    agents: list[str | dict[str, Any]] = field(default_factory=list)
    hooks: list[str | dict[str, Any]] = field(default_factory=list)
    mcp_servers: list[str | dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    dependencies: list[dict[str, str]] = field(default_factory=list)
    # e.g. [{"name": "other-plugin", "version": ">=1.0.0"}]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginManifest:
        if not isinstance(data, dict):
            raise ValueError("plugin manifest must be a JSON object")
        raw_schema_version = data.get(
            "schemaVersion",
            data.get("schema_version", CURRENT_PLUGIN_MANIFEST_SCHEMA_VERSION),
        )
        if not isinstance(raw_schema_version, int):
            raise ValueError("plugin schemaVersion must be an integer")
        if raw_schema_version > CURRENT_PLUGIN_MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                "plugin manifest schemaVersion "
                f"{raw_schema_version} is newer than supported "
                f"{CURRENT_PLUGIN_MANIFEST_SCHEMA_VERSION}; upgrade Ash"
            )
        name = data.get("name")
        version = data.get("version", "0.0.0")
        description = data.get("description", "")
        if not isinstance(name, str):
            raise ValueError("plugin name must be a string")
        if not isinstance(version, str):
            raise ValueError("plugin version must be a string")
        if not isinstance(description, str):
            raise ValueError("plugin description must be a string")
        commands = _component_list(data, "commands")
        agents = _component_list(data, "agents")
        hooks = _component_list(data, "hooks")
        mcp_servers = _component_list(data, "mcpServers")
        skills = data.get("skills", [])
        if not isinstance(skills, list) or not all(
            isinstance(path, str) and path for path in skills
        ):
            raise ValueError("plugin skills must be a list of non-empty paths")
        dependencies = data.get("dependencies", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(dependency, dict)
            and isinstance(dependency.get("name", ""), str)
            and isinstance(dependency.get("version", ""), str)
            for dependency in dependencies
        ):
            raise ValueError("plugin dependencies must be name/version objects")
        return cls(
            name=name,
            version=version,
            schema_version=raw_schema_version,
            description=description,
            commands=commands,
            agents=agents,
            hooks=hooks,
            mcp_servers=mcp_servers,
            skills=skills,
            dependencies=dependencies,
        )

    def check_dependencies(self, installed_plugins: Mapping[str, str]) -> list[str]:
        """Validate all declared plugin dependencies are installed.

        Returns a list of error messages for unmet dependencies.
        Returns an empty list if all dependencies are satisfied.

        ``installed_plugins`` maps plugin names to manifest versions.
        """
        errors: list[str] = []
        for dep in self.dependencies:
            name = dep.get("name", "")
            version_spec = dep.get("version", "")
            if not name:
                continue

            installed_version = installed_plugins.get(name)
            if installed_version is None:
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
                except InvalidSpecifier:
                    errors.append(
                        f"Invalid version specifier '{version_spec}' for {name}"
                    )
                except InvalidVersion:
                    # Installed package has malformed metadata version string.
                    errors.append(
                        f"{name} has invalid version string '{installed_version}'"
                    )

        return errors

    @classmethod
    def load(cls, path: Path) -> PluginManifest:
        if path.stat().st_size > MAX_PLUGIN_MANIFEST_BYTES:
            raise ValueError("plugin manifest exceeds 128 KiB")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("plugin manifest must be a JSON object")
        return cls.from_dict(data)


def _component_list(data: dict[str, Any], key: str) -> list[str | dict[str, Any]]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(
        (isinstance(item, str) and bool(item)) or isinstance(item, dict)
        for item in value
    ):
        raise ValueError(f"plugin {key} must be a list of non-empty paths or objects")
    return value


def validate_plugin_identity(manifest: PluginManifest) -> None:
    if not PLUGIN_NAME.fullmatch(manifest.name):
        raise ValueError("plugin name must be a portable path-safe identifier")
    try:
        parse(manifest.version)
    except InvalidVersion as exc:
        raise ValueError(f"invalid plugin version: {manifest.version!r}") from exc
    for dependency in manifest.dependencies:
        name = dependency.get("name", "")
        version_spec = dependency.get("version", "")
        if not PLUGIN_NAME.fullmatch(name):
            raise ValueError("plugin dependency names must be portable identifiers")
        if name == manifest.name:
            raise ValueError("plugin cannot depend on itself")
        if version_spec:
            try:
                SpecifierSet(version_spec)
            except InvalidSpecifier as exc:
                raise ValueError(
                    f"invalid dependency version specifier {version_spec!r} for {name}"
                ) from exc
