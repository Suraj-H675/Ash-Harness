from __future__ import annotations

import json
from pathlib import Path

from ash.cli import main
from cli.permissions import render_permission_grants
from safety.grants import load_tool_grants, set_tool_grant


def test_permission_grant_renderer_emits_json(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    payload = json.loads(
        render_permission_grants(
            workspace,
            {"run_command", "write_file"},
            json_output=True,
        )
    )

    assert payload["workspace"] == str(workspace.resolve())
    assert payload["persistent_grants"] == ["run_command", "write_file"]


def test_permissions_cli_status_allow_and_revoke(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    assert main(["permissions", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["persistent_grants"] == []

    assert main(["permissions", "allow", "run_command", "--json"]) == 0
    allow_payload = json.loads(capsys.readouterr().out)
    assert allow_payload["persistent_grants"] == ["run_command"]
    assert load_tool_grants(workspace) == {"run_command"}

    assert main(["permissions", "revoke", "run_command", "--json"]) == 0
    revoke_payload = json.loads(capsys.readouterr().out)
    assert revoke_payload["persistent_grants"] == []
    assert load_tool_grants(workspace) == set()


def test_permissions_cli_clear_requires_confirmation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    set_tool_grant(workspace, "run_command", True)

    assert main(["permissions", "clear"]) == 2
    assert load_tool_grants(workspace) == {"run_command"}
    assert "cancelled" in capsys.readouterr().err

    assert main(["permissions", "clear", "--yes", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["persistent_grants"] == []
    assert load_tool_grants(workspace) == set()


def test_permissions_cli_rejects_invalid_tool_name(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    assert main(["permissions", "allow", "bad name"]) == 2
    assert "tool name" in capsys.readouterr().err
