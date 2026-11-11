"""Compare existing Ash/Codex coding receipts without overstating parity.

Read-only operator tool. This never launches a model or evaluates private
source text; both benchmark runners must produce their own graded receipts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"receipt is not an object: {path.name}")
    return data


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing or invalid receipt field: {key}")
    return value


def _numeric(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def compare(ash_dir: Path, codex_dir: Path) -> dict[str, Any]:
    """Return content-free results and explicit comparability limitations."""
    ash = _load_json(ash_dir / "result.json")
    codex = _load_json(codex_dir / "grade.json")
    manifest = _load_json(codex_dir / "manifest.json")
    invocation = _load_json(codex_dir / "invocation.json")

    task = _required_str(ash, "task")
    if _required_str(codex, "task") != task or _required_str(manifest, "task") != task:
        raise ValueError("comparator receipts are for different tasks")
    base = _required_str(ash, "base_sha")
    reference = _required_str(ash, "reference_fix_sha")
    if (
        manifest.get("base_sha") != base
        or manifest.get("reference_fix_sha") != reference
    ):
        raise ValueError("comparator upstream revisions do not match")

    ash_model = _required_str(ash, "provider_model")
    codex_model = _required_str(codex, "model_requested")
    ash_effort = ash.get("reasoning_effort")
    codex_effort = codex.get("reasoning_effort_requested")
    requested_model_effort_match = (
        ash_model == f"openai/{codex_model}"
        and isinstance(ash_effort, str)
        and ash_effort == codex_effort
    )

    ash_oracle = ash.get("oracle") or {}
    codex_oracle = codex.get("oracles") or {}
    if not isinstance(ash_oracle, dict) or not isinstance(codex_oracle, dict):
        raise ValueError("invalid grader oracle result")
    ash_source_correct = bool(
        ash_oracle.get("correct") is True
        and ash.get("protected_files_unchanged") is True
        and ash.get("required_sources_changed") is True
    )
    codex_source_correct = bool(
        codex_oracle.get("correct") is True
        and codex.get("protected_files_unchanged") is True
        and codex.get("required_source_changed") is True
    )

    # Operator-only metadata: no prompts, source, shell commands, account data,
    # stderr, or model-generated text is reproduced by this report.
    prompt_file = codex_dir / "prompt.txt"
    prompt_sha = (
        hashlib.sha256(prompt_file.read_bytes()).hexdigest()
        if prompt_file.is_file()
        else None
    )
    return {
        "schema_version": 1,
        "task": task,
        "base_sha": base,
        "reference_fix_sha": reference,
        "requested_model_effort_match": requested_model_effort_match,
        "requested_model": ash_model if requested_model_effort_match else None,
        "requested_effort": ash_effort if requested_model_effort_match else None,
        "ash": {
            "correct_source": ash_source_correct,
            "visible_test_executed": ash.get("required_tools_succeeded") is True,
            "cli_complete": ash.get("exit_code") == 0 and ash.get("timed_out") is False,
            "scored_pass": ash.get("passed") is True,
            "elapsed_seconds": _numeric(ash, "elapsed_seconds"),
        },
        "codex": {
            "correct_source": codex_source_correct,
            "visible_test_executed": codex.get("visible_test_command_executed") is True,
            "cli_complete": codex.get("exit_code") == 0
            and codex.get("timed_out") is False
            and "turn.completed" in codex.get("terminal_events", []),
            "scored_pass": codex.get("passed") is True,
            "elapsed_seconds": _numeric(codex, "elapsed_seconds"),
            "non_test_shell_commands": codex.get("shell_non_test_commands"),
            "prompt_sha256": prompt_sha,
        },
        "evaluation": {
            # Shared revision pins do not attest that the Codex environment
            # could not inspect the grader or that both oracles were identical.
            "fully_controlled": False,
            "same_upstream_revision_pins": True,
            "identical_oracle_provenance_verified": False,
            "model_and_effort_only_requested_not_attested": True,
            "identical_prompts_verified": False,
            "same_skill_stack": False,
            "same_tool_and_command_authorizations": False,
            "matched_context_and_turn_limits": False,
            "codex_hidden_oracle_unreadable_to_agent_verified": False,
            "comparator_terminal_usage_available": bool(codex.get("usage")),
            "codex_ephemeral_requested": invocation.get("ephemeral") is True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ash-evidence", required=True, type=Path)
    parser.add_argument("--codex-evidence", required=True, type=Path)
    args = parser.parse_args()
    try:
        summary = compare(args.ash_evidence, args.codex_evidence)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # Do not print arbitrary parser or path error messages: local evidence
        # directory names can contain user-owned sensitive text.
        parser.error("invalid or mismatched comparison receipts: " + type(exc).__name__)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
