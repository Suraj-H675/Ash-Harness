"""Verify the Codex comparator never trusts agent prose or mutated tests."""

from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

_devtools = Path(__file__).resolve().parents[2] / "devtools"
if str(_devtools) not in sys.path:
    sys.path.insert(0, str(_devtools))

from codex_repository_benchmark import (  # noqa: E402
    _codex_env,
    _exact_test_command,
    _isolated_codex_profile,
    _native_codex_binary,
    _sealed_codex_argv,
    SEALED_PERMISSION_PROFILE,
    run_task,
    summarize_codex_events,
)
import codex_repository_benchmark as runner  # noqa: E402
import repository_benchmark as ash_runner  # noqa: E402
from repository_benchmark import RepositoryTask, shared_user_prompt  # noqa: E402


def test_sealed_plain_codex_argv_has_managed_profile_without_legacy_override(
    tmp_path: Path,
) -> None:
    """Preparing a command must not silently restore the broad host-read mode."""
    argv = _sealed_codex_argv(
        Path("/trusted/native/codex"),
        tmp_path,
        model="gpt-6-luna",
        effort="xhigh",
        context_tokens=32768,
    )
    assert argv[:2] == ["/trusted/native/codex", "exec"]
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "--ephemeral" in argv
    assert "-s" not in argv and "--sandbox" not in argv
    assert "workspace-write" not in argv
    assert "danger-full-access" not in argv
    assert "sandbox_workspace_write.network_access=false" not in argv
    assert f'default_permissions="{SEALED_PERMISSION_PROFILE}"' in argv
    assert 'approval_policy="never"' in argv
    assert 'model_reasoning_effort="xhigh"' in argv
    assert "model_context_window=32768" in argv
    assert "features.skip_host_skill_discovery=true" in argv
    assert "features.skill_search=false" in argv
    assert argv[-1] == "-"
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    assert argv[argv.index("-m") + 1] == "gpt-6-luna"


@pytest.mark.parametrize(
    ("command", "matches"),
    [
        ("python3 -B qa_visible.py", True),
        ("/usr/bin/zsh -lc 'python3 -B qa_visible.py'", True),
        ("/bin/bash -c 'python3 -B qa_visible.py'", True),
        ("python3 -B qa_visible.py && echo forged", False),
        ("/usr/bin/zsh -lc 'python3 -B qa_visible.py; rm -f source.py'", False),
        ("echo python3 -B qa_visible.py", False),
        ("python3 -B other.py", False),
        ("python3 -B qa_visible.py'", False),
    ],
)
def test_only_exact_visible_test_command_counts(command: str, matches: bool) -> None:
    assert _exact_test_command(command, "python3 -B qa_visible.py") is matches


def test_plain_codex_uses_identical_neutral_prompt_as_ash() -> None:
    task = runner.TASKS["h11-chunk-footer"]
    prompt = shared_user_prompt(task)
    assert prompt.startswith("Repair a real HTTP")
    assert "h11/_readers.py" in prompt
    assert "python3 -B qa_visible.py" in prompt
    assert "run_command" not in prompt
    assert "Use poteto-mode" not in prompt
    assert "Read-only source inspection commands" in prompt


def test_pytest_mro_task_uses_shared_identity_prompt_and_scoring() -> None:
    task = runner.TASKS["pytest-mro-marker-precedence"]
    assert runner.TASKS is ash_runner.TASKS
    assert runner.preflight is ash_runner.preflight
    assert runner.check_oracles is ash_runner.check_oracles
    assert runner.check_edits is ash_runner.check_edits
    assert runner.shared_user_prompt is ash_runner.shared_user_prompt
    assert task.command_grant == "/usr/bin/python3 -B qa_visible.py"
    assert _exact_test_command(task.command_grant, task.command_grant)
    assert not _exact_test_command(
        task.command_grant + " && echo forged", task.command_grant
    )
    assert shared_user_prompt(task) == ash_runner.shared_user_prompt(task)
    assert shared_user_prompt(task) == task.prompt.replace(
        "with run_command", "using the available command tool"
    ).replace(
        "No other commands are authorized.",
        "Read-only source inspection commands are allowed. "
        "Do not run other tests or shell commands that modify files.",
    )


