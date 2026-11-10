"""Independently grade a historical bug repair in a pinned upstream repository.

Opt-in, operator-run evaluation. Never run against a developer's live checkout.
The visible test is in the disposable source tree; the hidden oracle is not.
"""

from __future__ import annotations

import argparse
import asyncio
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
from dataclasses import dataclass
from pathlib import Path

from ash.commands.chatgpt_auth import list_chatgpt_models
from ash.core.activity import EDIT_TOOL_NAMES, summarize_operational_events
from ash.core.session import SessionStore
from ash.providers.openai_chatgpt_auth import ChatGPTAccount, ChatGPTCredentialStore

from coding_benchmark import (
    CHATGPT_TURN_ITERATIONS,
    PYTHON,
    REPO,
    _chatgpt_tmpfs_root,
    _stage_chatgpt_access_only,
    command,
    oracle_python,
)


H11_VISIBLE = """from h11._events import Data, EndOfMessage
from h11._readers import ChunkedReader
from h11._receivebuffer import ReceiveBuffer
from h11._util import LocalProtocolError

def events_for(chunks):
    reader, buffer, events = ChunkedReader(), ReceiveBuffer(), []
    for chunk in chunks:
        buffer += chunk
        while True:
            event = reader(buffer)
            if event is None:
                break
            events.append(event)
            if isinstance(event, EndOfMessage):
                return events
    return events

good = events_for([b"3\\r\\nabc\\r\\n0\\r\\n\\r\\n"])
assert [type(event) for event in good] == [Data, EndOfMessage]
assert good[0].data == b"abc"

for malformed in [b"3\\r\\nabc__0\\r\\n\\r\\n", b"3\\r\\nabc\\r_0\\r\\n\\r\\n"]:
    try:
        events_for([malformed])
    except LocalProtocolError:
        pass
    else:
        raise AssertionError("chunk footer accepted without mandatory CRLF")
"""

H11_HIDDEN = """from h11._events import Data, EndOfMessage
from h11._readers import ChunkedReader
from h11._receivebuffer import ReceiveBuffer
from h11._util import LocalProtocolError

def parse(fragments):
    reader, buffer, events = ChunkedReader(), ReceiveBuffer(), []
    for fragment in fragments:
        buffer += fragment
        while True:
            event = reader(buffer)
            if event is None:
                break
            events.append(event)
            if isinstance(event, EndOfMessage):
                return events
    return events

for bad in (b"3\\r\\nabc_\\n0\\r\\n\\r\\n", b"3\\r\\nabc\\r_0\\r\\n\\r\\n", b"3\\r\\nabc__0\\r\\n\\r\\n"):
    for fragments in ([bad], [bytes([byte]) for byte in bad], [bad[:6], bad[6:8], bad[8:]]):
        try:
            parse(fragments)
        except LocalProtocolError:
            pass
        else:
            raise AssertionError("bad chunk terminator was accepted")

good = b"3\\r\\nabc\\r\\n0\\r\\n\\r\\n"
for fragments in ([good], [bytes([byte]) for byte in good], [good[:6], good[6:8], good[8:]]):
    events = parse(fragments)
    assert events and isinstance(events[-1], EndOfMessage)
    assert b"".join(e.data for e in events if isinstance(e, Data)) == b"abc"
"""

URLLIB3_VISIBLE = r"""import sys
import types
# The upstream source checkout omits the generated build-time version module.
version = types.ModuleType("urllib3._version")
version.__version__ = "0.0+repository-benchmark"
sys.modules["urllib3._version"] = version
sys.path.insert(0, "src")
from urllib3.connection import _normalize_header_value

examples = [
    ("basic", "basic"),
    ("one\r\n two", "one two"),
    ("value\ninjected", "value injected"),
    ("value\rinjected", "value injected"),
    ("value\x00injected", "value injected"),
    (" \tvalue\t ", "value"),
]
for raw, expected in examples:
    actual = _normalize_header_value(raw)
    assert actual == expected, (repr(raw), repr(actual), repr(expected))
"""

URLLIB3_HIDDEN = r"""import sys
import types
version = types.ModuleType("urllib3._version")
version.__version__ = "0.0+repository-benchmark"
sys.modules["urllib3._version"] = version
sys.path.insert(0, "src")
from urllib3.connection import _normalize_header_value

examples = [
    ("one\r\n two\t\r\n\tthree", "one two three"),
    ("one\r\n \r\n\tthree", "one  three"),
    ("one\n two", "one  two"),
    ("one\r two", "one  two"),
    ("one\r\n\ttwo\n three", "one two  three"),
    ("value\r\n ", "value"),
    ("value\x00", "value"),
    (" \t", ""),
    ("\r\n\x00", ""),
]
for raw, expected in examples:
    actual = _normalize_header_value(raw)
    assert actual == expected, (repr(raw), repr(actual), repr(expected))
    assert "\r" not in actual and "\n" not in actual and "\x00" not in actual
assert _normalize_header_value("interior \t whitespace") == "interior \t whitespace"
"""


