"""Offline safeguards for graded harness-comparison receipts."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


_path = Path(__file__).resolve().parents[2] / "devtools/coding_comparison.py"
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
                "passed": True,
                "oracle": {"correct": True},
                "protected_files_unchanged": True,
                "required_sources_changed": True,
                "required_tools_succeeded": True,
                "exit_code": 0,
                "timed_out": False,
                "elapsed_seconds": 80,
            }
        )
    )
    (codex / "manifest.json").write_text(
        json.dumps(
            {"task": "real-bug", "base_sha": "a" * 40, "reference_fix_sha": "b" * 40}
        )
    )
    (codex / "invocation.json").write_text(json.dumps({"ephemeral": True}))
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
    return ash, codex


def test_comparison_separates_correct_code_from_terminal_completion(
    tmp_path: Path,
) -> None:
    ash, codex = _evidence(tmp_path)
    report = comparison.compare(ash, codex)
    assert report["requested_model_effort_match"] is True
    assert report["ash"]["scored_pass"] is True
    assert report["codex"]["correct_source"] is True
    assert report["codex"]["cli_complete"] is False
    assert report["codex"]["scored_pass"] is False
    assert report["evaluation"]["fully_controlled"] is False
    assert report["evaluation"]["same_upstream_revision_pins"] is True
    assert report["evaluation"]["identical_oracle_provenance_verified"] is False
    assert report["evaluation"]["same_tool_and_command_authorizations"] is False
    assert report["codex"]["prompt_sha256"] is not None
    assert "SECRET-TEST-PROMPT" not in repr(report)
    assert report["evaluation"]["codex_native_binary_direct"] is False


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
    assert report["evaluation"]["codex_ephemeral_auth_cleanup"] is True


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
