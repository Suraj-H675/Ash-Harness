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
from ash.commands.extensions import discover_extensions, render_extension_inventory
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


def test_extensions_cli_requires_management_target(capsys) -> None:
    assert main(["extensions", "install"]) == 2
    assert "requires a target" in capsys.readouterr().err


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

    original_run = subprocess.run
    monkeypatch.setattr(
        "ash.plugins.lifecycle.subprocess.run",
        lambda args, **kwargs: original_run(
            [
                *args[:-2],
                str(source),
                args[-1],
            ]
            if args[:2] == ["git", "clone"]
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
    assert "HTTPS URL" in capsys.readouterr().err
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
    sequence, entries = search_catalog_plugins(
        "remote",
        catalog=url,
        transport=transport,
    )

    assert sequence == 4
    assert entries[0].name == "remote"
    assert (home / ".ash" / "cache" / "catalogs").is_dir()


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
        "---\ndescription: Review changes\nbase-role: reviewer\n---\n"
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
