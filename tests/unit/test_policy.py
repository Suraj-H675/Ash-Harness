import pytest

from safety.policy import PermissionPolicy, PolicyAction


@pytest.mark.parametrize("mode", ["plan", "dry_run"])
def test_read_only_modes_deny_edits(mode: str) -> None:
    decision = PermissionPolicy(mode).evaluate("write_file", {"file_path": "x"})
    assert decision.action == PolicyAction.DENY


def test_plan_mode_allows_reads() -> None:
    decision = PermissionPolicy("plan").evaluate("read_file", {"file_path": "x"})
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
