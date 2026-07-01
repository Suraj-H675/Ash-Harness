# tests/unit/test_plugin_manifest.py
import json
import pytest

from plugins.manifest import MAX_PLUGIN_MANIFEST_BYTES, PluginManifest
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
    assert manifest.schema_version == 1
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
