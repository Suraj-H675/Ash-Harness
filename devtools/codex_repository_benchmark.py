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
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import tomllib
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
    hidden_oracle_classification,
    hidden_oracle_sha256,
    snapshot_files,
    validate_editable_paths,
    verify_task_runtime,
)
from coding_benchmark import _chatgpt_tmpfs_root


MAX_EVENT_BYTES = 32 * 1024 * 1024
SEALED_PERMISSION_PROFILE = "ash-benchmark-sealed"
_MANAGED_REQUIREMENTS = Path("/etc/codex/requirements.toml")
_LOCAL_ATTESTATION = (
    Path.home() / ".local/state/ash-benchmarks/codex-sealed-profile-attestation.json"
)
_REQUIRED_TOOL_OBSERVATIONS = {
    "host_evaluator": "BLOCKED",
    "isolated_codex_home_sentinel": "BLOCKED",
    "network_socket": "BLOCKED",
    "outside_fake_auth": "BLOCKED",
    "outside_grader": "BLOCKED",
    "symlink_escape": "BLOCKED",
    "visible_test": "PASS",
    "workspace_read": "ALLOWED",
    "workspace_write": "ALLOWED",
}
_TERMINAL_EVENTS = {"turn.completed", "turn.failed", "turn.cancelled"}
_SHELLS = frozenset({"bash", "zsh", "sh"})


def _read_sealed_metadata(path: Path, *, owner: int, mode: int) -> bytes:
    """Never follow links or trust other users' editable security metadata."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner:
        raise RuntimeError("Sealed comparator security metadata has unsafe ownership")
    if stat.S_IMODE(info.st_mode) != mode or info.st_size > 16 * 1024:
        raise RuntimeError("Sealed comparator security metadata has unsafe mode/size")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise RuntimeError("Sealed comparator metadata changed during open")
        return stream.read(16 * 1024 + 1)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sealed_comparator_profile() -> None:
    """Fail closed unless Linux, managed profile and live-tool proof still match.

    The owner-approved one-shot real Luna tool smoke is retained as private
    machine-local state, not committed to Git or treated as universal proof.
    Any native binary, policy, architecture, or attestation change blocks
    further model execution until independently re-attested.
    """
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise RuntimeError(
            "Sealed plain-Codex model trials need Linux x86_64 attestation"
        )
    try:
        local_directory = _LOCAL_ATTESTATION.parent
        owner = os.getuid()
        directory_stat = local_directory.lstat()
        if not stat.S_ISDIR(directory_stat.st_mode) or (
            directory_stat.st_uid != owner
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise RuntimeError("Sealed comparator attestation directory is not private")
        attestation = json.loads(
            _read_sealed_metadata(_LOCAL_ATTESTATION, owner=owner, mode=0o600)
        )
        policy_data = _read_sealed_metadata(_MANAGED_REQUIREMENTS, owner=0, mode=0o644)
        policy = tomllib.loads(policy_data.decode("utf-8"))
        binary = _native_codex_binary()
        release_root = Path.home() / ".codex/packages/standalone/releases"
        profile = policy["permissions"][SEALED_PERMISSION_PROFILE]
        filesystem = profile["filesystem"]
        expected_fs = {
            ":root": "deny",
            ":minimal": "read",
            ":tmpdir": "deny",
            ":slash_tmp": "deny",
            str(release_root): "read",
        }
        valid = (
            isinstance(attestation, dict)
            and attestation.get("schema") == 1
            and attestation.get("platform") == "linux"
            and attestation.get("machine") == "x86_64"
            and attestation.get("profile") == SEALED_PERMISSION_PROFILE
            and attestation.get("independent_live_tool_smoke") is True
            and attestation.get("live_shell_tool_count") == 1
            and attestation.get("required_observations") == _REQUIRED_TOOL_OBSERVATIONS
            and attestation.get("managed_policy_sha256")
            == hashlib.sha256(policy_data).hexdigest()
            and attestation.get("native_binary_sha256") == _sha256_file(binary)
            and policy["allowed_permission_profiles"].get(SEALED_PERMISSION_PROFILE)
            is True
            and profile.get("extends") == ":workspace"
            and filesystem == expected_fs
            and profile["network"].get("enabled") is False
            and binary.is_relative_to(release_root)
        )
        if not valid:
            raise RuntimeError(
                "Sealed comparator managed policy / live-tool proof drifted"
            )
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as error:
        raise RuntimeError(
            "Sealed comparator profile unavailable or not independently attested"
        ) from error


_SEALED_SANDBOX_PROBE = r"""
from pathlib import Path
import socket, sys, tempfile

