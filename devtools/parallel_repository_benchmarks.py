"""Run bounded, isolated Ash/plain-Codex repository comparisons concurrently.

Each pair runs its engines sequentially, but distinct pairs may overlap.
Parallel batches are for correctness and throughput, NOT controlled latency
ranking: concurrent provider load can change either engine's response time.
No supervisory model is given access to the tasks or hidden oracle sources.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

from coding_comparison import compare
from codex_repository_benchmark import (
    _require_sealed_comparator_profile,
    _verify_host_connector_isolation,
)
from coding_benchmark import _chatgpt_tmpfs_root
from ash.providers.openai_chatgpt_auth import ChatGPTAuthSession, ChatGPTCredentialStore
from repository_benchmark import (
    TASKS,
    PinnedSourceError,
    hidden_oracle_classification,
    prepare_pair_source,
    task_evidence_classification,
)


ROOT = Path(__file__).resolve().parents[1]
MAX_PARALLEL = 2  # Conserve local RAM and avoid an unbounded provider flood.
MAX_PAIRS = 6
Invoke = Callable[[list[str], Path], int]


def invoke_runner(argv: list[str], evidence: Path) -> int:
    """Retain private subprocess output, never put model traces in terminal."""
    # Both existing runners REQUIRE a previously nonexistent evidence path.
    # Put supervisor logs alongside that path, not inside it.
    with (
        (evidence.parent / f"{evidence.name}.runner.stdout").open("wb") as stdout,
        (evidence.parent / f"{evidence.name}.runner.stderr").open("wb") as stderr,
    ):
        return subprocess.run(
            argv, cwd=ROOT, stdout=stdout, stderr=stderr, check=False
        ).returncode


def preflight_plain_codex_readiness() -> None:
    """Fail before any Ash model call if the matched native Codex is unsealed.

    The child runner repeats these checks and its effective sandbox probe
    before its own dispatch. This early guard only prevents a wasted Ash arm;
    it does not assert identical Ash/Codex runtime tool permissions.
    """
    executable, attested_sha = _require_sealed_comparator_profile()
    readiness = _verify_host_connector_isolation(executable)
    if readiness["native_binary_sha256"] != attested_sha:
        raise RuntimeError("Native Codex no longer matches the sealed attestation")
    _chatgpt_tmpfs_root()


def commands(
    task: str,
    engine: str,
    evidence: Path,
    timeout: int,
    context: int,
    *,
    authorize_model_trial: bool = False,
) -> list[str]:
    if engine == "ash":
        return [
            sys.executable,
            str(ROOT / "devtools/repository_benchmark.py"),
            "--task", task,
            "--chatgpt-model", "gpt-6-luna",
            "--reasoning-effort", "xhigh",
            "--context-tokens", str(context),
            # Plain Codex has no corresponding cumulative turn-token cap.
            # Use Ash's normal uncapped default, retaining matched context,
            # model effort, task inputs, and bounded subprocess deadlines.
            "--turn-token-budget", "0",
            "--timeout", str(timeout),
            "--fair-prompt",
            "--evidence-dir", str(evidence),
        ]
    if engine == "codex":
        return [
            sys.executable,
            str(ROOT / "devtools/codex_repository_benchmark.py"),
            "--task", task,
            "--model", "gpt-6-luna",
            "--reasoning-effort", "xhigh",
            "--context-tokens", str(context),
            "--timeout", str(timeout),
            *(["--authorize-model-trial"] if authorize_model_trial else []),
            "--evidence-dir", str(evidence),
        ]
    raise ValueError("unsupported engine")


def run_pair(
    task: str,
    repetition: int,
    root: Path,
    timeout: int,
    context: int,
    *,
    invoke: Invoke = invoke_runner,
    order_offset: int = 0,
    authorize_model_trial: bool = False,
) -> dict[str, Any]:
    if TASKS[task].offline_only:
        raise ValueError("repository scenario is qualified for offline preflight only")
    if not authorize_model_trial:
        raise ValueError("paired model trials require explicit operator authorization")
    if invoke is invoke_runner:
        preflight_plain_codex_readiness()
    pair_id = f"{task}-r{repetition:02d}"
    directory = root / pair_id
    directory.mkdir(mode=0o700)
    order = (
        ("ash", "codex") if (repetition + order_offset) % 2
        else ("codex", "ash")
    )
    exit_codes: dict[str, int] = {}
    # One fresh, private preparation per pair. Never retain a reference tree
    # or let either arm fetch separately after the first inference begins.
    with tempfile.TemporaryDirectory(prefix="ash-pair-source-") as raw_prepared:
        prepared = Path(raw_prepared)
        digest = None
        if invoke is invoke_runner:
            try:
                digest = prepare_pair_source(TASKS[task], prepared)
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
                # No model has run. Do not classify this as a coding failure.
                failed = {
                    "task": task, "repetition": repetition,
                    "reason": "shared_source_preparation_failed",
                    "failure_type": type(exc).__name__,
                    "runner_exit_codes": {},
                    "comparison_verified": False, "score_eligible": False,
                    "fully_controlled": False,
                }
                if isinstance(exc, PinnedSourceError):
                    failed["source_stage"] = exc.stage
                    failed["source_exit_code"] = exc.exit_code
                    failed["source_revision_role"] = exc.revision_role
                (directory / "summary.json").write_text(
                    json.dumps(failed, indent=2) + "\n"
                )
                return failed
            (directory / "prepared-source.json").write_bytes(
                (prepared / "manifest.json").read_bytes()
            )
        for engine in order:
            evidence = directory / engine
            argv = commands(
                task, engine, evidence, timeout, context,
                authorize_model_trial=authorize_model_trial,
            )
            if digest is not None:
                argv += ["--prepared-source", str(prepared), digest]
            try:
                exit_codes[engine] = invoke(argv, evidence)
            except (OSError, subprocess.SubprocessError):
                # Private supervisor output is diagnostic, not scoring evidence.
                exit_codes[engine] = -1

    comparison_caveats = [
        "system prompts and built-in tool descriptions differ between Ash and native Codex",
        "Ash enforces exact command permissions; native Codex shell policy is not independently enforced",
        "feature checks cover named app and MCP flags; event logs do not inventory every possible host tool",
        "requested model settings do not attest every effective limit",
    ]
    if TASKS[task].diagnostic_retake_of is not None:
        comparison_caveats.append(
            "diagnostic retake of the same upstream source; no fresh held-out evidence"
        )
        comparison_caveats.append(
            "retake eligibility requires matching red/green and per-module ablation preflight results from both arms"
        )
    result: dict[str, Any] = {
        "task": task,
        "diagnostic_retake_of": TASKS[task].diagnostic_retake_of,
        "evidence_classification": task_evidence_classification(TASKS[task]),
        "repetition": repetition,
        "engine_order": list(order),
        "runner_exit_codes": exit_codes,
        "comparison_verified": False,
        "fully_controlled": False,
        "score_eligible": False,
        "preflight_verified": None,
        "host_connector_isolation_verified": None,
        "host_connector_disable_method": None,
        "host_connector_disabled_features": None,
        "native_binary_sha256": None,
        "comparison_scope": "operational_qualified",
        "comparison_caveats": comparison_caveats,
        "hidden_oracle_classification": hidden_oracle_classification(TASKS[task]),
    }
    try:
        comparison = compare(directory / "ash", directory / "codex")
    except (OSError, ValueError, json.JSONDecodeError):
        result["reason"] = "incomplete_or_mismatched_receipts"
    else:
        evaluation = comparison["evaluation"]
        exit_codes_consistent = all(
            exit_codes.get(engine) == (0 if comparison[engine]["scored_pass"] else 1)
            for engine in ("ash", "codex")
        )
        preparation_matches = True
        if digest is not None:
            preparation_matches = (
                comparison["shared_preparation_verified"] is True
                and hashlib.sha256(
                    (directory / "prepared-source.json").read_bytes()
                ).hexdigest() == digest
            )
        matched = (
            comparison["comparison_verified"] is True
            and exit_codes_consistent and preparation_matches
        )
        result.update(
            comparison_verified=bool(matched),
            reason="verified" if matched else "provider_outage_or_parity_unverified",
            fully_controlled=False,
            score_eligible=matched and comparison["score_eligible"] is True,
            runner_exit_codes_consistent=exit_codes_consistent,
            shared_preparation_verified=(
                preparation_matches if digest is not None else None
            ),
            preflight_verified=comparison["preflight_verified"],
            comparison_scope="operational_qualified",
            diagnostic_retake_of=comparison["diagnostic_retake_of"],
            evidence_classification=comparison["evidence_classification"],
            host_connector_isolation_verified=(
                evaluation["host_connector_isolation_verified"]
            ),
            host_connector_disable_method=(
                comparison["codex"]["host_connector_disable_method"]
            ),
            host_connector_disabled_features=(
                comparison["codex"]["host_connector_disabled_features"]
            ),
            native_binary_sha256=comparison["codex"]["native_binary_sha256"],
            ash_pass=comparison["ash"]["scored_pass"],
            codex_pass=comparison["codex"]["scored_pass"],
            # Preserve the evidence that a graded repair can be correct yet
            # fail the complete task (e.g. a timeout after a green test).
            # Missing receipts above never manufacture an engine outcome.
            engine_outcomes={
                engine: {
                    field: comparison[engine][field]
                    for field in (
                        "correct_source", "visible_test_command_executed",
                        "visible_test_command_passed",
                        "observed_edit_after_passing_test", "completion_verified",
                        "verification_order_version", "verification_freshness",
                        "host_connector_isolation_verified",
                        "host_connector_invocation_observed", "score_eligible",
                        "shell_command_policy_enforced",
                        "cli_complete", "timed_out", "scored_pass",
                    )
                    if field in comparison[engine]
                }
                for engine in ("ash", "codex")
            },
            final_source_evidence={
                engine: comparison[engine]["final_source_evidence"]
                for engine in ("ash", "codex")
            },
            ash_cli_seconds=comparison["ash"]["cli_seconds"],
            codex_cli_seconds=comparison["codex"]["cli_seconds"],
            timing_available=evaluation["matching_cli_timing_available"],
            same_verification_order_rubric=evaluation["same_verification_order_rubric"],
            verification_order_evidence_consistent=evaluation["verification_order_evidence_consistent"],
        )
        result["comparison_caveats"] = list(dict.fromkeys(
            [*comparison_caveats, *comparison["comparison_caveats"]]
        ))
    (directory / "summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def run_batch(
    tasks: list[str],
    repeats: int,
    parallel: int,
    root: Path,
    timeout: int,
    context: int,
    *,
    invoke: Invoke = invoke_runner,
    authorize_model_trial: bool = False,
) -> dict[str, Any]:
    if parallel not in (1, 2) or not 1 <= repeats <= 3:
        raise ValueError("parallel workers and repetition count exceed bounds")
    if len(tasks) * repeats > MAX_PAIRS:
        raise ValueError("too many model comparisons in one batch")
    if not tasks or len(tasks) != len(set(tasks)) or any(t not in TASKS for t in tasks):
        raise ValueError("tasks must be distinct known repository scenarios")
    if any(TASKS[t].offline_only for t in tasks):
        raise ValueError("repository scenario is qualified for offline preflight only")
    if not 30 <= timeout <= 300 or not 8192 <= context <= 65536:
        raise ValueError("out-of-range model limits")
    if root.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("private benchmark evidence must live outside the repo")
    if not authorize_model_trial:
        raise ValueError("paired model trials require explicit operator authorization")
    if invoke is invoke_runner:
        preflight_plain_codex_readiness()
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    jobs = [
        (task, i, offset)
        for offset, task in enumerate(tasks)
        for i in range(1, repeats + 1)
    ]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = [
            executor.submit(
                run_pair, task, rep, root, timeout, context,
                invoke=invoke, order_offset=offset,
                authorize_model_trial=authorize_model_trial,
            )
            for task, rep, offset in jobs
        ]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda entry: (entry["task"], entry["repetition"]))
    batch = {
        "schema_version": 1,
        "model_requested": "gpt-6-luna",
        "effort_requested": "xhigh",
        "context_requested": context,
        "timeout_seconds_per_engine": timeout,
        "parallel_pairs": parallel,
        "uncontended_latency_mode": parallel == 1,
        "latency_note": (
            "Sequential single-pair mode; model and tool parity limitations remain"
            if parallel == 1
            else "Concurrent model load; do not use elapsed times for speed rankings"
        ),
        "results": results,
    }
    (root / "batch.json").write_text(
        json.dumps(batch, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return batch


def refresh_existing_chatgpt_access(timeout: int) -> bool:
    """Refresh an already authorized plan login before launching workers.

    Never generate new credentials or print the token. Fail closed if the
    registration cannot cover one full model-execution deadline.
    """
    access = asyncio.run(ChatGPTAuthSession().access_token())
    del access
    account = ChatGPTCredentialStore().active()
    return bool(account and account.access_usable(now=time.time() + timeout + 30))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append", required=True, choices=sorted(TASKS))
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--parallel", type=int, default=2, choices=[1, 2])
    parser.add_argument("--timeout", type=int, default=170)
    parser.add_argument("--context-tokens", type=int, default=32768)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument(
        "--authorize-model-trial",
        action="store_true",
        help="Explicitly authorize model inference for every pair in this batch",
    )
    args = parser.parse_args()
    os.umask(0o077)
    if any(TASKS[t].offline_only for t in args.task):
        parser.error("repository scenario is qualified for offline preflight only")
    if (
        len(args.task) != len(set(args.task))
        or not 1 <= args.repetitions <= 3
        or len(args.task) * args.repetitions > MAX_PAIRS
        or not 30 <= args.timeout <= 300
        or not 8192 <= args.context_tokens <= 65536
    ):
        parser.error("invalid or duplicate task or unsupported model limits")
    if not args.authorize_model_trial:
        parser.error(
            "Paired model trials require --authorize-model-trial before "
            "credential access or running either engine"
        )
    root = args.evidence_root
    if root is None:
        root = Path(tempfile.gettempdir()) / (
            "ash-paired-batch-" + secrets.token_hex(10)
        )
    if root.exists() or root.resolve().is_relative_to(ROOT.resolve()):
        parser.error("private benchmark evidence path is not a new external directory")
    try:
        preflight_plain_codex_readiness()
    except (OSError, ValueError, RuntimeError):
        parser.error("sealed plain Codex comparator unavailable; no engine launched")
    try:
        access_covered = refresh_existing_chatgpt_access(args.timeout)
    except Exception:
        parser.error("existing ChatGPT authorization refresh failed")
    if not access_covered:
        parser.error("existing ChatGPT plan access cannot cover the trial")
    try:
        report = run_batch(
            args.task, args.repetitions, args.parallel, root,
            args.timeout, args.context_tokens,
            authorize_model_trial=args.authorize_model_trial,
        )
    except ValueError:
        parser.error("invalid scenario configuration or private evidence path")
    print(json.dumps({
        "evidence_root": str(root),
        "parallel_pairs": report["parallel_pairs"],
        "uncontended_latency_mode": report["uncontended_latency_mode"],
        "results": report["results"],
    }, indent=2))
    return 0 if all(row["comparison_verified"] for row in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
