"""Run small, scored coding tasks through Ash's real CLI and a selected model.

This is an operator-run evaluation, not a unit test or a claim about model
quality. Each trial receives fresh Ash state and a disposable workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
# Original three-module integration fixture. Keep the behavioral oracle outside
# the model-visible project: it probes variants beyond the visible example.
EVENTLOG_PARSER = (
    b"import json\n"
    b"\n"
    b"def parse_events(lines):\n"
    b"    events = []\n"
    b"    for line in lines:\n"
    b"        if not line.strip():\n"
    b"            continue\n"
    b"        event = json.loads(line)  # BUG: one bad record aborts the stream\n"
    b"        if event.get('request_id'):\n"
    b"            events.append(event)\n"
    b"    return events\n"
)
EVENTLOG_AGGREGATE = (
    b"def aggregate(events):\n"
    b"    results = {}\n"
    b"    for event in events:\n"
    b"        ms = event.get('duration_ms')\n"
    b"        if not isinstance(ms, int) or ms < 0:\n"
    b"            continue\n"
    b"        key = event['request_id']\n"
    b"        count, previous = results.get(key, (0, 0))\n"
    b"        results[key] = (count + 1, ms or previous)  # BUG: 0 is meaningful\n"
    b"    return results\n"
)
EVENTLOG_RENDER = (
    b"def render_csv(summary):\n"
    b"    return ''.join(\n"
    b"        f'{key},{count},{ms}\\n'\n"
    b"        for key, (count, ms) in sorted(summary.items(), reverse=True)\n"
    b"    )  # BUG: report order is reversed\n"
)
EVENTLOG_TEST = (
    b"from eventlog.parser import parse_events\n"
    b"from eventlog.aggregate import aggregate\n"
    b"from eventlog.render import render_csv\n"
    b"\n"
    b"lines = [\n"
    b'    \'{"request_id":"z","duration_ms":12}\',\n'
    b"    'not JSON',\n"
    b"    'null',\n"
    b'    \'{"request_id":"a","duration_ms":9}\',\n'
    b'    \'{"request_id":"a","duration_ms":0}\',\n'
    b'    \'{"request_id":"z","duration_ms":true}\',\n'
    b'    \'{"request_id":"","duration_ms":2}\',\n'
    b"    '\\n',\n"
    b"]\n"
    b"assert render_csv(aggregate(parse_events(lines))) == 'a,2,0\\nz,1,12\\n'\n"
)
EVENTLOG_README = (
    b"# Eventlog integration repair\n"
    b"Each line is an independent JSON record. Ignore blank lines, malformed JSON,\n"
    b"non-object JSON, and objects without a nonempty string request_id. Preserve\n"
    b"the order and fields of the remaining objects in parse_events(lines).\n"
    b"aggregate(events) returns {request_id: (accepted_count, last_duration_ms)}.\n"
    b"Accept only nonnegative integer duration_ms values, explicitly excluding\n"
    b"booleans. The latest accepted duration wins, including zero. Invalid\n"
    b"duration values do not increment count or replace the last valid duration.\n"
    b"render_csv(summary) sorts identifiers lexicographically (case-sensitive),\n"
    b"outputs request_id,count,last_duration_ms per line, and appends a newline\n"
    b"to each row; the empty report is an empty string. Keep public APIs intact.\n"
)
HIDDEN_EVENTLOG_ORACLE = (
    "from eventlog.parser import parse_events\n"
    "from eventlog.aggregate import aggregate\n"
    "from eventlog.render import render_csv\n"
    "valid = {'request_id':'task-A','duration_ms':0,'extra':{'ok':True}}\n"
    "assert parse_events(['', '\\n', 'bad-json', 'null', '[1]', '42',\n"
    '    \'{"request_id":"","duration_ms":4}\',\n'
    '    \'{"request_id":7,"duration_ms":4}\',\n'
    '    \'{"request_id":"task-A","duration_ms":0,"extra":{"ok":true}}\']) == [valid]\n'
    'assert parse_events([\'{"request_id":"ß","duration_ms":2}\']) == '
    "[{'request_id':'ß','duration_ms':2}]\n"
    "assert aggregate([]) == {}\n"
    "assert aggregate([{'request_id':'a','duration_ms':4},\n"
    " {'request_id':'a','duration_ms':0},\n"
    " {'request_id':'a','duration_ms':False},\n"
    " {'request_id':'a','duration_ms':-3},\n"
    " {'request_id':'a','duration_ms':2.5},\n"
    " {'request_id':'b','duration_ms':17}]) == {'a':(2,0),'b':(1,17)}\n"
    "assert render_csv({'b':(1,17),'A':(3,0),'a':(2,99)}) == "
    "'A,3,0\\na,2,99\\nb,1,17\\n'\n"
    "assert render_csv({}) == ''\n"
    "assert render_csv(aggregate(parse_events([\n"
    ' \'{"request_id":"B","duration_ms":1}\',\n'
    ' \'{"request_id":"B","duration_ms":0}\',\n'
    ' \'{"request_id":"A","duration_ms":20}\'\n'
    "]))) == 'A,1,20\\nB,2,0\\n'\n"
)
DEFAULT_TURN_TOKEN_BUDGET = 40_000
MAX_EVAL_TURN_TOKEN_BUDGET = 250_000
DEFAULT_EVAL_CONTEXT_TOKENS = 8_192
MAX_EVAL_CONTEXT_TOKENS = 16_384
MAX_REMOTE_CONTEXT_TOKENS = 65_536
MAX_REMOTE_TRIALS = 2
REMOTE_TURN_ITERATIONS = 16
OPENROUTER_CATALOG_URL = "https://openrouter.ai/api/v1/models"
FREE_MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*:free\Z")


def validate_free_openrouter_model(model_id: str) -> str:
    if not FREE_MODEL_ID.fullmatch(model_id):
        raise ValueError("OpenRouter evaluation requires an exact vendor/model:free ID")
    return model_id


def validate_free_openrouter_catalog(
    model_id: str, payload: object, *, context_tokens: int
) -> None:
    """Refuse unknown, billed, or tool-incompatible models before inference."""
    validate_free_openrouter_model(model_id)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("OpenRouter model catalog has an invalid shape")
    matches = [
        item
        for item in payload["data"]
        if isinstance(item, dict) and item.get("id") == model_id
    ]
    if len(matches) != 1:
        raise ValueError("Requested free model was not uniquely found in the catalog")
    item = matches[0]
    pricing = item.get("pricing")
    if not isinstance(pricing, dict):
        raise ValueError("Free model pricing could not be verified")
    for field in ("prompt", "completion"):
        value = pricing.get(field)
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("Free model pricing could not be verified")
        try:
            price = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("Free model pricing could not be verified") from exc
        if not price.is_finite() or price != 0:
            raise ValueError("OpenRouter model is not verified as zero-cost")
    # Free token rates do not imply zero per-request or reasoning charges.
    for field in (
        "request",
        "internal_reasoning",
        "input_cache_read",
        "input_cache_write",
    ):
        value = pricing.get(field)
        if value is None:
            continue
        try:
            if isinstance(value, bool) or Decimal(str(value)) != 0:
                raise ValueError("OpenRouter model has an unverified extra charge")
        except InvalidOperation as exc:
            raise ValueError("OpenRouter model has an invalid extra price") from exc
    if pricing.get("overrides"):
        raise ValueError("OpenRouter conditional pricing overrides are unsupported")
    parameters = item.get("supported_parameters")
    if not isinstance(parameters, list) or "tools" not in parameters:
        raise ValueError("Free model lacks verified native tool-call support")
    window = item.get("context_length")
    if (
        not isinstance(window, int)
        or isinstance(window, bool)
        or window < context_tokens
    ):
        raise ValueError("Free model context window is below the requested limit")


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
    "event-log": Task(
        "A line-delimited JSON telemetry pipeline produces incorrect reports "
        "and crashes on malformed input. Diagnose the package using README.md "
        "and test_eventlog.py, fix all three modules in eventlog/, and execute "
        "the exact command `python3 -B test_eventlog.py` with run_command. "
        "Only this command is authorized. Preserve all public function names, "
        "README.md, test_eventlog.py, eventlog/__init__.py, and keep.txt.",
        {
            "eventlog/__init__.py": b"",
            "eventlog/parser.py": EVENTLOG_PARSER,
            "eventlog/aggregate.py": EVENTLOG_AGGREGATE,
            "eventlog/render.py": EVENTLOG_RENDER,
            "test_eventlog.py": EVENTLOG_TEST,
            "README.md": EVENTLOG_README,
        },
        "python3 -B test_eventlog.py",
        "edit",
        frozenset(
            {"eventlog/parser.py", "eventlog/aggregate.py", "eventlog/render.py"}
        ),
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
        if task_name in {"two-file", "event-log"}:
            if any(
                (workspace / name).is_symlink()
                for name in TASKS[task_name].initial_files
            ):
                return {"correct": False, "oracle_error": "fixture contains a symlink"}
            visible_script, hidden_test = (
                ("test_billing.py", HIDDEN_BILLING_ORACLE)
                if task_name == "two-file"
                else ("test_eventlog.py", HIDDEN_EVENTLOG_ORACLE)
            )
            visible = oracle_python(workspace, visible_script)
            hidden = oracle_python(workspace, code=hidden_test)
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
    turn_token_budget: int = DEFAULT_TURN_TOKEN_BUDGET,
    context_tokens: int = DEFAULT_EVAL_CONTEXT_TOKENS,
    remote_model_id: str | None = None,
    remote_api_key: str | None = None,
    completion_tokens: int = 384,
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
        "turn_token_budget": turn_token_budget,
        "context_tokens": context_tokens,
        "completion_tokens": completion_tokens,
        "provider_model": (
            f"openrouter/{remote_model_id}" if remote_model_id else "eval/local-model"
        ),
    }
    if remote_model_id:
        validate_free_openrouter_model(remote_model_id)
        if not remote_api_key:
            raise ValueError("OpenRouter free evaluation requires an API key")
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
        config = 'sandbox_backend = "native"\n'
        if not remote_model_id:
            config += (
                "[custom_providers.eval]\n"
                f'base_url = "{endpoint}/v1"\n'
                'auth_mode = "none"\n'
                '[custom_providers.eval.model_capabilities."local-model"]\n'
                "native_tools = true\n"
                "context_window = 16384\n"
                f"max_output_tokens = {completion_tokens}\n"
            )
        (home / ".ash/ash.toml").write_text(config, encoding="utf-8")
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
            "ASH_MODEL": (
                f"openrouter/{remote_model_id}"
                if remote_model_id
                else "eval/local-model"
            ),
            "ASH_MAX_CONTEXT_TOKENS": str(context_tokens),
            "ASH_MAX_COMPLETION_TOKENS": str(completion_tokens),
            "ASH_MAX_TURN_TOTAL_TOKENS": str(turn_token_budget),
        }
        if remote_model_id:
            # A bounded fraction of the free tier's daily request quota;
            # provider retries are additional, so keep trial count low too.
            env["ASH_MAX_TURN_ITERATIONS"] = str(REMOTE_TURN_ITERATIONS)
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
        # Setup and permission commands need no inference credential. Pass it
        # only to the model-bearing CLI child, never to config or argv.
        cli_env = (
            {**env, "OPENROUTER_API_KEY": remote_api_key or ""}
            if remote_model_id
            else env
        )
        cli_started = time.monotonic()
        with (
            (trial_dir / "ash.stdout").open("wb") as stdout,
            (trial_dir / "ash.stderr").open("wb") as stderr,
        ):
            child = subprocess.Popen(
                invocation,
                cwd=workspace,
                env=cli_env,
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
    parser.add_argument(
        "--endpoint", help="Local 127.0.0.1 model server (default port 18105)"
    )
    parser.add_argument(
        "--openrouter-free-model",
        metavar="VENDOR/MODEL:free",
        help=(
            "Explicit opt-in: evaluate a verified zero-cost OpenRouter model "
            "using OPENROUTER_API_KEY from the process environment"
        ),
    )
    parser.add_argument(
        "--model-file", type=Path, help="Optional local GGUF to hash as run provenance"
    )
    parser.add_argument("--timeout", type=int, default=285)
    parser.add_argument(
        "--completion-tokens",
        type=int,
        help="Per-request output budget; defaults to 384 local or 4096 remote",
    )
    parser.add_argument(
        "--context-tokens",
        type=int,
        default=DEFAULT_EVAL_CONTEXT_TOKENS,
        help=(
            "Maximum per-request model context (default 8192, "
            "local max 16384, free remote max 65536)"
        ),
    )
    parser.add_argument(
        "--turn-token-budget",
        type=int,
        default=DEFAULT_TURN_TOKEN_BUDGET,
        help=(
            "Maximum prompt plus completion tokens per Ash trial (benchmark-only, "
            "default 40000); changing it invalidates direct comparisons"
        ),
    )
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
        help="Repeat to select tasks; default is all six",
    )
    args = parser.parse_args()
    if args.task and len(args.task) != len(set(args.task)):
        parser.error("duplicate --task selection; use --repeat for independent trials")
    if not (20 <= args.timeout <= 300):
        parser.error("--timeout must be between 20 and 300 seconds")
    maximum_context = (
        MAX_REMOTE_CONTEXT_TOKENS
        if args.openrouter_free_model
        else MAX_EVAL_CONTEXT_TOKENS
    )
    if not (4_096 <= args.context_tokens <= maximum_context):
        parser.error(f"--context-tokens must be between 4096 and {maximum_context}")
    if not (2_000 <= args.turn_token_budget <= MAX_EVAL_TURN_TOKEN_BUDGET):
        parser.error(
            f"--turn-token-budget must be between 2000 and {MAX_EVAL_TURN_TOKEN_BUDGET}"
        )
    if not (1 <= args.repeat <= 5):
        parser.error("--repeat must be between 1 and 5")
    completion_tokens = (
        args.completion_tokens
        if args.completion_tokens is not None
        else (4_096 if args.openrouter_free_model else 384)
    )
    if not (128 <= completion_tokens <= 16_384):
        parser.error("--completion-tokens must be between 128 and 16384")
    remote_key: str | None = None
    if args.openrouter_free_model:
        if args.endpoint or args.model_file:
            parser.error("remote free evaluation cannot use --endpoint or --model-file")
        if not args.task or len(args.task) * args.repeat > MAX_REMOTE_TRIALS:
            parser.error(
                "remote free evaluation requires 1-2 explicitly selected trials"
            )
        try:
            validate_free_openrouter_model(args.openrouter_free_model)
        except ValueError as exc:
            parser.error(str(exc))
        remote_key = os.environ.get("OPENROUTER_API_KEY")
        if not remote_key:
            parser.error("OPENROUTER_API_KEY is required for free-model evaluation")
    # Validate user options before checking Linux-only execution dependencies.
    # Invalid free-model requests must fail consistently on macOS CI too;
    # actual execution still requires Bubblewrap before any provider traffic.
    if not PYTHON.is_file():
        parser.error(f"repository virtual environment missing: {PYTHON}")
    if not shutil.which("bwrap"):
        parser.error("Bubblewrap is required to safely execute model-written code")
    if args.openrouter_free_model:
        try:
            # Public catalog check is unauthenticated. Never send the key
            # here, or guess that a model is free from its name alone.
            with urllib.request.urlopen(OPENROUTER_CATALOG_URL, timeout=10) as response:
                data = response.read(8_000_001)
                if len(data) > 8_000_000:
                    raise ValueError("OpenRouter catalog exceeded 8 MB")
            validate_free_openrouter_catalog(
                args.openrouter_free_model,
                json.loads(data),
                context_tokens=args.context_tokens,
            )
        except (OSError, ValueError) as exc:
            parser.error(f"OpenRouter free-model preflight refused: {exc}")
        endpoint = "https://openrouter.ai/api/v1"
        advertised_models = [args.openrouter_free_model]
    else:
        endpoint = args.endpoint or "http://127.0.0.1:18105"
        address = urlsplit(endpoint)
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
        try:
            with urllib.request.urlopen(endpoint + "/health", timeout=5) as response:
                if response.status != 200:
                    raise ValueError(f"model server returned {response.status}")
            with urllib.request.urlopen(endpoint + "/v1/models", timeout=5) as response:
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
                task_name,
                evidence,
                endpoint,
                args.timeout,
                model_hash,
                trial,
                args.turn_token_budget,
                args.context_tokens,
                remote_model_id=args.openrouter_free_model,
                remote_api_key=remote_key,
                completion_tokens=completion_tokens,
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
        "endpoint": endpoint,
        "provider_model": (
            f"openrouter/{args.openrouter_free_model}"
            if args.openrouter_free_model
            else "eval/local-model"
        ),
        "model_sha256": model_hash,
        "advertised_model_ids": advertised_models,
        "timeout_seconds": args.timeout,
        "turn_token_budget": args.turn_token_budget,
        "context_tokens": args.context_tokens,
        "completion_tokens": completion_tokens,
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
