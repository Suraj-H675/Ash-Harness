"""Run small, scored coding tasks through the real Ash CLI and a local model.

This is an operator-run evaluation, not a unit test or a claim about model
quality. Each trial receives fresh Ash state and a disposable workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ash.core.activity import EDIT_TOOL_NAMES, summarize_operational_events
from ash.core.session import SessionStore


REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / ".venv/bin/python"
PRESERVED = b"Preserve this unrelated file.\n"
CALC = b"def add(a, b):\n    return a - b\n"
CALC_TEST = b"from calc import add\nassert add(2, 3) == 5\n"
HIDDEN_ADD_ORACLE = (
    "from calc import add\n"
    "for a, b, expected in [(-7, 2, -5), (0, 0, 0), "
    "(100, -50, 50), (1.5, 0.25, 1.75)]:\n"
    "    assert add(a, b) == expected\n"
)
PRICING = (
    b"from decimal import Decimal\n"
    b"\n"
    b"def discounted_total(prices: list[Decimal], discount_percent: Decimal) -> Decimal:\n"
    b"    subtotal = sum(prices, start=Decimal('0'))\n"
    b"    return subtotal - discount_percent  # BUG: percentage is not currency\n"
)
CHECKOUT = (
    b"from decimal import Decimal\n"
    b"from inventory.pricing import discounted_total\n"
    b"\n"
    b"def quote(items: list[tuple[str, str]], discount_percent: str) -> str:\n"
    b"    prices = [Decimal(price) for _, price in items]\n"
    b"    return f'{discounted_total(prices, Decimal(discount_percent)):.2f}'\n"
)
CHECKOUT_TEST = (
    b"from inventory.checkout import quote\n"
    b"assert quote([('book', '10.00'), ('pen', '2.00')], '25') == '9.00'\n"
)
CHECKOUT_README = (
    b"# Checkout fixture\n"
    b"The quote API receives decimal price strings and a percentage (0..100) "
    b"and returns the discounted total with two decimal places.\n"
)
HIDDEN_CHECKOUT_ORACLE = (
    "from inventory.checkout import quote\n"
    "assert quote([('a', '10.00')], '0') == '10.00'\n"
    "assert quote([('a', '10.00'), ('b', '30.00')], '50') == '20.00'\n"
    "assert quote([('a', '7.50'), ('b', '2.50')], '20') == '8.00'\n"
    "assert quote([], '35') == '0.00'\n"
)
BILLING_TAX = (
    b"from decimal import Decimal\n"
    b"\n"
    b"def tax_amount(subtotal: Decimal, percent: Decimal) -> Decimal:\n"
    b"    return subtotal + subtotal * percent / Decimal('100')  # BUG: includes principal\n"
)
BILLING_RECEIPT = (
    b"from decimal import Decimal\n"
    b"from billing.tax import tax_amount\n"
    b"\n"
    b"def total_due(prices: list[Decimal], tax_percent: Decimal) -> str:\n"
    b"    subtotal = sum(prices, start=Decimal('0'))\n"
    b"    return f'{subtotal + tax_amount(subtotal, tax_percent):.1f}'  # BUG: wrong precision\n"
)
BILLING_TEST = (
    b"from decimal import Decimal\n"
    b"from billing.tax import tax_amount\n"
    b"from billing.receipt import total_due\n"
    b"\n"
    b"assert tax_amount(Decimal('10'), Decimal('10')) == Decimal('1')\n"
    b"assert total_due([Decimal('10')], Decimal('10')) == '11.00'\n"
)
BILLING_README = (
    b"# Billing fixture\n"
    b"tax_amount(subtotal, percent) returns only the tax, as an unrounded Decimal.\n"
    b"total_due(prices, tax_percent) returns the subtotal plus tax formatted to "
    b"exactly two decimal places. Keep both public functions and their inputs.\n"
)
HIDDEN_BILLING_ORACLE = (
    "from decimal import Decimal\n"
    "from billing.tax import tax_amount\n"
    "from billing.receipt import total_due\n"
    "assert tax_amount(Decimal('99.95'), Decimal('10')) == Decimal('9.995')\n"
    "assert tax_amount(Decimal('0'), Decimal('35')) == Decimal('0')\n"
    "assert total_due([], Decimal('8')) == '0.00'\n"
    "assert total_due([Decimal('1.99'), Decimal('2.01')], Decimal('7.5')) == '4.30'\n"
    "assert total_due([Decimal('12.35')], Decimal('0')) == '12.35'\n"
)


@dataclass(frozen=True)
class Task:
    prompt: str
    initial_files: dict[str, bytes]
    command_grant: str | None
    required_tool: str
    # Original source paths the agent must actually change; every other
    # initial file is protected against edits, replacements, and deletion.
    editable_files: frozenset[str] = frozenset()


TASKS = {
    "marker": Task(
        "Create marker.txt containing exactly ASH_FREE_MODEL_OK (no newline). "
        "Use the file tools and preserve keep.txt.",
        {},
        None,
        "edit",
    ),
    "build-run": Task(
        "Create hello.py that prints exactly ASH_REAL_MODEL_OK and then execute "
        "the exact command `python3 -B hello.py` with run_command to verify it. "
        "Only that test command is authorized. Preserve keep.txt.",
        {},
        "python3 -B hello.py",
        "run_command",
    ),
    "repair-test": Task(
        "Repair the bug in calc.py so that the existing test_calc.py passes. "
        "Read the existing files, make the smallest correct change, then execute "
        "the exact command `python3 -B test_calc.py` with run_command. "
        "Only that test command is authorized. Do not edit test_calc.py or keep.txt.",
        {"calc.py": CALC, "test_calc.py": CALC_TEST},
        "python3 -B test_calc.py",
        "edit",
        frozenset({"calc.py"}),
    ),
    "multi-file": Task(
        "A checkout calculates percentage discounts incorrectly. Investigate "
        "the inventory package and its test, fix the root cause, and execute "
        "the exact command `python3 -B test_checkout.py` using run_command. "
        "Only that test command is authorized. Preserve the quote() API, "
        "test_checkout.py, README.md, and keep.txt. Make the smallest needed fix.",
        {
            "inventory/__init__.py": b"",
            "inventory/pricing.py": PRICING,
            "inventory/checkout.py": CHECKOUT,
            "test_checkout.py": CHECKOUT_TEST,
            "README.md": CHECKOUT_README,
        },
        "python3 -B test_checkout.py",
        "edit",
        frozenset({"inventory/pricing.py"}),
    ),
    "two-file": Task(
        "The billing package has two bugs: tax calculation and receipt totals. "
        "Inspect billing/tax.py, billing/receipt.py, README.md, and test_billing.py. "
        "Fix both modules without changing their public functions, then execute "
        "the exact command `python3 -B test_billing.py` with run_command. "
        "Only that test command is authorized. Preserve README.md, "
        "test_billing.py, billing/__init__.py, and keep.txt.",
        {
            "billing/__init__.py": b"",
            "billing/tax.py": BILLING_TAX,
            "billing/receipt.py": BILLING_RECEIPT,
            "test_billing.py": BILLING_TEST,
            "README.md": BILLING_README,
        },
        "python3 -B test_billing.py",
        "edit",
        frozenset({"billing/tax.py", "billing/receipt.py"}),
    ),
}


def command(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def oracle_python(
    workspace: Path, script: str | None = None, *, code: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Execute model-written code inside a read-only, networkless OS sandbox."""
    if (script is None) == (code is None):
        raise ValueError("provide exactly one script or inline oracle")
    argv = [
        "bwrap",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--die-with-parent",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        str(workspace),
        "/workspace",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/tmp",
        "--chdir",
        "/workspace",
        "--",
        "/usr/bin/python3",
    ]
    argv.extend(["-B", "-c", code] if code is not None else ["-B", script or ""])
    for directory in ("/lib", "/lib64"):
        if Path(directory).exists():
            argv[argv.index("--proc") : argv.index("--proc")] = [
                "--ro-bind",
                directory,
                directory,
            ]
    return command(argv, cwd=workspace, env={"PATH": "/usr/bin:/bin"}, timeout=12)