def test_event_summary_records_only_metadata_not_model_content(tmp_path: Path) -> None:
    path = tmp_path / "codex.jsonl"
    records = [
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "/usr/bin/zsh -lc 'python3 -B qa_visible.py'",
                "exit_code": 0,
                "status": "completed",
                "aggregated_output": "PRIVATE-SOURCE-CONTENT",
            },
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 200, "output_tokens": 12, "secret": "PRIVATE"},
        },
    ]
    path.write_text("".join(json.dumps(x) + "\n" for x in records))
    result = summarize_codex_events(path, "python3 -B qa_visible.py")
    assert result["visible_test_command_executed"] is True
    assert result["shell_command_count"] == 1
    assert result["shell_non_test_commands"] == 0
    assert result["terminal_events"] == ["turn.completed"]
    assert result["usage"] == {"input_tokens": 200, "output_tokens": 12}
    assert result["infrastructure_unavailable"] is False
    assert "PRIVATE" not in repr(result)


def test_codex_routing_outage_is_not_counted_as_coding_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "codex.jsonl"
    path.write_text(
        "".join(
            json.dumps(event) + "\n"
            for event in [
                {"type": "thread.started", "thread_id": "test"},
                {"type": "turn.started"},
                {"type": "error", "message": "workspace routing discovery timed out"},
                {
                    "type": "item.completed",
                    "item": {
                        "type": "error",
                        "text": "PRIVATE-AUTH-DATA",
                    },
                },
                {"type": "error", "message": "workspace routing discovery failed"},
            ]
        )
    )
    result = summarize_codex_events(path, "python3 -B qa_visible.py")
    assert result["infrastructure_unavailable"] is True
    assert result["shell_command_count"] == 0
    assert "PRIVATE-AUTH-DATA" not in repr(result)


def test_codex_routing_error_after_work_does_not_invalidate_trial(
    tmp_path: Path,
) -> None:
    path = tmp_path / "codex.jsonl"
    path.write_text(
        "".join(
            json.dumps(event) + "\n"
            for event in [
                {"type": "error", "message": "workspace routing discovery failed"},
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "pwd",
                        "exit_code": 0,
                        "status": "completed",
                    },
                },
            ]
        )
    )
    result = summarize_codex_events(path, "python3 -B qa_visible.py")
    assert result["infrastructure_unavailable"] is False
    assert result["shell_command_count"] == 1


def test_codex_env_does_not_inherit_api_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "PRIVATE-API-KEY")
    monkeypatch.setenv("ASH_OPENROUTER_KEY", "PRIVATE-KEY")
    monkeypatch.setenv("GITHUB_TOKEN", "PRIVATE-GITHUB-TOKEN")
    isolated = tmp_path / "isolated-codex"
    isolated.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private_tmp = workspace / ".ash-private-tmp"
    private_tmp.mkdir(mode=0o700)
    env = _codex_env(tmp_path, isolated, sandbox_tmp=private_tmp)
    assert env["TMPDIR"] == str(private_tmp)
    assert env["CODEX_HOME"] == str(isolated)
    assert env["HOME"] == str(tmp_path / "home")
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "xdg-config")
    assert "CODEX_ACCOUNT_OVERLAY_ACCOUNT" not in env
    assert not (tmp_path / "home" / ".codex").exists()
    assert "OPENAI_API_KEY" not in env
    assert "ASH_OPENROUTER_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_isolated_codex_auth_is_memory_scoped_and_never_mutates_original(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "source-auth.json"
    data = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "ID-SECRET",
            "access_token": "ACCESS-SECRET",
            "refresh_token": "REFRESH-SECRET",
            "account_id": "ACCOUNT-SECRET",
        },
    }
    origin.write_text(json.dumps(data))
    before = origin.read_bytes()
    with _isolated_codex_profile(origin, memory_root=tmp_path) as private_home:
        private_root = private_home.parent
        assert private_root.exists()
        assert set(x.name for x in private_home.iterdir()) == {"auth.json"}
        assert private_home != origin.parent
        assert (private_home / "auth.json").read_bytes() == before
        assert (private_home / "auth.json").stat().st_mode & 0o777 == 0o600
        (private_home / "auth.json").write_text("updated-in-isolation")
        assert origin.read_bytes() == before
    assert not private_root.exists()
    assert origin.read_bytes() == before


