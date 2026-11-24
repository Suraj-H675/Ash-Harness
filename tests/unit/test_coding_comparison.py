"""Offline safeguards for graded harness-comparison receipts."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


_path = Path(__file__).resolve().parents[2] / "devtools/coding_comparison.py"
_devtools = _path.parent
if str(_devtools) not in sys.path:
    sys.path.insert(0, str(_devtools))
_spec = importlib.util.spec_from_file_location("coding_comparison_under_test", _path)
assert _spec is not None and _spec.loader is not None
comparison = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = comparison
_spec.loader.exec_module(comparison)


def _evidence(root: Path) -> tuple[Path, Path]:
    ash, codex = root / "ash", root / "codex"
    ash.mkdir()
    codex.mkdir()
    (ash / "result.json").write_text(
        json.dumps(
            {
                "task": "real-bug",
                "base_sha": "a" * 40,
                "reference_fix_sha": "b" * 40,
                "provider_model": "openai/gpt-6-luna",
                "reasoning_effort": "xhigh",
                "turn_token_budget": 0,
                "passed": True,
                "oracle": {"correct": True},
                "protected_files_unchanged": True,
                "required_sources_changed": True,
                "required_tools_succeeded": True,
                "visible_test_command_executed": True,
                "visible_test_command_passed": True,
                "diagnostic_retake_of": None,
                "evidence_classification": "source-backed-task",
                "exit_code": 0,
                "timed_out": False,
                "elapsed_seconds": 80,
                "cli_seconds": 72.5,
            }
        )
    )
    (codex / "manifest.json").write_text(
        json.dumps(
            {
                "task": "real-bug",
                "base_sha": "a" * 40,
                "reference_fix_sha": "b" * 40,
                "visible_test": "python3 -B qa_visible.py",
                "diagnostic_retake_of": None,
                "evidence_classification": "source-backed-task",
            }
        )
    )
    (codex / "invocation.json").write_text(json.dumps({
        "ephemeral": True,
        "cli_seconds": 170,
        "native_binary_direct": True,
        "host_connector_isolation_verified": True,
        "host_connector_disable_method": "native-features-and-mcp-list-per-invocation",
        "host_connector_disabled_features": [
            "apps", "codex_apps_mcp_2026_07_28", "enable_mcp_apps",
        ],
        "native_binary_sha256": "d" * 64,
    }))
    (codex / "grade.json").write_text(
        json.dumps(
            {
                "task": "real-bug",
                "model_requested": "gpt-6-luna",
                "reasoning_effort_requested": "xhigh",
                "oracles": {"correct": True},
                "protected_files_unchanged": True,
                "required_source_changed": True,
                "visible_test_command_executed": True,
                "visible_test_command_passed": False,
                "visible_test_command_count": 1,
                "host_connector_isolation_verified": True,
                "host_connector_invocation_observed": False,
                "score_eligible": True,
                "event_log_valid": True,
                "native_binary_direct": True,
                "native_binary_sha256": "d" * 64,
                "diagnostic_retake_of": None,
                "evidence_classification": "source-backed-task",
                "exit_code": -15,
                "timed_out": True,
                "terminal_events": [],
                "passed": False,
                "elapsed_seconds": 170,
                "shell_non_test_commands": 6,
            }
        )
    )
    (codex / "prompt.txt").write_text("SECRET-TEST-PROMPT")
    (codex / "codex.jsonl").write_text(
        json.dumps({
            "type": "item.completed",
            "item": {
                "id": "visible-test",
                "type": "command_execution",
                "command": "python3 -B qa_visible.py",
                "status": "completed",
                "exit_code": 1,
            },
        }) + "\n" + json.dumps({"type": "turn.completed"}) + "\n"
    )
    return ash, codex


def test_comparison_separates_correct_code_from_terminal_completion(
    tmp_path: Path,
) -> None:
    ash, codex = _evidence(tmp_path)
    report = comparison.compare(ash, codex)
    assert report["requested_model_effort_match"] is True
    assert report["ash"]["scored_pass"] is True
    assert report["codex"]["correct_source"] is True
    assert report["codex"]["visible_test_command_executed"] is True
    assert report["codex"]["visible_test_command_passed"] is False
    assert report["codex"]["recorded_visible_test_command_executed"] is True
    assert report["codex"]["event_trace_verified"] is True
    assert report["evaluation"]["host_connector_isolation_verified"] is True
    assert report["codex"]["cli_complete"] is False
    assert report["codex"]["scored_pass"] is False
    assert report["ash"]["timed_out"] is False
    assert report["codex"]["timed_out"] is True
    assert report["evaluation"]["fully_controlled"] is False
    assert report["evaluation"]["same_upstream_revision_pins"] is True
    assert report["evaluation"]["identical_oracle_provenance_verified"] is False
    assert report["evaluation"]["same_tool_and_command_authorizations"] is False
    assert report["codex"]["prompt_sha256"] is not None
    assert "SECRET-TEST-PROMPT" not in repr(report)
    assert report["evaluation"]["codex_native_binary_direct"] is True
    assert report["evaluation"]["excluded_provider_outage"] is False
    assert report["evaluation"]["matching_cli_timing_available"] is True
    assert report["evaluation"]["ash_unbounded_turn_configured"] is True
    assert report["evaluation"]["asymmetric_ash_turn_token_cap"] is False
    assert report["ash"]["cli_seconds"] == 72.5
    assert report["ash"]["elapsed_scope"] == "preflight_cli_and_grading"
    assert report["codex"]["cli_seconds"] == 170
    assert report["codex"]["elapsed_scope"] == "cli_only"


def test_legacy_receipts_keep_host_isolation_unknown_and_are_not_pair_eligible(
    tmp_path: Path,
) -> None:
    ash, codex = _evidence(tmp_path)
    grade_path = codex / "grade.json"
    grade = json.loads(grade_path.read_text())
    grade["visible_test_command_executed"] = False
    grade_path.write_text(json.dumps(grade))
    for path, field in (
        (ash / "result.json", "visible_test_command_executed"),
        (ash / "result.json", "visible_test_command_passed"),
        (codex / "grade.json", "host_connector_isolation_verified"),
        (codex / "grade.json", "score_eligible"),
    ):
        data = json.loads(path.read_text())
        data.pop(field, None)
        path.write_text(json.dumps(data))
    data = json.loads((codex / "manifest.json").read_text())
    data.pop("evidence_classification", None)
    data.pop("diagnostic_retake_of", None)
    (codex / "manifest.json").write_text(json.dumps(data))

    report = comparison.compare(ash, codex)

    assert report["codex"]["host_connector_isolation_verified"] is None
    assert report["codex"]["visible_test_command_executed"] is True
    assert report["codex"]["recorded_visible_test_command_executed"] is False
    assert report["codex"]["visible_test_command_passed"] is False
    assert report["evaluation"]["host_connector_isolation_verified"] is None
    assert report["comparison_verified"] is False
    assert report["score_eligible"] is False
    assert report["evidence_classification"] == "legacy-receipt-provenance-unrecorded"


def test_malformed_event_log_keeps_grade_outcomes_but_blocks_control_claim(
    tmp_path: Path,
) -> None:
    ash, codex = _evidence(tmp_path)
    (codex / "codex.jsonl").write_text('{"type":"item.started",\n')

    report = comparison.compare(ash, codex)

    assert report["codex"]["correct_source"] is True
    assert report["codex"]["visible_test_command_executed"] is True
    assert report["codex"]["event_trace_verified"] is False
    assert report["codex"]["host_connector_isolation_verified"] is None
    assert report["comparison_verified"] is False
    assert report["score_eligible"] is False


def test_connector_trace_is_checked_even_without_visible_test_metadata(
    tmp_path: Path,
) -> None:
    ash, codex = _evidence(tmp_path)
    manifest_path = codex / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("visible_test")
    manifest_path.write_text(json.dumps(manifest))
    event_path = codex / "codex.jsonl"
    event = {
        "type": "item.failed",
        "item": {
            "type": "mcp_tool_call",
            "server": "codex_apps_mcp_2026_07_28",
            "tool": "devspace.open_workspace",
            "status": "denied",
        },
    }
    event_path.write_text(json.dumps(event) + "\n")

    report = comparison.compare(ash, codex)

    assert report["codex"]["event_trace_verified"] is True
    assert report["codex"]["host_connector_invocation_observed"] is True
    assert report["codex"]["host_connector_isolation_verified"] is False
    assert report["codex"]["visible_test_command_executed"] is False
    assert report["comparison_verified"] is False
    assert report["score_eligible"] is False


def test_mcp_attempt_invalidates_pair_eligibility_but_preserves_grade_outcomes(
    tmp_path: Path,
) -> None:
    ash, codex = _evidence(tmp_path)
    event_path = codex / "codex.jsonl"
    events = [
        json.loads(line) for line in event_path.read_text().splitlines()
    ]
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
    event_path.write_text("".join(json.dumps(event) + "\n" for event in events))
    path = codex / "grade.json"
    grade = json.loads(path.read_text())
    grade.update({
        "host_connector_isolation_verified": False,
        "host_connector_invocation_observed": True,
        "score_eligible": False,
        "visible_test_command_executed": True,
        "visible_test_command_passed": False,
        "oracles": {"correct": True},
        "protected_files_unchanged": True,
        "required_source_changed": True,
        "passed": False,
    })
    path.write_text(json.dumps(grade))

    report = comparison.compare(ash, codex)

    assert report["codex"]["correct_source"] is True
    assert report["codex"]["visible_test_command_executed"] is True
    assert report["codex"]["visible_test_command_passed"] is False
    assert report["codex"]["host_connector_invocation_observed"] is True
    assert report["codex"]["score_eligible"] is False
    assert report["comparison_verified"] is False
    assert report["score_eligible"] is False


def test_historical_r23_is_read_as_failed_and_host_exposed_without_receipt_rewrite(
    tmp_path: Path,
) -> None:
    ash, codex = _evidence(tmp_path)
    ash_path = ash / "result.json"
    ash_result = json.loads(ash_path.read_text())
    ash_result.update({
        "task": "packaging-legacy-dataclass-pickles",
        "base_sha": comparison.TASKS[
            "packaging-legacy-dataclass-pickles"
        ].base_sha,
        "reference_fix_sha": comparison.TASKS[
            "packaging-legacy-dataclass-pickles"
        ].reference_fix_sha,
        "passed": False,
        "visible_test_command_executed": True,
        "visible_test_command_passed": True,
        "oracle": {"correct": False},
    })
    for key in ("diagnostic_retake_of", "evidence_classification"):
        ash_result.pop(key, None)
    ash_path.write_text(json.dumps(ash_result))
    for name in ("manifest.json", "grade.json"):
        path = codex / name
        receipt = json.loads(path.read_text())
        receipt["task"] = "packaging-legacy-dataclass-pickles"
        if name == "manifest.json":
            receipt["base_sha"] = comparison.TASKS[
                "packaging-legacy-dataclass-pickles"
            ].base_sha
            receipt["reference_fix_sha"] = comparison.TASKS[
                "packaging-legacy-dataclass-pickles"
            ].reference_fix_sha
        receipt.pop("diagnostic_retake_of", None)
        receipt.pop("evidence_classification", None)
        if name == "manifest.json":
            receipt["visible_test"] = "python3 -B qa_visible.py"
        else:
            receipt.update({
                "passed": False,
                "oracles": {"correct": False},
                "required_source_changed": False,
                "visible_test_command_executed": False,
                "visible_test_command_passed": False,
                "protected_files_unchanged": True,
            })
            for key in (
                "host_connector_isolation_verified",
                "host_connector_invocation_observed",
                "score_eligible",
            ):
                receipt.pop(key, None)
        path.write_text(json.dumps(receipt))
    event_path = codex / "codex.jsonl"
    events = [json.loads(line) for line in event_path.read_text().splitlines()]
    events.insert(0, {
        "type": "item.started",
        "item": {
            "id": "host-call",
            "type": "mcp_tool_call",
            "server": "codex_apps_mcp_2026_07_28",
            "tool": "devspace.open_workspace",
            "status": "completed",
        },
    })
    event_path.write_text("".join(json.dumps(event) + "\n" for event in events))
    before = {path.name: path.read_bytes() for path in (ash_path, codex / "grade.json")}

    report = comparison.compare(ash, codex)

    assert report["ash"]["scored_pass"] is False
    assert report["ash"]["visible_test_command_executed"] is True
    assert report["ash"]["visible_test_command_passed"] is True
    assert report["codex"]["scored_pass"] is False
    assert report["codex"]["correct_source"] is False
    assert report["codex"]["visible_test_command_executed"] is True
    assert report["codex"]["recorded_visible_test_command_executed"] is False
    assert report["codex"]["visible_test_command_passed"] is False
    assert report["codex"]["host_connector_invocation_observed"] is True
    assert report["codex"]["host_connector_isolation_verified"] is False
    assert report["comparison_verified"] is False
    assert report["score_eligible"] is False
    assert report["evaluation"]["fully_controlled"] is False
    assert before == {
        path.name: path.read_bytes() for path in (ash_path, codex / "grade.json")
    }


def test_diagnostic_retake_is_labeled_as_same_source_not_held_out(
    tmp_path: Path,
) -> None:
    ash, codex = _evidence(tmp_path)
    ash_data = json.loads((ash / "result.json").read_text())
    ash_data.update({
        "task": "packaging-legacy-dataclass-pickles-r23b",
        "base_sha": comparison.TASKS[
            "packaging-legacy-dataclass-pickles-r23b"
        ].base_sha,
        "reference_fix_sha": comparison.TASKS[
            "packaging-legacy-dataclass-pickles-r23b"
        ].reference_fix_sha,
        "diagnostic_retake_of": "packaging-legacy-dataclass-pickles",
        "evidence_classification": (
            "diagnostic-retake-same-upstream-source-not-fresh-held-out"
        ),
    })
    (ash / "result.json").write_text(json.dumps(ash_data))
    for name in ("manifest.json", "grade.json"):
        path = codex / name
        data = json.loads(path.read_text())
        data.update({
            "task": "packaging-legacy-dataclass-pickles-r23b",
            "diagnostic_retake_of": "packaging-legacy-dataclass-pickles",
            "evidence_classification": (
                "diagnostic-retake-same-upstream-source-not-fresh-held-out"
            ),
        })
        if name == "manifest.json":
            data["base_sha"] = comparison.TASKS[
                "packaging-legacy-dataclass-pickles-r23b"
            ].base_sha
            data["reference_fix_sha"] = comparison.TASKS[
                "packaging-legacy-dataclass-pickles-r23b"
            ].reference_fix_sha
        path.write_text(json.dumps(data))

    report = comparison.compare(ash, codex)

    assert report["diagnostic_retake_of"] == "packaging-legacy-dataclass-pickles"
    assert report["evidence_classification"] == (
        "diagnostic-retake-same-upstream-source-not-fresh-held-out"
    )
    assert report["preflight_verified"] is False
    assert report["comparison_verified"] is False
    assert any("same upstream source" in item for item in report["comparison_caveats"])
    assert any("different system prompts and built-in tool descriptions" in item for item in report["comparison_caveats"])
    assert any("shell policy is not independently enforced" in item for item in report["comparison_caveats"])


def test_r23b_registry_label_survives_missing_receipt_provenance(
    tmp_path: Path,
) -> None:
    ash, codex = _evidence(tmp_path)
    ash_path = ash / "result.json"
    ash_data = json.loads(ash_path.read_text())
    ash_data.update({
        "task": "packaging-legacy-dataclass-pickles-r23b",
        "base_sha": comparison.TASKS[
            "packaging-legacy-dataclass-pickles-r23b"
        ].base_sha,
        "reference_fix_sha": comparison.TASKS[
            "packaging-legacy-dataclass-pickles-r23b"
        ].reference_fix_sha,
        "diagnostic_retake_of": None,
        "evidence_classification": "source-backed-task",
    })
    ash_path.write_text(json.dumps(ash_data))
    for name in ("manifest.json", "grade.json"):
        path = codex / name
        data = json.loads(path.read_text())
        data.update({
            "task": "packaging-legacy-dataclass-pickles-r23b",
            "diagnostic_retake_of": None,
            "evidence_classification": "source-backed-task",
        })
        if name == "manifest.json":
            data["base_sha"] = comparison.TASKS[
                "packaging-legacy-dataclass-pickles-r23b"
            ].base_sha
            data["reference_fix_sha"] = comparison.TASKS[
                "packaging-legacy-dataclass-pickles-r23b"
            ].reference_fix_sha
        path.write_text(json.dumps(data))

    report = comparison.compare(ash, codex)

    assert report["diagnostic_retake_of"] == "packaging-legacy-dataclass-pickles"
    assert report["evidence_classification"] == (
        "diagnostic-retake-same-upstream-source-not-fresh-held-out"
    )
    assert report["evaluation"]["evidence_classification_verified"] is False
    assert report["preflight_verified"] is False
    assert report["comparison_verified"] is False


@pytest.mark.parametrize("budget,unbounded,asymmetric", [
    (0, True, False), (120000, False, True), (None, False, False),
])
def test_comparison_discloses_unmatched_ash_turn_token_cap(
    tmp_path: Path, budget: int | None, unbounded: bool, asymmetric: bool,
) -> None:
    ash, codex = _evidence(tmp_path)
    path = ash / "result.json"
    data = json.loads(path.read_text())
    if budget is None:
        data.pop("turn_token_budget", None)
    else:
        data["turn_token_budget"] = budget
    path.write_text(json.dumps(data))
    report = comparison.compare(ash, codex)
    evaluation = report["evaluation"]
    assert evaluation["ash_turn_token_budget"] == budget
    assert evaluation["ash_unbounded_turn_configured"] is unbounded
    assert evaluation["asymmetric_ash_turn_token_cap"] is asymmetric
    assert evaluation["matched_context_and_turn_limits"] is False


def test_comparison_does_not_invent_missing_or_malformed_timeout_receipts(
    tmp_path: Path,
) -> None:
    ash, codex = _evidence(tmp_path)
    left = ash / "result.json"
    right = codex / "grade.json"
    ash_data = json.loads(left.read_text())
    codex_data = json.loads(right.read_text())
    ash_data.pop("timed_out")
    codex_data["timed_out"] = "false"
    left.write_text(json.dumps(ash_data))
    right.write_text(json.dumps(codex_data))

    report = comparison.compare(ash, codex)
    assert report["ash"]["timed_out"] is None
    assert report["codex"]["timed_out"] is None
    assert report["codex"]["correct_source"] is True
    assert report["codex"]["scored_pass"] is False


def test_comparison_does_not_conflate_cli_and_grading_timing(
    tmp_path: Path,
) -> None:
    ash, codex = _evidence(tmp_path)
    invocation = codex / "invocation.json"
    data = json.loads(invocation.read_text())
    data["cli_seconds"] = 169
    invocation.write_text(json.dumps(data))
    report = comparison.compare(ash, codex)
    assert report["evaluation"]["matching_cli_timing_available"] is False
    assert report["ash"]["elapsed_seconds"] == 80
    assert report["ash"]["cli_seconds"] == 72.5
    assert report["codex"]["elapsed_seconds"] == 170
    assert report["codex"]["cli_seconds"] == 169


def test_comparison_marks_prework_codex_provider_outage_as_excluded(
    tmp_path: Path,
) -> None:
    ash, codex = _evidence(tmp_path)
    receipt = codex / "grade.json"
    data = json.loads(receipt.read_text())
    data.update({
        "infrastructure_unavailable": True,
        "required_source_changed": False,
        "visible_test_command_executed": False,
        "shell_non_test_commands": 0,
    })
    receipt.write_text(json.dumps(data))
    report = comparison.compare(ash, codex)
    assert report["evaluation"]["excluded_provider_outage"] is True
    assert report["codex"]["scored_pass"] is False


def test_isolated_codex_receipt_is_distinguished_from_legacy_trial(
    tmp_path: Path,
) -> None:
    ash, codex = _evidence(tmp_path)
    receipt = codex / "grade.json"
    data = json.loads(receipt.read_text())
    data.update(
        {
            "native_binary_direct": True,
            "isolated_codex_home": True,
            "isolated_home_xdg": True,
            "memory_backed_auth_copy_cleaned": True,
        }
    )
    receipt.write_text(json.dumps(data))
    report = comparison.compare(ash, codex)
    assert report["evaluation"]["codex_native_binary_direct"] is True
    assert report["evaluation"]["codex_private_home_and_xdg"] is True
    assert report["evaluation"]["codex_ephemeral_auth_cleanup"] is False
    invocation_path = codex / "invocation.json"
    invocation = json.loads(invocation_path.read_text())
    invocation["memory_backed_auth_copy_cleaned"] = True
    invocation_path.write_text(json.dumps(invocation))
    assert comparison.compare(ash, codex)["evaluation"]["codex_ephemeral_auth_cleanup"] is True


@pytest.mark.parametrize("window", [32768, 65536])
def test_comparison_discloses_requested_context_window_parity(
    tmp_path: Path, window: int
) -> None:
    ash, codex = _evidence(tmp_path)
    ash_file = ash / "result.json"
    a = json.loads(ash_file.read_text())
    a["context_tokens"] = 32768
    ash_file.write_text(json.dumps(a))
    codex_file = codex / "grade.json"
    c = json.loads(codex_file.read_text())
    c["context_tokens_requested"] = window
    codex_file.write_text(json.dumps(c))
    report = comparison.compare(ash, codex)
    assert report["evaluation"]["same_requested_context_window"] is (window == 32768)
    assert report["evaluation"]["matched_context_and_turn_limits"] is False


@pytest.mark.parametrize("matching", [True, False])
def test_comparison_rejects_different_visible_or_hidden_oracle_source(
    tmp_path: Path, matching: bool
) -> None:
    ash, codex = _evidence(tmp_path)
    source_hashes = {
        "visible_oracle_sha256": "a" * 64,
        "hidden_oracle_sha256": "b" * 64,
    }
    ash_file = ash / "result.json"
    a = json.loads(ash_file.read_text())
    a.update(source_hashes)
    ash_file.write_text(json.dumps(a))
    codex_file = codex / "manifest.json"
    c = json.loads(codex_file.read_text())
    c.update(source_hashes)
    if not matching:
        c["hidden_oracle_sha256"] = "c" * 64
    codex_file.write_text(json.dumps(c))
    if matching:
        report = comparison.compare(ash, codex)
        assert report["evaluation"]["identical_oracle_source_verified"] is True
        assert report["evaluation"]["identical_oracle_provenance_verified"] is False
    else:
        with pytest.raises(ValueError, match="oracle source fingerprints do not match"):
            comparison.compare(ash, codex)


@pytest.mark.parametrize("tampered", ["task", "base_sha", "reference_fix_sha"])
def test_comparison_rejects_different_upstream_task_or_revision(
    tmp_path: Path, tampered: str
) -> None:
    ash, codex = _evidence(tmp_path)
    path = codex / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest[tampered] = "other"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="different tasks|revisions do not match"):
        comparison.compare(ash, codex)


def test_mismatched_effort_does_not_claim_model_parity(tmp_path: Path) -> None:
    ash, codex = _evidence(tmp_path)
    path = codex / "grade.json"
    grade = json.loads(path.read_text())
    grade["reasoning_effort_requested"] = "high"
    path.write_text(json.dumps(grade))
    report = comparison.compare(ash, codex)
    assert report["requested_model_effort_match"] is False
    assert report["requested_model"] is None
    assert report["evaluation"]["fully_controlled"] is False


@pytest.mark.parametrize("matching", [True, False])
def test_task_fingerprint_requires_identical_declared_objective(
    tmp_path: Path, matching: bool
) -> None:
    ash, codex = _evidence(tmp_path)
    ash_file = ash / "result.json"
    ash_result = json.loads(ash_file.read_text())
    ash_result["task_prompt_sha256"] = "a" * 64
    ash_file.write_text(json.dumps(ash_result))
    codex_file = codex / "manifest.json"
    codex_manifest = json.loads(codex_file.read_text())
    codex_manifest["task_prompt_sha256"] = "a" * 64 if matching else "b" * 64
    codex_file.write_text(json.dumps(codex_manifest))
    if matching:
        report = comparison.compare(ash, codex)
        assert report["evaluation"]["same_task_objective_fingerprint_verified"] is True
    else:
        with pytest.raises(ValueError, match="objectives do not match"):
            comparison.compare(ash, codex)


@pytest.mark.parametrize("matching", [True, False])
def test_comparison_requires_byte_identical_user_prompts_when_recorded(
    tmp_path: Path, matching: bool
) -> None:
    ash, codex = _evidence(tmp_path)
    ash_file = ash / "result.json"
    ash_receipt = json.loads(ash_file.read_text())
    ash_receipt["user_prompt_sha256"] = "a" * 64
    ash_file.write_text(json.dumps(ash_receipt))
    codex_file = codex / "manifest.json"
    codex_receipt = json.loads(codex_file.read_text())
    codex_receipt["user_prompt_sha256"] = "a" * 64 if matching else "b" * 64
    codex_file.write_text(json.dumps(codex_receipt))
    if matching:
        result = comparison.compare(ash, codex)
        assert result["evaluation"]["identical_user_prompts_verified"] is True
    else:
        with pytest.raises(ValueError, match="user prompts do not match"):
            comparison.compare(ash, codex)
