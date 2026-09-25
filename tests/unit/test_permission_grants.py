import json
from pathlib import Path

import pytest

from ash.safety.grants import (
    ArgumentMatcher,
    MAX_RULE_FILE_BYTES,
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
    assert payload["version"] == 3
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


def test_permission_rule_composes_multiple_matchers_for_one_argument() -> None:
    rule = PermissionRule.create(
        RuleEffect.ALLOW,
        "write_file",
        [
            ArgumentMatcher("file_path", MatchOperator.PATH_PREFIX, "docs"),
            ArgumentMatcher("file_path", MatchOperator.SUFFIX, ".md"),
        ],
    )

    assert rule.matches("write_file", {"file_path": "docs/guide.md"}) is True
    assert rule.matches("write_file", {"file_path": "docs/guide.txt"}) is False
    assert rule.matches("write_file", {"file_path": "README.md"}) is False
    assert PermissionRule.from_payload(rule.as_payload()) == rule


def test_path_glob_matcher_is_whole_value_and_traversal_safe() -> None:
    matcher = ArgumentMatcher(
        "file_path", MatchOperator.PATH_GLOB, "packages/*/README.md"
    )
    single = ArgumentMatcher(
        "file_path", MatchOperator.PATH_GLOB, "packages/pkg?/README.md"
    )

    assert matcher.matches({"file_path": "packages/app/README.md"}) is True
    assert matcher.matches({"file_path": "./packages/app/docs/README.md"}) is True
    assert matcher.matches({"file_path": "packages/app/README.txt"}) is False
    assert matcher.matches({"file_path": "../packages/app/README.md"}) is False
    assert single.matches({"file_path": "packages/pkg1/README.md"}) is True
    assert single.matches({"file_path": "packages/pkg12/README.md"}) is False

    for invalid in ("/packages/*", "../packages/*", r"packages\*\README.md"):
        with pytest.raises(PermissionGrantError, match="path_glob"):
            ArgumentMatcher("file_path", MatchOperator.PATH_GLOB, invalid)
    with pytest.raises(PermissionGrantError, match="path arguments"):
        ArgumentMatcher("content", MatchOperator.PATH_GLOB, "packages/*")


def test_version_two_permission_rules_remain_readable_and_upgrade_on_write(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    path = grants_path()
    path.parent.mkdir(parents=True)
    legacy_rule = PermissionRule.create(
        RuleEffect.ALLOW,
        "read_file",
        [ArgumentMatcher("file_path", MatchOperator.PATH_PREFIX, "docs")],
    )
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "workspaces": {str(workspace.resolve()): [legacy_rule.as_payload()]},
            }
        ),
        encoding="utf-8",
    )

    assert load_permission_rules(workspace) == [legacy_rule]
    add_permission_rule(
        workspace,
        PermissionRule.create(RuleEffect.DENY, "run_command"),
    )
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 3


def test_domain_matcher_accepts_urls_and_hostnames_only() -> None:
    wildcard = ArgumentMatcher("url", MatchOperator.DOMAIN, "*.Example.COM")
    exact = ArgumentMatcher("url", MatchOperator.DOMAIN, "api.example.com")
    domain_matcher = ArgumentMatcher("domain", MatchOperator.DOMAIN, "docs.example.com")

    assert wildcard.matches({"url": "https://api.example.com/path"}) is True
    assert wildcard.matches({"url": "https://example.com/path"}) is False
    assert wildcard.matches({"url": "https://badexample.com/path"}) is False
    assert exact.matches({"url": "https://api.example.com/path"}) is True
    assert exact.matches({"url": "https://evil.api.example.com/path"}) is False
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

    path.write_text('{"version": true, "workspaces": {}}', encoding="utf-8")
    with pytest.raises(PermissionGrantError, match="version is invalid"):
        load_permission_rules(workspace)


