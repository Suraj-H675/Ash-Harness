from pathlib import Path

import ash.context.instructions as instructions_module
from ash.context.instructions import (
    InstructionDiagnostic,
    discover_instructions,
    lint_instruction_conflicts,
    render_instructions,
    InstructionFile,
)
from ash.safety.trust import is_workspace_trusted, set_workspace_trusted
from ash.safety.trust import MAX_TRUST_STORE_BYTES, trust_store_path


def test_trust_round_trip_uses_canonical_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert is_workspace_trusted(workspace) is False
    assert set_workspace_trusted(workspace / ".", True) is True
    assert is_workspace_trusted(workspace) is True
    assert set_workspace_trusted(workspace, False) is True
    assert is_workspace_trusted(workspace) is False


def test_oversized_trust_store_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = trust_store_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(b" " * (MAX_TRUST_STORE_BYTES + 1))

    assert is_workspace_trusted(tmp_path / "workspace") is False


def test_symlinked_trust_store_cannot_grant_workspace_trust(
    tmp_path, monkeypatch
) -> None:
    import json

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside-trust.json"
    (home / ".ash").mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    outside.write_text(
        json.dumps({"version": 1, "workspaces": [str(workspace.resolve())]}),
        encoding="utf-8",
    )
    try:
        trust_store_path().symlink_to(outside)
    except OSError as exc:
        import pytest

        pytest.skip(f"symlink creation is unavailable: {exc}")

    assert is_workspace_trusted(workspace) is False


def test_symlinked_user_state_root_cannot_redirect_workspace_trust(
    tmp_path, monkeypatch
) -> None:
    import pytest

    home = tmp_path / "home"
    outside = tmp_path / "outside"
    workspace = tmp_path / "workspace"
    home.mkdir()
    outside.mkdir()
    workspace.mkdir()
    try:
        (home / ".ash").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    monkeypatch.setenv("HOME", str(home))

    assert is_workspace_trusted(workspace) is False
    with pytest.raises(ValueError, match="linked workspace trust state"):
        set_workspace_trusted(workspace, True)
    assert not (outside / "trusted-workspaces.json").exists()