def test_isolated_codex_auth_refuses_symlinks_and_missing_credentials(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original.json"
    original.write_text('{"auth_mode":"chatgpt","tokens":{}}')
    alias = tmp_path / "alias.json"
    alias.symlink_to(original)
    with pytest.raises(OSError):
        with _isolated_codex_profile(alias, memory_root=tmp_path):
            pytest.fail("symlinked Codex auth must not be accepted")
    with pytest.raises(ValueError, match="existing ChatGPT-based Codex login"):
        with _isolated_codex_profile(original, memory_root=tmp_path):
            pytest.fail("incomplete Codex auth must not be accepted")


def test_native_codex_resolver_refuses_non_native_account_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = tmp_path / "codex-real"
    wrapper.write_text("#!/usr/bin/python3\nprint('overlay')\n")
    wrapper.chmod(0o700)
    monkeypatch.setattr(runner.shutil, "which", lambda _command: str(wrapper))
    with pytest.raises(RuntimeError, match="not a native executable"):
        _native_codex_binary()


def test_native_codex_resolver_accepts_stock_binary_without_custom_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = tmp_path / "codex"
    native.write_bytes(b"\x7fELF" + b"\x00" * 12)
    native.chmod(0o700)
    monkeypatch.setattr(
        runner.shutil,
        "which",
        lambda name: str(native) if name == "codex" else None,
    )
    assert _native_codex_binary() == native


@pytest.mark.parametrize(
    ("system", "architecture", "expected_denial"),
    [
        ("linux", "x86_64", "not independently attested"),
        ("darwin", "arm64", "need Linux x86_64 attestation"),
    ],
)
def test_unsealed_plain_codex_model_dispatch_fails_before_host_or_auth_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    architecture: str,
    expected_denial: str,
) -> None:
    """Both missing proof and unsupported macOS must refuse before auth or model."""
    monkeypatch.setattr(runner.sys, "platform", system)
    monkeypatch.setattr(runner.platform, "machine", lambda: architecture)

    def unexpected_call(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unsealed dispatch reached native binary, profile, or process")

    monkeypatch.setattr(runner, "_native_codex_binary", unexpected_call)
    monkeypatch.setattr(runner, "_isolated_codex_profile", unexpected_call)
    monkeypatch.setattr(runner.subprocess, "Popen", unexpected_call)
    private = tmp_path / "attestation"
    private.mkdir(mode=0o700)
    monkeypatch.setattr(runner, "_LOCAL_ATTESTATION", private / "absent.json")
    with pytest.raises(RuntimeError, match=expected_denial):
        runner._run_codex(
            tmp_path / "task-workspace",
            tmp_path / "evidence",
            model="gpt-6-luna",
            effort="xhigh",
            timeout=40,
            prompt="ordinary benchmark repair",
            scratch=tmp_path,
        )


def test_unsealed_plain_codex_cli_refuses_before_making_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = tmp_path / "should-not-exist"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "codex_repository_benchmark.py",
            "--task",
            "h11-chunk-footer",
            "--evidence-dir",
            str(evidence),
        ],
    )
    monkeypatch.setattr(
        runner,
        "_native_codex_binary",
        lambda: pytest.fail("unsealed CLI attempted native model setup"),
    )
    with pytest.raises(SystemExit) as exit_info:
        runner.main()
    assert exit_info.value.code == 2
    assert "require --authorize-model-trial" in capsys.readouterr().err
    assert not evidence.exists()