def test_permission_rule_file_rejects_duplicate_json_keys(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    path = grants_path()
    path.parent.mkdir(parents=True)
    rule = PermissionRule.create(RuleEffect.ALLOW, "run_command")
    path.write_text(
        '{"version":2,"workspaces":{},"workspaces":{'
        + json.dumps(str(workspace.resolve()))
        + ":["
        + json.dumps(rule.as_payload(), separators=(",", ":"))
        + "]}}",
        encoding="utf-8",
    )

    with pytest.raises(PermissionGrantError, match="duplicate JSON object key"):
        load_permission_rules(workspace)


def test_permission_rule_file_rejects_oversized_payload(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    path = grants_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(b" " * (MAX_RULE_FILE_BYTES + 1))

    with pytest.raises(PermissionGrantError, match="exceeds 1 MB"):
        load_permission_rules(workspace)


def test_permission_rule_file_rejects_symlinked_user_state_root(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    try:
        (home / ".ash").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "repo"
    workspace.mkdir()

    with pytest.raises(PermissionGrantError, match="symlink or junction"):
        set_tool_grant(workspace, "run_command", True)

    assert not (outside / "permission-grants.json").exists()


def test_permission_rule_read_rejects_parent_swapped_after_validation(
    tmp_path, monkeypatch
) -> None:
    import ash.safety.grants as grants_module

    home = tmp_path / "home"
    state_dir = home / ".ash"
    state_dir.mkdir(parents=True)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    rule = PermissionRule.create(RuleEffect.ALLOW, "run_command")
    (outside / "permission-grants.json").write_text(
        json.dumps(
            {
                "version": 3,
                "workspaces": {
                    str(workspace.resolve()): [rule.as_payload()],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    real_open = grants_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == state_dir and not swapped:
            swapped = True
            state_dir.rename(home / ".ash-real")
            try:
                state_dir.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return directory

    monkeypatch.setattr(
        grants_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises(PermissionGrantError):
        load_permission_rules(workspace)
    assert swapped is True


def test_permission_rule_read_rejects_plain_parent_directory_swap(
    tmp_path, monkeypatch
) -> None:
    import ash.safety.grants as grants_module

    home = tmp_path / "home"
    state_dir = home / ".ash"
    saved_dir = home / ".ash-original"
    replacement_dir = tmp_path / "replacement"
    state_dir.mkdir(parents=True)
    replacement_dir.mkdir()
    workspace = tmp_path / "repo"
    workspace.mkdir()
    forged = PermissionRule.create(RuleEffect.ALLOW, "run_command")
    (replacement_dir / "permission-grants.json").write_text(
        json.dumps(
            {
                "version": 3,
                "workspaces": {
                    str(workspace.resolve()): [forged.as_payload()],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    real_open = grants_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == state_dir and not swapped:
            swapped = True
            state_dir.rename(saved_dir)
            replacement_dir.rename(state_dir)
        return directory

    monkeypatch.setattr(
        grants_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises(PermissionGrantError):
        load_permission_rules(workspace)

    assert swapped is True
    assert not (saved_dir / "permission-grants.json").exists()


def test_permission_rule_write_rejects_parent_swapped_after_validation(
    tmp_path, monkeypatch
) -> None:
    import ash.safety.grants as grants_module

    home = tmp_path / "home"
    state_dir = home / ".ash"
    state_dir.mkdir(parents=True)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "permission-grants.json"
    victim.write_text("DO NOT TOUCH\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    real_open = grants_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == state_dir and not swapped:
            swapped = True
            state_dir.rename(home / ".ash-real")
            try:
                state_dir.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return directory

    monkeypatch.setattr(
        grants_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises(PermissionGrantError):
        add_permission_rule(
            workspace,
            PermissionRule.create(RuleEffect.ALLOW, "run_command"),
        )
    assert swapped is True
    assert victim.read_text(encoding="utf-8") == "DO NOT TOUCH\n"


def test_permission_rule_write_rejects_plain_parent_directory_swap(
    tmp_path, monkeypatch
) -> None:
    import ash.safety.grants as grants_module

    home = tmp_path / "home"
    state_dir = home / ".ash"
    saved_dir = home / ".ash-original"
    replacement_dir = tmp_path / "replacement"
    state_dir.mkdir(parents=True)
    replacement_dir.mkdir()
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (replacement_dir / "permission-grants.json").write_text(
        json.dumps({"version": 3, "workspaces": {}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    real_open = grants_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == state_dir and not swapped:
            swapped = True
            state_dir.rename(saved_dir)
            replacement_dir.rename(state_dir)
        return directory

    monkeypatch.setattr(
        grants_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises(PermissionGrantError):
        add_permission_rule(
            workspace,
            PermissionRule.create(RuleEffect.ALLOW, "run_command"),
        )

    assert swapped is True
    visible = json.loads(
        (state_dir / "permission-grants.json").read_text(encoding="utf-8")
    )
    assert visible == {"version": 3, "workspaces": {}}
    assert not (saved_dir / "permission-grants.json").exists()


def test_permission_rule_write_rejects_parent_swap_before_publication(
    tmp_path, monkeypatch
) -> None:
    import ash.safety.grants as grants_module

    home = tmp_path / "home"
    state_dir = home / ".ash"
    saved_dir = home / ".ash-original"
    replacement_dir = tmp_path / "replacement"
    state_dir.mkdir(parents=True)
    replacement_dir.mkdir()
    workspace = tmp_path / "repo"
    workspace.mkdir()
    victim = replacement_dir / "permission-grants.json"
    victim.write_text(
        json.dumps({"version": 3, "workspaces": {}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    real_rename = grants_module.AnchoredDirectory.rename
    swapped = False

    def rename_then_swap(self, source, destination, **kwargs):
        nonlocal swapped
        if destination == "permission-grants.json" and not swapped:
            swapped = True
            state_dir.rename(saved_dir)
            replacement_dir.rename(state_dir)
        return real_rename(self, source, destination, **kwargs)

    monkeypatch.setattr(
        grants_module.AnchoredDirectory,
        "rename",
        rename_then_swap,
    )

    with pytest.raises(PermissionGrantError):
        add_permission_rule(
            workspace,
            PermissionRule.create(RuleEffect.ALLOW, "run_command"),
        )

    assert swapped is True
    assert json.loads(
        (state_dir / "permission-grants.json").read_text(encoding="utf-8")
    ) == {"version": 3, "workspaces": {}}
    assert not (saved_dir / "permission-grants.json").exists()


def test_permission_rule_concurrent_process_writers_preserve_both_updates(
    tmp_path, monkeypatch
) -> None:
    import os
    import subprocess
    import sys
    import time

    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    ready = tmp_path / "writer-a-ready"
    release = tmp_path / "writer-a-release"
    writer_b_started = tmp_path / "writer-b-started"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    environment = dict(os.environ)

    writer_a = "\n".join(
        [
            "import sys, time",
            "from pathlib import Path",
            "import ash.safety.grants as g",
            "from ash.safety.grants import PermissionRule, RuleEffect",
            "workspace, ready, release = map(Path, sys.argv[1:4])",
            "original = g._read_user_payload",
            "def blocked_read(directory, name):",
            "    payload = original(directory, name)",
            "    ready.write_text('ready', encoding='utf-8')",
            "    deadline = time.time() + 10",
            "    while not release.exists():",
            "        if time.time() >= deadline:",
            "            raise RuntimeError('timed out waiting to release writer A')",
            "        time.sleep(0.01)",
            "    return payload",
            "g._read_user_payload = blocked_read",
            "g.add_permission_rule(workspace, PermissionRule.create(RuleEffect.ALLOW, 'run_command'))",
        ]
    )
    writer_b = "\n".join(
        [
            "import sys",
            "from pathlib import Path",
            "from ash.safety.grants import PermissionRule, RuleEffect, add_permission_rule",
            "workspace, started = map(Path, sys.argv[1:3])",
            "started.write_text('started', encoding='utf-8')",
            "add_permission_rule(workspace, PermissionRule.create(RuleEffect.ALLOW, 'read_file'))",
        ]
    )

    first = subprocess.Popen(
        [sys.executable, "-c", writer_a, str(workspace), str(ready), str(release)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            if first.poll() is not None:
                break
            time.sleep(0.01)
        assert ready.exists(), first.communicate(timeout=1)

        second = subprocess.Popen(
            [sys.executable, "-c", writer_b, str(workspace), str(writer_b_started)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while not writer_b_started.exists() and time.monotonic() < deadline:
            if second.poll() is not None:
                break
            time.sleep(0.01)
        assert writer_b_started.exists(), second.communicate(timeout=1)
        assert second.poll() is None

        release.write_text("release", encoding="utf-8")
        first_stdout, first_stderr = first.communicate(timeout=10)
        second_stdout, second_stderr = second.communicate(timeout=10)
        assert first.returncode == 0, (first_stdout, first_stderr)
        assert second.returncode == 0, (second_stdout, second_stderr)
    finally:
        release.write_text("release", encoding="utf-8")
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    assert {rule.tool_name for rule in load_permission_rules(workspace)} == {
        "run_command",
        "read_file",
    }


def test_permission_rule_stale_lock_cleanup_rejects_parent_swap(
    tmp_path, monkeypatch
) -> None:
    import os
    import time as time_module
    import ash.safety.grants as grants_module

    home = tmp_path / "home"
    state_dir = home / ".ash"
    state_dir.mkdir(parents=True)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    lock_path = state_dir / "permission-grants.json.lock"
    lock_path.write_text("stale\n", encoding="utf-8")
    os.utime(lock_path, (0, 0))
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "permission-grants.json.lock"
    victim.write_text("DO NOT TOUCH\n", encoding="utf-8")
    os.utime(victim, (0, 0))
    monkeypatch.setenv("HOME", str(home))
    real_time = time_module.time
    swapped = False

    def time_then_swap():
        nonlocal swapped
        now = real_time()
        if not swapped:
            swapped = True
            state_dir.rename(home / ".ash-real")
            try:
                state_dir.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return now

    monkeypatch.setattr(grants_module.time, "time", time_then_swap)

    with pytest.raises(PermissionGrantError):
        add_permission_rule(
            workspace,
            PermissionRule.create(RuleEffect.ALLOW, "run_command"),
        )
    assert swapped is True
    assert victim.read_text(encoding="utf-8") == "DO NOT TOUCH\n"


def test_permission_rule_lock_does_not_consume_body_file_exists(
    tmp_path, monkeypatch
) -> None:
    import ash.safety.grants as grants_module

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = grants_path()
    path.parent.mkdir(parents=True)

    with grants_module.AnchoredDirectory.open(
        path.parent,
        create=False,
        private=False,
        pin_path=True,
    ) as directory:
        with pytest.raises(FileExistsError, match="body failure"):
            with grants_module._locked_rule_entry(directory, path.name):
                raise FileExistsError("body failure")

    assert not path.with_suffix(path.suffix + ".lock").exists()


def test_exact_scope_never_silently_drops_large_non_content_arguments() -> None:
    with pytest.raises(PermissionGrantError, match="exceeds 8 KiB"):
        build_exact_scope_matchers(
            {"resource": "x" * 9000, "content": "bulk payload is intentionally omitted"}
        )