def grade(task_name: str, workspace: Path) -> dict[str, Any]:
    try:
        if task_name == "marker":
            if (workspace / "marker.txt").is_symlink():
                return {"correct": False, "oracle_error": "output is a symlink"}
            return {
                "correct": (workspace / "marker.txt").read_bytes()
                == b"ASH_FREE_MODEL_OK"
            }
        if task_name == "build-run":
            if (workspace / "hello.py").is_symlink():
                return {"correct": False, "oracle_error": "output is a symlink"}
            result = oracle_python(workspace, "hello.py")
            return {
                "correct": result.returncode == 0
                and result.stdout.strip() == "ASH_REAL_MODEL_OK",
                "oracle_exit": result.returncode,
                "oracle_stderr": result.stderr[-250:],
            }
        if task_name == "multi-file":
            if (workspace / "inventory/checkout.py").is_symlink() or (
                workspace / "inventory/pricing.py"
            ).is_symlink():
                return {"correct": False, "oracle_error": "source is a symlink"}
            visible = oracle_python(workspace, "test_checkout.py")
            hidden = oracle_python(workspace, code=HIDDEN_CHECKOUT_ORACLE)
            return {
                "correct": visible.returncode == 0 and hidden.returncode == 0,
                "visible_oracle_exit": visible.returncode,
                "hidden_oracle_exit": hidden.returncode,
                "oracle_stderr": (visible.stderr + hidden.stderr)[-250:],
            }
        if task_name == "two-file":
            if any(
                (workspace / name).is_symlink()
                for name in TASKS[task_name].initial_files
            ):
                return {"correct": False, "oracle_error": "fixture contains a symlink"}
            visible = oracle_python(workspace, "test_billing.py")
            hidden = oracle_python(workspace, code=HIDDEN_BILLING_ORACLE)
            return {
                "correct": visible.returncode == 0 and hidden.returncode == 0,
                "visible_oracle_exit": visible.returncode,
                "hidden_oracle_exit": hidden.returncode,
                "oracle_stderr": (visible.stderr + hidden.stderr)[-250:],
            }
        if (workspace / "test_calc.py").is_symlink() or (
            workspace / "calc.py"
        ).is_symlink():
            return {"correct": False, "oracle_error": "test or source is a symlink"}
        visible = oracle_python(workspace, "test_calc.py")
        hidden = oracle_python(workspace, code=HIDDEN_ADD_ORACLE)
        return {
            "correct": visible.returncode == 0 and hidden.returncode == 0,
            "visible_oracle_exit": visible.returncode,
            "hidden_oracle_exit": hidden.returncode,
            "oracle_stderr": (visible.stderr + hidden.stderr)[-250:],
        }
    except (OSError, subprocess.SubprocessError) as error:
        return {"correct": False, "oracle_error": str(error)[:250]}


