import json
from pathlib import Path

import pytest

from ash.safety.grants import (
    ArgumentMatcher,
    MatchOperator,
    PermissionRule,
    RuleEffect,
    load_managed_permission_rules,
)
from ash.safety.policy import PermissionPolicy, PolicyAction


@pytest.mark.parametrize("mode", ["plan", "dry_run"])
def test_read_only_modes_deny_edits(mode: str) -> None:
    decision = PermissionPolicy(mode).evaluate("write_file", {"file_path": "x"})
    assert decision.action == PolicyAction.DENY


def test_plan_mode_allows_reads() -> None:
    decision = PermissionPolicy("plan").evaluate("read_file", {"file_path": "x"})
    assert decision.action == PolicyAction.ALLOW


@pytest.mark.parametrize(
    "tool_name", ["find_symbol", "find_references", "search_tools"]
)
def test_structural_navigation_tools_are_read_only(tool_name: str) -> None:
    decision = PermissionPolicy("plan").evaluate(tool_name, {"query": "Example"})
    assert decision.action == PolicyAction.ALLOW


def test_auto_edit_allows_edits_but_asks_for_commands() -> None:
    policy = PermissionPolicy("auto_edit")
    assert policy.evaluate("replace_file_content", {}).action == PolicyAction.ALLOW
    assert policy.evaluate("replace_file_edits", {}).action == PolicyAction.ALLOW
    assert policy.evaluate("run_command", {}).action == PolicyAction.ASK


def test_full_auto_allows_non_blocklisted_tools() -> None:
    assert (
        PermissionPolicy("auto_approve").evaluate("run_command", {}).action
        == PolicyAction.ALLOW
    )


def test_unknown_mode_fails_closed() -> None:
    with pytest.raises(ValueError):
        PermissionPolicy("unknown")


def test_deny_and_ask_rules_override_broad_modes() -> None:
    deny = PermissionRule.create(RuleEffect.DENY, "run_command")
    ask = PermissionRule.create(RuleEffect.ASK, "read_file")
    policy = PermissionPolicy("auto_approve", persistent_rules=[deny, ask])

    denied = policy.evaluate("run_command", {"command_line": "pytest"})
    prompted = policy.evaluate("read_file", {"file_path": "README.md"})

    assert denied.action == PolicyAction.DENY
    assert denied.rule_id == deny.rule_id
    assert prompted.action == PolicyAction.ASK
    assert prompted.rule_id == ask.rule_id


def test_argument_scoped_rules_match_all_conditions() -> None:
    allow = PermissionRule.create(
        RuleEffect.ALLOW,
        "write_file",
        [
            ArgumentMatcher("file_path", MatchOperator.PREFIX, "docs/"),
            ArgumentMatcher("content", MatchOperator.EXACT, "approved"),
        ],
    )
    policy = PermissionPolicy("interactive", persistent_rules=[allow])

    assert (
        policy.evaluate(
            "write_file",
            {"file_path": "docs/readme.md", "content": "approved"},
        ).action
        == PolicyAction.ALLOW
    )
    assert (
        policy.evaluate(
            "write_file",
            {"file_path": "src/main.py", "content": "approved"},
        ).action
        == PolicyAction.ASK
    )


def test_plan_mode_cannot_be_overridden_by_allow_rule() -> None:
    allow = PermissionRule.create(RuleEffect.ALLOW, "write_file")
    decision = PermissionPolicy("plan", persistent_rules=[allow]).evaluate(
        "write_file", {"file_path": "x"}
    )

    assert decision.action == PolicyAction.DENY
    assert decision.reason == "plan mode is read-only"


def test_managed_deny_and_ask_override_lower_rules() -> None:
    managed_deny = PermissionRule.create(RuleEffect.DENY, "run_command")
    managed_ask = PermissionRule.create(RuleEffect.ASK, "read_file")
    user_deny = PermissionRule.create(
        RuleEffect.DENY,
        "run_command",
        [ArgumentMatcher("command_line", MatchOperator.COMMAND_PREFIX, ["pytest"])],
    )
    user_allow = PermissionRule.create(RuleEffect.ALLOW, "read_file")
    policy = PermissionPolicy(
        "auto_approve",
        managed_rules=[managed_deny, managed_ask],
        persistent_rules=[user_deny, user_allow],
    )

    assert (
        policy.evaluate("run_command", {"command_line": "pytest -q"}).action
        == PolicyAction.DENY
    )
    decision = policy.evaluate("read_file", {"file_path": "README.md"})
    assert decision.action == PolicyAction.ASK
    assert decision.rule_id == managed_ask.rule_id


def test_managed_allow_does_not_bypass_read_only_mode() -> None:
    managed_allow = PermissionRule.create(RuleEffect.ALLOW, "write_file")
    decision = PermissionPolicy(
        "plan",
        managed_rules=[managed_allow],
    ).evaluate("write_file", {"file_path": "x"})

    assert decision.action == PolicyAction.DENY
    assert decision.reason == "plan mode is read-only"


def test_load_managed_permission_rules_reads_sorted_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    directory = tmp_path / "policy"
    directory.mkdir()
    deny_payload = {
        "version": 2,
        "workspaces": {
            str(workspace.resolve()): [
                PermissionRule.create(
                    RuleEffect.DENY,
                    "web_fetch",
                ).as_payload(),
            ],
        },
    }
    allow_payload = {
        "version": 2,
        "workspaces": {
            str(workspace.resolve()): [
                PermissionRule.create(RuleEffect.ALLOW, "web_fetch").as_payload(),
            ],
        },
    }
    (directory / "20-deny.json").write_text(json.dumps(deny_payload))
    (directory / "10-allow.json").write_text(json.dumps(allow_payload))
    monkeypatch.setattr("ash.safety.grants.managed_policy_paths", lambda: (directory,))

    rules = load_managed_permission_rules(workspace)

    assert len(rules) == 2
    assert [rule.effect for rule in rules] == [RuleEffect.ALLOW, RuleEffect.DENY]


def test_load_managed_policy_fails_closed_on_invalid_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    directory = tmp_path / "policy"
    directory.mkdir()
    (directory / "broken.json").write_text("{invalid")
    monkeypatch.setattr("ash.safety.grants.managed_policy_paths", lambda: (directory,))

    with pytest.raises(ValueError, match="invalid managed policy"):
        load_managed_permission_rules(tmp_path)


def test_load_managed_policy_rejects_too_many_files(tmp_path: Path, monkeypatch) -> None:
    from ash.safety import grants

    directory = tmp_path / "policy"
    directory.mkdir()
    for index in range(3):
        (directory / f"{index}.json").write_text(
            json.dumps({"version": 2, "workspaces": {}})
        )
    monkeypatch.setattr(grants, "MAX_MANAGED_RULE_FILES", 2)
    monkeypatch.setattr(grants, "managed_policy_paths", lambda: (directory,))

    with pytest.raises(ValueError, match="more than 2 files"):
        load_managed_permission_rules(tmp_path)
