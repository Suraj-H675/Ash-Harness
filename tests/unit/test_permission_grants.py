import json

import pytest

from ash.safety.grants import (
    ArgumentMatcher,
    MatchOperator,
    PermissionGrantError,
    PermissionRule,
    RuleEffect,
    add_permission_rule,
    build_exact_scope_matchers,
    grants_path,
    load_permission_rules,
    load_tool_grants,
    set_tool_grant,
)
from ash.safety.policy import PermissionPolicy, PolicyAction


def test_persistent_grants_round_trip_and_cannot_override_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    set_tool_grant(workspace, "run_command", True)
    assert load_tool_grants(workspace) == {"run_command"}
    assert (
        PermissionPolicy("interactive", persistent_tool_grants={"run_command"})
        .evaluate("run_command", {})
        .action
        == PolicyAction.ALLOW
    )
    assert (
        PermissionPolicy("plan", persistent_tool_grants={"run_command"})
        .evaluate("run_command", {})
        .action
        == PolicyAction.DENY
    )
    set_tool_grant(workspace, "run_command", False)
    assert load_tool_grants(workspace) == set()


def test_legacy_grants_migrate_on_the_next_atomic_write(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    path = grants_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "workspaces": {str(workspace.resolve()): ["run_command"]},
            }
        ),
        encoding="utf-8",
    )

    legacy = load_permission_rules(workspace)
    assert len(legacy) == 1
    assert legacy[0].effect == RuleEffect.ALLOW
    assert legacy[0].tool_name == "run_command"
    add_permission_rule(
        workspace,
        PermissionRule.create(
            RuleEffect.DENY,
            "write_file",
            [ArgumentMatcher("file_path", MatchOperator.EXACT, ".env")],
        ),
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert len(payload["workspaces"][str(workspace.resolve())]) == 2
    assert path.stat().st_mode & 0o777 == 0o600


def test_command_prefix_matcher_rejects_ambiguous_shell_programs() -> None:
    matcher = ArgumentMatcher(
        "command_line",
        MatchOperator.COMMAND_PREFIX,
        ["pytest"],
    )

    assert matcher.matches({"command_line": "pytest tests/unit -q"}) is True
    assert matcher.matches({"command_line": "MODE=test pytest -q"}) is True
    assert matcher.matches({"command_line": "pytester -q"}) is False
    assert matcher.matches({"command_line": "pytest -q && rm marker"}) is False
    assert matcher.matches({"command_line": "pytest -q > result.txt"}) is False
    assert matcher.matches({"command_line": 'pytest "$(touch marker)"'}) is False


def test_path_prefix_matcher_is_workspace_scoped_and_safe() -> None:
    matcher = ArgumentMatcher("file_path", MatchOperator.PATH_PREFIX, "docs")

    assert matcher.value == "docs/"
    assert matcher.matches({"file_path": "docs/readme.md"}) is True
    assert matcher.matches({"file_path": "/docs/readme.md"}) is True
    assert matcher.matches({"file_path": "./docs/readme.md"}) is True
    assert matcher.matches({"file_path": "docs/../secret"}) is False
    assert matcher.matches({"file_path": "/../docs/secret"}) is False
    assert matcher.matches({"file_path": "docs/../../secret"}) is False
    assert matcher.matches({"file_path": "documentation/x"}) is False
    with pytest.raises(PermissionGrantError, match="relative workspace path"):
        ArgumentMatcher("file_path", MatchOperator.PATH_PREFIX, "../outside")
    with pytest.raises(PermissionGrantError, match="path arguments"):
        ArgumentMatcher("command_line", MatchOperator.PATH_PREFIX, "docs")


def test_domain_matcher_accepts_urls_and_hostnames_only() -> None:
    url_matcher = ArgumentMatcher("url", MatchOperator.DOMAIN, "*.Example.COM")
    domain_matcher = ArgumentMatcher("domain", MatchOperator.DOMAIN, "docs.example.com")

    assert url_matcher.matches({"url": "https://api.example.com/path"}) is True
    assert url_matcher.matches({"url": "https://example.com/path"}) is True
    assert url_matcher.matches({"url": "https://badexample.com/path"}) is False
    assert domain_matcher.matches({"domain": "DOCS.EXAMPLE.COM"}) is True
    assert domain_matcher.matches({"url": "https://user@example.com"}) is False

    for invalid in ("https://example.com", "*", "example"):
        with pytest.raises(PermissionGrantError, match="domain"):
            ArgumentMatcher("url", MatchOperator.DOMAIN, invalid)


def test_suffix_matcher_is_safe_case_insensitive_extension_matching() -> None:
    matcher = ArgumentMatcher("file_path", MatchOperator.SUFFIX, ".MD")

    assert matcher.value == ".md"
    assert matcher.matches({"file_path": "docs/readme.md"}) is True
    assert matcher.matches({"file_path": "/tmp/notes.MD"}) is True
    assert matcher.matches({"file_path": "./archive/report.Markdown"}) is False
    assert matcher.matches({"file_path": "archive.md/secret"}) is False
    assert matcher.matches({"file_path": "plain"}) is False
    assert matcher.matches({"command_line": "cat notes.md"}) is False

    with pytest.raises(PermissionGrantError, match="one POSIX filename extension"):
        ArgumentMatcher("file_path", MatchOperator.SUFFIX, "md")
    with pytest.raises(PermissionGrantError, match="one POSIX filename extension"):
        ArgumentMatcher("file_path", MatchOperator.SUFFIX, ".tar.gz")
    with pytest.raises(PermissionGrantError, match="one POSIX filename extension"):
        ArgumentMatcher("file_path", MatchOperator.SUFFIX, "../md")
    with pytest.raises(PermissionGrantError, match="path-like string arguments"):
        ArgumentMatcher("content", MatchOperator.SUFFIX, ".md")


def test_permission_rule_file_refuses_corruption_and_future_versions(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    path = grants_path()
    path.parent.mkdir(parents=True)

    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(PermissionGrantError, match="cannot read"):
        load_permission_rules(workspace)

    path.write_text('{"version": 999, "workspaces": {}}', encoding="utf-8")
    with pytest.raises(PermissionGrantError, match="newer than supported"):
        load_permission_rules(workspace)


def test_exact_scope_never_silently_drops_large_non_content_arguments() -> None:
    with pytest.raises(PermissionGrantError, match="exceeds 8 KiB"):
        build_exact_scope_matchers(
            {"resource": "x" * 9000, "content": "bulk payload is intentionally omitted"}
        )
