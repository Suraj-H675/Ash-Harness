from context.instructions import (
    InstructionDiagnostic,
    discover_instructions,
    render_instructions,
)
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


def test_project_instruction_imports_are_expanded(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    rules = workspace / "docs" / "rules.md"
    (home / ".ash").mkdir(parents=True)
    rules.parent.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    (workspace / "ASH.md").write_text("project rule\n@import docs/rules.md")
    rules.write_text("imported rule")

    diagnostics: list[InstructionDiagnostic] = []
    discovered = discover_instructions(
        workspace,
        include_project=True,
        current_directory=workspace,
        diagnostics=diagnostics,
    )
    rendered = render_instructions(discovered, diagnostics)

    assert diagnostics == []
    assert [item.path for item in discovered] == [workspace / "ASH.md", rules]
    assert "project rule" in rendered
    assert "imported rule" in rendered
    assert "@import" not in rendered


def test_project_instruction_imports_cannot_escape_workspace(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    secret = tmp_path / "secret.md"
    (home / ".ash").mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (workspace / "ASH.md").write_text("@import ../secret.md")
    secret.write_text("secret rule")

    diagnostics: list[InstructionDiagnostic] = []
    discovered = discover_instructions(
        workspace,
        include_project=True,
        current_directory=workspace,
        diagnostics=diagnostics,
    )
    rendered = render_instructions(discovered, diagnostics)

    assert "secret rule" not in rendered
    assert len(discovered) == 1
    assert [diagnostic.message for diagnostic in diagnostics] == [
        f"instruction import escapes trusted root: {workspace.resolve()}"
    ]


def test_missing_instruction_import_is_reported(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    (home / ".ash").mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (workspace / "ASH.md").write_text("@import missing.md")

    diagnostics: list[InstructionDiagnostic] = []
    discover_instructions(
        workspace,
        include_project=True,
        current_directory=workspace,
        diagnostics=diagnostics,
    )

    assert [diagnostic.message for diagnostic in diagnostics] == [
        "instruction import file does not exist"
    ]


def test_cyclic_instruction_import_is_reported_once(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    (home / ".ash").mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (workspace / "ASH.md").write_text("root\n@import extra.md")
    (workspace / "extra.md").write_text("extra\n@import ASH.md")

    diagnostics: list[InstructionDiagnostic] = []
    discovered = discover_instructions(
        workspace,
        include_project=True,
        current_directory=workspace,
        diagnostics=diagnostics,
    )

    assert [item.path for item in discovered] == [
        workspace / "ASH.md",
        workspace / "extra.md",
    ]
    assert [diagnostic.message for diagnostic in diagnostics] == [
        "instruction import skipped because it is cyclic"
    ]