def test_codex_timeout_terminates_process_group_without_leaking_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_args: list[str] = []
    child_env: dict[str, str] = {}
    signals: list[int] = []

    class Child:
        pid = 12345
        returncode: int | None = None

        def communicate(self, *, input: bytes, timeout: int) -> None:
            assert input.startswith(b"Repair the bug")
            assert timeout == 50
            raise subprocess.TimeoutExpired("codex", timeout)

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, *, timeout: int) -> int:
            assert timeout == 4
            self.returncode = -15
            return -15

    def popen(argv, *, env, **kwargs):
        child_args.extend(argv)
        child_env.update(env)
        assert kwargs["start_new_session"] is True
        return Child()

    @contextlib.contextmanager
    def fake_profile():
        isolated = tmp_path / "isolated-codex-home"
        isolated.mkdir()
        yield isolated

    # This simulated child exercises process-group cleanup, not actual model
    # authorization or sandbox isolation. Security checks are stubbed only here.
    monkeypatch.setattr(runner, "_require_sealed_comparator_profile", lambda: None)
    monkeypatch.setattr(
        runner, "_probe_effective_sealed_sandbox", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        runner, "_native_codex_binary", lambda: Path("/usr/bin/codex-real")
    )
    monkeypatch.setattr(runner, "_isolated_codex_profile", fake_profile)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: signals.append(sig))
    monkeypatch.setenv("OPENAI_API_KEY", "PRIVATE-SENTINEL")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (tmp_path / "tmp").mkdir()
    receipt = runner._run_codex(
        tmp_path,
        evidence,
        model="gpt-6-luna",
        effort="xhigh",
        timeout=50,
        prompt="Repair the bug",
        scratch=tmp_path,
    )
    assert receipt["timed_out"] is True
    assert receipt["exit_code"] == -15
    assert signals == [runner.signal.SIGTERM]
    assert "--ephemeral" in child_args
    assert "--ignore-user-config" in child_args
    assert "--ignore-rules" in child_args
    assert "features.skip_host_skill_discovery=true" in child_args
    assert "features.skill_search=false" in child_args
    assert "model_context_window=32768" in child_args
    assert 'default_permissions="ash-benchmark-sealed"' in child_args
    assert "-s" not in child_args and "--sandbox" not in child_args
    assert "PRIVATE-SENTINEL" not in repr(child_env)
    assert child_env["CODEX_HOME"] == str(tmp_path / "isolated-codex-home")
    assert Path(child_env["TMPDIR"]).parent == tmp_path
    assert Path(child_env["TMPDIR"]).name.startswith(".ash-codex-tmp-")
    assert not Path(child_env["TMPDIR"]).exists(), "temporary workspace scratch leaked"
    assert child_args[0] == "/usr/bin/codex-real"


