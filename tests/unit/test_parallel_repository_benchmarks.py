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
from repository_benchmark import (  # noqa: E402
    TASKS,
    hidden_oracle_classification,
    hidden_oracle_sha256,
    shared_user_prompt,
)


def _emit_receipt(task_name: str, evidence: Path, *, outage: bool = False) -> None:
    """Synthetic receipts exercise the real read-only comparator, not a model."""
    evidence.mkdir(mode=0o700)
    task = TASKS[task_name]
    diagnostic_retake_of = task.diagnostic_retake_of
    evidence_classification = (
        "diagnostic-retake-same-upstream-source-not-fresh-held-out"
        if diagnostic_retake_of
        else "source-backed-task"
    )
    preflight = {
        "base_visible_exit": 1,
        "base_hidden_exit": 1,
        "reference_visible_exit": 0,
        "reference_hidden_exit": 0,
        "reference_added_paths": [],
        "reference_removed_paths": [],
        "reference_modified_paths": [],
        "editable_ablation_results": [
            {
                "path": path,
                "visible_oracle_exit": 1,
                "hidden_oracle_exit": 1,
            }
            for path in sorted(task.editable)
        ],
    }
    base = {
        "task": task_name,
        "base_sha": task.base_sha,
        "reference_fix_sha": task.reference_fix_sha,
        "diagnostic_retake_of": diagnostic_retake_of,
        "evidence_classification": evidence_classification,
        "preflight": preflight,
    }
    hashes = {
        "task_prompt_sha256": hashlib.sha256(task.prompt.encode()).hexdigest(),
        "user_prompt_sha256": hashlib.sha256(
            shared_user_prompt(task).encode()
        ).hexdigest(),
        "visible_oracle_sha256": hashlib.sha256(task.visible_source.encode()).hexdigest(),
        "hidden_oracle_sha256": hidden_oracle_sha256(task),
    }
    if evidence.name == "ash":
        (evidence / "result.json").write_text(json.dumps({
            **base, **hashes, "provider_model": "openai/gpt-6-luna",
            "reasoning_effort": "xhigh", "context_tokens": 32768,
            "turn_token_budget": 0,
            "passed": True, "oracle": {"correct": True},
            "visible_test_command_executed": True,
            "visible_test_command_passed": True,
            "protected_files_unchanged": True, "required_sources_changed": True,
            "required_tools_succeeded": True, "exit_code": 0,
            "timed_out": False, "cli_seconds": 25.0, "elapsed_seconds": 29.0,
        }))
        return
    (evidence / "manifest.json").write_text(json.dumps({
        **base, **hashes, "visible_test": task.command_grant,
        "editable": sorted(task.editable),
    }))
    (evidence / "prompt.txt").write_text(shared_user_prompt(task))
    (evidence / "invocation.json").write_text(json.dumps({
        "ephemeral": True, "memory_backed_auth_copy_cleaned": True, "cli_seconds": 30.0,
        "native_binary_direct": True,
        "host_connector_isolation_verified": True,
        "host_connector_disable_method": "native-features-and-mcp-list-per-invocation",
        "host_connector_disabled_features": [
            "apps", "codex_apps_mcp_2026_07_28", "enable_mcp_apps",
        ],
        "native_binary_sha256": "e" * 64,
    }))
    (evidence / "grade.json").write_text(json.dumps({
        "task": task_name, "model_requested": "gpt-6-luna",
        "diagnostic_retake_of": diagnostic_retake_of,
        "evidence_classification": evidence_classification,
        "reasoning_effort_requested": "xhigh", "context_tokens_requested": 32768,
        "passed": not outage,
        "oracles": {"correct": not outage},
        "protected_files_unchanged": True,
        "required_source_changed": not outage,
        "visible_test_command_executed": not outage,
        "visible_test_command_passed": not outage,
        "visible_test_command_count": 1 if not outage else 0,
        "host_connector_isolation_verified": True,
        "host_connector_invocation_observed": False,
        "score_eligible": not outage,
        "shell_command_policy_enforced": False,
        "event_log_valid": True,
        "native_binary_sha256": "e" * 64,
        "exit_code": 0 if not outage else -15,
        "timed_out": outage, "terminal_events": ["turn.completed"] if not outage else [],
        "elapsed_seconds": 30.0, "shell_non_test_commands": 2,
        "native_binary_direct": True, "isolated_codex_home": True,
        "isolated_home_xdg": True, "memory_backed_auth_copy_cleaned": True,
        "infrastructure_unavailable": outage,
    }))
    event_rows = []
    if outage:
        event_rows.append({
            "type": "error",
            "message": "workspace routing discovery timed out",
        })
    else:
        event_rows.extend([
            {
                "type": "item.completed",
                "item": {
                    "id": "visible-test",
                    "type": "command_execution",
                    "command": task.command_grant,
                    "status": "completed",
                    "exit_code": 0,
                },
            },
            {"type": "turn.completed"},
        ])
    (evidence / "codex.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in event_rows)
    )


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