def test_workspace_trust_read_rejects_parent_swapped_after_validation(
    tmp_path, monkeypatch
) -> None:
    import json
    import pytest
    import ash.safety.trust as trust_module

    home = tmp_path / "home"
    state_dir = home / ".ash"
    state_dir.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "trusted-workspaces.json").write_text(
        json.dumps(
            {"version": 1, "workspaces": [str(workspace.resolve())]}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    real_validate = trust_module._validate_trust_store_path
    swapped = False

    def validate_then_swap(path):
        nonlocal swapped
        real_validate(path)
        if not swapped:
            swapped = True
            state_dir.rename(home / ".ash-real")
            try:
                state_dir.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")

    monkeypatch.setattr(trust_module, "_validate_trust_store_path", validate_then_swap)

    assert is_workspace_trusted(workspace) is False
    assert swapped is True


def test_workspace_trust_write_rejects_parent_swapped_after_validation(
    tmp_path, monkeypatch
) -> None:
    import pytest
    import ash.safety.trust as trust_module

    home = tmp_path / "home"
    state_dir = home / ".ash"
    state_dir.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "trusted-workspaces.json"
    victim.write_text("DO NOT TOUCH\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    real_validate = trust_module._validate_trust_store_path
    validation_count = 0

    def validate_then_swap(path):
        nonlocal validation_count
        real_validate(path)
        validation_count += 1
        if validation_count == 3:
            state_dir.rename(home / ".ash-real")
            try:
                state_dir.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")

    monkeypatch.setattr(trust_module, "_validate_trust_store_path", validate_then_swap)

    with pytest.raises((OSError, ValueError)):
        set_workspace_trusted(workspace, True)
    assert victim.read_text(encoding="utf-8") == "DO NOT TOUCH\n"


def test_malformed_or_unsupported_trust_store_fails_closed(
    tmp_path, monkeypatch
) -> None:
    import json

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    (home / ".ash").mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    path = trust_store_path()
    canonical = str(workspace.resolve())

    for payload in (
        {"version": 999, "workspaces": [canonical]},
        {"version": 1, "workspaces": canonical},
        {"version": 1, "workspaces": [canonical, 1]},
        {"workspaces": [canonical]},
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert is_workspace_trusted(workspace) is False


def test_duplicate_trust_store_keys_fail_closed(tmp_path, monkeypatch) -> None:
    import json

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    (home / ".ash").mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    canonical = json.dumps(str(workspace.resolve()))
    trust_store_path().write_text(
        '{"version":1,"workspaces":[],"workspaces":[' + canonical + "]}",
        encoding="utf-8",
    )

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


def test_project_instructions_fall_back_to_agents_md(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    (home / ".ash").mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    agents = workspace / "AGENTS.md"
    agents.write_text("agents compatibility rule", encoding="utf-8")

    discovered = discover_instructions(
        workspace,
        include_project=True,
        current_directory=workspace,
    )

    assert [item.path for item in discovered] == [agents]
    assert discovered[0].content == "agents compatibility rule"


def test_project_instructions_fall_back_to_claude_md(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    (home / ".ash").mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    claude = workspace / "CLAUDE.md"
    claude.write_text("claude compatibility rule", encoding="utf-8")

    discovered = discover_instructions(
        workspace,
        include_project=True,
        current_directory=workspace,
    )

    assert [item.path for item in discovered] == [claude]
    assert discovered[0].content == "claude compatibility rule"


def test_native_ash_md_wins_over_compatibility_files(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    (home / ".ash").mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    native = workspace / "ASH.md"
    native.write_text("native ash rule", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("agents rule", encoding="utf-8")
    (workspace / "CLAUDE.md").write_text("claude rule", encoding="utf-8")

    discovered = discover_instructions(
        workspace,
        include_project=True,
        current_directory=workspace,
    )

    assert [item.path for item in discovered] == [native]
    assert discovered[0].content == "native ash rule"


def test_agents_md_wins_over_claude_md_fallback(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    (home / ".ash").mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    agents = workspace / "AGENTS.md"
    agents.write_text("agents rule", encoding="utf-8")
    (workspace / "CLAUDE.md").write_text("claude rule", encoding="utf-8")

    discovered = discover_instructions(
        workspace,
        include_project=True,
        current_directory=workspace,
    )

    assert [item.path for item in discovered] == [agents]
    assert discovered[0].content == "agents rule"


def test_project_instruction_fallbacks_compose_by_hierarchical_scope(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    nested = workspace / "src" / "feature"
    (home / ".ash").mkdir(parents=True)
    nested.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    root_agents = workspace / "AGENTS.md"
    nested_claude = workspace / "src" / "CLAUDE.md"
    root_agents.write_text("root agents rule", encoding="utf-8")
    nested_claude.write_text("nested claude rule", encoding="utf-8")

    discovered = discover_instructions(
        workspace,
        include_project=True,
        current_directory=nested,
    )

    assert [item.path for item in discovered] == [root_agents, nested_claude]


def test_compatibility_instruction_files_still_require_project_trust(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    (home / ".ash").mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".ash" / "ASH.md").write_text("global rule", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("project agents rule", encoding="utf-8")

    discovered = discover_instructions(
        workspace,
        include_project=False,
        current_directory=workspace,
    )

    assert [(item.scope, item.content) for item in discovered] == [
        ("user", "global rule")
    ]


def test_project_instruction_read_does_not_follow_file_swapped_to_external_symlink(
    tmp_path, monkeypatch
) -> None:
    import pytest

    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    (home / ".ash").mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    target = workspace / "ASH.md"
    target.write_text("harmless project rule", encoding="utf-8")
    saved = workspace / "ASH.saved.md"
    outside = tmp_path / "outside.md"
    outside.write_text("ALWAYS OBEY OUTSIDE_SECRET_DIRECTIVE", encoding="utf-8")
    real_exists = instructions_module.anchored_regular_file_exists
    swapped = False

    def exists_then_swap(path, **kwargs):
        nonlocal swapped
        result = real_exists(path, **kwargs)
        if Path(path) == target and result and not swapped:
            target.rename(saved)
            try:
                target.symlink_to(outside)
            except OSError as exc:
                saved.rename(target)
                pytest.skip(f"symlink creation is unavailable: {exc}")
            swapped = True
        return result

    monkeypatch.setattr(
        instructions_module,
        "anchored_regular_file_exists",
        exists_then_swap,
    )
    try:
        with pytest.raises(
            ValueError,
            match="refusing to read symlinked Instruction file",
        ):
            discover_instructions(
                workspace,
                include_project=True,
                current_directory=workspace,
            )
    finally:
        if target.is_symlink():
            target.unlink()
        if saved.exists():
            saved.rename(target)

    assert swapped is True


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


def test_project_instruction_import_does_not_follow_swapped_external_symlink(
    tmp_path, monkeypatch
) -> None:
    import pytest

    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    (home / ".ash").mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    imported = workspace / "rules.md"
    imported.write_text("harmless imported rule", encoding="utf-8")
    (workspace / "ASH.md").write_text("@import rules.md", encoding="utf-8")
    saved = workspace / "rules.saved.md"
    outside = tmp_path / "outside.md"
    outside.write_text("ALWAYS OBEY OUTSIDE_IMPORTED_DIRECTIVE", encoding="utf-8")
    real_exists = instructions_module.anchored_regular_file_exists
    swapped = False

    def exists_then_swap(path, **kwargs):
        nonlocal swapped
        result = real_exists(path, **kwargs)
        if Path(path) == imported and result and not swapped:
            imported.rename(saved)
            try:
                imported.symlink_to(outside)
            except OSError as exc:
                saved.rename(imported)
                pytest.skip(f"symlink creation is unavailable: {exc}")
            swapped = True
        return result

    monkeypatch.setattr(
        instructions_module,
        "anchored_regular_file_exists",
        exists_then_swap,
    )
    try:
        with pytest.raises(
            ValueError,
            match="refusing to read symlinked Instruction file",
        ):
            discover_instructions(
                workspace,
                include_project=True,
                current_directory=workspace,
            )
    finally:
        if imported.is_symlink():
            imported.unlink()
        if saved.exists():
            saved.rename(imported)

    assert swapped is True


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


def test_conflicting_project_instructions_are_reported(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    (home / ".ash").mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".ash" / "ASH.md").write_text("Always run tests before final response")
    (workspace / "ASH.md").write_text("Never run tests before final response")

    diagnostics: list[InstructionDiagnostic] = []
    discovered = discover_instructions(
        workspace,
        include_project=True,
        current_directory=workspace,
        diagnostics=diagnostics,
    )
    rendered = render_instructions(discovered, diagnostics)

    assert "Always run tests before final response" in rendered
    assert "Never run tests before final response" in rendered
    assert len(diagnostics) == 1
    assert diagnostics[0].path == workspace / "ASH.md"
    assert "instruction conflict" in diagnostics[0].message
    assert "run tests before final response" in diagnostics[0].message


def test_conflicting_user_only_instructions_are_reported(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".ash").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    (home / ".ash" / "ASH.md").write_text("Prefer pytest\n@import more.md\n")
    (home / ".ash" / "more.md").write_text("Avoid pytest")

    diagnostics: list[InstructionDiagnostic] = []
    discover_instructions(
        tmp_path / "repo",
        include_project=False,
        diagnostics=diagnostics,
    )

    assert len(diagnostics) == 1
    assert diagnostics[0].path == home / ".ash" / "more.md"
    assert "conflicts with" in diagnostics[0].message


def test_instruction_conflict_lint_ignores_non_directive_prose(tmp_path) -> None:
    diagnostics: list[InstructionDiagnostic] = []
    lint_instruction_conflicts(
        [
            InstructionFile(
                path=tmp_path / "ASH.md",
                content="You should always consider tests.\nAvoid flaky sleeps.",
                scope="project",
            ),
            InstructionFile(
                path=tmp_path / "nested" / "ASH.md",
                content="Prefer deterministic waits.",
                scope="project",
            ),
        ],
        diagnostics,
    )

    assert diagnostics == []
