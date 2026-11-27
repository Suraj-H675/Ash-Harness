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

from codex_repository_benchmark import (
    HOST_APP_FEATURES, HOST_CONNECTOR_CHECK_METHOD, summarize_codex_events,
)
from evaluation_files import VERIFICATION_ORDER_VERSION, verification_freshness
from repository_benchmark import (
    TASKS,
    ash_completion_verified,
    hidden_oracle_sha256,
    observed_edit_after_passing_test,
    shared_user_prompt,
    task_evidence_classification,
)


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


def _recorded_bool(data: dict[str, Any], key: str) -> bool | None:
    """Distinguish a genuine recorded false from missing/invalid evidence."""
    value = data.get(key)
    return value if type(value) is bool else None


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def compare(ash_dir: Path, codex_dir: Path) -> dict[str, Any]:
    """Return content-free results and explicit comparability limitations."""
    ash = _load_json(ash_dir / "result.json")
    codex = _load_json(codex_dir / "grade.json")
    manifest = _load_json(codex_dir / "manifest.json")
    invocation = _load_json(codex_dir / "invocation.json")
    exact_test = manifest.get("visible_test")
    event_trace: dict[str, Any] | None = None
    event_trace_valid = False
    try:
        event_trace = summarize_codex_events(
            codex_dir / "codex.jsonl",
            exact_test if isinstance(exact_test, str) and exact_test else None,
        )
        event_trace_valid = True
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        event_trace = None

    task = _required_str(ash, "task")
    if _required_str(codex, "task") != task or _required_str(manifest, "task") != task:
        raise ValueError("comparator receipts are for different tasks")
    registered_task = TASKS.get(task)
    base = _required_str(ash, "base_sha")
    reference = _required_str(ash, "reference_fix_sha")
    if (
        manifest.get("base_sha") != base
        or manifest.get("reference_fix_sha") != reference
    ):
        raise ValueError("comparator upstream revisions do not match")
    if registered_task is not None and (
        base != registered_task.base_sha
        or reference != registered_task.reference_fix_sha
    ):
        raise ValueError("comparator revisions do not match the registered task")
    same_upstream_revision_pins = True
    ash_task_hash = ash.get("task_prompt_sha256")
    codex_task_hash = manifest.get("task_prompt_sha256")
    registered_task_hash = (
        hashlib.sha256(registered_task.prompt.encode()).hexdigest()
        if registered_task is not None
        else None
    )
    objective_verified = (
        isinstance(ash_task_hash, str)
        and isinstance(codex_task_hash, str)
        and len(ash_task_hash) == len(codex_task_hash) == 64
        and ash_task_hash == codex_task_hash
        and (registered_task_hash is None or ash_task_hash == registered_task_hash)
    )
    if (
        ash_task_hash is not None
        and codex_task_hash is not None
        and not objective_verified
    ):
        raise ValueError("comparator objectives do not match")
    ash_user_hash = ash.get("user_prompt_sha256")
    codex_user_hash = manifest.get("user_prompt_sha256")
    registered_user_hash = (
        hashlib.sha256(shared_user_prompt(registered_task).encode()).hexdigest()
        if registered_task is not None
        else None
    )
    same_user_prompt = (
        isinstance(ash_user_hash, str)
        and isinstance(codex_user_hash, str)
        and len(ash_user_hash) == len(codex_user_hash) == 64
        and ash_user_hash == codex_user_hash
        and (registered_user_hash is None or ash_user_hash == registered_user_hash)
    )
    if (
        ash_user_hash is not None
        and codex_user_hash is not None
        and not same_user_prompt
    ):
        raise ValueError("comparator user prompts do not match")
    expected_oracle_hashes = (
        {
            "visible_oracle_sha256": hashlib.sha256(
                registered_task.visible_source.encode()
            ).hexdigest(),
            "hidden_oracle_sha256": hidden_oracle_sha256(registered_task),
        }
        if registered_task is not None
        else None
    )
    oracle_source_identical = all(
        isinstance(ash.get(key), str)
        and isinstance(manifest.get(key), str)
        and len(ash[key]) == len(manifest[key]) == 64
        and ash[key] == manifest[key]
        and (
            expected_oracle_hashes is None
            or ash[key] == expected_oracle_hashes[key]
        )
        for key in ("visible_oracle_sha256", "hidden_oracle_sha256")
    )
    if (
        any(
            ash.get(key) is not None and manifest.get(key) is not None
            for key in ("visible_oracle_sha256", "hidden_oracle_sha256")
        )
        and not oracle_source_identical
    ):
        raise ValueError("comparator oracle source fingerprints do not match")

    retake_values = [
        source.get("diagnostic_retake_of")
        for source in (ash, manifest, codex)
        if "diagnostic_retake_of" in source
    ]
    classification_values = [
        source.get("evidence_classification")
        for source in (ash, manifest, codex)
        if "evidence_classification" in source
    ]
    if retake_values and any(value != retake_values[0] for value in retake_values):
        raise ValueError("comparator diagnostic retake provenance does not match")
    if classification_values and any(
        value != classification_values[0] for value in classification_values
    ):
        raise ValueError("comparator evidence classification does not match")
    recorded_provenance_matches = bool(
        len(retake_values) == len(classification_values) == 3
        and isinstance(classification_values[0], str)
        and classification_values[0]
    )
    registered_retake_of = (
        registered_task.diagnostic_retake_of if registered_task is not None else None
    )
    registered_classification = (
        task_evidence_classification(registered_task)
        if registered_task is not None
        else None
    )
    if registered_task is not None:
        diagnostic_retake_of = registered_retake_of
        provenance_verified = bool(
            recorded_provenance_matches
            and all(value == registered_retake_of for value in retake_values)
            and all(value == registered_classification for value in classification_values)
        )
        evidence_classification = (
            registered_classification
            if provenance_verified or registered_retake_of is not None
            else "legacy-receipt-provenance-unrecorded"
        )
    else:
        diagnostic_retake_of = (
            retake_values[0]
            if recorded_provenance_matches and isinstance(retake_values[0], str)
            else None
        )
        evidence_classification = (
            classification_values[0]
            if recorded_provenance_matches
            else "legacy-receipt-provenance-unrecorded"
        )
        provenance_verified = recorded_provenance_matches
    preflight_verified: bool | None = None
    if diagnostic_retake_of is not None:
        ash_preflight = ash.get("preflight")
        codex_preflight = manifest.get("preflight")
        red_green_keys = (
            "base_visible_exit", "base_hidden_exit",
            "reference_visible_exit", "reference_hidden_exit",
        )
        def red_green(preflight: object) -> bool:
            return bool(
                isinstance(preflight, dict)
                and all(type(preflight.get(key)) is int for key in red_green_keys)
                and preflight["base_visible_exit"] != 0
                and preflight["base_hidden_exit"] != 0
                and preflight["reference_visible_exit"] == 0
                and preflight["reference_hidden_exit"] == 0
            )

        def ablations_are_red(preflight: object, editable: object) -> bool:
            if (
                not isinstance(preflight, dict)
                or not isinstance(editable, list)
                or not editable
                or any(not isinstance(path, str) for path in editable)
            ):
                return False
            results = preflight.get("editable_ablation_results")
            if not isinstance(results, list) or len(results) != len(editable):
                return False
            observed_paths: list[str] = []
            for row in results:
                if not isinstance(row, dict):
                    return False
                path = row.get("path")
                visible_exit = row.get("visible_oracle_exit")
                hidden_exit = row.get("hidden_oracle_exit")
                if (
                    not isinstance(path, str)
                    or type(visible_exit) is not int
                    or type(hidden_exit) is not int
                    or (visible_exit == 0 and hidden_exit == 0)
                ):
                    return False
                observed_paths.append(path)
            return sorted(observed_paths) == sorted(editable) and len(
                set(observed_paths)
            ) == len(observed_paths)

        preflight_verified = bool(
            isinstance(ash_preflight, dict)
            and isinstance(codex_preflight, dict)
            and red_green(ash_preflight)
            and red_green(codex_preflight)
            and all(
                ash_preflight.get(key) == codex_preflight.get(key)
                for key in red_green_keys
            )
            and all(
                isinstance(ash_preflight.get(key), list)
                and ash_preflight.get(key) == codex_preflight.get(key)
                for key in (
                    "reference_added_paths", "reference_removed_paths",
                    "reference_modified_paths",
                )
            )
            and ash_preflight.get("editable_ablation_results")
            == codex_preflight.get("editable_ablation_results")
            and ablations_are_red(
                ash_preflight, manifest.get("editable")
            )
            and ablations_are_red(
                codex_preflight, manifest.get("editable")
            )
        )
    comparison_caveats = [
        "Ash and native Codex have different system prompts and built-in tool descriptions.",
        "Ash enforces exact command permissions; native Codex shell policy is not independently enforced.",
        "Feature checks cover the named app and MCP flags; event logs record observed calls, not every available host tool.",
    ]
    if diagnostic_retake_of is not None:
        comparison_caveats.append(
            "This is a diagnostic retake of the same upstream source, with no fresh held-out evidence."
        )
        comparison_caveats.append(
            "Retake eligibility requires matching red/green and per-module ablation preflight receipts from both arms."
        )

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
    # The Ash receipt's elapsed_seconds includes preflight and grading,
    # whereas the Codex receipt's elapsed_seconds is only the CLI. Use the
    # instrumented child-process intervals for any speed comparison.
    ash_cli_seconds = _numeric(ash, "cli_seconds")
    codex_cli_seconds = _numeric(invocation, "cli_seconds")
    codex_elapsed = _numeric(codex, "elapsed_seconds")
    cli_timing_compatible = bool(
        ash_cli_seconds is not None
        and codex_cli_seconds is not None
        and codex_elapsed is not None
        and abs(codex_cli_seconds - codex_elapsed) <= 0.02
    )

    recorded_host_isolation = _recorded_bool(
        codex, "host_connector_isolation_verified"
    )
    recorded_host_invocation = _recorded_bool(
        codex, "host_connector_invocation_observed"
    )
    trace_host_invocation = (
        event_trace.get("host_connector_invocation_observed")
        if event_trace_valid and event_trace is not None
        else None
    )
    host_connector_invocation_observed = (
        True
        if trace_host_invocation is True or recorded_host_invocation is True
        else False
        if trace_host_invocation is False and recorded_host_invocation is False
        else None
    )
    recorded_score_eligible = _recorded_bool(codex, "score_eligible")
    trace_matches_grade = bool(
        event_trace_valid
        and isinstance(exact_test, str)
        and exact_test
        and (registered_task is None or exact_test == registered_task.command_grant)
        and event_trace is not None
        and all(
            _recorded_bool(codex, key) is not None
            and _recorded_bool(codex, key) is event_trace.get(key)
            for key in (
                "visible_test_command_executed",
                "visible_test_command_passed",
                "host_connector_invocation_observed",
            )
        )
        and (
            type(codex.get("visible_test_command_count")) is int
            and codex.get("visible_test_command_count")
            == event_trace.get("visible_test_command_count")
        )
    )
    expected_disabled_features = sorted(HOST_APP_FEATURES)
    feature_check_verified = bool(
        invocation.get("host_connector_isolation_verified") is True
        and invocation.get("host_connector_disable_method")
        == HOST_CONNECTOR_CHECK_METHOD
        and invocation.get("host_connector_disabled_features")
        == expected_disabled_features
        and _valid_sha256(invocation.get("native_binary_sha256"))
        and invocation.get("native_binary_direct") is True
        and codex.get("native_binary_direct") is True
        and recorded_host_isolation is True
        and host_connector_invocation_observed is False
        and recorded_score_eligible is True
        and codex.get("event_log_valid") is True
        and trace_matches_grade
        and codex.get("native_binary_sha256") == invocation.get("native_binary_sha256")
    )
    if host_connector_invocation_observed is True or recorded_host_isolation is False:
        host_connector_isolation_verified = False
    elif feature_check_verified:
        host_connector_isolation_verified = True
    else:
        host_connector_isolation_verified = None
    if host_connector_invocation_observed is True or recorded_score_eligible is False:
        codex_score_eligible = False
    elif feature_check_verified:
        codex_score_eligible = True
    else:
        codex_score_eligible = None

    # A claimed pass must satisfy the same task-level conditions as the
    # runner. Genuine failed tasks remain comparable; missing or contradictory
    # evidence is not a failure score, it is ineligible for comparison.
    codex_claimed_pass = _recorded_bool(codex, "passed")
    ash_claimed_pass = _recorded_bool(ash, "passed")
    # Old receipts retain their original rubric. A versioned receipt cannot
    # claim completion merely by supplying a successful subprocess exit.
    completion_version = ash.get("completion_evidence_version")
    completion_verified = (
        ash_completion_verified(ash)
        if type(completion_version) is int and completion_version == 1 else None
    )
    legacy_completion = (
        "completion_evidence_version" not in ash
        and "completion_verified" not in ash
    )
    completion_consistent = legacy_completion or (
        completion_verified is not None
        and _recorded_bool(ash, "completion_verified") is completion_verified
        and (not ash_claimed_pass or completion_verified)
    )
    ash_calls = ash.get("tool_calls")
    observed_ash_edit = (
        observed_edit_after_passing_test(ash_calls, exact_test)
        if isinstance(exact_test, str) and exact_test
        and isinstance(ash_calls, list) and all(isinstance(c, dict) for c in ash_calls)
        else None
    )
    observed_codex_edit = (
        event_trace.get("observed_edit_after_passing_test")
        if event_trace_valid and event_trace is not None else None
    )
    # Preserve historical grades, but never pool different grading rubrics.
    # Recompute the order classification rather than trusting a claimed pass.
    def order_version(receipt: dict[str, Any]) -> int | None:
        if ("verification_order_version" not in receipt
                and "verification_freshness" not in receipt):
            return 0  # legacy rubric
        version = receipt.get("verification_order_version")
        return version if type(version) is int and version == VERIFICATION_ORDER_VERSION else None

    ash_order_version = order_version(ash)
    codex_order_version = order_version(codex)
    same_order_rubric = ash_order_version is not None and ash_order_version == codex_order_version

    def order_consistent(receipt: dict[str, Any], version: int | None,
                         observed: bool | None, trace_valid: bool) -> bool:
        if version == 0:
            return True
        return (
            version == VERIFICATION_ORDER_VERSION and trace_valid
            and "observed_edit_after_passing_test" in receipt
            and receipt["observed_edit_after_passing_test"] is observed
            and receipt.get("verification_freshness") == verification_freshness(observed)
            and (receipt.get("passed") is not True or observed is False)
        )

    order_evidence_consistent = (
        same_order_rubric
        and order_consistent(ash, ash_order_version, observed_ash_edit,
                             isinstance(exact_test, str) and bool(exact_test)
                             and isinstance(ash_calls, list)
                             and all(isinstance(c, dict) for c in ash_calls))
        and order_consistent(codex, codex_order_version, observed_codex_edit,
                             event_trace_valid)
    )
    if not same_order_rubric:
        comparison_caveats.append("Verification-order scoring rubrics differ or are unsupported.")
    if ash_order_version == codex_order_version == VERIFICATION_ORDER_VERSION:
        comparison_caveats.append(
            "Observed stale verification disqualifies a full pass; no observed late edit does not attest tested source hashes."
        )
    score_claims_consistent = bool(
        completion_consistent
        and order_evidence_consistent
        and codex_claimed_pass is not None and ash_claimed_pass is not None
        and (
            not codex_claimed_pass
            or (
                event_trace is not None
                and event_trace.get("visible_test_command_executed") is True
                and event_trace.get("visible_test_command_passed") is True
                and event_trace.get("terminal_events") == ["turn.completed"]
                and codex.get("terminal_events") == ["turn.completed"]
                and codex.get("exit_code") == 0
                and codex.get("timed_out") is False
                and codex_source_correct
                and (
                    registered_task is None
                    or not registered_task.require_single_visible_test
                    or event_trace.get("visible_test_command_count") == 1
                )
            )
        )
        and (
            not ash_claimed_pass
            or (
                ash_source_correct
                and ash.get("required_tools_succeeded") is True
                and ash.get("visible_test_command_executed") is True
                and ash.get("visible_test_command_passed") is True
                and ash.get("exit_code") == 0
                and ash.get("timed_out") is False
                and (
                    registered_task is None
                    or not registered_task.require_single_visible_test
                    or ash.get("visible_test_command_count") == 1
                )
            )
        )
    )
    auth_cleanup_verified = bool(
        invocation.get("ephemeral") is True
        and invocation.get("memory_backed_auth_copy_cleaned") is True
        and codex.get("memory_backed_auth_copy_cleaned") is True
    )
    if codex_score_eligible is True and (not auth_cleanup_verified or not score_claims_consistent):
        codex_score_eligible = False
    comparison_verified = bool(
        same_upstream_revision_pins
        and objective_verified
        and same_user_prompt
        and oracle_source_identical
        and provenance_verified
        and (diagnostic_retake_of is None or preflight_verified is True)
        and requested_model_effort_match
        and type(ash.get("context_tokens")) is int
        and ash.get("context_tokens") == codex.get("context_tokens_requested")
        and type(ash.get("turn_token_budget")) is int
        and ash.get("turn_token_budget") == 0
        and codex.get("native_binary_direct") is True
        and codex.get("isolated_codex_home") is True
        and codex.get("isolated_home_xdg") is True
        and auth_cleanup_verified
        and score_claims_consistent
        and feature_check_verified
        and codex.get("infrastructure_unavailable") is not True
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
        "diagnostic_retake_of": diagnostic_retake_of,
        "evidence_classification": evidence_classification,
        "preflight_verified": preflight_verified,
        "comparison_caveats": comparison_caveats,
        "comparison_verified": comparison_verified,
        "score_eligible": comparison_verified,
        "requested_model_effort_match": requested_model_effort_match,
        "requested_model": ash_model if requested_model_effort_match else None,
        "requested_effort": ash_effort if requested_model_effort_match else None,
        "ash": {
            "correct_source": ash_source_correct,
            "visible_test_command_executed": _recorded_bool(
                ash, "visible_test_command_executed"
            ),
            "visible_test_command_passed": _recorded_bool(
                ash, "visible_test_command_passed"
            ),
            "completion_verified": completion_verified,
            "observed_edit_after_passing_test": observed_ash_edit,
            "verification_order_version": ash_order_version,
            "verification_freshness": verification_freshness(observed_ash_edit),
            "cli_complete": ash.get("exit_code") == 0 and ash.get("timed_out") is False
            and (legacy_completion or completion_verified is True),
            "timed_out": _recorded_bool(ash, "timed_out"),
            "scored_pass": ash.get("passed") is True,
            "elapsed_seconds": _numeric(ash, "elapsed_seconds"),
            "cli_seconds": ash_cli_seconds,
            "elapsed_scope": "preflight_cli_and_grading",
        },
        "codex": {
            "correct_source": codex_source_correct,
            "visible_test_command_executed": (
                event_trace.get("visible_test_command_executed")
                if event_trace_valid and event_trace is not None
                else _recorded_bool(codex, "visible_test_command_executed")
            ),
            "visible_test_command_passed": (
                event_trace.get("visible_test_command_passed")
                if event_trace_valid and event_trace is not None
                else _recorded_bool(codex, "visible_test_command_passed")
            ),
            "observed_edit_after_passing_test": observed_codex_edit,
            "verification_order_version": codex_order_version,
            "verification_freshness": verification_freshness(observed_codex_edit),
            "recorded_visible_test_command_executed": _recorded_bool(
                codex, "visible_test_command_executed"
            ),
            "recorded_visible_test_command_passed": _recorded_bool(
                codex, "visible_test_command_passed"
            ),
            "visible_test_command_count": (
                event_trace.get("visible_test_command_count")
                if event_trace_valid and event_trace is not None
                else codex.get("visible_test_command_count")
                if type(codex.get("visible_test_command_count")) is int
                else None
            ),
            "host_connector_isolation_verified": host_connector_isolation_verified,
            "host_connector_invocation_observed": host_connector_invocation_observed,
            "host_connector_disable_method": (
                HOST_CONNECTOR_CHECK_METHOD
                if invocation.get("host_connector_disable_method")
                == HOST_CONNECTOR_CHECK_METHOD
                else None
            ),
            "host_connector_disabled_features": (
                expected_disabled_features
                if invocation.get("host_connector_disabled_features")
                == expected_disabled_features
                else None
            ),
            "native_binary_sha256": (
                invocation.get("native_binary_sha256")
                if _valid_sha256(invocation.get("native_binary_sha256"))
                else None
            ),
            "score_eligible": codex_score_eligible,
            "shell_command_policy_enforced": _recorded_bool(
                codex, "shell_command_policy_enforced"
            ),
            "event_trace_verified": event_trace_valid,
            "event_metadata_matches_grade": trace_matches_grade,
            "cli_complete": codex.get("exit_code") == 0
            and codex.get("timed_out") is False
            and "turn.completed" in codex.get("terminal_events", []),
            "timed_out": _recorded_bool(codex, "timed_out"),
            "scored_pass": codex.get("passed") is True,
            "elapsed_seconds": codex_elapsed,
            "cli_seconds": codex_cli_seconds,
            "elapsed_scope": "cli_only",
            "non_test_shell_commands": codex.get("shell_non_test_commands"),
            "prompt_sha256": prompt_sha,
        },
        "evaluation": {
            "matching_cli_timing_available": cli_timing_compatible,
            # A provider outage before any Codex work is not a coding loss.
            # Other parity gaps below still apply even to scorable trials.
            "excluded_provider_outage": (
                codex.get("infrastructure_unavailable") is True
            ),
            # Shared revision pins do not attest that the Codex environment
            # could not inspect the grader or that both oracles were identical.
            "fully_controlled": False,
            "same_upstream_revision_pins": True,
            "diagnostic_retake_preflight_verified": preflight_verified,
            "evidence_classification_verified": provenance_verified,
            "same_task_objective_fingerprint_verified": objective_verified,
            "identical_oracle_provenance_verified": False,
            "identical_oracle_source_verified": oracle_source_identical,
            "model_and_effort_only_requested_not_attested": True,
            "identical_user_prompts_verified": same_user_prompt,
            "identical_system_prompts_verified": False,
            "same_skill_stack": False,
            "same_tool_and_command_authorizations": False,
            "matched_context_and_turn_limits": False,
            # Native plain Codex exposes no corresponding per-turn cumulative
            # token cap to this runner. A positive cap on Ash can stop a
            # multi-file task before its deadline, independently of model
            # quality; do not certify such pairs as equally budgeted.
            "ash_turn_token_budget": (
                ash["turn_token_budget"]
                if type(ash.get("turn_token_budget")) is int
                else None
            ),
            "ash_unbounded_turn_configured": (
                type(ash.get("turn_token_budget")) is int
                and ash["turn_token_budget"] == 0
            ),
            "asymmetric_ash_turn_token_cap": (
                type(ash.get("turn_token_budget")) is int
                and ash["turn_token_budget"] > 0
            ),
            "same_requested_context_window": (
                type(ash.get("context_tokens")) is int
                and type(codex.get("context_tokens_requested")) is int
                and ash["context_tokens"] == codex["context_tokens_requested"]
            ),
            "codex_hidden_oracle_unreadable_to_agent_verified": False,
            "comparator_terminal_usage_available": bool(codex.get("usage")),
            "codex_ephemeral_requested": invocation.get("ephemeral") is True,
            "codex_native_binary_direct": codex.get("native_binary_direct") is True,
            "codex_private_home_and_xdg": (
                codex.get("isolated_codex_home") is True
                and codex.get("isolated_home_xdg") is True
            ),
            "codex_ephemeral_auth_cleanup": auth_cleanup_verified,
            "score_claims_consistent": score_claims_consistent,
            "same_verification_order_rubric": same_order_rubric,
            "verification_order_evidence_consistent": order_evidence_consistent,
            "host_connector_isolation_verified": host_connector_isolation_verified,
            "host_connector_feature_check_verified": feature_check_verified,
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