def test_batch_and_pair_refuse_unapproved_model_dispatch_before_any_effect(
    tmp_path: Path,
) -> None:
    called: list[str] = []

    def fake_invoke(argv: list[str], evidence: Path) -> int:
        called.append(evidence.name)
        raise AssertionError("model runner was invoked before operator consent")

    pair_root = tmp_path / "pair"
    pair_root.mkdir()
    with pytest.raises(ValueError, match="explicit.*authorization"):
        run_pair(
            "h11-chunk-footer", 1, pair_root, 170, 32768,
            invoke=fake_invoke,
        )
    assert list(pair_root.iterdir()) == []

    batch_root = tmp_path / "batch"
    with pytest.raises(ValueError, match="explicit.*authorization"):
        run_batch(
            ["h11-chunk-footer"], 1, 1, batch_root, 170, 32768,
            invoke=fake_invoke,
        )
    assert not batch_root.exists()
    assert not called


def test_explicit_batch_consent_reaches_only_plain_codex_runner(
    tmp_path: Path,
) -> None:
    ash = commands(
        "h11-chunk-footer", "ash", tmp_path, 170, 32768,
        authorize_model_trial=True,
    )
    codex = commands(
        "h11-chunk-footer", "codex", tmp_path, 170, 32768,
        authorize_model_trial=True,
    )
    assert "--authorize-model-trial" not in ash
    assert codex.count("--authorize-model-trial") == 1
    assert "--authorize-model-trial" not in commands(
        "h11-chunk-footer", "codex", tmp_path, 170, 32768
    )


def test_native_readiness_checks_apps_before_any_credential_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence: list[str] = []
    native = Path("/isolated/native/codex")
    monkeypatch.setattr(
        batch_runner, "_require_sealed_comparator_profile",
        lambda: (sequence.append("attestation") or (native, "e" * 64)),
    )
    monkeypatch.setattr(
        batch_runner, "_verify_host_connector_isolation",
        lambda executable: (sequence.append(
            "features" if executable == native else "wrong-binary"
        ) or {"native_binary_sha256": "e" * 64}),
    )
    monkeypatch.setattr(
        batch_runner, "_chatgpt_tmpfs_root",
        lambda: (sequence.append("credential-storage-check") or Path("/dev/shm")),
    )

    batch_runner.preflight_plain_codex_readiness()

    assert sequence == [
        "attestation", "features", "credential-storage-check",
    ]


