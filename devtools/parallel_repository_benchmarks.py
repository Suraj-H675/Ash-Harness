"""Run bounded, isolated Ash/plain-Codex repository comparisons concurrently.

Each pair runs its engines sequentially, but distinct pairs may overlap.
Parallel batches are for correctness and throughput, NOT controlled latency
ranking: concurrent provider load can change either engine's response time.
No supervisory model is given access to the tasks or hidden oracle sources.
"""

from __future__ import annotations

import argparse
import asyncio
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
from ash.providers.openai_chatgpt_auth import ChatGPTAuthSession, ChatGPTCredentialStore
from repository_benchmark import TASKS


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


def commands(
    task: str, engine: str, evidence: Path, timeout: int, context: int
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
) -> dict[str, Any]:
    pair_id = f"{task}-r{repetition:02d}"
    directory = root / pair_id
    directory.mkdir(mode=0o700)
    order = (
        ("ash", "codex") if (repetition + order_offset) % 2
        else ("codex", "ash")
    )
    exit_codes: dict[str, int] = {}
    for engine in order:
        evidence = directory / engine
        try:
            exit_codes[engine] = invoke(
                commands(task, engine, evidence, timeout, context), evidence
            )
        except (OSError, subprocess.SubprocessError):
            # Never put paths, provider payloads, or credential errors in
            # the aggregate report. The private stderr file is diagnostic.
            exit_codes[engine] = -1

    result: dict[str, Any] = {
        "task": task,
        "repetition": repetition,
        "engine_order": list(order),
        "runner_exit_codes": exit_codes,
        "comparison_verified": False,
    }
    try:
        comparison = compare(directory / "ash", directory / "codex")
    except (OSError, ValueError, json.JSONDecodeError):
        result["reason"] = "incomplete_or_mismatched_receipts"
    else:
        evaluation = comparison["evaluation"]
        matched = all(
            evaluation[key]
            for key in (
                "same_upstream_revision_pins",
                "same_task_objective_fingerprint_verified",
                "identical_user_prompts_verified",
                "identical_oracle_source_verified",
                "same_requested_context_window",
                "ash_unbounded_turn_configured",
                "codex_native_binary_direct",
                "codex_private_home_and_xdg",
                "codex_ephemeral_auth_cleanup",
            )
        ) and comparison["requested_model_effort_match"]
        matched = matched and not evaluation["excluded_provider_outage"]
        result.update(
            comparison_verified=bool(matched),
            reason="verified" if matched else "provider_outage_or_parity_unverified",
            ash_pass=comparison["ash"]["scored_pass"],
            codex_pass=comparison["codex"]["scored_pass"],
            ash_cli_seconds=comparison["ash"]["cli_seconds"],
            codex_cli_seconds=comparison["codex"]["cli_seconds"],
            timing_available=evaluation["matching_cli_timing_available"],
        )
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
) -> dict[str, Any]:
    if parallel not in (1, 2) or not 1 <= repeats <= 3:
        raise ValueError("parallel workers and repetition count exceed bounds")
    if len(tasks) * repeats > MAX_PAIRS:
        raise ValueError("too many model comparisons in one batch")
    if not tasks or len(tasks) != len(set(tasks)) or any(t not in TASKS for t in tasks):
        raise ValueError("tasks must be distinct known repository scenarios")
    if not 30 <= timeout <= 300 or not 8192 <= context <= 65536:
        raise ValueError("out-of-range model limits")
    if root.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("private benchmark evidence must live outside the repo")
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
    args = parser.parse_args()
    os.umask(0o077)
    if (
        len(args.task) != len(set(args.task))
        or not 1 <= args.repetitions <= 3
        or len(args.task) * args.repetitions > MAX_PAIRS
        or not 30 <= args.timeout <= 300
        or not 8192 <= args.context_tokens <= 65536
    ):
        parser.error("invalid or duplicate task or unsupported model limits")
    root = args.evidence_root
    if root is None:
        root = Path(tempfile.gettempdir()) / (
            "ash-paired-batch-" + secrets.token_hex(10)
        )
    if root.exists() or root.resolve().is_relative_to(ROOT.resolve()):
        parser.error("private benchmark evidence path is not a new external directory")
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
