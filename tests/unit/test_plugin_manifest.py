# tests/unit/test_plugin_manifest.py
import json
import pytest

from ash.plugins.manifest import MAX_PLUGIN_MANIFEST_BYTES, PluginManifest
from pathlib import Path


def test_load_minimal_manifest(tmp_path: Path) -> None:
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(
        json.dumps(
            {
                "name": "my-plugin",
                "version": "1.0.0",
            }
        )
    )
    manifest = PluginManifest.load(manifest_file)
    assert manifest.name == "my-plugin"
    assert manifest.version == "1.0.0"
    assert manifest.schema_version == 2
    assert manifest.description == ""


def test_load_full_manifest(tmp_path: Path) -> None:
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(
        json.dumps(
            {
                "name": "test-plugin",
                "version": "0.1.0",
                "description": "A test plugin",
                "commands": [{"name": "hello", "description": "Say hello"}],
                "agents": [
                    {"identifier": "helper", "systemPrompt": "You are helpful."}
                ],
            }
        )
    )
    manifest = PluginManifest.load(manifest_file)
    assert len(manifest.commands) == 1
    assert manifest.commands[0]["name"] == "hello"
    assert len(manifest.agents) == 1


def test_load_manifest_schema_version_from_camel_case(tmp_path: Path) -> None:
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "name": "my-plugin",
                "version": "1.0.0",
            }
        )
    )
    manifest = PluginManifest.load(manifest_file)
    assert manifest.schema_version == 1


def test_load_manifest_schema_version_from_snake_case(tmp_path: Path) -> None:
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "my-plugin",
                "version": "1.0.0",
            }
        )
    )
    manifest = PluginManifest.load(manifest_file)
    assert manifest.schema_version == 1


def test_future_manifest_schema_version_is_refused(tmp_path: Path) -> None:
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(
        json.dumps(
            {
                "schemaVersion": 999,
                "name": "my-plugin",
                "version": "1.0.0",
            }
        )
    )
    try:
        PluginManifest.load(manifest_file)
    except ValueError as exc:
        assert "newer than supported" in str(exc)
    else:
        raise AssertionError("future plugin manifest schema version was accepted")


def test_deprecated_manifest_schema_version_exposes_notice(tmp_path: Path) -> None:
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "name": "legacy-plugin",
                "version": "1.0.0",
            }
        )
    )
    manifest = PluginManifest.load(manifest_file)
    assert manifest.deprecation_notice is not None
    assert "schemaVersion 1 is deprecated" in manifest.deprecation_notice


def test_current_manifest_schema_version_has_no_notice(tmp_path: Path) -> None:
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "name": "current-plugin",
                "version": "1.0.0",
            }
        )
    )
    assert PluginManifest.load(manifest_file).deprecation_notice is None


def test_pre_minimum_manifest_schema_version_is_refused(tmp_path: Path) -> None:
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(
        json.dumps(
            {
                "schemaVersion": 0,
                "name": "old-plugin",
                "version": "1.0.0",
            }
        )
    )
    with pytest.raises(ValueError, match="older than supported"):
        PluginManifest.load(manifest_file)


def test_check_dependencies_returns_empty_for_installed_plugins(tmp_path: Path) -> None:
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(
        json.dumps(
            {
                "name": "my-plugin",
                "version": "1.0.0",
                "dependencies": [{"name": "base-plugin", "version": ">=1.0"}],
            }
        )
    )
    manifest = PluginManifest.load(manifest_file)
    errors = manifest.check_dependencies({"base-plugin": "1.2.0"})
    assert errors == []


def test_check_dependencies_returns_error_for_missing_plugin(tmp_path: Path) -> None:
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(
        json.dumps(
            {
                "name": "my-plugin",
                "version": "1.0.0",
                "dependencies": [{"name": "missing-plugin"}],
            }
        )
    )
    manifest = PluginManifest.load(manifest_file)
    errors = manifest.check_dependencies({})
    assert len(errors) == 1
    assert "missing-plugin" in errors[0]


def test_manifest_parses_versioned_out_of_process_tool_runtime(tmp_path: Path) -> None:
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(
        json.dumps(
            {
                "name": "formatter",
                "version": "1.0.0",
                "runtime": {
                    "command": ["python", "runtime.py"],
                    "protocolVersion": 1,
                    "timeoutSeconds": 12.5,
                },
                "tools": [
                    {
                        "name": "format_text",
                        "description": "Format supplied text",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                            "additionalProperties": False,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    manifest = PluginManifest.load(manifest_file)

    assert manifest.runtime is not None
    assert manifest.runtime.command == ("python", "runtime.py")
    assert manifest.runtime.timeout_seconds == 12.5
    assert manifest.tools[0].name == "format_text"
    assert manifest.tools[0].input_schema["required"] == ["text"]


@pytest.mark.parametrize(
    ("runtime", "tools", "message"),
    [
        ({"command": ["python", "runtime.py"]}, [], "declared together"),
        (
            None,
            [{"name": "x", "description": "x", "inputSchema": {"type": "object"}}],
            "declared together",
        ),
        (
            {"command": ["python", "runtime.py"], "protocolVersion": 2},
            [{"name": "x", "description": "x", "inputSchema": {"type": "object"}}],
            "protocolVersion",
        ),
        (
            {"command": ["python", "runtime.py"]},
            [
                {
                    "name": "bad.name",
                    "description": "x",
                    "inputSchema": {"type": "object"},
                }
            ],
            "tool name",
        ),
        (
            {"command": ["python", "runtime.py"]},
            [{"name": "valid", "description": "x", "inputSchema": {"type": "array"}}],
            "object JSON Schema",
        ),
    ],
)
def test_manifest_rejects_invalid_runtime_contracts(runtime, tools, message) -> None:
    payload = {"name": "runtime-plugin", "runtime": runtime, "tools": tools}
    if runtime is None:
        payload.pop("runtime")

    with pytest.raises(ValueError, match=message):
        PluginManifest.from_dict(payload)


def test_manifest_rejects_tool_name_too_long_after_namespacing() -> None:
    with pytest.raises(ValueError, match="after namespacing"):
        PluginManifest.from_dict(
            {
                "name": "plugin-with-a-long-but-valid-name",
                "runtime": {"command": ["python3", "runtime.py"]},
                "tools": [
                    {
                        "name": "tool_name_that_is_individually_valid_but_far_too_long",
                        "description": "x",
                        "inputSchema": {"type": "object"},
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "JSON object"),
        ({"name": 42}, "name must be a string"),
        ({"name": "example", "version": 1}, "version must be a string"),
        ({"name": "example", "skills": "skills"}, "skills must be a list"),
        (
            {"name": "example", "commands": [""]},
            "commands must be a list",
        ),
        (
            {"name": "example", "dependencies": ["package"]},
            "dependencies must be name/version objects",
        ),
    ],
)
def test_manifest_rejects_invalid_field_types(
    tmp_path: Path, payload, message: str
) -> None:
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        PluginManifest.load(manifest_file)


def test_manifest_rejects_oversized_file(tmp_path: Path) -> None:
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_bytes(b" " * (MAX_PLUGIN_MANIFEST_BYTES + 1))

    with pytest.raises(ValueError, match="exceeds 128 KiB"):
        PluginManifest.load(manifest_file)
