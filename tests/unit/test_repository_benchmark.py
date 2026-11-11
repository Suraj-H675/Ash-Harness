"""Offline checks for sealed upstream-repository coding evaluations."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from ash.core.session import ToolCallRecord


_devtools = Path(__file__).resolve().parents[2] / "devtools"
if str(_devtools) not in sys.path:
    sys.path.insert(0, str(_devtools))
_path = _devtools / "repository_benchmark.py"
_spec = importlib.util.spec_from_file_location("repository_benchmark_under_test", _path)
assert _spec is not None and _spec.loader is not None
bench = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bench
_spec.loader.exec_module(bench)


def test_size_telemetry_excludes_tool_payload_and_private_file_path() -> None:
    record = ToolCallRecord(
        call_id="read-1",
        tool_name="read_file",
        arguments={
            "file_path": "/private/SECRET-SOURCE-PATH",
            "start_line": 40,
            "max_chars": 32768,
            "expected_sha256": "PRIVATE-HASH",
        },
        approved=True,
        executed=True,
        dispatched=True,
        result="SECRET-FILE-CONTENT" * 150,
        timestamp=datetime.now(timezone.utc),
    )
    metrics = bench.tool_call_size_metrics(record)
    assert metrics == {
        "result_chars": len(record.result),
        "explicit_max_chars": True,
        "max_chars": 32768,
        "start_line": 40,
    }
    assert "SECRET" not in repr(metrics)
    assert "PRIVATE" not in repr(metrics)
    assert not bench.tool_call_size_metrics(
        record.model_copy(
            update={"arguments": {"file_path": "SECRET", "max_chars": "SECRET"}}
        )
    ).get("max_chars")


@pytest.mark.parametrize(
    ("task_id", "upstream", "editable"),
    [
        (
            "h11-chunk-footer",
            "https://github.com/python-hyper/h11.git",
            {"h11/_readers.py"},
        ),
        (
            "urllib3-header-sanitization",
            "https://github.com/urllib3/urllib3.git",
            {"src/urllib3/connection.py"},
        ),
        (
            "urllib3-proxy-redirect",
            "https://github.com/urllib3/urllib3.git",
            {"src/urllib3/connectionpool.py", "src/urllib3/poolmanager.py"},
        ),
        (
            "urllib3-url-auth-feature",
            "https://github.com/urllib3/urllib3.git",
            {"src/urllib3/util/url.py"},
        ),
    ],
)
def test_upstream_tasks_are_pinned_and_oracles_sealed(
    task_id: str, upstream: str, editable: set[str]
) -> None:
    task = bench.TASKS[task_id]
    assert task.upstream == upstream
    assert task.base_sha != task.reference_fix_sha
    assert re.fullmatch("[a-f0-9]{40}", task.base_sha)
    assert re.fullmatch("[a-f0-9]{40}", task.reference_fix_sha)
    assert task.editable == editable
    assert task.visible_script == "qa_visible.py"
    assert task.visible_script not in task.editable
    assert task.command_grant == "python3 -B qa_visible.py"
    assert task.hidden_source != task.visible_source
    assert task.hidden_source not in task.prompt
    compile(task.visible_source, task.visible_script, "exec")
    compile(task.hidden_source, "<sealed-oracle>", "exec")


def test_repository_snapshot_and_protected_edits_refuse_unexpected_changes(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("private transport metadata")
    (tmp_path / "h11").mkdir()
    source = tmp_path / "h11/_readers.py"
    source.write_text("def decode(): return False\n")
    (tmp_path / "qa_visible.py").write_text("assert True\n")
    baseline = bench.snapshot_files(tmp_path)
    assert ".git/config" not in baseline
    assert set(baseline) == {"h11/_readers.py", "qa_visible.py"}
    assert bench.check_edits(baseline, baseline, frozenset({"h11/_readers.py"})) == (
        True,
        False,
    )

    source.write_text("def decode(): return True\n")
    updated = bench.snapshot_files(tmp_path)
    assert bench.check_edits(baseline, updated, frozenset({"h11/_readers.py"})) == (
        True,
        True,
    )
    (tmp_path / "qa_visible.py").write_text("assert False\n")
    assert bench.check_edits(
        baseline, bench.snapshot_files(tmp_path), frozenset({"h11/_readers.py"})
    ) == (False, True)
    (tmp_path / "qa_visible.py").write_text("assert True\n")
    (tmp_path / "unexpected.txt").write_text("extra")
    assert bench.check_edits(
        baseline, bench.snapshot_files(tmp_path), frozenset({"h11/_readers.py"})
    ) == (False, False)
    (tmp_path / "unexpected.txt").unlink()
    source.unlink()
    source.symlink_to(tmp_path / "qa_visible.py")
    assert bench.check_edits(
        baseline, bench.snapshot_files(tmp_path), frozenset({"h11/_readers.py"})
    ) == (False, False)


def test_two_module_scoring_rejects_partial_repair_and_modified_qa() -> None:
    required = bench.TASKS["urllib3-proxy-redirect"].editable
    pool = "src/urllib3/connectionpool.py"
    manager = "src/urllib3/poolmanager.py"
    original = {
        pool: "sha-original-pool",
        manager: "sha-original-manager",
        "qa_visible.py": "sha-test",
    }
    partially_fixed = {**original, pool: "sha-fixed-pool"}
    assert bench.check_edits(original, partially_fixed, required) == (True, False)
    both_fixed = {**partially_fixed, manager: "sha-fixed-manager"}
    assert bench.check_edits(original, both_fixed, required) == (True, True)
    altered_test = {**both_fixed, "qa_visible.py": "sha-altered-test"}
    assert bench.check_edits(original, altered_test, required) == (False, True)


def test_fetch_pinned_checks_out_only_exact_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = bench.TASKS["h11-chunk-footer"]
    invocations: list[list[str]] = []

    def fake_command(
        argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == tmp_path / "source"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "OPENROUTER_API_KEY" not in env
        assert 0 < timeout <= 45
        invocations.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=task.base_sha + "\n" if argv[1:] == ["rev-parse", "HEAD"] else "",
        )

    monkeypatch.setattr(bench, "command", fake_command)
    bench.fetch_pinned(tmp_path / "source", task, task.base_sha)
    assert invocations == [
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", task.upstream],
        [
            "git",
            "-c",
            "protocol.version=2",
            "fetch",
            "--depth=1",
            "origin",
            task.base_sha,
        ],
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "checkout",
            "-q",
            "--detach",
            task.base_sha,
        ],
        ["git", "rev-parse", "HEAD"],
    ]


def test_preflight_requires_red_base_and_green_upstream_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = bench.TASKS["h11-chunk-footer"]

    def fake_fetch(destination: Path, _: object, sha: str) -> None:
        destination.mkdir()
        (destination / "h11").mkdir()
        (destination / "h11/_readers.py").write_text(sha)

    def fake_grade(workspace: Path, _: object) -> dict[str, object]:
        assert (workspace / "qa_visible.py").is_file()
        good = workspace.name == "reference"
        return {
            "correct": good,
            "visible_oracle_exit": 0 if good else 1,
            "hidden_oracle_exit": 0 if good else 1,
        }

    monkeypatch.setattr(bench, "fetch_pinned", fake_fetch)
    monkeypatch.setattr(bench, "check_oracles", fake_grade)
    baseline, proof = bench.preflight(task, tmp_path)
    assert baseline.name == "baseline"
    assert proof == {
        "base_visible_exit": 1,
        "base_hidden_exit": 1,
        "reference_visible_exit": 0,
        "reference_hidden_exit": 0,
    }

    def broken_grade(workspace: Path, _: object) -> dict[str, object]:
        return {"correct": True, "visible_oracle_exit": 0, "hidden_oracle_exit": 0}

    monkeypatch.setattr(bench, "check_oracles", broken_grade)
    (tmp_path / "broken").mkdir()
    with pytest.raises(RuntimeError, match="not independently reproducible"):
        bench.preflight(task, tmp_path / "broken")

    def linked_source(destination: Path, selected: object, sha: str) -> None:
        fake_fetch(destination, selected, sha)
        (destination / "unsafe-link").symlink_to("/etc/passwd")

    monkeypatch.setattr(bench, "fetch_pinned", linked_source)
    (tmp_path / "linked").mkdir()
    with pytest.raises(RuntimeError, match="contains a symlink"):
        bench.preflight(task, tmp_path / "linked")


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (["--chatgpt-model", "!invalid"], "invalid exact ChatGPT model ID"),
        (["--chatgpt-model", "gpt-6-luna", "--timeout", "500"], "timeout must"),
        (["--chatgpt-model", "gpt-6-luna", "--context-tokens", "5"], "context budget"),
    ],
)
def test_real_repo_cli_rejects_invalid_options_without_evidence(
    tmp_path: Path, arguments: list[str], error: str
) -> None:
    evidence = tmp_path / "must-not-exist"
    child = subprocess.run(
        [
            sys.executable,
            str(_path),
            "--task",
            "h11-chunk-footer",
            "--evidence-dir",
            str(evidence),
            *arguments,
        ],
        cwd=_devtools.parent,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(_devtools.parent / "src")},
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert child.returncode == 2
    assert error in child.stderr
    assert not evidence.exists()