@pytest.mark.parametrize("tamper_visible", [False, True])
def test_scoring_rejects_modified_oracle_and_cleans_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper_visible: bool
) -> None:
    task = RepositoryTask(
        source="local-fixture",
        upstream="https://example.invalid/example.git",
        base_sha="a" * 40,
        reference_fix_sha="b" * 40,
        editable=frozenset({"source.py"}),
        visible_script="qa_visible.py",
        visible_source="assert False\n",
        hidden_source="assert True\n",
        command_grant="python3 -B qa_visible.py",
        prompt="Fix source.py, then run exactly python3 -B qa_visible.py with run_command.",
    )
    monkeypatch.setitem(runner.TASKS, "fixture", task)
    scratch_roots: list[Path] = []

    def preflight(_task: RepositoryTask, scratch: Path):
        scratch_roots.append(scratch)
        base = scratch / "baseline"
        base.mkdir()
        (base / "source.py").write_text("BROKEN")
        (base / "qa_visible.py").write_text("assert True\n")
        reference = scratch / "reference"
        reference.mkdir()
        (reference / "source.py").write_text("REFERENCE-SOLUTION")
        return base, {
            "base_visible_exit": 1,
            "base_hidden_exit": 1,
            "reference_visible_exit": 0,
            "reference_hidden_exit": 0,
        }

    def run_codex(workspace: Path, evidence: Path, **_kwargs):
        assert not (workspace.parent / "reference").exists()
        (workspace / "source.py").write_text("FIXED")
        if tamper_visible:
            (workspace / "qa_visible.py").write_text("assert 'tampered'\n")
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "/usr/bin/zsh -lc 'python3 -B qa_visible.py'",
                    "status": "completed",
                    "exit_code": 0,
                },
            },
            {"type": "turn.completed", "usage": {"input_tokens": 80}},
        ]
        (evidence / "codex.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in events)
        )
        return {
            "exit_code": 0,
            "timed_out": False,
            "cli_seconds": 10.1,
            "codex_model": "gpt-6-luna",
            "codex_reasoning_effort": "xhigh",
            "sandbox": "workspace-write",
            "ephemeral": True,
            "native_binary_direct": True,
            "isolated_codex_home": True,
            "isolated_home_xdg": True,
            "memory_backed_auth_copy_cleaned": True,
        }

    monkeypatch.setattr(runner, "preflight", preflight)
    monkeypatch.setattr(runner, "_run_codex", run_codex)
    monkeypatch.setattr(
        runner,
        "check_oracles",
        lambda *_args: {
            "correct": True,
            "visible_oracle_exit": 0,
            "hidden_oracle_exit": 0,
        },
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    result = run_task(
        "fixture",
        evidence=evidence,
        model="gpt-6-luna",
        effort="xhigh",
        timeout=170,
        authorize_model_trial=True,  # mock executor only; no model call
    )
    assert result["passed"] is not tamper_visible
    assert result["protected_files_unchanged"] is not tamper_visible
    assert result["visible_test_command_executed"] is True
    assert result["scratch_removed"] is True
    assert result["context_tokens_requested"] == 32768
    assert not scratch_roots[0].exists()
    assert (evidence / "grade.json").is_file()
    assert (evidence / "manifest.json").is_file()
    assert (evidence / "prompt.txt").read_text() == shared_user_prompt(task)
    assert result["runner"] == "plain-codex-cli"
    assert result["host_skill_discovery_disabled_requested"] is True


def test_codex_scoring_accepts_new_editable_source_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = RepositoryTask(
        source="local-refactor-fixture",
        upstream="https://example.invalid/refactor.git",
        base_sha="c" * 40,
        reference_fix_sha="d" * 40,
        editable=frozenset({"src/pkg/_hooks.py", "src/pkg/_caller.py"}),
        visible_script="qa_visible.py",
        visible_source="assert True\n",
        hidden_source="assert True\n",
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Split hook roles into src/pkg/_caller.py while preserving "
            "src/pkg/_hooks.py; run exactly python3 -B qa_visible.py with run_command."
        ),
    )
    monkeypatch.setitem(runner.TASKS, "new-module-fixture", task)

    def preflight(_task: RepositoryTask, scratch: Path):
        base = scratch / "baseline"
        base.mkdir()
        (scratch / "reference").mkdir()
        package = base / "src/pkg"
        package.mkdir(parents=True)
        (package / "_hooks.py").write_text("old combined module")
        (base / "qa_visible.py").write_text(task.visible_source)
        return base, {
            "base_visible_exit": 1,
            "base_hidden_exit": 1,
            "reference_visible_exit": 0,
            "reference_hidden_exit": 0,
            "reference_added_paths": ["src/pkg/_caller.py"],
            "reference_removed_paths": [],
            "reference_modified_paths": ["src/pkg/_hooks.py"],
        }

    def run_codex(workspace: Path, evidence: Path, **_kwargs):
        assert not (workspace.parent / "reference").exists()
        (workspace / "src/pkg/_hooks.py").write_text("compatibility shim")
        (workspace / "src/pkg/_caller.py").write_text("new caller module")
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "/usr/bin/zsh -lc 'python3 -B qa_visible.py'",
                    "status": "completed",
                    "exit_code": 0,
                },
            },
            {"type": "turn.completed", "usage": {"input_tokens": 80}},
        ]
        (evidence / "codex.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in events)
        )
        return {
            "exit_code": 0,
            "timed_out": False,
            "cli_seconds": 8.0,
            "codex_model": "gpt-6-luna",
            "codex_reasoning_effort": "xhigh",
            "sandbox": "workspace-write",
            "ephemeral": True,
            "native_binary_direct": True,
            "isolated_codex_home": True,
            "isolated_home_xdg": True,
            "memory_backed_auth_copy_cleaned": True,
        }

    monkeypatch.setattr(runner, "preflight", preflight)
    monkeypatch.setattr(runner, "_run_codex", run_codex)
    monkeypatch.setattr(
        runner,
        "check_oracles",
        lambda *_args: {
            "correct": True,
            "visible_oracle_exit": 0,
            "hidden_oracle_exit": 0,
        },
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    result = run_task(
        "new-module-fixture",
        evidence=evidence,
        model="gpt-6-luna",
        effort="xhigh",
        timeout=170,
        authorize_model_trial=True,  # mock executor only; no model call
    )
    assert result["passed"] is True
    assert result["protected_files_unchanged"] is True
    assert result["required_source_changed"] is True
    assert result["changed_paths"] == ["src/pkg/_caller.py", "src/pkg/_hooks.py"]
    assert result["visible_test_command_executed"] is True


def test_r17_codex_fake_grade_accepts_one_semantic_source_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_name = "pytest-mixed-indirect-fixture-precedence"
    task = runner.TASKS[task_name]
    scratch_roots: list[Path] = []

    def preflight(_task: RepositoryTask, scratch: Path):
        scratch_roots.append(scratch)
        baseline = scratch / "baseline"
        source = baseline / "src/_pytest"
        source.mkdir(parents=True)
        (source / "fixtures.py").write_text("baseline", encoding="utf-8")
        (source / "python.py").write_text("unchanged", encoding="utf-8")
        (source / "_version.py").write_text("generated", encoding="utf-8")
        overlay = baseline / ".benchmark_runtime/site/pluggy"
        overlay.mkdir(parents=True)
        (overlay / "__init__.py").write_text("locked source", encoding="utf-8")
        (baseline / task.visible_script).write_text(
            task.visible_source, encoding="utf-8"
        )
        (scratch / "reference").mkdir()
        return baseline, {
            "base_visible_exit": 2,
            "base_hidden_exit": 2,
            "reference_visible_exit": 0,
            "reference_hidden_exit": 0,
            "reference_added_paths": [],
            "runtime_overlay": {"overlay_sha256": "fixture"},
        }

    def verify_runtime(workspace: Path, _runtime: object, receipt: object) -> None:
        assert receipt == {"overlay_sha256": "fixture"}
        assert (workspace / ".benchmark_runtime/site/pluggy/__init__.py").is_file()
        assert (workspace / "src/_pytest/_version.py").is_file()

    def fake_codex(workspace: Path, evidence: Path, **_kwargs: object):
        assert not (workspace.parent / "reference").exists()
        assert all(
            task.hidden_expected_json not in path.read_text(encoding="utf-8")
            for path in workspace.rglob("*")
            if path.is_file()
        )
        (workspace / "src/_pytest/fixtures.py").write_text("repaired", encoding="utf-8")
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "/usr/bin/zsh -lc 'python3 -B qa_visible.py'",
                    "status": "completed",
                    "exit_code": 0,
                },
            },
            {"type": "turn.completed", "usage": {"input_tokens": 40}},
        ]
        (evidence / "codex.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in events),
            encoding="utf-8",
        )
        return {
            "exit_code": 0,
            "timed_out": False,
            "cli_seconds": 1.0,
            "native_binary_direct": True,
            "isolated_codex_home": True,
            "isolated_home_xdg": True,
            "memory_backed_auth_copy_cleaned": True,
        }

    monkeypatch.setattr(runner, "preflight", preflight)
    monkeypatch.setattr(runner, "verify_task_runtime", verify_runtime)
    monkeypatch.setattr(runner, "_run_codex", fake_codex)
    monkeypatch.setattr(
        runner,
        "check_oracles",
        lambda *_args: {
            "correct": True,
            "visible_oracle_exit": 0,
            "hidden_oracle_exit": 0,
        },
    )
    evidence = tmp_path / "r17-evidence"
    evidence.mkdir()
    result = run_task(
        task_name,
        evidence=evidence,
        model="gpt-6-luna",
        effort="xhigh",
        timeout=30,
        authorize_model_trial=True,  # mock executor only; no model call
    )
    assert result["passed"] is True
    assert result["required_source_changed"] is True
    assert result["visible_test_command_executed"] is True
    assert result["shell_command_policy_enforced"] is False
    assert result["scratch_removed"] is True
    assert not scratch_roots[0].exists()
    manifest = json.loads((evidence / "manifest.json").read_text())
    assert manifest["license"] == "MIT"
    assert manifest["hidden_oracle_sha256"] == runner.hidden_oracle_sha256(task)
    assert (
        manifest["hidden_probe_sha256"]
        == hashlib.sha256(task.hidden_source.encode()).hexdigest()
    )
    assert manifest["hidden_oracle_classification"] == (
        "arm-sealed-authored-parent-checker; not an independent holdout"
    )