@dataclass(frozen=True)
class RepositoryTask:
    source: str
    upstream: str
    base_sha: str
    reference_fix_sha: str
    editable: frozenset[str]
    visible_script: str
    visible_source: str
    hidden_source: str
    command_grant: str
    prompt: str


TASKS = {
    "h11-chunk-footer": RepositoryTask(
        source="python-hyper/h11",
        upstream="https://github.com/python-hyper/h11.git",
        base_sha="31e626c64e1e28db3cd73a6aa0ac057f1b915c18",
        reference_fix_sha="dff7cc397a26ed4acdedd92d1bda6c8f18a6ed9f",
        editable=frozenset({"h11/_readers.py"}),
        visible_script="qa_visible.py",
        visible_source=H11_VISIBLE,
        hidden_source=H11_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Repair a real HTTP/1.1 chunked-body parser bug in this pinned h11 "
            "repository. After a nonempty chunk's data, the parser currently "
            "discards two arbitrary bytes; it must reject any chunk footer "
            "that is not exactly CRLF, including when the two bytes arrive "
            "in separate reads. Preserve successful incremental decoding, "
            "public APIs, and existing valid behavior. Examine the source and "
            "qa_visible.py, change only h11/_readers.py, and run exactly "
            "`python3 -B qa_visible.py` using run_command. No other commands "
            "are authorized. Do not modify tests, metadata, or other files."
        ),
    ),
    "urllib3-header-sanitization": RepositoryTask(
        source="urllib3/urllib3",
        upstream="https://github.com/urllib3/urllib3.git",
        base_sha="8883fd900057ee65f553586389ecd70269f05e04",
        reference_fix_sha="96449eb1ac8cfca8fb9605b8bbff7a5fe0081dd2",
        editable=frozenset({"src/urllib3/connection.py"}),
        visible_script="qa_visible.py",
        visible_source=URLLIB3_VISIBLE,
        hidden_source=URLLIB3_HIDDEN,
        command_grant="python3 -B qa_visible.py",
        prompt=(
            "Fix a real HTTP response header normalization defect in this "
            "pinned urllib3 repository. In src/urllib3/connection.py, "
            "the existing header-value normalizer handles obsolete CRLF "
            "folding, but can still leave raw bare CR, bare LF, or NUL "
            "characters and leading/trailing SP or HTAB in header values. "
            "Normalize these safely while preserving ordinary text and "
            "obsolete folding semantics. Inspect the source and qa_visible.py, "
            "change only src/urllib3/connection.py, and run exactly "
            "python3 -B qa_visible.py with run_command. No other commands "
            "are authorized. Preserve all public APIs and other files."
        ),
    ),
}


def fetch_pinned(destination: Path, task: RepositoryTask, sha: str) -> None:
    """Clone a constant, exact-commit public source; never infer HEAD from a branch."""
    destination.mkdir()
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(destination.parent),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for args in (
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", task.upstream],
        ["git", "-c", "protocol.version=2", "fetch", "--depth=1", "origin", sha],
        ["git", "-c", "core.hooksPath=/dev/null", "checkout", "-q", "--detach", sha],
    ):
        result = command(args, cwd=destination, env=environment, timeout=45)
        if result.returncode:
            raise RuntimeError(f"pinned upstream checkout failed: {args[1]}")
    result = command(
        ["git", "rev-parse", "HEAD"], cwd=destination, env=environment, timeout=5
    )
    if result.returncode or result.stdout.strip() != sha:
        raise RuntimeError("pinned upstream checkout does not match manifest commit")


