"""Run a pinned real-repository task through plain Codex CLI; score it.

Operator-initiated only. This is a plain-Codex comparison, NOT a claim that
Codex's shell policy, sandbox or context window matches the Ash runner.
The fixed reference and hidden tests are never copied into the agent checkout.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

from repository_benchmark import (
    TASKS,
    check_edits,
    check_oracles,
    preflight,
    remove_reference_checkout,
    shared_user_prompt,
    snapshot_files,
)
from coding_benchmark import _chatgpt_tmpfs_root


MAX_EVENT_BYTES = 32 * 1024 * 1024
_TERMINAL_EVENTS = {"turn.completed", "turn.failed", "turn.cancelled"}
_SHELLS = frozenset({"bash", "zsh", "sh"})


def _exact_test_command(command: str, expected: str) -> bool:
    """Do not mistake a shell compound command containing the test for it."""
    try:
        tokens = shlex.split(command)
        if len(tokens) >= 3 and Path(tokens[0]).name in _SHELLS:
            if tokens[1] not in {"-lc", "-c"} or len(tokens) != 3:
                return False
            tokens = shlex.split(tokens[2])
        return tokens == shlex.split(expected)
    except ValueError:
        return False


def summarize_codex_events(log: Path, exact_test: str) -> dict[str, Any]:
    """Summarize a bounded private JSONL log without copying tool contents."""
    if log.stat().st_size > MAX_EVENT_BYTES:
        raise ValueError("Codex event log exceeds the bounded evidence limit")
    commands = 0
    non_test_commands = 0
    successful_test_commands = 0
    terminal: list[str] = []
    last_usage: dict[str, int] = {}
    routing_errors = 0
    substantive_events = 0
    with log.open(encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("Codex event was not an object")
            kind = event.get("type")
            # A provider-routing failure before any agent action cannot be
            # graded as a coding failure or a competitor loss. Retain only a
            # boolean, never the backend's potentially sensitive message.
            if kind == "error" and isinstance(event.get("message"), str):
                message = event["message"].lower()
                if "workspace routing discovery failed" in message or (
                    "workspace routing discovery timed out" in message
                ):
                    routing_errors += 1
            if kind in _TERMINAL_EVENTS:
                substantive_events += 1
                terminal.append(kind)
                usage = event.get("usage")
                if isinstance(usage, dict):
                    last_usage = {
                        key: value
                        for key, value in usage.items()
                        if key
                        in {"input_tokens", "cached_input_tokens", "output_tokens"}
                        and type(value) is int
                        and value >= 0
                    }
            item = event.get("item")
            if kind != "item.completed" or not isinstance(item, dict):
                continue
            if item.get("type") != "error":
                substantive_events += 1
            if item.get("type") != "command_execution":
                continue
            commands += 1
            is_test = _exact_test_command(str(item.get("command", "")), exact_test)
            if not is_test:
                non_test_commands += 1
            elif item.get("exit_code") == 0 and item.get("status") == "completed":
                successful_test_commands += 1
    return {
        "shell_command_count": commands,
        "shell_non_test_commands": non_test_commands,
        "visible_test_command_executed": successful_test_commands > 0,
        "terminal_events": terminal,
        "usage": last_usage,
        "infrastructure_unavailable": bool(
            routing_errors and not substantive_events and not commands
        ),
    }


def _native_codex_binary() -> Path:
    """Bypass account overlays; do not silently route into user Codex settings."""
    candidate = shutil.which("codex-real") or shutil.which("codex")
    if candidate is None:
        raise RuntimeError(
            "A native Codex CLI executable is required for isolated trials"
        )
    binary = Path(candidate).resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError("Native Codex executable is unavailable")
    with binary.open("rb") as source:
        header = source.read(4)
    if header not in {
        b"\x7fELF",  # Linux
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",  # macOS
    }:
        raise RuntimeError("Codex binary is not a native executable")
    return binary


@contextlib.contextmanager
def _isolated_codex_profile(
    auth_source: Path | None = None, *, memory_root: Path | None = None
) -> Iterator[Path]:
    """Copy only auth, never plugins/config, into private, ephemeral RAM."""
    origin = auth_source or (Path.home() / ".codex" / "auth.json")
    fd = os.open(origin, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Codex authentication source must be a user-owned file")
        if info.st_size > 64 * 1024:
            raise ValueError("Codex authentication source exceeds the bounded limit")
        with os.fdopen(fd, "rb", closefd=False) as source:
            raw = source.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("Codex authentication source exceeds the bounded limit")
        parsed = json.loads(raw)
        if (
            not isinstance(parsed, dict)
            or parsed.get("auth_mode") != "chatgpt"
            or not isinstance(parsed.get("tokens"), dict)
            or not all(
                isinstance(parsed["tokens"].get(key), str)
                for key in ("access_token", "refresh_token", "account_id")
            )
        ):
            raise ValueError("An existing ChatGPT-based Codex login is required")
    finally:
        os.close(fd)
    with tempfile.TemporaryDirectory(
        prefix="ash-codex-credential-", dir=memory_root or _chatgpt_tmpfs_root()
    ) as private:
        codex_home = Path(private) / "codex"
        codex_home.mkdir(mode=0o700)
        target = codex_home / "auth.json"
        output_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(output_fd, "wb") as output:
            output.write(raw)
        yield codex_home


def _codex_env(scratch: Path, codex_home: Path) -> dict[str, str]:
    """Fully separate HOME/XDG paths and block inherited overlay/plugins."""
    for name in ("home", "xdg-config", "xdg-data", "xdg-cache", "runtime"):
        (scratch / name).mkdir(mode=0o700, exist_ok=True)
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(scratch / "home"),
        "CODEX_HOME": str(codex_home),
        "USER": os.environ.get("USER", "user"),
        "LANG": "C.UTF-8",
        "NO_COLOR": "1",
        "XDG_CONFIG_HOME": str(scratch / "xdg-config"),
        "XDG_DATA_HOME": str(scratch / "xdg-data"),
        "XDG_CACHE_HOME": str(scratch / "xdg-cache"),
        "XDG_RUNTIME_DIR": str(scratch / "runtime"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(scratch / "tmp"),
    }


def _stop_child(child: subprocess.Popen[bytes]) -> None:
    """Reap the CLI process group even when interrupted during a model run."""
    if child.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=4)
            return
        except subprocess.TimeoutExpired:
            continue
    child.wait(timeout=5)


def _run_codex(
    workspace: Path,
    evidence: Path,
    *,
    model: str,
    effort: str,
    timeout: int,
    prompt: str,
    scratch: Path,
    context_tokens: int = 32768,
) -> dict[str, Any]:
    executable = _native_codex_binary()
    argv = [
        str(executable),
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--json",
        "--color",
        "never",
        "-C",
        str(workspace),
        "-m",
        model,
        "-s",
        "workspace-write",
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-c",
        f"model_context_window={context_tokens}",
        "-c",
        'approval_policy="never"',
        "-c",
        "sandbox_workspace_write.network_access=false",
        "-c",
        "features.skip_host_skill_discovery=true",
        "-c",
        "features.skill_search=false",
        "-",
    ]
    timed_out = False
    with _isolated_codex_profile() as codex_home:
        with (
            (evidence / "codex.jsonl").open("wb") as stdout,
            (evidence / "codex.stderr").open("wb") as stderr,
        ):
            # Match the Ash CLI timer: start immediately before spawning
            # the agent, not while preparing the isolated auth profile.
            start = time.monotonic()
            child = subprocess.Popen(
                argv,
                cwd=workspace,
                env=_codex_env(scratch, codex_home),
                stdin=subprocess.PIPE,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            try:
                child.communicate(input=prompt.encode("utf-8"), timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _stop_child(child)
            except BaseException:
                _stop_child(child)
                raise
            cli_seconds = round(time.monotonic() - start, 2)
    return {
        "exit_code": child.returncode,
        "timed_out": timed_out,
        "cli_seconds": cli_seconds,
        "codex_model": model,
        "codex_reasoning_effort": effort,
        "context_tokens_requested": context_tokens,
        "sandbox": "workspace-write",
        "approval_policy": "never",
        "network_access_requested": False,
        "ephemeral": True,
        "workflow": "plain-codex",
        "user_config_ignored": True,
        "user_rules_ignored": True,
        "host_skill_discovery_disabled_requested": True,
        "native_binary_direct": True,
        "isolated_codex_home": True,
        "isolated_home_xdg": True,
        "memory_backed_auth_copy_cleaned": True,
    }


def run_task(
    name: str,
    *,
    evidence: Path,
    model: str,
    effort: str,
    timeout: int,
    preflight_only: bool = False,
    context_tokens: int = 32768,
) -> dict[str, Any]:
    """Isolate source, run Codex once at most, and score outside the checkout."""
    task = TASKS[name]
    with tempfile.TemporaryDirectory(prefix="ash-codex-repo-") as temp:
        scratch = Path(temp)
        (scratch / "tmp").mkdir()
        baseline, preflight_receipt = preflight(task, scratch)
        remove_reference_checkout(scratch)
        workspace = scratch / "workspace"
        shutil.copytree(baseline, workspace, symlinks=True)
        initial = snapshot_files(workspace)
        if any(path not in initial for path in task.editable):
            raise RuntimeError("required source file missing in pinned baseline")

        prompt = shared_user_prompt(task)
        (evidence / "prompt.txt").write_text(prompt, encoding="utf-8")
        manifest = {
            "task": name,
            "source": task.source,
            "base_sha": task.base_sha,
            "reference_fix_sha": task.reference_fix_sha,
            "preflight": preflight_receipt,
            "editable": sorted(task.editable),
            "visible_test": task.command_grant,
            "task_prompt_sha256": hashlib.sha256(task.prompt.encode()).hexdigest(),
            "codex_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "user_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "visible_oracle_sha256": hashlib.sha256(
                task.visible_source.encode()
            ).hexdigest(),
            "hidden_oracle_sha256": hashlib.sha256(
                task.hidden_source.encode()
            ).hexdigest(),
        }
        (evidence / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if preflight_only:
            return {"task": name, "preflight": preflight_receipt, "ran_model": False}

        invocation = _run_codex(
            workspace,
            evidence,
            model=model,
            effort=effort,
            timeout=timeout,
            prompt=prompt,
            scratch=scratch,
            context_tokens=context_tokens,
        )
        (evidence / "invocation.json").write_text(
            json.dumps(invocation, indent=2) + "\n"
        )
        updated = snapshot_files(workspace)
        protected, changed = check_edits(initial, updated, task.editable)
        oracles = check_oracles(workspace, task)
        try:
            events = summarize_codex_events(
                evidence / "codex.jsonl", task.command_grant
            )
            event_log_valid = True
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
            event_log_valid = False
            events = {
                "shell_command_count": 0,
                "shell_non_test_commands": 0,
                "visible_test_command_executed": False,
                "terminal_events": [],
                "usage": {},
            }
        passed = bool(
            invocation["exit_code"] == 0
            and not invocation["timed_out"]
            and event_log_valid
            and events["terminal_events"] == ["turn.completed"]
            and protected
            and changed
            and oracles["correct"]
            and events["visible_test_command_executed"]
        )
        result = {
            "task": name,
            "runner": "plain-codex-cli",
            "model_requested": model,
            "reasoning_effort_requested": effort,
            "context_tokens_requested": context_tokens,
            "sandbox": "workspace-write",
            "shell_command_policy_enforced": False,
            "host_skill_discovery_disabled_requested": True,
            "skill_stack_isolated_verified": False,
            "native_binary_direct": invocation["native_binary_direct"],
            "isolated_codex_home": invocation["isolated_codex_home"],
            "isolated_home_xdg": invocation["isolated_home_xdg"],
            "memory_backed_auth_copy_cleaned": invocation[
                "memory_backed_auth_copy_cleaned"
            ],
            "network_block_independently_verified": False,
            "passed": passed,
            "protected_files_unchanged": protected,
            "required_source_changed": changed,
            "oracles": {
                key: value
                for key, value in oracles.items()
                if key != "oracle_stderr_tail"
            },
            "event_log_valid": event_log_valid,
            "infrastructure_unavailable": events.get(
                "infrastructure_unavailable", False
            ),
            "elapsed_seconds": invocation["cli_seconds"],
            "exit_code": invocation["exit_code"],
            "timed_out": invocation["timed_out"],
            "changed_paths": sorted(
                path
                for path in set(initial) | set(updated)
                if initial.get(path) != updated.get(path)
            ),
            **events,
        }
        (evidence / "grade.json").write_text(json.dumps(result, indent=2) + "\n")
    result["scratch_removed"] = True
    (evidence / "grade.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=tuple(TASKS), required=True)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        default="xhigh",
    )
    parser.add_argument("--timeout", type=int, default=170)
    parser.add_argument("--context-tokens", type=int, default=32768)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", args.model):
        parser.error("model must be an explicit simple model ID")
    if not 30 <= args.timeout <= 300:
        parser.error("timeout must be between 30 and 300 seconds")
    if not 8192 <= args.context_tokens <= 65536:
        parser.error("context window must be between 8192 and 65536 tokens")
    if not args.preflight_only:
        try:
            _native_codex_binary()
            _chatgpt_tmpfs_root()
        except (OSError, ValueError, RuntimeError):
            parser.error("Native Codex and memory-backed authentication are required")
    if not shutil.which("git") or not shutil.which("bwrap"):
        parser.error("Git and Bubblewrap are required")
    if args.evidence_dir.exists():
        parser.error("evidence directory already exists; refusing overwrite")
    previous_umask = os.umask(0o077)
    try:
        args.evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        result = run_task(
            args.task,
            evidence=args.evidence_dir,
            model=args.model,
            effort=args.reasoning_effort,
            timeout=args.timeout,
            preflight_only=args.preflight_only,
            context_tokens=args.context_tokens,
        )
    finally:
        os.umask(previous_umask)
    print(
        json.dumps(
            {
                "task": result["task"],
                "passed": result.get("passed"),
                "preflight": result.get("preflight"),
                "elapsed_seconds": result.get("elapsed_seconds"),
            }
        )
    )
    return 0 if result.get("passed") or args.preflight_only else 1


if __name__ == "__main__":
    sys.exit(main())
