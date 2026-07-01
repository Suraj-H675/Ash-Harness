from __future__ import annotations

import json
from pathlib import Path

from ash.cli import main
from cli.extensions import discover_extensions, render_extension_inventory
from safety.trust import set_workspace_trusted


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
    (skill / "SKILL.md").write_text("# Review\n", encoding="utf-8")
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
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _write_skill(home / ".ash" / "skills", "review", "Review code")
    _write_plugin(home / ".ash" / "plugins", "example")
    hooks_path = home / ".ash" / "hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(
        json.dumps({"session_start": [{"command": ["echo", "hello"]}]}),
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