def check_workspace_artifacts(
    task_name: str, workspace: Path
) -> tuple[bool, bool, dict[str, str]]:
    """Check protected fixture bytes and required source changes, not model claims."""
    task = TASKS[task_name]
    keep = workspace / "keep.txt"
    protected = keep.is_file() and not keep.is_symlink()
    if protected:
        protected = keep.read_bytes() == PRESERVED
    for filename, expected in task.initial_files.items():
        if filename in task.editable_files:
            continue
        target = workspace / filename
        protected = (
            protected
            and target.is_file()
            and not target.is_symlink()
            and target.read_bytes() == expected
        )
    allowed_files = {"keep.txt", *task.initial_files}
    if task_name == "marker":
        allowed_files.add("marker.txt")
    elif task_name == "build-run":
        allowed_files.add("hello.py")
    files = {
        str(path.relative_to(workspace)): (
            "SYMLINK"
            if path.is_symlink()
            else hashlib.sha256(path.read_bytes()).hexdigest()
        )
        for path in workspace.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    protected = (
        protected and set(files) <= allowed_files and "SYMLINK" not in files.values()
    )
    sources_changed = all(
        files.get(filename) is not None
        and files[filename] != hashlib.sha256(task.initial_files[filename]).hexdigest()
        for filename in task.editable_files
    )
    return protected, sources_changed, files


def run_trial(
    task_name: str,
    evidence: Path,
    endpoint: str,
    timeout: int,
    model_hash: str | None,
    trial_index: int = 1,
) -> dict[str, Any]:
    task = TASKS[task_name]
    trial_dir = evidence / (
        task_name if trial_index == 1 else f"{task_name}-{trial_index}"
    )
    trial_dir.mkdir()
    start = time.monotonic()
    result: dict[str, Any] = {
        "task": task_name,
        "trial": trial_index,
        "model_sha256": model_hash,
    }
    with tempfile.TemporaryDirectory(prefix="trial-", dir=trial_dir) as scratch_name:
        scratch = Path(scratch_name)
        workspace, home, db, tmp = [
            scratch / name for name in ("workspace", "home", "db", "tmp")
        ]
        for path in (workspace, home, db, tmp):
            path.mkdir()
        (workspace / "keep.txt").write_bytes(PRESERVED)
        for name, content in task.initial_files.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (home / ".ash").mkdir()
        (home / ".ash/ash.toml").write_text(
            'sandbox_backend = "native"\n'
            "[custom_providers.eval]\n"
            f'base_url = "{endpoint}/v1"\n'
            'auth_mode = "none"\n'
            '[custom_providers.eval.model_capabilities."local-model"]\n'
            "native_tools = true\n"
            "context_window = 16384\n"
            "max_output_tokens = 384\n",
            encoding="utf-8",
        )
        env = {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "USER": "ash-eval",
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "TERM": "dumb",
            "NO_COLOR": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(tmp),
            "PYTHONPATH": str(REPO / "src"),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "ASH_MODEL": "eval/local-model",
            "ASH_MAX_CONTEXT_TOKENS": "8192",
            "ASH_MAX_COMPLETION_TOKENS": "384",
            "ASH_MAX_TURN_TOTAL_TOKENS": "40000",
        }
        setup_started = time.monotonic()
        setup = command(
            [str(PYTHON), "-m", "ash", "trust", "add", str(workspace)],
            cwd=workspace,
            env=env,
            timeout=20,
        )
        if setup.returncode:
            raise RuntimeError(f"isolated trust setup failed: {setup.stderr[-400:]}")
        if task.command_grant is not None:
            grant = command(
                [
                    str(PYTHON),
                    "-m",
                    "ash",
                    "permissions",
                    "allow",
                    "run_command",
                    "--exact",
                    "command_line=" + json.dumps(task.command_grant),
                ],
                cwd=workspace,
                env=env,
                timeout=20,
            )
            if grant.returncode:
                raise RuntimeError(
                    f"isolated command grant failed: {grant.stderr[-400:]}"
                )
        setup_seconds = round(time.monotonic() - setup_started, 3)

        invocation = [
            str(PYTHON),
            "-m",
            "ash",
            "--db-directory",
            str(db),
            "--mode",
            "auto_edit",
            "-p",
            task.prompt,
            "--output-format",
            "json",
        ]
        cli_started = time.monotonic()
        with (
            (trial_dir / "ash.stdout").open("wb") as stdout,
            (trial_dir / "ash.stderr").open("wb") as stderr,
        ):
            child = subprocess.Popen(
                invocation,
                cwd=workspace,
                env=env,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            timed_out = False
            try:
                exit_code = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    exit_code = child.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    exit_code = child.wait(timeout=5)
        cli_seconds = round(time.monotonic() - cli_started, 3)

        try:
            payload = json.loads((trial_dir / "ash.stdout").read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        store = SessionStore(db / "sessions.db")
        sessions = store.list_sessions(project_path=str(workspace))
        calls: list[dict[str, Any]] = []
        audit_ok = False
        usage: dict[str, int] = {}
        terminal_events: list[str] = []
        last_event_type: str | None = None
        performance: dict[str, Any] = summarize_operational_events([])
        if sessions:
            session = store.load_session(sessions[0].session_id)
            calls = [
                {
                    "tool": call.tool_name,
                    "success": call.error is None,
                    "executed": call.executed,
                    "approved": call.approved,
                    "arguments": call.arguments,
                    "error": (call.error or "")[:300],
                }
                for call in session.tool_calls
            ]
            audit_ok = not store.verify_audit_log(session.session_id)
            used = store.get_session_usage(session.session_id)
            usage = {
                "prompt_tokens": used.prompt_tokens,
                "completion_tokens": used.completion_tokens,
            }
            events = store.list_runtime_events(session.session_id, limit=10_000)
            performance = summarize_operational_events(events)
            if events:
                last_event_type = str(events[-1].event.get("type", ""))
                terminal_events = [
                    str(item.event.get("type", ""))
                    for item in events
                    if item.event.get("type")
                    in {"turn.completed", "turn.cancelled", "turn.error"}
                ]
        outcome = grade(task_name, workspace)
        protected, sources_changed, files = check_workspace_artifacts(
            task_name, workspace
        )
        required = any(
            (
                call["tool"] in EDIT_TOOL_NAMES
                if task.required_tool == "edit"
                else call["tool"] == task.required_tool
            )
            and call["success"]
            and call["executed"]
            for call in calls
        )
        required = required and sources_changed
        if task.command_grant is not None:
            required = required and any(
                call["tool"] == "run_command"
                and call["success"]
                and call["executed"]
                and call["arguments"].get("command_line") == task.command_grant
                for call in calls
            )
        result.update(
            {
                "exit_code": exit_code,
                "timed_out": timed_out,
                "elapsed_seconds": round(time.monotonic() - start, 2),
                "setup_seconds": setup_seconds,
                "cli_seconds": cli_seconds,
                "performance": performance,
                "task_correct": outcome["correct"],
                "oracle": outcome,
                "protected_files_unchanged": protected,
                "required_sources_changed": sources_changed,
                "required_tools_succeeded": required,
                "audit_ok": audit_ok,
                "terminal_events": terminal_events,
                "last_runtime_event_type": last_event_type,
                "out_of_scope_tool_calls": sum(
                    "outside project scope" in call["error"] for call in calls
                ),
                "failed_tool_calls": sum(not call["success"] for call in calls),
                "usage": usage,
                "model_response": str(payload.get("response", ""))[:800],
                "tool_calls": calls,
                "workspace_files_sha256": files,
                "passed": bool(
                    not timed_out
                    and exit_code == 0
                    and outcome["correct"]
                    and protected
                    and required
                    and audit_ok
                ),
            }
        )
        (trial_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    result["scratch_removed"] = True
    (trial_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        required=True,
        help="New directory; never reuses prior results",
    )
    parser.add_argument("--endpoint", default="http://127.0.0.1:18105")
    parser.add_argument(
        "--model-file", type=Path, help="Optional local GGUF to hash as run provenance"
    )
    parser.add_argument("--timeout", type=int, default=285)
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Independent trials per selected task, maximum 5",
    )
    parser.add_argument(
        "--task",
        choices=tuple(TASKS),
        action="append",
        help="Repeat to select tasks; default is all five",
    )
    args = parser.parse_args()
    if args.task and len(args.task) != len(set(args.task)):
        parser.error("duplicate --task selection; use --repeat for independent trials")
    if not (20 <= args.timeout <= 300):
        parser.error("--timeout must be between 20 and 300 seconds")
    if not (1 <= args.repeat <= 5):
        parser.error("--repeat must be between 1 and 5")
    address = urlsplit(args.endpoint)
    if (
        address.scheme != "http"
        or address.hostname != "127.0.0.1"
        or not address.port
        or address.username
        or address.password
        or address.path
        or address.query
        or address.fragment
    ):
        parser.error("--endpoint must use a local 127.0.0.1 model server")
    if not PYTHON.is_file():
        parser.error(f"repository virtual environment missing: {PYTHON}")
    if not shutil.which("bwrap"):
        parser.error("Bubblewrap is required to safely execute model-written code")
    try:
        with urllib.request.urlopen(args.endpoint + "/health", timeout=5) as response:
            if response.status != 200:
                raise ValueError(f"model server returned {response.status}")
        with urllib.request.urlopen(
            args.endpoint + "/v1/models", timeout=5
        ) as response:
            advertised_models = [
                str(entry.get("id", "")) for entry in json.load(response)["data"]
            ]
    except (OSError, ValueError) as error:
        parser.error(f"local model server not ready: {error}")
    model_hash = None
    if args.model_file:
        digest = hashlib.sha256()
        with args.model_file.open("rb") as model:
            for chunk in iter(lambda: model.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        model_hash = digest.hexdigest()
    evidence: Path = args.evidence_dir.resolve()
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    head = command(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        env={"PATH": "/usr/bin:/bin"},
        timeout=10,
    ).stdout.strip()
    runtime_diff = command(
        ["git", "diff", "--binary", "HEAD", "--", "src/ash"],
        cwd=REPO,
        env={"PATH": "/usr/bin:/bin"},
        timeout=10,
    )
    if runtime_diff.returncode:
        raise RuntimeError(f"cannot capture runtime diff: {runtime_diff.stderr[-400:]}")
    results = []
    for task_name in args.task or list(TASKS):
        for trial in range(1, args.repeat + 1):
            result = run_trial(
                task_name, evidence, args.endpoint, args.timeout, model_hash, trial
            )
            results.append(result)
            print(
                json.dumps(
                    {
                        "task": task_name,
                        "trial": trial,
                        "passed": result["passed"],
                        "task_correct": result["task_correct"],
                        "required_tools_succeeded": result["required_tools_succeeded"],
                        "elapsed_seconds": result["elapsed_seconds"],
                    }
                ),
                flush=True,
            )
    summary = {
        "ash_head": head,
        "runtime_diff_sha256": hashlib.sha256(runtime_diff.stdout.encode()).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": str(PYTHON),
        "endpoint": args.endpoint,
        "model_sha256": model_hash,
        "advertised_model_ids": advertised_models,
        "timeout_seconds": args.timeout,
        "repeat": args.repeat,
        "tasks": [item["task"] for item in results],
        "passed": sum(bool(item["passed"]) for item in results),
        "total": len(results),
        "results": results,
    }
    (evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"evidence={evidence} passed={summary['passed']}/{summary['total']}")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
