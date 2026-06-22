from context.instructions import discover_instructions, render_instructions
from safety.trust import is_workspace_trusted, set_workspace_trusted


def test_trust_round_trip_uses_canonical_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert is_workspace_trusted(workspace) is False
    assert set_workspace_trusted(workspace / ".", True) is True
    assert is_workspace_trusted(workspace) is True
    assert set_workspace_trusted(workspace, False) is True
    assert is_workspace_trusted(workspace) is False


def test_project_instructions_require_trust_flag(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    nested = workspace / "src"
    (home / ".ash").mkdir(parents=True)
    nested.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    (home / ".ash" / "ASH.md").write_text("global rule")
    (workspace / "ASH.md").write_text("project rule")
    (nested / "ASH.md").write_text("nested rule")

    untrusted = discover_instructions(
        workspace, include_project=False, current_directory=nested
    )
    assert [item.scope for item in untrusted] == ["user"]

    trusted = discover_instructions(
        workspace, include_project=True, current_directory=nested
    )
    rendered = render_instructions(trusted)
    assert "global rule" in rendered
    assert "project rule" in rendered
    assert "nested rule" in rendered
