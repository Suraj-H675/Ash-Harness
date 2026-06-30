from __future__ import annotations

import json
from pathlib import Path

from ash.cli import main
from cli.permissions import render_permission_grants
from safety.grants import load_permission_rules, load_tool_grants, set_tool_grant
from safety.policy import PermissionPolicy, PolicyAction


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


def test_permissions_cli_manages_scoped_rules_by_stable_id(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)

    assert (
        main(
            [
                "permissions",
                "allow",
                "run_command",
                "--command-prefix",
                "pytest",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    allow_rule = payload["rules"][0]
    assert allow_rule["effect"] == "allow"
    assert allow_rule["matches"][0]["operator"] == "command_prefix"

    rules = load_permission_rules(workspace)
    policy = PermissionPolicy("interactive", persistent_rules=rules)
    assert (
        policy.evaluate("run_command", {"command_line": "pytest -q"}).action
        == PolicyAction.ALLOW
    )
    assert (
        policy.evaluate(
            "run_command", {"command_line": "pytest -q && echo unsafe"}
        ).action
        == PolicyAction.ASK
    )

    assert (
        main(
            [
                "permissions",
                "deny",
                "write_file",
                "--exact",
                'file_path=".env"',
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    deny_rule = next(rule for rule in payload["rules"] if rule["effect"] == "deny")
    policy = PermissionPolicy(
        "auto_approve",
        persistent_rules=load_permission_rules(workspace),
    )
    assert (
        policy.evaluate("write_file", {"file_path": ".env"}).action == PolicyAction.DENY
    )

    assert (
        main(
            ["permissions", "remove", deny_rule["id"], "--json"],
        )
        == 0
    )
    remaining = json.loads(capsys.readouterr().out)
    assert [rule["id"] for rule in remaining["rules"]] == [allow_rule["id"]]


def test_permissions_cli_rejects_invalid_scopes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(workspace)

    assert main(["permissions", "allow", "write_file", "--command-prefix", "git"]) == 2
    assert "only valid for run_command" in capsys.readouterr().err

    assert (
        main(["permissions", "allow", "write_file", "--exact", "file_path=README"]) == 2
    )
    assert "must be JSON" in capsys.readouterr().err


def test_interactive_runtime_reports_invalid_permission_policy(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("ASH_DB_DIRECTORY", str(home / ".ash" / "db"))
    path = home / ".ash" / "permission-grants.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    assert main(["-p", "hello"]) == 2
    error = capsys.readouterr().err
    assert "invalid permission policy" in error
    assert "cannot read permission rule file" in error
