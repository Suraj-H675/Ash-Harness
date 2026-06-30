import pytest

from safety.grants import ArgumentMatcher, MatchOperator, PermissionRule, RuleEffect
from safety.policy import PermissionPolicy, PolicyAction


@pytest.mark.parametrize("mode", ["plan", "dry_run"])
def test_read_only_modes_deny_edits(mode: str) -> None:
    decision = PermissionPolicy(mode).evaluate("write_file", {"file_path": "x"})
    assert decision.action == PolicyAction.DENY


def test_plan_mode_allows_reads() -> None:
    decision = PermissionPolicy("plan").evaluate("read_file", {"file_path": "x"})
    assert decision.action == PolicyAction.ALLOW


@pytest.mark.parametrize("tool_name", ["find_symbol", "find_references"])
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
