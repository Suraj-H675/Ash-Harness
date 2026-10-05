from __future__ import annotations

import json
import subprocess
import httpx
import base64
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ash.cli import main
from ash.commands.extensions import render_extension_inventory, render_plugin_action
from ash.plugins.inventory import discover_extensions
from ash.plugins.catalog import sign_catalog
from ash.safety.trust import set_workspace_trusted


def _write_skill(root: Path, name: str, description: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )


def _write_plugin(root: Path, name: str) -> None:
    plugin = root / name
    skill = plugin / "skills" / "plugin-review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: plugin-review\ndescription: Review plugin code\n---\n# Review\n",
        encoding="utf-8",
    )
    (plugin / "plugin.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "description": "Plugin description",
                "skills": ["skills/plugin-review/SKILL.md"],
            }
        ),
        encoding="utf-8",
    )


def test_extension_inventory_discovers_user_extensions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _write_skill(home / ".ash" / "skills", "review", "Review code")
    _write_plugin(home / ".ash" / "plugins", "example")
    hooks_path = home / ".ash" / "hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(
        json.dumps(
            {
                "session_start": [{"command": ["echo", "hello"]}],
                "turn_end": [{"command": ["echo", "done"]}],
            }
        ),
        encoding="utf-8",
    )

    inventory = discover_extensions(workspace)
    payload = json.loads(render_extension_inventory(inventory, json_output=True))

    assert payload["project_trusted"] is False
    assert {skill["name"] for skill in payload["skills"]} == {
        "example:plugin-review",
        "review",
    }
    assert payload["plugins"][0]["name"] == "example"
    assert payload["commands"] == []
    assert payload["hooks"][0]["session_start"] == 1
    assert payload["hooks"][0]["turn_end"] == 1
    assert payload["errors"] == []


