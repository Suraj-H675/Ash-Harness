from safety.grants import load_tool_grants, set_tool_grant
from safety.policy import PermissionPolicy, PolicyAction


def test_persistent_grants_round_trip_and_cannot_override_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    set_tool_grant(workspace, "run_command", True)
    assert load_tool_grants(workspace) == {"run_command"}
    assert (
        PermissionPolicy(
            "interactive", persistent_tool_grants={"run_command"}
        ).evaluate("run_command", {}).action
        == PolicyAction.ALLOW
    )
    assert (
        PermissionPolicy(
            "plan", persistent_tool_grants={"run_command"}
        ).evaluate("run_command", {}).action
        == PolicyAction.DENY
    )
    set_tool_grant(workspace, "run_command", False)
    assert load_tool_grants(workspace) == set()
