"""Verify the Codex comparator never trusts agent prose or mutated tests."""

from __future__ import annotations

import contextlib
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
    run_task,
    summarize_codex_events,
)
import codex_repository_benchmark as runner  # noqa: E402
from repository_benchmark import RepositoryTask, shared_user_prompt  # noqa: E402


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
    path.write_text("".join(json.dumps(event) + "\n" for event in [
        {"type": "thread.started", "thread_id": "test"},
        {"type": "turn.started"},
        {"type": "error", "message": "workspace routing discovery timed out"},
        {"type": "item.completed", "item": {
            "type": "error", "text": "PRIVATE-AUTH-DATA",
        }},
        {"type": "error", "message": "workspace routing discovery failed"},
    ]))
    result = summarize_codex_events(path, "python3 -B qa_visible.py")
    assert result["infrastructure_unavailable"] is True
    assert result["shell_command_count"] == 0
    assert "PRIVATE-AUTH-DATA" not in repr(result)


def test_codex_routing_error_after_work_does_not_invalidate_trial(
    tmp_path: Path,
) -> None:
    path = tmp_path / "codex.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in [
        {"type": "error", "message": "workspace routing discovery failed"},
        {"type": "item.completed", "item": {
            "type": "command_execution", "command": "pwd", "exit_code": 0,
            "status": "completed",
        }},
    ]))
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
    env = _codex_env(tmp_path, isolated)
    assert env["TMPDIR"] == str(tmp_path / "tmp")
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
    assert "workspace-write" in child_args
    assert "PRIVATE-SENTINEL" not in repr(child_env)
    assert child_env["CODEX_HOME"] == str(tmp_path / "isolated-codex-home")
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
