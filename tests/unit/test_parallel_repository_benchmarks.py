"""Parallel benchmark scheduling must not weaken paired grading or isolation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from threading import Barrier

import pytest


_devtools = Path(__file__).resolve().parents[2] / "devtools"
if str(_devtools) not in sys.path:
    sys.path.insert(0, str(_devtools))

from parallel_repository_benchmarks import (  # noqa: E402
    ROOT,
    commands,
    invoke_runner,
    refresh_existing_chatgpt_access,
    run_batch,
    run_pair,
)
import parallel_repository_benchmarks as batch_runner  # noqa: E402
from repository_benchmark import TASKS, shared_user_prompt  # noqa: E402


def _emit_receipt(task_name: str, evidence: Path, *, outage: bool = False) -> None:
    """Synthetic receipts exercise the real read-only comparator, not a model."""
    evidence.mkdir(mode=0o700)
    task = TASKS[task_name]
    base = {
        "task": task_name,
        "base_sha": task.base_sha,
        "reference_fix_sha": task.reference_fix_sha,
    }
    hashes = {
        "task_prompt_sha256": hashlib.sha256(task.prompt.encode()).hexdigest(),
        "user_prompt_sha256": hashlib.sha256(
            shared_user_prompt(task).encode()
        ).hexdigest(),
        "visible_oracle_sha256": hashlib.sha256(task.visible_source.encode()).hexdigest(),
        "hidden_oracle_sha256": hashlib.sha256(task.hidden_source.encode()).hexdigest(),
    }
    if evidence.name == "ash":
        (evidence / "result.json").write_text(json.dumps({
            **base, **hashes, "provider_model": "openai/gpt-6-luna",
            "reasoning_effort": "xhigh", "context_tokens": 32768,
            "turn_token_budget": 0,
            "passed": True, "oracle": {"correct": True},
            "protected_files_unchanged": True, "required_sources_changed": True,
            "required_tools_succeeded": True, "exit_code": 0,
            "timed_out": False, "cli_seconds": 25.0, "elapsed_seconds": 29.0,
        }))
        return
    (evidence / "manifest.json").write_text(json.dumps({**base, **hashes}))
    (evidence / "prompt.txt").write_text(shared_user_prompt(task))
    (evidence / "invocation.json").write_text(json.dumps({
        "ephemeral": True, "cli_seconds": 30.0,
    }))
    (evidence / "grade.json").write_text(json.dumps({
        "task": task_name, "model_requested": "gpt-6-luna",
        "reasoning_effort_requested": "xhigh", "context_tokens_requested": 32768,
        "passed": not outage,
        "oracles": {"correct": not outage},
        "protected_files_unchanged": True,
        "required_source_changed": not outage,
        "visible_test_command_executed": not outage,
        "exit_code": 0 if not outage else -15,
        "timed_out": outage, "terminal_events": ["turn.completed"] if not outage else [],
        "elapsed_seconds": 30.0, "shell_non_test_commands": 2,
        "native_binary_direct": True, "isolated_codex_home": True,
        "isolated_home_xdg": True, "memory_backed_auth_copy_cleaned": True,
        "infrastructure_unavailable": outage,
    }))


def test_engine_commands_select_plain_codex_with_identical_model_limits(
    tmp_path: Path,
) -> None:
    ash = commands("h11-chunk-footer", "ash", tmp_path, 170, 32768)
    codex = commands("h11-chunk-footer", "codex", tmp_path, 170, 32768)
    assert ash[0] == codex[0] == sys.executable
    assert ash[1] == str(ROOT / "devtools/repository_benchmark.py")
    assert codex[1] == str(ROOT / "devtools/codex_repository_benchmark.py")
    assert "--fair-prompt" in ash
    assert ash[ash.index("--turn-token-budget") + 1] == "0"
    for argv in (ash, codex):
        assert "gpt-6-luna" in argv
        assert "xhigh" in argv
        assert "32768" in argv
        assert "170" in argv
        assert "poteto" not in " ".join(argv).lower()


def test_runner_receipt_directory_is_created_by_runner_not_supervisor(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "codex"
    argv = [sys.executable, "-c", (
        "import pathlib,sys; "
        "p=pathlib.Path(sys.argv[1]); "
        "assert not p.exists(); p.mkdir(); "
        "(p/'grade.json').write_text('{}')"
    ), str(evidence)]
    assert invoke_runner(argv, evidence) == 0
    assert (evidence / "grade.json").is_file()
    assert (tmp_path / "codex.runner.stdout").is_file()
    assert (tmp_path / "codex.runner.stderr").is_file()


@pytest.mark.parametrize("rep,expected", [
    (1, ["ash", "codex"]), (2, ["codex", "ash"]),
])
def test_pair_alternates_order_and_validates_external_receipts(
    tmp_path: Path, rep: int, expected: list[str],
) -> None:
    seen: list[str] = []

    def fake_invoke(argv: list[str], evidence: Path) -> int:
        seen.append(evidence.name)
        _emit_receipt("h11-chunk-footer", evidence)
        return 0

    report = run_pair("h11-chunk-footer", rep, tmp_path, 170, 32768, invoke=fake_invoke)
    assert seen == expected
    assert report["comparison_verified"] is True
    assert report["ash_pass"] is True
    assert report["codex_pass"] is True
    assert report["timing_available"] is True
    assert report["ash_cli_seconds"] == 25.0
    assert report["codex_cli_seconds"] == 30.0
    assert (tmp_path / f"h11-chunk-footer-r{rep:02d}/summary.json").is_file()


def test_pair_excludes_provider_outage_from_valid_comparisons(tmp_path: Path) -> None:
    def fake_invoke(argv: list[str], evidence: Path) -> int:
        _emit_receipt("h11-chunk-footer", evidence, outage=evidence.name == "codex")
        return 1 if evidence.name == "codex" else 0

    report = run_pair("h11-chunk-footer", 1, tmp_path, 170, 32768, invoke=fake_invoke)
    assert report["comparison_verified"] is False
    assert report["reason"] == "provider_outage_or_parity_unverified"
    assert report["runner_exit_codes"] == {"ash": 0, "codex": 1}


def test_two_scenarios_execute_concurrently_with_separate_evidence(
    tmp_path: Path,
) -> None:
    gate = Barrier(2, timeout=5)
    seen: list[Path] = []

    def fake_invoke(argv: list[str], evidence: Path) -> int:
        if evidence.parent not in seen:
            seen.append(evidence.parent)
            gate.wait()
        return 0  # Incomplete mock evidence is classified as unverifiable.

    root = tmp_path / "private-batch"
    report = run_batch(
        ["h11-chunk-footer", "urllib3-header-sanitization"],
        1, 2, root, 170, 32768, invoke=fake_invoke,
    )
    assert len(set(seen)) == 2
    assert len(report["results"]) == 2
    assert [row["engine_order"] for row in report["results"]] == [
        ["ash", "codex"], ["codex", "ash"],
    ]
    assert all(not pair["comparison_verified"] for pair in report["results"])
    assert report["uncontended_latency_mode"] is False
    assert "do not use elapsed times" in report["latency_note"]
    assert (root.stat().st_mode & 0o777) == 0o700
    assert (root / "batch.json").is_file()


@pytest.mark.parametrize("tasks,repeats,workers", [
    (["h11-chunk-footer"], 4, 2),
    (["h11-chunk-footer"], 1, 3),
    (["h11-chunk-footer", "h11-chunk-footer"], 1, 2),
    (["not-a-task"], 1, 2),
    (list(TASKS), 2, 2),
])
def test_batch_rejects_unbounded_or_duplicate_jobs(
    tmp_path: Path, tasks: list[str], repeats: int, workers: int,
) -> None:
    with pytest.raises(ValueError):
        run_batch(tasks, repeats, workers, tmp_path / "batch", 170, 32768)
    assert not (tmp_path / "batch").exists()


def test_private_evidence_cannot_be_created_inside_source_repository() -> None:
    with pytest.raises(ValueError, match="outside the repo"):
        run_batch(["h11-chunk-footer"], 1, 1, ROOT / "my-bench", 170, 32768)


@pytest.mark.parametrize("timeout,context", [(301, 32768), (170, 65537)])
def test_batch_rejects_limits_unsupported_by_individual_runners(
    tmp_path: Path, timeout: int, context: int,
) -> None:
    with pytest.raises(ValueError, match="out-of-range"):
        run_batch(["h11-chunk-footer"], 1, 1, tmp_path / "batch", timeout, context)


def test_benchmark_credential_preflight_never_exposes_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Account:
        def access_usable(self, *, now: float) -> bool:
            return now < 1000

    class Session:
        async def access_token(self) -> str:
            return "PRIVATE-ACCESS-TOKEN"

    class Store:
        def active(self) -> Account:
            return Account()

    monkeypatch.setattr(batch_runner, "ChatGPTAuthSession", Session)
    monkeypatch.setattr(batch_runner, "ChatGPTCredentialStore", Store)
    monkeypatch.setattr(batch_runner.time, "time", lambda: 800)
    assert refresh_existing_chatgpt_access(160) is True
    assert refresh_existing_chatgpt_access(180) is False