def test_paired_cli_requires_consent_before_reading_auth_or_creating_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unsafe_auth(_timeout: int) -> bool:
        raise AssertionError("auth was touched before trial consent")

    root = tmp_path / "new-private-evidence"
    monkeypatch.setattr(batch_runner, "refresh_existing_chatgpt_access", unsafe_auth)
    monkeypatch.setattr(
        sys, "argv", [
            "parallel_repository_benchmarks.py", "--task", "h11-chunk-footer",
            "--evidence-root", str(root),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        batch_runner.main()
    assert exc.value.code == 2
    assert "--authorize-model-trial" in capsys.readouterr().err
    assert not root.exists()


def test_approved_pair_refuses_unsealed_comparator_before_first_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted: list[Path] = []

    def unexpected_invoke(argv: list[str], evidence: Path) -> int:
        attempted.append(evidence)
        raise AssertionError("plain Codex is unsealed; no Ash trial may run")

    # Exercise real-runner dispatch with a simulated missing sealed profile.
    def refuse_unsealed() -> None:
        raise RuntimeError("sealed profile unavailable")

    monkeypatch.setattr(batch_runner, "preflight_plain_codex_readiness", refuse_unsealed)
    monkeypatch.setattr(batch_runner, "invoke_runner", unexpected_invoke)
    root = tmp_path / "would-be-private-batch"
    with pytest.raises(RuntimeError, match="sealed profile unavailable"):
        run_batch(
            ["h11-chunk-footer"], 1, 1, root, 170, 32768,
            invoke=unexpected_invoke, authorize_model_trial=True,
        )
    assert not root.exists()
    assert not attempted


def test_approved_cli_rejects_unsealed_native_before_accessing_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def no_seal() -> None:
        raise RuntimeError("private attestation drift")

    def no_access(_timeout: int) -> bool:
        raise AssertionError("auth must not be accessed when native comparator is unsealed")

    monkeypatch.setattr(batch_runner, "preflight_plain_codex_readiness", no_seal)
    monkeypatch.setattr(batch_runner, "refresh_existing_chatgpt_access", no_access)
    root = tmp_path / "uncreated"
    monkeypatch.setattr(
        sys, "argv", [
            "parallel_repository_benchmarks.py", "--task", "h11-chunk-footer",
            "--authorize-model-trial", "--evidence-root", str(root),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        batch_runner.main()
    assert exc.value.code == 2
    assert "sealed plain Codex" in capsys.readouterr().err
    assert not root.exists()


def test_paired_cli_with_explicit_consent_passes_authorization_to_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: list[dict[str, object]] = []

    def fake_batch(*args: object, **kwargs: object) -> dict[str, object]:
        observed.append({"args": args, **kwargs})
        return {"parallel_pairs": 1, "uncontended_latency_mode": True, "results": [
            {"comparison_verified": True}
        ]}

    monkeypatch.setattr(batch_runner, "refresh_existing_chatgpt_access", lambda _: True)
    monkeypatch.setattr(batch_runner, "preflight_plain_codex_readiness", lambda: None)
    monkeypatch.setattr(batch_runner, "run_batch", fake_batch)
    monkeypatch.setattr(
        sys, "argv", [
            "parallel_repository_benchmarks.py", "--task", "h11-chunk-footer",
            "--parallel", "1", "--evidence-root", str(tmp_path / "never-created"),
            "--authorize-model-trial",
        ],
    )
    assert batch_runner.main() == 0
    assert len(observed) == 1
    assert observed[0]["authorize_model_trial"] is True
    assert len(json.loads(capsys.readouterr().out)["results"]) == 1
    assert not (tmp_path / "never-created").exists()


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
        assert ("--authorize-model-trial" in argv) == (evidence.name == "codex")
        _emit_receipt("h11-chunk-footer", evidence)
        return 0

    report = run_pair(
        "h11-chunk-footer", rep, tmp_path, 170, 32768,
        invoke=fake_invoke, authorize_model_trial=True,
    )
    assert seen == expected
    assert report["comparison_verified"] is True
    assert report["ash_pass"] is True
    assert report["codex_pass"] is True
    assert report["engine_outcomes"] == {
        "ash": {
            "correct_source": True,
            "visible_test_command_executed": True,
            "visible_test_command_passed": True,
            "observed_edit_after_passing_test": None,
            "verification_order_version": 0,
            "verification_freshness": "unknown",
            "completion_verified": None,
            "cli_complete": True,
            "timed_out": False,
            "scored_pass": True,
        },
        "codex": {
            "correct_source": True,
            "visible_test_command_executed": True,
            "visible_test_command_passed": True,
            "observed_edit_after_passing_test": False,
            "verification_order_version": 0,
            "verification_freshness": "unknown",
            "host_connector_isolation_verified": True,
            "host_connector_invocation_observed": False,
            "score_eligible": True,
            "shell_command_policy_enforced": False,
            "cli_complete": True,
            "timed_out": False,
            "scored_pass": True,
        },
    }
    assert report["timing_available"] is True
    assert report["fully_controlled"] is False
    assert report["score_eligible"] is True
    assert report["preflight_verified"] is None
    assert report["host_connector_isolation_verified"] is True
    assert report["host_connector_disable_method"] == (
        "native-features-and-mcp-list-per-invocation"
    )
    assert report["host_connector_disabled_features"] == [
        "apps", "codex_apps_mcp_2026_07_28", "enable_mcp_apps",
    ]
    assert report["native_binary_sha256"] == "e" * 64
    assert report["comparison_scope"] == "operational_qualified"
    assert report["ash_cli_seconds"] == 25.0
    assert report["codex_cli_seconds"] == 30.0
    assert (tmp_path / f"h11-chunk-footer-r{rep:02d}/summary.json").is_file()


def test_pair_preserves_correct_code_and_test_but_failed_completion(
    tmp_path: Path,
) -> None:
    """Reproduce R18 grading distinction without inference or private receipts."""
    task = "pytest-mro-marker-precedence"

    def fake_invoke(_argv: list[str], evidence: Path) -> int:
        _emit_receipt(task, evidence)
        filename = "result.json" if evidence.name == "ash" else "grade.json"
        path = evidence / filename
        receipt = json.loads(path.read_text())
        receipt["passed"] = False
        receipt["exit_code"] = 1 if evidence.name == "ash" else -15
        receipt["timed_out"] = evidence.name == "codex"
        if evidence.name == "ash":
            receipt["oracle"]["correct"] = False
            receipt["required_sources_changed"] = False
            receipt["required_tools_succeeded"] = False
            receipt["visible_test_command_executed"] = False
            receipt["visible_test_command_passed"] = False
        else:
            receipt["terminal_events"] = []
        path.write_text(json.dumps(receipt))
        return 1

    report = run_pair(
        task, 1, tmp_path, 300, 32768,
        invoke=fake_invoke, authorize_model_trial=True,
    )
    assert report["comparison_verified"] is True
    assert report["fully_controlled"] is False
    assert report["ash_pass"] is report["codex_pass"] is False
    assert report["engine_outcomes"] == {
        "ash": {
            "correct_source": False,
            "visible_test_command_executed": False,
            "visible_test_command_passed": False,
            "observed_edit_after_passing_test": None,
            "verification_order_version": 0,
            "verification_freshness": "unknown",
            "completion_verified": None,
            "cli_complete": False,
            "timed_out": False,
            "scored_pass": False,
        },
        "codex": {
            "correct_source": True,
            "visible_test_command_executed": True,
            "visible_test_command_passed": True,
            "observed_edit_after_passing_test": False,
            "verification_order_version": 0,
            "verification_freshness": "unknown",
            "host_connector_isolation_verified": True,
            "host_connector_invocation_observed": False,
            "score_eligible": True,
            "shell_command_policy_enforced": False,
            "cli_complete": False,
            "timed_out": True,
            "scored_pass": False,
        },
    }
    stored = json.loads((tmp_path / f"{task}-r01/summary.json").read_text())
    assert stored["engine_outcomes"] == report["engine_outcomes"]
    assert stored["comparison_verified"] is True


def test_pair_excludes_provider_outage_from_valid_comparisons(tmp_path: Path) -> None:
    def fake_invoke(argv: list[str], evidence: Path) -> int:
        _emit_receipt("h11-chunk-footer", evidence, outage=evidence.name == "codex")
        return 1 if evidence.name == "codex" else 0

    report = run_pair(
        "h11-chunk-footer", 1, tmp_path, 170, 32768,
        invoke=fake_invoke, authorize_model_trial=True,
    )
    assert report["comparison_verified"] is False
    assert report["reason"] == "provider_outage_or_parity_unverified"
    assert report["runner_exit_codes"] == {"ash": 0, "codex": 1}


def test_pair_mcp_attempt_blocks_comparison_but_keeps_source_outcomes(
    tmp_path: Path,
) -> None:
    def fake_invoke(_argv: list[str], evidence: Path) -> int:
        _emit_receipt("h11-chunk-footer", evidence)
        if evidence.name == "codex":
            path = evidence / "grade.json"
            grade = json.loads(path.read_text())
            grade.update({
                "passed": False,
                "score_eligible": False,
                "host_connector_isolation_verified": False,
                "host_connector_invocation_observed": True,
            })
            path.write_text(json.dumps(grade))
            event_path = evidence / "codex.jsonl"
            events = [json.loads(line) for line in event_path.read_text().splitlines()]
            events.insert(0, {
                "type": "item.started",
                "item": {
                    "id": "host-call",
                    "type": "mcp_tool_call",
                    "server": "codex_apps_mcp_2026_07_28",
                    "tool": "devspace.open_workspace",
                    "status": "in_progress",
                },
            })
            event_path.write_text(
                "".join(json.dumps(event) + "\n" for event in events)
            )
        return 0

    report = run_pair(
        "h11-chunk-footer", 1, tmp_path, 170, 32768,
        invoke=fake_invoke, authorize_model_trial=True,
    )

    assert report["comparison_verified"] is False
    assert report["score_eligible"] is False
    assert report["codex_pass"] is False
    assert report["engine_outcomes"]["codex"]["correct_source"] is True
    assert report["engine_outcomes"]["codex"]["host_connector_isolation_verified"] is False
    assert report["engine_outcomes"]["codex"]["host_connector_invocation_observed"] is True
    assert report["engine_outcomes"]["codex"]["visible_test_command_executed"] is True
    assert report["engine_outcomes"]["codex"]["visible_test_command_passed"] is True


def test_r23b_pair_is_labeled_diagnostic_same_source_not_held_out(
    tmp_path: Path,
) -> None:
    task = "packaging-legacy-dataclass-pickles-r23b"

    def fake_invoke(_argv: list[str], evidence: Path) -> int:
        _emit_receipt(task, evidence)
        return 0

    report = run_pair(
        task, 1, tmp_path, 170, 32768,
        invoke=fake_invoke, authorize_model_trial=True,
    )

    assert report["comparison_verified"] is True
    assert report["fully_controlled"] is False
    assert report["score_eligible"] is True
    assert report["preflight_verified"] is True
    assert report["diagnostic_retake_of"] == "packaging-legacy-dataclass-pickles"
    assert report["evidence_classification"] == (
        "diagnostic-retake-same-upstream-source-not-fresh-held-out"
    )
    assert any("same upstream source" in item for item in report["comparison_caveats"])


def test_r17_synthetic_pair_uses_the_standard_grade_path(tmp_path: Path) -> None:
    task = "pytest-mixed-indirect-fixture-precedence"
    seen: list[str] = []

    def fake_invoke(argv: list[str], evidence: Path) -> int:
        seen.append(evidence.name)
        assert task in argv
        _emit_receipt(task, evidence)
        return 0

    report = run_pair(
        task, 1, tmp_path, 170, 32768,
        invoke=fake_invoke, authorize_model_trial=True,
    )
    assert seen == ["ash", "codex"]
    assert report["comparison_verified"] is True
    assert report["fully_controlled"] is False
    assert report["comparison_scope"] == "operational_qualified"
    assert report["hidden_oracle_classification"] == hidden_oracle_classification(
        TASKS[task]
    )
    assert report["comparison_caveats"]
    assert report["ash_pass"] is True
    assert report["codex_pass"] is True
    assert (tmp_path / f"{task}-r01/summary.json").is_file()


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
        authorize_model_trial=True,
    )
    assert len(set(seen)) == 2
    assert len(report["results"]) == 2
    assert [row["engine_order"] for row in report["results"]] == [
        ["ash", "codex"], ["codex", "ash"],
    ]
    assert all(not pair["comparison_verified"] for pair in report["results"])
    assert all("engine_outcomes" not in pair for pair in report["results"])
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


@pytest.mark.parametrize("fault", [
    "missing_test_metadata", "wrong_test_command", "contradictory_command",
    "missing_cleanup", "failed_cleanup", "non_ephemeral", "unexpected_exit",
])
def test_r23b_rejects_inconsistent_evidence_without_model(tmp_path: Path, fault: str) -> None:
    task = "packaging-legacy-dataclass-pickles-r23b"

    def fake_invoke(_argv: list[str], evidence: Path) -> int:
        _emit_receipt(task, evidence)
        if evidence.name == "ash":
            return 0
        grade_path = evidence / "grade.json"
        invocation_path = evidence / "invocation.json"
        manifest_path = evidence / "manifest.json"
        trace_path = evidence / "codex.jsonl"
        grade = json.loads(grade_path.read_text())
        invocation = json.loads(invocation_path.read_text())
        invocation.update(memory_backed_auth_copy_cleaned=True)
        trace = [json.loads(row) for row in trace_path.read_text().splitlines()]
        if fault == "missing_test_metadata":
            for key in ("visible_test_command_executed", "visible_test_command_passed", "visible_test_command_count"):
                grade.pop(key)
            trace = [{"type": "turn.completed"}]
        elif fault == "wrong_test_command":
            manifest = json.loads(manifest_path.read_text())
            manifest["visible_test"] = "python3 -B wrong.py"
            manifest_path.write_text(json.dumps(manifest))
            trace[0]["item"]["command"] = "python3 -B wrong.py"
        elif fault == "contradictory_command":
            trace.insert(0, {"type": "item.started", "item": {
                "id": "visible-test", "type": "command_execution", "command": "python3 -B wrong.py", "status": "in_progress",
            }})
        elif fault == "missing_cleanup":
            invocation.pop("memory_backed_auth_copy_cleaned")
        elif fault == "failed_cleanup":
            invocation["memory_backed_auth_copy_cleaned"] = False
        elif fault == "non_ephemeral":
            invocation["ephemeral"] = False
        grade_path.write_text(json.dumps(grade))
        invocation_path.write_text(json.dumps(invocation))
        trace_path.write_text("".join(json.dumps(row) + "\n" for row in trace))
        return 137 if fault == "unexpected_exit" else 0

    result = run_pair(task, 1, tmp_path, 300, 32768, invoke=fake_invoke, authorize_model_trial=True)
    assert result["comparison_verified"] is False
    assert result["score_eligible"] is False


@pytest.mark.parametrize("fault,eligible", [
    ("none", True), ("missing_terminal", False), ("contradictory_terminal", False),
    ("invalid_version", False), ("missing_claim", False), ("genuine_incomplete", True),
])
def test_versioned_ash_completion_evidence_in_pair(tmp_path: Path, fault: str, eligible: bool) -> None:
    task = "h11-chunk-footer"

    def fake_invoke(_argv: list[str], evidence: Path) -> int:
        _emit_receipt(task, evidence)
        if evidence.name != "ash":
            return 0
        path = evidence / "result.json"
        data = json.loads(path.read_text())
        data.update(completion_evidence_version=1, completion_verified=True,
                    terminal_events=["turn.completed"], terminal_stop_reason="completed",
                    requested_context_continuations=0, context_continuations=0, session_count=1)
        if fault == "missing_terminal":
            data.pop("terminal_events")
        elif fault == "contradictory_terminal":
            data["terminal_stop_reason"] = "iteration_limit"
        elif fault == "invalid_version":
            data["completion_evidence_version"] = True
        elif fault == "missing_claim":
            data.pop("completion_verified")
        elif fault == "genuine_incomplete":
            data.update(passed=False, exit_code=1, terminal_events=["turn.error"],
                        terminal_stop_reason=None, completion_verified=False)
        path.write_text(json.dumps(data))
        return 1 if fault == "genuine_incomplete" else 0

    result = run_pair(task, 1, tmp_path, 300, 32768, invoke=fake_invoke, authorize_model_trial=True)
    assert result["comparison_verified"] is eligible
    assert result["score_eligible"] is eligible
    if fault == "genuine_incomplete":
        assert result["ash_pass"] is False
        assert result["engine_outcomes"]["ash"]["completion_verified"] is False
        assert result["engine_outcomes"]["ash"]["cli_complete"] is False
