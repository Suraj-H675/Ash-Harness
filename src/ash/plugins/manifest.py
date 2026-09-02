"""Plugin manifest schema and validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, parse
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]


CURRENT_PLUGIN_MANIFEST_SCHEMA_VERSION = 2
MINIMUM_SUPPORTED_PLUGIN_MANIFEST_SCHEMA_VERSION = 1
DEPRECATED_PLUGIN_MANIFEST_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})
MAX_PLUGIN_MANIFEST_BYTES = 128 * 1024
PLUGIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PLUGIN_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
PLUGIN_RUNTIME_PROTOCOL_VERSION = 1
MAX_PLUGIN_RUNTIME_TOOLS = 64
PLUGIN_TOOL_NAME_MAX_LENGTH = 64


def namespaced_plugin_tool_name(plugin_name: str, tool_name: str) -> str:
    """Build an injective, provider-portable executable plugin tool name."""

    namespace = plugin_name.replace(".", "_dot_")
    candidate = f"plugin_{len(plugin_name)}_{namespace}__{tool_name}"
    if len(candidate) > PLUGIN_TOOL_NAME_MAX_LENGTH:
        raise ValueError(
            f"plugin tool name {candidate!r} exceeds "
            f"{PLUGIN_TOOL_NAME_MAX_LENGTH} characters after namespacing"
        )
    if not PLUGIN_TOOL_NAME.fullmatch(candidate):
        raise ValueError(f"plugin tool name {candidate!r} is not provider-portable")
    return candidate


@dataclass(frozen=True)
class PluginRuntimeManifest:
    command: tuple[str, ...]
    protocol_version: int = PLUGIN_RUNTIME_PROTOCOL_VERSION
    timeout_seconds: float = 30.0

    @classmethod
    def from_dict(cls, data: Any) -> "PluginRuntimeManifest":
        if not isinstance(data, dict):
            raise ValueError("plugin runtime must be an object")
        command = data.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise ValueError("plugin runtime command must be a non-empty argv list")
        if len(command) > 128 or any(len(part) > 4096 for part in command):
            raise ValueError("plugin runtime command is too large")
        protocol_version = data.get(
            "protocolVersion",
            data.get("protocol_version", PLUGIN_RUNTIME_PROTOCOL_VERSION),
        )
        if protocol_version != PLUGIN_RUNTIME_PROTOCOL_VERSION:
            raise ValueError(
                "plugin runtime protocolVersion must be "
                f"{PLUGIN_RUNTIME_PROTOCOL_VERSION}"
            )
        timeout_seconds = data.get("timeoutSeconds", data.get("timeout_seconds", 30))
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 0.1 <= float(timeout_seconds) <= 300
        ):
            raise ValueError(
                "plugin runtime timeoutSeconds must be between 0.1 and 300"
            )
        return cls(tuple(command), protocol_version, float(timeout_seconds))


@dataclass(frozen=True)
class PluginToolManifest:
    name: str
    description: str
    input_schema: dict[str, Any]

    @classmethod
    def from_dict(cls, data: Any) -> "PluginToolManifest":
        if not isinstance(data, dict):
            raise ValueError("plugin tool declarations must be objects")
        name = data.get("name")
        description = data.get("description")
        schema = data.get("inputSchema", data.get("input_schema"))
        if not isinstance(name, str) or not PLUGIN_TOOL_NAME.fullmatch(name):
            raise ValueError(
                "plugin tool name must start with a letter and contain only "
                "letters, numbers, underscores, or hyphens"
            )
        if not isinstance(description, str) or not description.strip():
            raise ValueError("plugin tool description must be a non-empty string")
        if len(description) > 4096:
            raise ValueError("plugin tool description cannot exceed 4096 characters")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("plugin tool inputSchema must be an object JSON Schema")
        try:
            encoded_schema = json.dumps(
                schema,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "plugin tool inputSchema must contain JSON values"
            ) from exc
        if len(encoded_schema.encode("utf-8")) > 128 * 1024:
            raise ValueError("plugin tool inputSchema exceeds 128 KiB")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ValueError(f"invalid plugin tool inputSchema: {exc.message}") from exc
        return cls(name, description.strip(), schema)


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
    runtime: PluginRuntimeManifest | None = None
    tools: list[PluginToolManifest] = field(default_factory=list)
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
        if raw_schema_version < MINIMUM_SUPPORTED_PLUGIN_MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                "plugin manifest schemaVersion "
                f"{raw_schema_version} is older than supported "
                f"{MINIMUM_SUPPORTED_PLUGIN_MANIFEST_SCHEMA_VERSION}; upgrade the plugin"
            )
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
        runtime_data = data.get("runtime")
        runtime = (
            PluginRuntimeManifest.from_dict(runtime_data)
            if runtime_data is not None
            else None
        )
        tool_data = data.get("tools", [])
        if not isinstance(tool_data, list):
            raise ValueError("plugin tools must be a list")
        if len(tool_data) > MAX_PLUGIN_RUNTIME_TOOLS:
            raise ValueError(
                f"plugin cannot declare more than {MAX_PLUGIN_RUNTIME_TOOLS} tools"
            )
        tools = [PluginToolManifest.from_dict(item) for item in tool_data]
        if bool(runtime) != bool(tools):
            raise ValueError("plugin runtime and tools must be declared together")
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("plugin tool names must be unique")
        namespaced_tools = [
            namespaced_plugin_tool_name(name, tool.name) for tool in tools
        ]
        if len(set(namespaced_tools)) != len(namespaced_tools):
            raise ValueError("plugin tool names collide after namespacing")
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
            runtime=runtime,
            tools=tools,
        )

    @property
    def deprecation_notice(self) -> str | None:
        """Return the active deprecation warning for this manifest, if any."""

        if self.schema_version in DEPRECATED_PLUGIN_MANIFEST_SCHEMA_VERSIONS:
            return (
                f"plugin manifest schemaVersion {self.schema_version} is deprecated "
                "and will be removed in a future Ash release; migrate to "
                f"schemaVersion {CURRENT_PLUGIN_MANIFEST_SCHEMA_VERSION}"
            )
        return None

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
        if path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        ):
            raise ValueError("plugin manifest cannot be a link")
        with path.open("rb") as handle:
            raw = handle.read(MAX_PLUGIN_MANIFEST_BYTES + 1)
        if len(raw) > MAX_PLUGIN_MANIFEST_BYTES:
            raise ValueError("plugin manifest exceeds 128 KiB")
        data = json.loads(raw.decode("utf-8"))
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