def test_extensions_cli_respects_project_trust(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    _write_skill(workspace / ".ash" / "skills", "project-review", "Project skill")

    assert main(["extensions", "skills", "--json"]) == 0
    untrusted = json.loads(capsys.readouterr().out)
    assert untrusted["skills"] == []

    set_workspace_trusted(workspace, True)
    assert main(["extensions", "skills", "--json"]) == 0
    trusted = json.loads(capsys.readouterr().out)
    assert trusted["skills"][0]["name"] == "project-review"


def test_extensions_cli_reports_invalid_hook_config(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    hook_path = home / ".ash" / "hooks.json"
    hook_path.parent.mkdir(parents=True)
    hook_path.write_text(json.dumps({"pre_tool": "bad"}), encoding="utf-8")

    assert main(["extensions", "hooks", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["hooks"] == []
    assert "pre_tool hooks must be a list" in payload["errors"][0]


def test_extension_inventory_surfaces_managed_provenance_and_schema_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _write_plugin(home / ".ash" / "plugins", "example")
    manifest_path = home / ".ash" / "plugins" / "example" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schemaVersion"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    records_path = home / ".ash" / "plugins" / ".ash-install-records.json"
    records_path.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "example": {
                        "version": "1.0.0",
                        "source": "https://plugins.example/example.git",
                        "ref": "v1.0.0",
                        "digest": "a" * 40,
                        "publisher": "ash",
                        "origin": "catalog",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    payload = json.loads(
        render_extension_inventory(
            discover_extensions(workspace),
            kind="plugins",
            json_output=True,
        )
    )

    plugin = payload["plugins"][0]
    assert plugin["provenance"]["origin"] == "catalog"
    assert plugin["provenance"]["publisher"] == "ash"
    assert plugin["provenance"]["ref"] == "v1.0.0"
    assert "schemaVersion 1 is deprecated" in plugin["warnings"][0]


def test_extensions_inventory_rejects_duplicate_hook_config_fields(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    hook_path = home / ".ash" / "hooks.json"
    hook_path.parent.mkdir(parents=True)
    hook_path.write_text(
        '{"turn_end":[],"turn_end":[{"command":["echo","duplicate"]}]}',
        encoding="utf-8",
    )

    inventory = discover_extensions(workspace)

    assert inventory.hooks == ()
    assert any("duplicate JSON object key" in error for error in inventory.errors)


def test_extension_inventory_validates_new_lifecycle_hook_commands(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    hook_path = home / ".ash" / "hooks.json"
    hook_path.parent.mkdir(parents=True)
    hook_path.write_text(
        json.dumps({"turn_end": [{"command": ["echo", 123]}]}),
        encoding="utf-8",
    )

    inventory = discover_extensions(workspace)

    assert inventory.hooks == ()
    assert "arguments must be strings" in inventory.errors[0]


def test_extensions_cli_reports_invalid_skill_without_hiding_valid_skills(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    skill_root = home / ".ash" / "skills"
    _write_skill(skill_root, "valid", "Valid skill")
    invalid = skill_root / "invalid"
    invalid.mkdir(parents=True)
    (invalid / "SKILL.md").write_bytes(b"\xff\xfe")

    assert main(["extensions", "skills", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert [skill["name"] for skill in payload["skills"]] == ["valid"]
    assert "Invalid skill" in payload["errors"][0]


def test_extensions_inventory_lists_namespaced_plugin_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    plugin = home / ".ash" / "plugins" / "example"
    command = plugin / "commands" / "review.md"
    command.parent.mkdir(parents=True)
    command.write_text(
        "---\ndescription: Review a path\n---\nReview $ARGUMENTS",
        encoding="utf-8",
    )
    (plugin / "plugin.json").write_text(
        json.dumps({"name": "example", "version": "1.0.0"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    inventory = discover_extensions(workspace)

    assert inventory.commands[0].name == "example:review"
    assert inventory.commands[0].description == "Review a path"
    assert inventory.commands[0].source == "plugin:example"


def test_extensions_cli_commands_respects_project_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    command = workspace / ".ash" / "commands" / "project-review.md"
    command.parent.mkdir(parents=True)
    command.write_text("Review $ARGUMENTS", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    assert main(["extensions", "commands", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["commands"] == []

    set_workspace_trusted(workspace, True)
    assert main(["extensions", "commands", "--json"]) == 0
    commands = json.loads(capsys.readouterr().out)["commands"]
    assert commands[0]["name"] == "project-review"
    assert commands[0]["source"] == "project"


def test_extensions_inventory_keeps_disabled_plugin_but_removes_its_skills(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _write_plugin(home / ".ash" / "plugins", "example")
    state_path = home / ".ash" / "extensions.json"
    state_path.write_text(
        json.dumps({"version": 1, "disabled_plugins": ["example"]}),
        encoding="utf-8",
    )

    inventory = discover_extensions(workspace)

    assert inventory.plugins[0].enabled is False
    assert inventory.skills == ()


def test_plugin_action_human_rendering_neutralizes_terminal_controls() -> None:
    result = {
        "action": "install",
        "name": "demo",
        "version": "1.0.0",
        "root": "/tmp/plugin\x1b[2J\u202ehidden\u202c\nforged",
        "enabled": True,
    }

    rendered = render_plugin_action(result, json_output=False)

    assert "\x1b[2J" not in rendered
    assert "\u202e" not in rendered
    assert "\n" not in rendered
    assert "/tmp/plugin\\x1b[2J\\u202ehidden\\u202c\\x0aforged" in rendered
    assert json.loads(render_plugin_action(result, json_output=True))["root"] == result["root"]


def test_extension_inventory_human_rendering_neutralizes_plugin_metadata() -> None:
    from ash.plugins.inventory import (
        ExtensionInventory,
        PluginProvenanceSummary,
        PluginSummary,
    )

    inventory = ExtensionInventory(
        workspace="/tmp/repo",
        project_trusted=True,
        skills=(),
        commands=(),
        agents=(),
        plugins=(
            PluginSummary(
                name="demo",
                version="1.0.0",
                description="clean\x1b[2J\u202ehidden\u202c\nforged",
                source="user",
                root="/tmp/demo",
                skills=(),
                commands=(),
                hooks=(),
                mcp_servers=(),
                agents=(),
                runtime_protocol=None,
                tools=(),
                enabled=True,
                provenance=PluginProvenanceSummary(
                    origin="catalog",
                    source="https://plugins.example/demo.git",
                    ref="v1.0.0\x1b[2J",
                    digest="a" * 40,
                    publisher="ash",
                ),
                warnings=(),
            ),
        ),
        hooks=(),
        errors=(),
    )

    rendered = render_extension_inventory(inventory, kind="plugins")

    assert "\x1b[2J" not in rendered
    assert "\u202e" not in rendered
    assert "\nforged" not in rendered
    assert "clean\\x1b[2J\\u202ehidden\\u202c\\x0aforged" in rendered


def test_extensions_inventory_reports_invalid_lifecycle_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    state_path = home / ".ash" / "extensions.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"version": 999}', encoding="utf-8")

    inventory = discover_extensions(workspace)

    assert "invalid extension state" in inventory.errors[0]


def test_extensions_cli_installs_disables_enables_and_uninstalls_local_plugin(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    source = tmp_path / "source"
    _write_plugin(tmp_path, "source")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    assert main(["extensions", "install", str(source), "--json"]) == 0
    installed = json.loads(capsys.readouterr().out)
    assert installed["name"] == "source"
    assert installed["enabled"] is True

    assert main(["extensions", "disable", "source", "--json"]) == 0
    disabled = json.loads(capsys.readouterr().out)
    assert disabled["enabled"] is False
    inventory = discover_extensions(workspace)
    assert inventory.plugins[0].enabled is False

    assert main(["extensions", "enable", "source", "--json"]) == 0
    enabled = json.loads(capsys.readouterr().out)
    assert enabled["enabled"] is True

    assert main(["extensions", "uninstall", "source"]) == 2
    assert "confirmation" in capsys.readouterr().err
    assert main(["extensions", "uninstall", "source", "--yes", "--json"]) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["removed"] is True
    assert not (home / ".ash" / "plugins" / "source").exists()


def test_replace_rolls_back_previous_plugin_when_activation_state_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import ash.plugins.state as plugin_state

    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _write_plugin(tmp_path, "source")
    source = tmp_path / "source"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    assert main(["extensions", "install", str(source)]) == 0
    capsys.readouterr()
    installed_manifest = home / ".ash" / "plugins" / "source" / "plugin.json"
    before = installed_manifest.read_bytes()
    payload = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
    payload["version"] = "2.0.0"
    (source / "plugin.json").write_text(json.dumps(payload), encoding="utf-8")

    def fail_state_write(*args, **kwargs):
        del args, kwargs
        raise OSError("injected activation-state failure")

    monkeypatch.setattr(plugin_state, "_save_extension_state_at", fail_state_write)

    assert main(["extensions", "install", str(source), "--replace"]) == 2
    captured = capsys.readouterr()

    assert "activation-state failure" in captured.err
    assert installed_manifest.read_bytes() == before


def test_extensions_cli_requires_management_target(capsys) -> None:
    assert main(["extensions", "install"]) == 2
    assert "requires a target" in capsys.readouterr().err


def test_extensions_cli_validates_plugin_source_without_installing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    source = tmp_path / "source"
    _write_plugin(tmp_path, "source")
    manifest_path = source / "plugin.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["schemaVersion"] = 2
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    assert main(["extensions", "validate", str(source), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["valid"] is True
    assert result["name"] == "source"
    assert result["schema_version"] == 2
    assert result["components"]["skills"] == ["skills/plugin-review/SKILL.md"]
    assert result["warnings"] == []
    assert not (home / ".ash" / "plugins").exists()


def test_extensions_cli_validate_reports_component_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "plugin.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "name": "source",
                "version": "1.0.0",
                "skills": ["missing/SKILL.md"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)

    assert main(["extensions", "validate", str(source)]) == 2
    assert "component path does not exist" in capsys.readouterr().err


def test_extensions_cli_inspects_visible_local_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    source = tmp_path / "source"
    _write_plugin(tmp_path, "source")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    assert main(["extensions", "install", str(source), "--json"]) == 0
    capsys.readouterr()
    assert main(["extensions", "inspect", "source", "--json"]) == 0
    inspected = json.loads(capsys.readouterr().out)

    assert inspected["name"] == "source"
    assert inspected["source"] == "user"
    assert inspected["enabled"] is True
    assert inspected["components"]["skills"] == ["skills/plugin-review/SKILL.md"]
    assert inspected["provenance"] is None


def test_extensions_cli_inspect_accepts_local_plugin_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    source = tmp_path / "source"
    _write_plugin(tmp_path, "source")
    monkeypatch.chdir(workspace)

    assert main(["extensions", "inspect", str(source), "--json"]) == 0
    inspected = json.loads(capsys.readouterr().out)

    assert inspected["name"] == "source"
    assert inspected["source"] == "path"
    assert inspected["enabled"] is None


def test_extensions_cli_installs_https_git_plugin(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    source = tmp_path / "source"
    _write_plugin(tmp_path, "source")
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(source)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Ash",
            "-c",
            "user.email=ash@example.test",
            "commit",
            "-m",
            "plugin",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    original_popen = subprocess.Popen
    monkeypatch.setattr(
        "ash.plugins.lifecycle.subprocess.Popen",
        lambda args, **kwargs: original_popen(
            [
                *args[:-2],
                str(source),
                args[-1],
            ]
            if Path(args[0]).stem.casefold() == "git" and args[1:2] == ["clone"]
            else args,
            **kwargs,
        ),
    )

    assert (
        main(
            [
                "extensions",
                "install",
                "https://plugins.example/source.git",
                "--ref",
                "main",
                "--json",
            ]
        )
        == 0
    )
    installed = json.loads(capsys.readouterr().out)
    assert installed["name"] == "source"
    assert installed["enabled"] is True
    assert Path(installed["root"]).is_relative_to(home / ".ash" / "plugins")
    assert not (Path(installed["root"]) / ".git").exists()

    assert main(["extensions", "inspect", "source", "--json"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["provenance"]["origin"] == "git"
    assert inspected["provenance"]["source"] == "https://plugins.example/source.git"
    assert inspected["provenance"]["ref"] == "main"


def test_extensions_cli_rejects_non_https_and_missing_git_ref(capsys) -> None:
    assert (
        main(
            [
                "extensions",
                "install",
                "http://plugins.example/plugin.git",
                "--ref",
                "main",
            ]
        )
        == 2
    )
    assert "HTTPS or a local file URI" in capsys.readouterr().err
    assert main(["extensions", "install", "https://plugins.example/plugin.git"]) == 2
    assert "requires an explicit --ref" in capsys.readouterr().err


def test_extensions_install_validates_components_before_replacing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    source = tmp_path / "source"
    _write_plugin(tmp_path, "source")
    command = source / "commands" / "broken.md"
    command.parent.mkdir()
    command.write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    assert main(["extensions", "install", str(source)]) == 2

    assert "command template is empty" in capsys.readouterr().err
    assert not (home / ".ash" / "plugins" / "source").exists()


def test_extensions_replace_preserves_previous_plugin_when_validation_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    source = tmp_path / "source"
    _write_plugin(tmp_path, "source")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    assert main(["extensions", "install", str(source)]) == 0
    capsys.readouterr()
    manifest_path = source / "plugin.json"
    payload = json.loads(manifest_path.read_text())
    payload["version"] = "2.0.0"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    command = source / "commands" / "broken.md"
    command.parent.mkdir()
    command.write_text("", encoding="utf-8")

    assert main(["extensions", "install", str(source), "--replace"]) == 2

    assert "command template is empty" in capsys.readouterr().err
    installed = json.loads(
        (home / ".ash" / "plugins" / "source" / "plugin.json").read_text()
    )
    assert installed["version"] == "1.0.0"


def test_extensions_enforces_enabled_plugin_dependencies(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    base = tmp_path / "base"
    dependent = tmp_path / "dependent"
    _write_plugin(tmp_path, "base")
    _write_plugin(tmp_path, "dependent")
    dependent_manifest = json.loads((dependent / "plugin.json").read_text())
    dependent_manifest["dependencies"] = [{"name": "base", "version": ">=1"}]
    (dependent / "plugin.json").write_text(
        json.dumps(dependent_manifest), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    assert main(["extensions", "install", str(base)]) == 0
    assert main(["extensions", "install", str(dependent)]) == 0
    capsys.readouterr()

    assert main(["extensions", "disable", "base"]) == 2
    assert "required by: dependent" in capsys.readouterr().err
    assert main(["extensions", "disable", "dependent"]) == 0
    assert main(["extensions", "disable", "base"]) == 0
    capsys.readouterr()
    assert main(["extensions", "enable", "dependent"]) == 2
    assert "Missing dependency: base" in capsys.readouterr().err
    assert main(["extensions", "uninstall", "base", "--yes"]) == 2
    assert "required by: dependent" in capsys.readouterr().err


def test_disable_dependency_check_serializes_against_concurrent_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import ash.commands.extensions as extension_commands
    import ash.plugins.lifecycle as lifecycle
    from ash.plugins.errors import PluginLifecycleError
    from ash.plugins.lifecycle import user_plugin_root
    from ash.plugins.state import load_extension_state

    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    base = tmp_path / "base"
    dependent = tmp_path / "dependent"
    _write_plugin(tmp_path, "base")
    _write_plugin(tmp_path, "dependent")
    dependent_manifest = json.loads((dependent / "plugin.json").read_text())
    dependent_manifest["dependencies"] = [{"name": "base", "version": ">=1"}]
    (dependent / "plugin.json").write_text(
        json.dumps(dependent_manifest), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    extension_commands.manage_local_plugin("install", str(base))

    original_manifests = lifecycle._installed_plugin_manifests_strict_at
    checked = threading.Event()
    release_check = threading.Event()
    disable_errors: list[BaseException] = []
    install_errors: list[BaseException] = []
    paused = False

    def paused_manifests(directory, *, excluding=None):
        nonlocal paused
        result = original_manifests(directory, excluding=excluding)
        if not paused and excluding is None and set(result) == {"base"}:
            paused = True
            checked.set()
            assert release_check.wait(5)
        return result

    monkeypatch.setattr(lifecycle, "_installed_plugin_manifests_strict_at", paused_manifests)

    def disable_base() -> None:
        try:
            extension_commands.manage_local_plugin("disable", "base")
        except BaseException as exc:
            disable_errors.append(exc)

    def install_dependent() -> None:
        try:
            extension_commands.manage_local_plugin("install", str(dependent))
        except BaseException as exc:
            install_errors.append(exc)

    disable_thread = threading.Thread(target=disable_base)
    install_thread = threading.Thread(target=install_dependent)
    disable_thread.start()
    assert checked.wait(5)
    install_thread.start()
    release_check.set()
    disable_thread.join(5)
    install_thread.join(5)

    assert not disable_thread.is_alive()
    assert not install_thread.is_alive()
    assert disable_errors == []
    assert len(install_errors) == 1
    assert isinstance(install_errors[0], PluginLifecycleError)
    assert "Missing dependency: base" in str(install_errors[0])
    assert "base" in load_extension_state().disabled_plugins
    assert not (user_plugin_root() / "dependent").exists()


def test_uninstall_dependency_check_serializes_against_concurrent_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import ash.commands.extensions as extension_commands
    import ash.plugins.lifecycle as lifecycle
    from ash.plugins.errors import PluginLifecycleError
    from ash.plugins.lifecycle import user_plugin_root

    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    base = tmp_path / "base"
    dependent = tmp_path / "dependent"
    _write_plugin(tmp_path, "base")
    _write_plugin(tmp_path, "dependent")
    dependent_manifest = json.loads((dependent / "plugin.json").read_text())
    dependent_manifest["dependencies"] = [{"name": "base", "version": ">=1"}]
    (dependent / "plugin.json").write_text(
        json.dumps(dependent_manifest), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    extension_commands.manage_local_plugin("install", str(base))

    original_manifests = lifecycle._installed_plugin_manifests_strict_at
    checked = threading.Event()
    release_check = threading.Event()
    uninstall_errors: list[BaseException] = []
    install_errors: list[BaseException] = []
    paused = False

    def paused_manifests(directory, *, excluding=None):
        nonlocal paused
        result = original_manifests(directory, excluding=excluding)
        if not paused and excluding is None and set(result) == {"base"}:
            paused = True
            checked.set()
            assert release_check.wait(5)
        return result

    monkeypatch.setattr(lifecycle, "_installed_plugin_manifests_strict_at", paused_manifests)

    def uninstall_base() -> None:
        try:
            extension_commands.manage_local_plugin(
                "uninstall",
                "base",
                confirmed=True,
            )
        except BaseException as exc:
            uninstall_errors.append(exc)

    def install_dependent() -> None:
        try:
            extension_commands.manage_local_plugin("install", str(dependent))
        except BaseException as exc:
            install_errors.append(exc)

    uninstall_thread = threading.Thread(target=uninstall_base)
    install_thread = threading.Thread(target=install_dependent)
    uninstall_thread.start()
    assert checked.wait(5)
    install_thread.start()
    release_check.set()
    uninstall_thread.join(5)
    install_thread.join(5)

    assert not uninstall_thread.is_alive()
    assert not install_thread.is_alive()
    assert uninstall_errors == []
    assert len(install_errors) == 1
    assert isinstance(install_errors[0], PluginLifecycleError)
    assert "Missing dependency: base" in str(install_errors[0])
    assert not (user_plugin_root() / "base").exists()
    assert not (user_plugin_root() / "dependent").exists()


def test_enable_dependency_check_serializes_against_concurrent_disable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import ash.commands.extensions as extension_commands
    import ash.plugins.lifecycle as lifecycle
    from ash.plugins.errors import PluginLifecycleError
    from ash.plugins.state import load_extension_state

    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    base = tmp_path / "base"
    dependent = tmp_path / "dependent"
    _write_plugin(tmp_path, "base")
    _write_plugin(tmp_path, "dependent")
    dependent_manifest = json.loads((dependent / "plugin.json").read_text())
    dependent_manifest["dependencies"] = [{"name": "base", "version": ">=1"}]
    (dependent / "plugin.json").write_text(
        json.dumps(dependent_manifest), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    extension_commands.manage_local_plugin("install", str(base))
    extension_commands.manage_local_plugin("install", str(dependent))
    extension_commands.manage_local_plugin("disable", "dependent")

    original_transition = lifecycle._transition_extension_state_checked
    checked = threading.Event()
    release_check = threading.Event()
    enable_errors: list[BaseException] = []
    disable_errors: list[BaseException] = []
    paused = False

    def paused_transition(name, *, enabled, path=None, validator=None):
        nonlocal paused

        def wrapped_validator(state) -> None:
            nonlocal paused
            if validator is not None:
                validator(state)
            if name == "dependent" and enabled and not paused:
                paused = True
                checked.set()
                assert release_check.wait(5)

        return original_transition(
            name,
            enabled=enabled,
            path=path,
            validator=wrapped_validator,
        )

    monkeypatch.setattr(lifecycle, "_transition_extension_state_checked", paused_transition)

    def enable_dependent() -> None:
        try:
            extension_commands.manage_local_plugin("enable", "dependent")
        except BaseException as exc:
            enable_errors.append(exc)

    def disable_base() -> None:
        try:
            extension_commands.manage_local_plugin("disable", "base")
        except BaseException as exc:
            disable_errors.append(exc)

    enable_thread = threading.Thread(target=enable_dependent)
    disable_thread = threading.Thread(target=disable_base)
    enable_thread.start()
    assert checked.wait(5)
    disable_thread.start()
    release_check.set()
    enable_thread.join(5)
    disable_thread.join(5)

    assert not enable_thread.is_alive()
    assert not disable_thread.is_alive()
    assert enable_errors == []
    assert len(disable_errors) == 1
    assert isinstance(disable_errors[0], PluginLifecycleError)
    assert "required by: dependent" in str(disable_errors[0])
    disabled = load_extension_state().disabled_plugins
    assert "base" not in disabled
    assert "dependent" not in disabled


def test_enable_does_not_accept_dependency_from_mismatched_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.plugins.lifecycle import set_plugin_enabled

    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    plugin_root = home / ".ash" / "plugins"
    _write_plugin(plugin_root, "impostor")
    impostor_manifest = plugin_root / "impostor" / "plugin.json"
    payload = json.loads(impostor_manifest.read_text(encoding="utf-8"))
    payload["name"] = "base"
    impostor_manifest.write_text(json.dumps(payload), encoding="utf-8")
    _write_plugin(plugin_root, "dependent")
    dependent_manifest = plugin_root / "dependent" / "plugin.json"
    payload = json.loads(dependent_manifest.read_text(encoding="utf-8"))
    payload["dependencies"] = [{"name": "base", "version": ">=1"}]
    dependent_manifest.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    set_plugin_enabled("dependent", enabled=False)

    assert main(["extensions", "enable", "dependent"]) == 2
    assert "directory 'impostor' contains manifest 'base'" in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments",
    [
        ["extensions", "plugins", "extra"],
        ["extensions", "disable", "example", "--replace"],
        ["extensions", "enable", "example", "--yes"],
    ],
)
def test_extensions_cli_rejects_irrelevant_arguments(arguments, capsys) -> None:
    assert main(arguments) == 2
    assert "Error:" in capsys.readouterr().err


def test_extensions_single_plugin_error_redacts_signed_url_and_terminal_controls(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from ash.plugins.lifecycle import PluginLifecycleError

    marker = "plugin-signed-secret"
    osc = "\x1b]52;c;YXR0YWNrZXI=\x07"

    def fail(*args, **kwargs):
        del args, kwargs
        raise PluginLifecycleError(
            "could not clone plugin source: fatal: "
            "https://plugins.example/demo.git?"
            f"X-Amz-Signature={marker}&view=full {osc}"
        )

    monkeypatch.setattr("ash.commands.extensions.manage_local_plugin", fail)

    assert main(["extensions", "install", "demo"]) == 2
    captured = capsys.readouterr()
    assert marker not in captured.err
    assert osc not in captured.err
    assert "X-Amz-Signature=[REDACTED]" in captured.err
    assert "\\x1b" in captured.err
    assert captured.out == ""


def test_extensions_catalog_error_redacts_signed_url_and_terminal_controls(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from ash.plugins.lifecycle import PluginLifecycleError

    marker = "catalog-signed-secret"
    osc = "\x1b]52;c;YXR0YWNrZXI=\x07"

    def fail(*args, **kwargs):
        del args, kwargs
        raise PluginLifecycleError(
            "could not fetch plugin catalog: "
            "https://catalog.example/plugins.json?"
            f"X-Amz-Signature={marker}&view=full {osc}"
        )

    monkeypatch.setattr("ash.commands.extensions.search_catalog_plugins", fail)

    assert main(["extensions", "search", "demo", "--catalog", "catalog.json"]) == 2
    captured = capsys.readouterr()
    assert marker not in captured.err
    assert osc not in captured.err
    assert "X-Amz-Signature=[REDACTED]" in captured.err
    assert "\\x1b" in captured.err
    assert captured.out == ""


def _write_signed_catalog(
    root: Path,
    *,
    source: str,
    digest: str,
) -> Path:
    private_key = Ed25519PrivateKey.generate()
    encoded_private = (
        base64.urlsafe_b64encode(private_key.private_bytes_raw()).rstrip(b"=").decode()
    )
    public_key = private_key.public_key().public_bytes_raw()
    (root / "keys.json").write_text(
        json.dumps(
            {
                "version": 1,
                "keys": [
                    {
                        "keyId": "test-key",
                        "algorithm": "ed25519",
                        "publicKey": base64.urlsafe_b64encode(public_key)
                        .rstrip(b"=")
                        .decode(),
                    }
                ],
            }
        )
    )
    catalog_payload = {
        "version": 1,
        "sequence": 1,
        "entries": [
            {
                "name": "demo",
                "version": "1.2.3",
                "source": source,
                "ref": "v1.2.3",
                "digest": digest,
            }
        ],
    }
    catalog_path = root / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "catalog": catalog_payload,
                "keyId": "test-key",
                "algorithm": "ed25519",
                "signature": sign_catalog(catalog_payload, encoded_private),
            }
        )
    )
    return catalog_path


def _write_publisher_catalog(
    root: Path,
    *,
    filename: str,
    publisher: str,
    name: str,
    source: str,
    digest: str,
    private_key: Ed25519PrivateKey,
    key_id: str = "publisher-key",
    sequence: int = 1,
    version: str = "1.0.0",
    ref: str = "v1.0.0",
    description: str = "",
) -> Path:
    encoded_private = (
        base64.urlsafe_b64encode(private_key.private_bytes_raw()).rstrip(b"=").decode()
    )
    entry = {
        "name": name,
        "version": version,
        "source": source,
        "ref": ref,
        "digest": digest,
    }
    if description:
        entry["description"] = description
    catalog_payload = {
        "version": 2,
        "publisher": publisher,
        "sequence": sequence,
        "entries": [entry],
    }
    path = root / filename
    path.write_text(
        json.dumps(
            {
                "catalog": catalog_payload,
                "keyId": key_id,
                "algorithm": "ed25519",
                "signature": sign_catalog(catalog_payload, encoded_private),
            }
        )
    )
    return path


def test_catalog_search_human_output_sanitizes_signed_terminal_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    keys = _write_publisher_key(tmp_path, private_key)
    marker = "\x1b]52;c;YXR0YWNrZXI=\x07"
    catalog = _write_publisher_catalog(
        tmp_path,
        filename="control-catalog.json",
        publisher="alpha",
        name="demo",
        source="https://plugins.example/demo.git",
        digest="a" * 64,
        private_key=private_key,
        version="1.0.0",
        ref=f"v1.0.0{marker}",
    )
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(keys))

    assert main(["extensions", "search", "demo", "--catalog", str(catalog)]) == 0
    human = capsys.readouterr().out
    assert marker not in human
    assert "\\x1b]52" in human

    assert (
        main(
            [
                "extensions",
                "search",
                "demo",
                "--catalog",
                str(catalog),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    plugin = payload["plugins"][0]
    assert plugin["version"] == "1.0.0"
    assert plugin["source"] == "https://plugins.example/demo.git"
    assert marker in plugin["ref"]


def _write_publisher_key(
    root: Path,
    private_key: Ed25519PrivateKey,
    *,
    key_id: str = "publisher-key",
) -> Path:
    public_key = private_key.public_key().public_bytes_raw()
    path = root / "publisher-keys.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "keys": [
                    {
                        "keyId": key_id,
                        "algorithm": "ed25519",
                        "publicKey": base64.urlsafe_b64encode(public_key)
                        .rstrip(b"=")
                        .decode(),
                    }
                ],
            }
        )
    )
    return path


@pytest.mark.asyncio
async def test_https_catalog_is_cached_and_verified_for_search(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from ash.commands.extensions import search_catalog_plugins

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(tmp_path / "keys.json"))
    private_key = Ed25519PrivateKey.generate()
    encoded_private = (
        base64.urlsafe_b64encode(private_key.private_bytes_raw()).rstrip(b"=").decode()
    )
    public_key = private_key.public_key().public_bytes_raw()
    catalog_payload = {
        "version": 1,
        "sequence": 4,
        "entries": [
            {
                "name": "remote",
                "version": "2.0.0",
                "source": "https://plugins.example/remote.git",
                "ref": "v2.0.0",
                "digest": "a" * 64,
            }
        ],
    }
    body = json.dumps(
        {
            "catalog": catalog_payload,
            "keyId": "test-key",
            "algorithm": "ed25519",
            "signature": sign_catalog(catalog_payload, encoded_private),
        }
    ).encode()
    (tmp_path / "keys.json").write_text(
        json.dumps(
            {
                "version": 1,
                "keys": [
                    {
                        "keyId": "test-key",
                        "algorithm": "ed25519",
                        "publicKey": base64.urlsafe_b64encode(public_key)
                        .rstrip(b"=")
                        .decode(),
                    }
                ],
            }
        )
    )
    url = "https://catalog.example/ash/plugins.json"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "catalog.example"
        assert request.url.path == "/ash/plugins.json"
        return httpx.Response(200, content=body)

    transport = httpx.MockTransport(handler)
    catalogs, entries = search_catalog_plugins(
        "remote",
        catalog=url,
        transport=transport,
    )

    assert catalogs[0].sequence == 4
    assert catalogs[0].publisher is None
    assert entries[0].name == "remote"
    assert (home / ".ash" / "cache" / "catalogs").is_dir()


def test_remote_catalog_rejects_local_file_plugin_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands import extensions
    from ash.plugins.lifecycle import PluginLifecycleError

    repository = tmp_path / "repository"
    repository.mkdir()
    catalog = _write_signed_catalog(
        tmp_path,
        source=repository.as_uri(),
        digest="a" * 40,
    )
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(tmp_path / "keys.json"))
    monkeypatch.setattr(
        extensions,
        "fetch_catalog",
        lambda url, *, transport=None: catalog,
    )

    with pytest.raises(
        PluginLifecycleError,
        match="remotely fetched plugin catalogs may only reference HTTPS",
    ):
        extensions.search_catalog_plugins(
            "demo",
            catalog="https://catalog.example/plugins.json",
        )


def test_extensions_cli_preserves_https_catalog_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    from ash.commands import extensions

    catalog_path = _write_signed_catalog(
        tmp_path,
        source="https://plugins.example/demo.git",
        digest="a" * 64,
    )
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(tmp_path / "keys.json"))
    requested: list[str] = []

    def fetch(url: str, *, transport=None):
        assert transport is None
        requested.append(url)
        return catalog_path

    monkeypatch.setattr(extensions, "fetch_catalog", fetch)
    url = "https://catalog.example/ash/plugins.json"

    assert main(["extensions", "search", "demo", "--catalog", url, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert requested == [url]
    assert payload["plugins"][0]["name"] == "demo"


def test_multi_catalog_search_and_publisher_qualified_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands.extensions import (
        catalog_entry_for_name,
        render_catalog_search,
        search_catalog_plugins,
    )
    from ash.plugins.lifecycle import PluginLifecycleError

    private_key = Ed25519PrivateKey.generate()
    keys = _write_publisher_key(tmp_path, private_key)
    alpha = _write_publisher_catalog(
        tmp_path,
        filename="alpha.json",
        publisher="alpha",
        name="demo",
        source="https://plugins.example/alpha-demo.git",
        digest="a" * 64,
        private_key=private_key,
        sequence=3,
    )
    beta = _write_publisher_catalog(
        tmp_path,
        filename="beta.json",
        publisher="beta",
        name="demo",
        source="https://plugins.example/beta-demo.git",
        digest="b" * 64,
        private_key=private_key,
        sequence=9,
        description="Beta review helpers",
    )
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(keys))

    catalogs, entries = search_catalog_plugins("demo", catalog=[alpha, beta])

    assert [(item.publisher, item.sequence) for item in catalogs] == [
        ("alpha", 3),
        ("beta", 9),
    ]
    assert [(entry.publisher, entry.name) for entry in entries] == [
        ("alpha", "demo"),
        ("beta", "demo"),
    ]
    with pytest.raises(PluginLifecycleError, match="ambiguous catalog plugin"):
        catalog_entry_for_name("demo", catalog=[alpha, beta])
    selected = catalog_entry_for_name("@beta/demo", catalog=[alpha, beta])
    assert selected.publisher == "beta"
    assert selected.source == "https://plugins.example/beta-demo.git"
    assert selected.description == "Beta review helpers"
    _, description_matches = search_catalog_plugins(
        "review helpers",
        catalog=[alpha, beta],
    )
    assert [(entry.publisher, entry.name) for entry in description_matches] == [
        ("beta", "demo")
    ]
    rendered = render_catalog_search(catalogs, entries)
    assert "Beta review helpers" in rendered
    with pytest.raises(PluginLifecycleError, match="@publisher/name"):
        catalog_entry_for_name("@beta/demo/extra", catalog=[alpha, beta])


def test_multi_catalog_selection_rejects_v1_or_duplicate_publishers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands.extensions import search_catalog_plugins
    from ash.plugins.lifecycle import PluginLifecycleError

    private_key = Ed25519PrivateKey.generate()
    keys = _write_publisher_key(tmp_path, private_key)
    first = _write_publisher_catalog(
        tmp_path,
        filename="first.json",
        publisher="alpha",
        name="first",
        source="https://plugins.example/first.git",
        digest="a" * 64,
        private_key=private_key,
    )
    duplicate = _write_publisher_catalog(
        tmp_path,
        filename="duplicate.json",
        publisher="alpha",
        name="second",
        source="https://plugins.example/second.git",
        digest="b" * 64,
        private_key=private_key,
    )
    encoded_private = (
        base64.urlsafe_b64encode(private_key.private_bytes_raw()).rstrip(b"=").decode()
    )
    legacy_payload = {
        "version": 1,
        "sequence": 1,
        "entries": [
            {
                "name": "legacy",
                "version": "1.0.0",
                "source": "https://plugins.example/legacy.git",
                "ref": "v1.0.0",
                "digest": "c" * 64,
            }
        ],
    }
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "catalog": legacy_payload,
                "keyId": "publisher-key",
                "algorithm": "ed25519",
                "signature": sign_catalog(legacy_payload, encoded_private),
            }
        )
    )
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(keys))

    with pytest.raises(PluginLifecycleError, match="duplicate.*publisher"):
        search_catalog_plugins("", catalog=[first, duplicate])

    with pytest.raises(PluginLifecycleError, match="version 2.*publisher"):
        search_catalog_plugins("", catalog=[first, legacy])


def test_extensions_cli_accepts_repeatable_publisher_catalogs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    keys = _write_publisher_key(tmp_path, private_key)
    alpha = _write_publisher_catalog(
        tmp_path,
        filename="alpha.json",
        publisher="alpha",
        name="demo",
        source="https://plugins.example/alpha-demo.git",
        digest="a" * 64,
        private_key=private_key,
        sequence=2,
    )
    beta = _write_publisher_catalog(
        tmp_path,
        filename="beta.json",
        publisher="beta",
        name="demo",
        source="https://plugins.example/beta-demo.git",
        digest="b" * 64,
        private_key=private_key,
        sequence=4,
    )
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(keys))

    assert (
        main(
            [
                "extensions",
                "search",
                "demo",
                "--catalog",
                str(alpha),
                "--catalog",
                str(beta),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["catalogs"] == [
        {"publisher": "alpha", "sequence": 2},
        {"publisher": "beta", "sequence": 4},
    ]
    assert [item["publisher"] for item in payload["plugins"]] == ["alpha", "beta"]


def test_single_v1_catalog_json_contract_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    catalog = _write_signed_catalog(
        tmp_path,
        source="https://plugins.example/demo.git",
        digest="d" * 64,
    )
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(tmp_path / "keys.json"))

    assert (
        main(
            [
                "extensions",
                "search",
                "demo",
                "--catalog",
                str(catalog),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "sequence": 1,
        "plugins": [
            {
                "name": "demo",
                "version": "1.2.3",
                "source": "https://plugins.example/demo.git",
                "ref": "v1.2.3",
                "digest": "d" * 64,
            }
        ],
    }


def test_publisher_qualified_install_uses_exact_signed_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from ash.commands.extensions import manage_local_plugin
    from ash.plugins.lifecycle import InstalledPlugin

    private_key = Ed25519PrivateKey.generate()
    keys = _write_publisher_key(tmp_path, private_key)
    alpha = _write_publisher_catalog(
        tmp_path,
        filename="alpha-install.json",
        publisher="alpha",
        name="demo",
        source="https://plugins.example/alpha-demo.git",
        digest="a" * 64,
        private_key=private_key,
    )
    beta = _write_publisher_catalog(
        tmp_path,
        filename="beta-install.json",
        publisher="beta",
        name="demo",
        source="https://plugins.example/beta-demo.git",
        digest="b" * 64,
        private_key=private_key,
    )
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(keys))
    monkeypatch.setattr(
        "ash.commands.extensions.load_extension_state",
        lambda: SimpleNamespace(disabled_plugins=frozenset()),
    )
    observed: dict[str, object] = {}

    def install(source_arg: str, **kwargs):
        observed["source"] = source_arg
        observed.update(kwargs)
        return InstalledPlugin("demo", "1.0.0", tmp_path / "installed" / "demo")

    monkeypatch.setattr("ash.commands.extensions.install_git_plugin", install)

    result = manage_local_plugin(
        "install",
        "@beta/demo",
        catalog=[alpha, beta],
    )

    expected = observed["expected"]
    assert result["name"] == "demo"
    assert observed["source"] == "https://plugins.example/beta-demo.git"
    assert observed["ref"] == "v1.0.0"
    assert expected.publisher == "beta"
    assert expected.digest == "b" * 64


def test_direct_url_install_rejects_ambiguous_multi_catalog_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from ash.commands.extensions import manage_local_plugin
    from ash.plugins.lifecycle import PluginLifecycleError

    private_key = Ed25519PrivateKey.generate()
    keys = _write_publisher_key(tmp_path, private_key)
    source = "https://plugins.example/shared.git"
    alpha = _write_publisher_catalog(
        tmp_path,
        filename="alpha-shared.json",
        publisher="alpha",
        name="alpha-demo",
        source=source,
        digest="a" * 64,
        private_key=private_key,
    )
    beta = _write_publisher_catalog(
        tmp_path,
        filename="beta-shared.json",
        publisher="beta",
        name="beta-demo",
        source=source,
        digest="b" * 64,
        private_key=private_key,
    )
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(keys))
    monkeypatch.setattr(
        "ash.commands.extensions.load_extension_state",
        lambda: SimpleNamespace(disabled_plugins=frozenset()),
    )

    def unexpected_install(*args, **kwargs):
        raise AssertionError("ambiguous signed pin must not start installation")

    monkeypatch.setattr(
        "ash.commands.extensions.install_git_plugin", unexpected_install
    )

    with pytest.raises(PluginLifecycleError, match="exactly one entry"):
        manage_local_plugin(
            "install",
            source,
            git_ref="v1.0.0",
            catalog=[alpha, beta],
        )


def test_extensions_catalog_search_and_name_install_are_pinned(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    repository = tmp_path / "repository"
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / "plugin.json").write_text(
        json.dumps({"name": "demo", "version": "1.2.3", "description": "Demo"})
    )
    git_env = {
        "GIT_AUTHOR_NAME": "Ash",
        "GIT_AUTHOR_EMAIL": "ash@example.invalid",
        "HOME": os.environ["HOME"],
        "GIT_COMMITTER_NAME": "Ash",
        "GIT_COMMITTER_EMAIL": "ash@example.invalid",
        "PATH": os.environ["PATH"],
    }
    for arguments in (("init", "-q"), ("add", "."), ("commit", "-m", "demo")):
        subprocess.run(
            ["git", "-C", str(plugin_root), *arguments], check=True, env=git_env
        )
    digest = subprocess.run(
        ["git", "-C", str(plugin_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(plugin_root), "tag", "v1.2.3"], check=True)
    subprocess.run(
        ["git", "clone", "-q", str(plugin_root), str(repository)], check=True
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    catalog_path = _write_signed_catalog(
        tmp_path, source=repository.as_uri(), digest=digest
    )
    monkeypatch.setenv("ASH_CATALOG_KEYS", str(tmp_path / "keys.json"))

    assert (
        main(["extensions", "search", "", "--catalog", str(catalog_path), "--json"])
        == 0
    )
    search = json.loads(capsys.readouterr().out)
    assert search["sequence"] == 1
    assert search["plugins"][0]["name"] == "demo"

    assert (
        main(
            [
                "extensions",
                "install",
                "demo",
                "--catalog",
                str(catalog_path),
                "--json",
            ]
        )
        == 0
    )
    installed = json.loads(capsys.readouterr().out)
    assert installed["name"] == "demo"
    assert installed["enabled"] is True
    assert Path(installed["root"]).is_relative_to(home / ".ash" / "plugins")


def test_direct_url_install_uses_matching_signed_catalog_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from ash.commands.extensions import manage_local_plugin
    from ash.plugins.catalog import CatalogEntry
    from ash.plugins.lifecycle import InstalledPlugin

    source = "https://plugins.example/demo.git"
    expected = CatalogEntry(
        name="demo",
        version="1.2.3",
        source=source,
        ref="v1.2.3",
        digest="a" * 40,
    )
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        "ash.commands.extensions._verified_catalog",
        lambda catalog=None, *, transport=None: SimpleNamespace(entries={"demo": expected}),
    )
    monkeypatch.setattr(
        "ash.commands.extensions.load_extension_state",
        lambda: SimpleNamespace(disabled_plugins=frozenset()),
    )

    def install(source_arg: str, **kwargs):
        observed["source"] = source_arg
        observed.update(kwargs)
        return InstalledPlugin("demo", "1.2.3", tmp_path / "installed" / "demo")

    monkeypatch.setattr("ash.commands.extensions.install_git_plugin", install)

    result = manage_local_plugin(
        "install",
        source,
        git_ref="v1.2.3",
        catalog=tmp_path / "catalog.json",
    )

    assert result["name"] == "demo"
    assert observed["source"] == source
    assert observed["expected"] == expected


def test_direct_url_install_fails_closed_when_catalog_does_not_pin_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from ash.commands.extensions import manage_local_plugin
    from ash.plugins.catalog import CatalogEntry
    from ash.plugins.lifecycle import PluginLifecycleError

    source = "https://plugins.example/demo.git"
    entry = CatalogEntry(
        name="demo",
        version="1.2.3",
        source=source,
        ref="v1.2.3",
        digest="a" * 40,
    )
    monkeypatch.setattr(
        "ash.commands.extensions._verified_catalog",
        lambda catalog=None, *, transport=None: SimpleNamespace(entries={"demo": entry}),
    )
    monkeypatch.setattr(
        "ash.commands.extensions.load_extension_state",
        lambda: SimpleNamespace(disabled_plugins=frozenset()),
    )

    def unexpected_install(*args, **kwargs):
        raise AssertionError("unverified Git install must not start")

    monkeypatch.setattr(
        "ash.commands.extensions.install_git_plugin", unexpected_install
    )

    with pytest.raises(PluginLifecycleError, match="does not contain exactly one entry"):
        manage_local_plugin(
            "install",
            source,
            git_ref="v9.9.9",
            catalog=tmp_path / "catalog.json",
        )


def test_extensions_catalog_requires_configuration(capsys) -> None:
    assert main(["extensions", "search"]) == 2
    assert "plugin catalog is not configured" in capsys.readouterr().err


def test_extensions_inventory_validates_enabled_plugin_hooks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    plugin = home / ".ash" / "plugins" / "example"
    hook = plugin / "hooks" / "hooks.json"
    hook.parent.mkdir(parents=True)
    hook.write_text('{"pre_tool": "invalid"}', encoding="utf-8")
    (plugin / "plugin.json").write_text(
        json.dumps({"name": "example"}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))

    inventory = discover_extensions(workspace)

    assert "pre_tool hooks must be a list" in inventory.errors[0]


def test_enable_rejects_semantically_invalid_disabled_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ash.plugins.lifecycle import set_plugin_enabled
    from ash.plugins.state import load_extension_state

    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    plugin = home / ".ash" / "plugins" / "example"
    hook = plugin / "hooks" / "hooks.json"
    hook.parent.mkdir(parents=True)
    hook.write_text('{"pre_tool": []}', encoding="utf-8")
    (plugin / "plugin.json").write_text(
        json.dumps({"name": "example", "version": "1.0.0"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    set_plugin_enabled("example", enabled=False)
    hook.write_text('{"pre_tool": "invalid"}', encoding="utf-8")

    assert main(["extensions", "enable", "example"]) == 2
    captured = capsys.readouterr()

    assert "Hook event 'pre_tool' must be a list" in captured.err
    assert "example" in load_extension_state().disabled_plugins


def test_enable_rejects_visible_plugin_substitution_after_snapshot_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import ash.plugins.lifecycle as lifecycle
    from ash.plugins.lifecycle import set_plugin_enabled
    from ash.plugins.state import load_extension_state

    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    plugin = home / ".ash" / "plugins" / "example"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(
        json.dumps({"name": "example", "version": "1.0.0"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    set_plugin_enabled("example", enabled=False)

    displaced = plugin.parent / ".example-displaced"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (attacker / "plugin.json").write_text(
        json.dumps({"name": "example", "version": "9.0.0"}),
        encoding="utf-8",
    )
    original_validate = lifecycle.validate_plugin_contents_at
    swapped = False

    def swap_after_validation(snapshot, manifest) -> None:
        nonlocal swapped
        original_validate(snapshot, manifest)
        if not swapped:
            plugin.rename(displaced)
            attacker.rename(plugin)
            swapped = True

    monkeypatch.setattr(lifecycle, "validate_plugin_contents_at", swap_after_validation)

    assert main(["extensions", "enable", "example"]) == 2
    captured = capsys.readouterr()

    assert swapped is True
    assert "changed while enablement was checked" in captured.err
    assert "example" in load_extension_state().disabled_plugins
    assert json.loads((plugin / "plugin.json").read_text(encoding="utf-8"))[
        "version"
    ] == "9.0.0"
    assert json.loads((displaced / "plugin.json").read_text(encoding="utf-8"))[
        "version"
    ] == "1.0.0"


def test_extensions_inventory_validates_enabled_plugin_mcp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    plugin = home / ".ash" / "plugins" / "example"
    plugin.mkdir(parents=True)
    (plugin / ".mcp.json").write_text("[]", encoding="utf-8")
    (plugin / "plugin.json").write_text(
        json.dumps({"name": "example"}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))

    inventory = discover_extensions(workspace)

    assert "MCP config must be an object" in inventory.errors[0]


def test_extensions_inventory_lists_namespaced_plugin_agents(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    plugin = home / ".ash" / "plugins" / "example"
    agent = plugin / "agents" / "reviewer.md"
    agent.parent.mkdir(parents=True)
    agent.write_text(
        "---\ndescription: Review changes\x1b[2J\nbase-role: reviewer\n"
        "model: openai/gpt-5-mini\n---\n"
        "Review correctness.\n",
        encoding="utf-8",
    )
    (plugin / "plugin.json").write_text(
        json.dumps({"name": "example"}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))

    inventory = discover_extensions(workspace)

    assert inventory.agents[0].name == "example:reviewer"
    assert inventory.agents[0].base_role == "reviewer"
    assert inventory.agents[0].model == "openai/gpt-5-mini"
    rendered = render_extension_inventory(inventory, kind="agents")
    assert "\x1b" not in rendered
    assert "Review changes\\x1b[2J" in rendered
    assert "model=openai/gpt-5-mini" in rendered