def test_no_paid_trial_without_explicit_approval_before_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner,
        "preflight",
        lambda *_args: pytest.fail("unauthorized trial ran preflight"),
    )
    with pytest.raises(RuntimeError, match="explicit operator authorization"):
        runner.run_task(
            "h11-chunk-footer",
            evidence=tmp_path / "evidence",
            model="gpt-6-luna",
            effort="xhigh",
            timeout=40,
        )
    with pytest.raises(RuntimeError, match="GPT-6 Luna xhigh"):
        runner.run_task(
            "h11-chunk-footer",
            evidence=tmp_path / "evidence",
            model="gpt-6-something",
            effort="xhigh",
            timeout=40,
            authorize_model_trial=True,
        )


def test_sealed_metadata_rejects_symlinks_and_mode_changes(tmp_path: Path) -> None:
    metadata = tmp_path / "attestation.json"
    metadata.write_text('"synthetic"')
    metadata.chmod(0o600)
    assert runner._read_sealed_metadata(
        metadata, owner=metadata.stat().st_uid, mode=0o600
    )
    alias = tmp_path / "alias.json"
    alias.symlink_to(metadata)
    with pytest.raises(RuntimeError, match="unsafe ownership"):
        runner._read_sealed_metadata(alias, owner=metadata.stat().st_uid, mode=0o600)
    metadata.chmod(0o644)
    with pytest.raises(RuntimeError, match="unsafe mode"):
        runner._read_sealed_metadata(metadata, owner=metadata.stat().st_uid, mode=0o600)