parent_sentinel, auth_sentinel, evaluator = map(Path, sys.argv[1:4])
def blocked(path):
    try:
        with path.open("rb") as stream:
            stream.read(1)
    except OSError:
        return True
    return False

assert all(blocked(p) for p in (parent_sentinel, auth_sentinel, evaluator))
with tempfile.NamedTemporaryFile(prefix="ash-sealed-write-", dir=Path.cwd()) as item:
    item.write(b"sandbox write check")
    item.flush()
    item.seek(0)
    assert item.read() == b"sandbox write check"
try:
    sock = socket.socket()
except OSError:
    pass
else:
    sock.close()
    raise AssertionError("sandbox socket creation unexpectedly allowed")
print("ASH_SEALED_NO_MODEL_PROBE_OK")
"""


def _probe_effective_sealed_sandbox(
    executable: Path, workspace: Path, scratch: Path, codex_home: Path
) -> None:
    """Independently enforce real file-open/write/socket restrictions per run."""
    if workspace.is_symlink() or not workspace.resolve().is_relative_to(
        scratch.resolve()
    ):
        raise RuntimeError("Sealed comparator workspace is outside isolated scratch")
    outside = scratch / ".ash-sealed-parent-sentinel"
    private = codex_home / ".ash-sealed-auth-sentinel"
    paths = (outside, private)
    try:
        for path in paths:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(b"synthetic-security-probe-only")
        command = [
            str(executable),
            "sandbox",
            "--include-managed-config",
            "-P",
            SEALED_PERMISSION_PROFILE,
            "-C",
            str(workspace),
            "-c",
            f'default_permissions="{SEALED_PERMISSION_PROFILE}"',
            "/usr/bin/python3",
            "-I",
            "-B",
            "-S",
            "-c",
            _SEALED_SANDBOX_PROBE,
            str(outside),
            str(private),
            str(Path(__file__).resolve()),
        ]
        outcome = subprocess.run(
            command,
            cwd=workspace,
            env=_codex_env(scratch, codex_home),
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
        if (
            outcome.returncode != 0
            or outcome.stdout.strip() != "ASH_SEALED_NO_MODEL_PROBE_OK"
        ):
            raise RuntimeError(
                "Sealed comparator no-model sandbox read/write/network probe failed"
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("Sealed comparator sandbox probe unavailable") from error
    finally:
        for path in paths:
            # Only our own exclusive, ordinary sentinel files are ever removed.
            if path.is_file() and not path.is_symlink():
                path.unlink()


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


def _sealed_codex_argv(
    executable: Path,
    workspace: Path,
    *,
    model: str,
    effort: str,
    context_tokens: int,
) -> list[str]:
    """Prepare a plain native Codex invocation without legacy sandbox overrides.

    Constructing argv cannot authorize inference: caller must verify the
    private live-tool attestation, effective per-run isolation, and explicit
    operator opt-in before dispatch.
    """
    return [
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
        "-c",
        f'default_permissions="{SEALED_PERMISSION_PROFILE}"',
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-c",
        f"model_context_window={context_tokens}",
        "-c",
        'approval_policy="never"',
        "-c",
        "features.skip_host_skill_discovery=true",
        "-c",
        "features.skill_search=false",
        "-",
    ]


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
    _require_sealed_comparator_profile()
    executable = _native_codex_binary()
    argv = _sealed_codex_argv(
        executable,
        workspace,
        model=model,
        effort=effort,
        context_tokens=context_tokens,
    )
    timed_out = False
    with _isolated_codex_profile() as codex_home:
        _probe_effective_sealed_sandbox(executable, workspace, scratch, codex_home)
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
        "sandbox": SEALED_PERMISSION_PROFILE,
        "permissions_profile_requested": SEALED_PERMISSION_PROFILE,
        "permissions_profile_independently_verified": True,
        "native_live_tool_smoke_fingerprint_verified": True,
        "no_model_sandbox_probe_verified": True,
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
    authorize_model_trial: bool = False,
) -> dict[str, Any]:
    """Isolate source, run Codex once at most, and score outside the checkout."""
    if not preflight_only:
        if not authorize_model_trial:
            raise RuntimeError(
                "Plain Codex model trials require explicit operator authorization"
            )
        if (model, effort) != ("gpt-6-luna", "xhigh"):
            raise RuntimeError("Plain Codex model trials require GPT-6 Luna xhigh")
    task = TASKS[name]
    with tempfile.TemporaryDirectory(prefix="ash-codex-repo-") as temp:
        scratch = Path(temp)
        (scratch / "tmp").mkdir()
        baseline, preflight_receipt = preflight(task, scratch)
        remove_reference_checkout(scratch)
        workspace = scratch / "workspace"
        shutil.copytree(baseline, workspace, symlinks=True)
        if task.runtime is not None:
            runtime_receipt = preflight_receipt.get("runtime_overlay")
            if not isinstance(runtime_receipt, dict):
                raise RuntimeError("preflight omitted the required runtime receipt")
            verify_task_runtime(workspace, task.runtime, runtime_receipt)
        initial = snapshot_files(workspace)
        validate_editable_paths(initial, task.editable, preflight_receipt)

        prompt = shared_user_prompt(task)
        (evidence / "prompt.txt").write_text(prompt, encoding="utf-8")
        manifest = {
            "task": name,
            "source": task.source,
            "upstream_pr": task.upstream_pr,
            "license": task.license,
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
            "hidden_oracle_sha256": hidden_oracle_sha256(task),
            "hidden_probe_sha256": hashlib.sha256(
                task.hidden_source.encode()
            ).hexdigest(),
            "hidden_oracle_classification": hidden_oracle_classification(task),
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
        protected, changed = check_edits(
            initial,
            updated,
            task.editable,
            require_all_editable_changed=task.require_all_editable_changed,
        )
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
            # A simulated invocation may not report an actual sandbox. Never
            # manufacture an effective-permission attestation from argv alone.
            "sandbox": invocation.get("sandbox", "unverified"),
            "shell_command_policy_enforced": False,
            "host_skill_discovery_disabled_requested": True,
            "skill_stack_isolated_verified": False,
            "native_binary_direct": invocation["native_binary_direct"],
            "isolated_codex_home": invocation["isolated_codex_home"],
            "isolated_home_xdg": invocation["isolated_home_xdg"],
            "memory_backed_auth_copy_cleaned": invocation[
                "memory_backed_auth_copy_cleaned"
            ],
            # Do not treat a requested profile or a mock receipt as proof.
            # These booleans become true only after the live-tool fingerprint
            # and separate managed sandbox probe were independently checked.
            "permissions_profile_independently_verified": bool(
                invocation.get("permissions_profile_independently_verified", False)
            ),
            "native_live_tool_smoke_fingerprint_verified": bool(
                invocation.get("native_live_tool_smoke_fingerprint_verified", False)
            ),
            "network_block_independently_verified": bool(
                invocation.get("no_model_sandbox_probe_verified", False)
            ),
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
    parser.add_argument(
        "--authorize-model-trial",
        action="store_true",
        help="Explicitly opt into one model-using comparison after owner approval",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", args.model):
        parser.error("model must be an explicit simple model ID")
    if not 30 <= args.timeout <= 300:
        parser.error("timeout must be between 30 and 300 seconds")
    if not 8192 <= args.context_tokens <= 65536:
        parser.error("context window must be between 8192 and 65536 tokens")
    if not args.preflight_only:
        if not args.authorize_model_trial:
            parser.error(
                "Paid plain-Codex model trials require --authorize-model-trial"
            )
        if (args.model, args.reasoning_effort) != ("gpt-6-luna", "xhigh"):
            parser.error("Matched plain-Codex comparison requires gpt-6-luna xhigh")
        try:
            _require_sealed_comparator_profile()
        except RuntimeError as error:
            parser.error(str(error))
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
            authorize_model_trial=args.authorize_model_trial,
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