def snapshot_files(root: Path) -> dict[str, str]:
    """Track every source-tree entry, including unexpected files and symlinks."""
    hashes: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            hashes[str(relative)] = "SYMLINK"
        elif path.is_file():
            hashes[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def check_edits(
    original: dict[str, str], updated: dict[str, str], editable: frozenset[str]
) -> tuple[bool, bool]:
    if set(original) != set(updated) or "SYMLINK" in updated.values():
        return False, False
    protected = all(
        updated[path] == digest
        for path, digest in original.items()
        if path not in editable
    )
    changed = bool(editable) and all(
        path in original and updated.get(path) != original[path] for path in editable
    )
    return protected, changed


def check_oracles(workspace: Path, task: RepositoryTask) -> dict[str, object]:
    visible = oracle_python(workspace, task.visible_script)
    hidden = oracle_python(workspace, code=task.hidden_source)
    return {
        "correct": visible.returncode == 0 and hidden.returncode == 0,
        "visible_oracle_exit": visible.returncode,
        "hidden_oracle_exit": hidden.returncode,
        "oracle_stderr_tail": (visible.stderr + hidden.stderr)[-400:],
    }


def preflight(task: RepositoryTask, root: Path) -> tuple[Path, dict[str, object]]:
    baseline = root / "baseline"
    reference = root / "reference"
    fetch_pinned(baseline, task, task.base_sha)
    fetch_pinned(reference, task, task.reference_fix_sha)
    if any(
        "SYMLINK" in snapshot_files(tree).values() for tree in (baseline, reference)
    ):
        raise RuntimeError("upstream checkout contains a symlink; refusing model trial")
    for tree in (baseline, reference):
        visible = tree / task.visible_script
        if visible.exists() or visible.is_symlink():
            raise RuntimeError(
                "upstream checkout collides with visible evaluation test"
            )
        visible.write_text(task.visible_source, encoding="utf-8")
    base_score = check_oracles(baseline, task)
    reference_score = check_oracles(reference, task)
    if base_score["correct"] or not reference_score["correct"]:
        raise RuntimeError(
            "upstream historical regression is not independently reproducible"
        )
    if any(
        (baseline / path).read_bytes() == (reference / path).read_bytes()
        for path in task.editable
    ):
        raise RuntimeError("reference fix does not modify all required source files")
    return baseline, {
        "base_visible_exit": base_score["visible_oracle_exit"],
        "base_hidden_exit": base_score["hidden_oracle_exit"],
        "reference_visible_exit": reference_score["visible_oracle_exit"],
        "reference_hidden_exit": reference_score["hidden_oracle_exit"],
    }


def run_task(
    task_name: str,
    *,
    model: str,
    account: ChatGPTAccount,
    evidence: Path,
    timeout: int,
    context_tokens: int,
    turn_token_budget: int,
) -> dict[str, object]:
    task = TASKS[task_name]
    started = time.monotonic()
    with (
        tempfile.TemporaryDirectory(prefix="ash-repository-eval-") as scratch_str,
        tempfile.TemporaryDirectory(
            prefix="ash-chatgpt-benchmark-", dir=_chatgpt_tmpfs_root()
        ) as home_str,
    ):
        scratch = Path(scratch_str)
        home = Path(home_str)
        baseline, preflight_receipt = preflight(task, scratch)
        workspace = scratch / "workspace"
        # Preserve link types instead of following upstream links into any
        # filesystem paths outside the pinned source checkout.
        shutil.copytree(baseline, workspace, symlinks=True)
        original = snapshot_files(workspace)
        if any(path not in original for path in task.editable):
            raise RuntimeError(
                "pinned upstream source does not contain required modules"
            )
        _stage_chatgpt_access_only(home, account, timeout=timeout)
        (home / ".ash" / "ash.toml").write_text('sandbox_backend = "native"\n')
        db = scratch / "db"
        db.mkdir()
        ephemeral_tmp = scratch / "tmp"
        ephemeral_tmp.mkdir()
        env = {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "USER": "ash-repository-eval",
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "TERM": "dumb",
            "NO_COLOR": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(ephemeral_tmp),
            "PYTHONPATH": str(REPO / "src"),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "ASH_MODEL": f"openai/{model}",
            "ASH_OPENAI_AUTH_MODE": "chatgpt",
            "ASH_MAX_CONTEXT_TOKENS": str(context_tokens),
            "ASH_MAX_TURN_TOTAL_TOKENS": str(turn_token_budget),
            "ASH_MAX_TURN_ITERATIONS": str(CHATGPT_TURN_ITERATIONS),
            "NO_PROXY": "127.0.0.1,localhost",
        }
        trust = command(
            [str(PYTHON), "-m", "ash", "trust", "add", str(workspace)],
            cwd=workspace,
            env=env,
            timeout=20,
        )
        if trust.returncode:
            raise RuntimeError("isolated workspace trust setup failed")
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
            raise RuntimeError("isolated test command grant failed")

        argv = [
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
            (evidence / "ash.stdout").open("wb") as stdout,
            (evidence / "ash.stderr").open("wb") as stderr,
        ):
            child = subprocess.Popen(
                argv,
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
        cli_seconds = round(time.monotonic() - cli_started, 2)

        updated = snapshot_files(workspace)
        protected, changed = check_edits(original, updated, task.editable)
        oracle = check_oracles(workspace, task)
        store = SessionStore(db / "sessions.db")
        sessions = store.list_sessions(project_path=str(workspace))
        calls: list[dict[str, object]] = []
        audit_ok = False
        event_stats = summarize_operational_events([])
        usage: dict[str, int] = {}
        terminals: list[str] = []
        if sessions:
            session = store.load_session(sessions[0].session_id)
            calls = [
                {
                    "tool": entry.tool_name,
                    "success": entry.error is None,
                    "executed": entry.executed,
                    "approved": entry.approved,
                    "error": (entry.error or "")[:180],
                    "command": entry.arguments.get("command_line", "")
                    if entry.tool_name == "run_command"
                    else "",
                }
                for entry in session.tool_calls
            ]
            audit_ok = not store.verify_audit_log(session.session_id)
            used = store.get_session_usage(session.session_id)
            usage = {
                "prompt_tokens": used.prompt_tokens,
                "completion_tokens": used.completion_tokens,
            }
            events = store.list_runtime_events(session.session_id, limit=10_000)
            event_stats = summarize_operational_events(events)
            terminals = [
                str(row.event.get("type"))
                for row in events
                if row.event.get("type")
                in {"turn.completed", "turn.cancelled", "turn.error"}
            ]
        required = any(
            call["tool"] in EDIT_TOOL_NAMES and call["success"] and call["executed"]
            for call in calls
        ) and any(
            call["tool"] == "run_command"
            and call["success"]
            and call["executed"]
            and call["command"] == task.command_grant
            for call in calls
        )
        result: dict[str, object] = {
            "task": task_name,
            "source": task.source,
            "base_sha": task.base_sha,
            "reference_fix_sha": task.reference_fix_sha,
            "preflight": preflight_receipt,
            "ash_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
            ).strip(),
            "provider_model": f"openai/{model}",
            "context_tokens": context_tokens,
            "turn_token_budget": turn_token_budget,
            "iteration_limit": CHATGPT_TURN_ITERATIONS,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "cli_seconds": cli_seconds,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "oracle": oracle,
            "protected_files_unchanged": protected,
            "required_sources_changed": changed,
            "required_tools_succeeded": required,
            "audit_ok": audit_ok,
            "terminal_events": terminals,
            "usage": usage,
            "tool_calls": calls,
            "performance": event_stats,
            "changed_paths": sorted(
                path
                for path in set(original) | set(updated)
                if original.get(path) != updated.get(path)
            ),
            "passed": bool(
                exit_code == 0
                and not timed_out
                and oracle["correct"]
                and protected
                and changed
                and required
                and audit_ok
            ),
        }
        (evidence / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    result["scratch_removed"] = True
    result["credential_home_removed"] = True
    (evidence / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=tuple(TASKS), required=True)
    parser.add_argument("--chatgpt-model", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=210)
    parser.add_argument("--context-tokens", type=int, default=32768)
    parser.add_argument("--turn-token-budget", type=int, default=120000)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", args.chatgpt_model):
        parser.error("invalid exact ChatGPT model ID")
    if not 30 <= args.timeout <= 300:
        parser.error("timeout must be between 30 and 300 seconds")
    if not 8192 <= args.context_tokens <= 65536:
        parser.error("context budget must be between 8192 and 65536")
    if not 2000 <= args.turn_token_budget <= 250000:
        parser.error("turn budget must be between 2000 and 250000")
    if not PYTHON.is_file() or not shutil.which("bwrap") or not shutil.which("git"):
        parser.error("repository benchmark requires Ash venv, Git, and Bubblewrap")
    try:
        _chatgpt_tmpfs_root()
        account = ChatGPTCredentialStore().active()
        if account is None or not account.access_usable(
            now=time.time() + args.timeout + 30
        ):
            raise ValueError("active ChatGPT access must cover the trial")
        catalog = {slug for slug, _ in asyncio.run(list_chatgpt_models())}
        if args.chatgpt_model not in catalog:
            raise ValueError("selected model not available in active ChatGPT plan")
    except ValueError as exc:
        parser.error(str(exc))
    except Exception as exc:
        parser.error("ChatGPT authorization preflight failed: " + type(exc).__name__)
    if args.evidence_dir.exists():
        parser.error("evidence directory already exists; never overwrite a trial")
    args.evidence_dir.mkdir(parents=True, mode=0o700)
    result = run_task(
        args.task,
        model=args.chatgpt_model,
        account=account,
        evidence=args.evidence_dir,
        timeout=args.timeout,
        context_tokens=args.context_tokens,
        turn_token_budget=args.turn_token_budget,
    )
    print(
        json.dumps(
            {
                "task": result["task"],
                "passed": result["passed"],
                "oracle": result["oracle"],
                "elapsed_seconds": result["elapsed_seconds"],
            }
        )
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