def test_sealed_profile_requires_matching_attestation_and_managed_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import platform

    user = tmp_path / "user"
    attest_dir = user / ".local/state/ash-benchmarks"
    attest_dir.mkdir(parents=True, mode=0o700)
    attest_file = attest_dir / "codex-sealed-profile-attestation.json"
    binary = user / ".codex/packages/standalone/releases/version/bin/codex"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\x7fELFfake")
    release_root = user / ".codex/packages/standalone/releases"
    managed = tmp_path / "requirements.toml"
    lines = [
        "[allowed_permission_profiles]",
        '"ash-benchmark-sealed" = true',
        "[permissions.ash-benchmark-sealed]",
        'extends = ":workspace"',
        "[permissions.ash-benchmark-sealed.filesystem]",
        '":root" = "deny"',
        '":minimal" = "read"',
        '":tmpdir" = "deny"',
        '":slash_tmp" = "deny"',
        f'"{release_root}" = "read"',
        "[permissions.ash-benchmark-sealed.network]",
        "enabled = false",
    ]
    raw = ("\n".join(lines) + "\n").encode()
    profile = {
        "schema": 1,
        "platform": "linux",
        "machine": "x86_64",
        "profile": runner.SEALED_PERMISSION_PROFILE,
        "independent_live_tool_smoke": True,
        "live_shell_tool_count": 1,
        "required_observations": runner._REQUIRED_TOOL_OBSERVATIONS,
        "managed_policy_sha256": hashlib.sha256(raw).hexdigest(),
        "native_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }
    attest_file.write_text(json.dumps(profile))
    attest_file.chmod(0o600)
    actual_reader = runner._read_sealed_metadata

    def read(path: Path, *, owner: int, mode: int) -> bytes:
        if path == managed:
            assert (owner, mode) == (0, 0o644)
            return raw
        return actual_reader(path, owner=owner, mode=mode)

    monkeypatch.setattr(runner, "_LOCAL_ATTESTATION", attest_file)
    monkeypatch.setattr(runner, "_MANAGED_REQUIREMENTS", managed)
    monkeypatch.setattr(runner, "_read_sealed_metadata", read)
    monkeypatch.setattr(runner, "_native_codex_binary", lambda: binary)
    monkeypatch.setattr(
        runner, "_sha256_file", lambda _binary: profile["native_binary_sha256"]
    )
    monkeypatch.setattr(runner.Path, "home", lambda: user)
    monkeypatch.setattr(runner.sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    runner._require_sealed_comparator_profile()

    profile["required_observations"] = dict(profile["required_observations"])
    profile["required_observations"]["outside_grader"] = "ALLOWED"
    attest_file.write_text(json.dumps(profile))
    with pytest.raises(RuntimeError, match="proof drifted"):
        runner._require_sealed_comparator_profile()
    profile["required_observations"]["outside_grader"] = "BLOCKED"
    attest_file.write_text(json.dumps(profile))

    def bad_read(path: Path, *, owner: int, mode: int) -> bytes:
        if path == managed:
            return raw + b"\n# policy changed"
        return actual_reader(path, owner=owner, mode=mode)

    monkeypatch.setattr(runner, "_read_sealed_metadata", bad_read)
    with pytest.raises(RuntimeError, match="proof drifted"):
        runner._require_sealed_comparator_profile()
    monkeypatch.setattr(runner.sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="Linux x86_64"):
        runner._require_sealed_comparator_profile()


def test_sealed_no_model_probe_checks_actual_command_and_cleans_sentinels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    workspace = scratch / "workspace"
    auth_home = tmp_path / "private-auth"
    workspace.mkdir(parents=True)
    auth_home.mkdir()
    sandbox_tmp = workspace / ".ash-private-tmp"
    sandbox_tmp.mkdir(mode=0o700)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        assert command[1:7] == [
            "sandbox",
            "--include-managed-config",
            "-P",
            "ash-benchmark-sealed",
            "-C",
            str(workspace),
        ]
        assert command[-3] == str(scratch / ".ash-sealed-parent-sentinel")
        assert command[-2] == str(auth_home / ".ash-sealed-auth-sentinel")
        assert command[-1].endswith("devtools/codex_repository_benchmark.py")
        assert (scratch / ".ash-sealed-parent-sentinel").is_file()
        assert (auth_home / ".ash-sealed-auth-sentinel").is_file()
        assert kwargs["env"]["CODEX_HOME"] == str(auth_home)
        assert kwargs["env"]["TMPDIR"] == str(sandbox_tmp)
        return subprocess.CompletedProcess(
            command, 0, "ASH_SEALED_NO_MODEL_PROBE_OK\n", ""
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner._probe_effective_sealed_sandbox(
        Path("/trusted/codex"), workspace, scratch, auth_home,
        sandbox_tmp=sandbox_tmp,
    )
    assert len(calls) == 1
    assert not (scratch / ".ash-sealed-parent-sentinel").exists()
    assert not (auth_home / ".ash-sealed-auth-sentinel").exists()

    def wrong_result(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "UNVERIFIED", "")

    monkeypatch.setattr(runner.subprocess, "run", wrong_result)
    with pytest.raises(RuntimeError, match="probe failed"):
        runner._probe_effective_sealed_sandbox(
            Path("/trusted/codex"), workspace, scratch, auth_home,
            sandbox_tmp=sandbox_tmp,
        )
    assert not (scratch / ".ash-sealed-parent-sentinel").exists()
    assert not (auth_home / ".ash-sealed-auth-sentinel").exists()


def test_sealed_probe_refuses_tmp_outside_workspace_before_native_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    workspace = scratch / "workspace"
    workspace.mkdir(parents=True)
    private_home = scratch / "codex-home"
    private_home.mkdir()
    outside_tmp = scratch / "tmp"
    outside_tmp.mkdir()

    def no_native_dispatch(*_args: object, **_kwargs: object) -> None:
        pytest.fail("out-of-workspace TMPDIR must not reach native sandbox")

    monkeypatch.setattr(runner.subprocess, "run", no_native_dispatch)
    with pytest.raises(RuntimeError, match="sandbox temporary directory"):
        runner._probe_effective_sealed_sandbox(
            Path("/trusted/native/codex"), workspace, scratch, private_home,
            sandbox_tmp=outside_tmp,
        )
    assert not (scratch / ".ash-sealed-parent-sentinel").exists()
    assert not (private_home / ".ash-sealed-auth-sentinel").exists()


def test_explicit_opt_in_passes_one_trial_authorization_without_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "codex_repository_benchmark.py",
            "--task",
            "pytest-mro-marker-precedence",
            "--model",
            "gpt-6-luna",
            "--reasoning-effort",
            "xhigh",
            "--evidence-dir",
            str(evidence),
            "--authorize-model-trial",
        ],
    )
    monkeypatch.setattr(runner, "_require_sealed_comparator_profile", lambda: None)
    monkeypatch.setattr(
        runner, "_native_codex_binary", lambda: Path("/mock/native/codex")
    )
    monkeypatch.setattr(runner, "_chatgpt_tmpfs_root", lambda: tmp_path)
    monkeypatch.setattr(runner.shutil, "which", lambda name: f"/usr/bin/{name}")
    invoked = []

    def fake_run(task, *, evidence, **kwargs):
        invoked.append((task, evidence, kwargs))
        return {"task": task, "passed": True, "preflight": {}, "ran_model": False}

    monkeypatch.setattr(runner, "run_task", fake_run)
    assert runner.main() == 0
    assert evidence.is_dir()
    assert len(invoked) == 1
    assert invoked[0][0] == "pytest-mro-marker-precedence"
    assert invoked[0][2]["authorize_model_trial"] is True
    assert invoked[0][2]["preflight_only"] is False
    assert invoked[0][2]["model"] == "gpt-6-luna"
    assert invoked[0][2]["effort"] == "xhigh"
