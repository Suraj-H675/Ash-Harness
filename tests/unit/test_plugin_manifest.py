# tests/unit/test_plugin_manifest.py
import json
import pytest
from ash.plugins.manifest import PluginManifest
from pathlib import Path


def test_load_minimal_manifest(tmp_path: Path) -> None:
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(json.dumps({
        "name": "my-plugin",
        "version": "1.0.0",
    }))
    manifest = PluginManifest.load(manifest_file)
    assert manifest.name == "my-plugin"
    assert manifest.version == "1.0.0"
    assert manifest.description == ""


def test_load_full_manifest(tmp_path: Path) -> None:
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(json.dumps({
        "name": "test-plugin",
        "version": "0.1.0",
        "description": "A test plugin",
        "commands": [{"name": "hello", "description": "Say hello"}],
        "agents": [{"identifier": "helper", "systemPrompt": "You are helpful."}],
    }))
    manifest = PluginManifest.load(manifest_file)
    assert len(manifest.commands) == 1
    assert manifest.commands[0]["name"] == "hello"
    assert len(manifest.agents) == 1
