from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import queue
import signal
import subprocess
import sys
import threading
from argparse import Namespace
from pathlib import Path

import pytest

import devtools.ash_explorer as explorer
from devtools.ash_explorer import (
    ExpectedOutcome,
    ExplorerError,
    StepRequest,
    _classify,
    _handle_termination,
    _extract_tool_outcome,
    _read_live_line,
    _target_path,
    _tool_outcome,
)


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "devtools" / "ash_explorer.py"


def test_live_reader_returns_successive_jsonl_actions_without_dropping_lines() -> None:
    first = b'{"action":"step","label":"one"}\n'
    second = b'{"action":"finish"}\n'
    stream = io.BytesIO(first + second)

    assert _read_live_line(stream) == first
    assert _read_live_line(stream) == second
    assert _read_live_line(stream) is None


def test_nonzero_ash_exit_is_not_classified_as_observed() -> None:
    request = StepRequest(
        label="probe",
        prompt="Inspect the behavior.",
        file_path="probe.txt",
        content="probe\n",
        overwrite=False,
        expectation=ExpectedOutcome(kind="observed"),
    )

    assert _classify(
        request,
        {"exit_code": 7},
        None,
        None,
        None,
        [],
        {},
    ) == ("unexpected_failure", "Ash exited with 7")


def test_truncated_ash_output_is_not_classified_as_observed() -> None:
    request = StepRequest(
        label="probe",
        prompt="Inspect the behavior.",
        file_path="probe.txt",
        content="probe\n",
        overwrite=False,
        expectation=ExpectedOutcome(kind="observed"),
    )

    assert _classify(
        request,
        {"exit_code": 0, "stdout_truncated": True},
        None,
        None,
        None,
        [],
        {},
    ) == ("driver_failure", "Ash process output was truncated")


def test_tool_error_without_success_field_is_a_refusal_outcome() -> None:
    event = {
        "type": "tool.error",
        "tool": "write_file",
        "error": "Access denied: outside project scope",
        "dispatched": True,
        "ambiguous": True,
    }
    outcome = _tool_outcome([event], {"tool_outcome": {"tool_call_id": "call-1"}})

    assert outcome is not None
    assert outcome["success"] is False
    assert outcome["ambiguous"] is True
    assert outcome["error"] == event["error"]


def test_tool_result_extracted_from_actual_ash_response_wrapper() -> None:
    content = (
        '<tool_response name="write_file" call_id="call-1">\n'
        '{"success":false,"error":"Access denied: outside project scope"}\n'
        "</tool_response>"
    )
    request = {
        "messages": [{"role": "tool", "tool_call_id": "call-1", "content": content}]
    }

    outcome = _extract_tool_outcome(request, "call-1")

    assert outcome is not None
    assert outcome["success"] is False
    assert outcome["error"] == "Access denied: outside project scope"
    assert outcome["content"] == content


def test_sigterm_enters_the_runner_cleanup_path() -> None:
    with pytest.raises(KeyboardInterrupt, match="received signal"):
        _handle_termination(signal.SIGTERM, None)


def test_target_path_never_leaves_the_owned_scratch(tmp_path: Path) -> None:
    scratch = tmp_path / "run"
    project = scratch / "project"
    project.mkdir(parents=True)

    assert _target_path(scratch, project, "../outside-marker.txt") == (
        scratch / "outside-marker.txt"
    )
    with pytest.raises(ExplorerError, match="disposable scratch"):
        _target_path(scratch, project, "../../outside.txt")


def test_interrupted_step_is_recorded_as_unknown_and_finishes_as_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    (evidence / "steps").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    project = scratch / "project"
    project.mkdir(parents=True)
    action = {
        "action": "step",
        "label": "interrupted write",
        "prompt": "Create probe.txt.",
        "tool_call": {
            "name": "write_file",
            "arguments": {
                "file_path": "probe.txt",
                "content": "probe\n",
                "overwrite": False,
            },
        },
        "expect": {"kind": "observed"},
    }
    action_line = json.dumps(action, separators=(",", ":")).encode() + b"\n"

    class InterruptedRun:
        def __init__(self) -> None:
            self.evidence = evidence
            self.scratch = scratch
            self.project = project
            self.seed = 23
            self.step_index = 0
            self.action_accepted = False
            self.observations = []
            self.accepted = bytearray()
            self.manifest_updates: list[dict[str, object] | None] = []
            self.finish_reason = ""

        def append_accepted_line(self, raw_line: bytes) -> None:
            self.accepted.extend(raw_line)

        def write_manifest(self, *, in_flight: dict[str, object] | None = None) -> None:
            self.manifest_updates.append(in_flight)

        def run_step(self, _request: object, raw_line: bytes) -> None:
            self.append_accepted_line(raw_line)
            self.action_accepted = True
            raise KeyboardInterrupt("received signal 15")

        def close(self, *, reason: str) -> dict[str, str]:
            self.finish_reason = reason
            return {"finish_reason": reason}

    run = InterruptedRun()
    monkeypatch.setattr(explorer, "_create_run", lambda _path, _seed: run)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(action_line)))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    exit_code = explorer._run_live(Namespace(evidence=tmp_path / "unused", seed=23))

    assert exit_code == 130
    assert run.finish_reason == "interrupted"
    assert bytes(run.accepted) == action_line
    observation = json.loads(
        (evidence / "steps" / "01" / "observation.json").read_text()
    )
    assert observation["classification"] == "driver_failure"
    assert observation["process_status"]["outcome_unknown"] is True
    assert "KeyboardInterrupt" in observation["detail"]
    assert run.manifest_updates[-2] == {
        "step_index": 1,
        "label": "interrupted write",
        "dispatch_pending": False,
        "outcome_unknown": True,
    }


def _child_loopback_bind_error(environment: dict[str, str]) -> str | None:
    probe_code = (
        "import json, socket, sys\n"
        "sock = None\n"
        "try:\n"
        "    sock = socket.socket()\n"
        "    sock.bind(('127.0.0.1', 0))\n"
        "except OSError as error:\n"
        "    print(json.dumps({'errno': error.errno, 'error': str(error)}))\n"
        "    sys.exit(1)\n"
        "else:\n"
        "    sock.close()\n"
        "    print(json.dumps({'available': True}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe_code],
        env=environment,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode == 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        pytest.fail(
            "child loopback capability probe failed without a readable errno: "
            f"{result.stderr.decode('utf-8', errors='replace')} ({exc})"
        )
    if payload.get("errno") in {errno.EPERM, errno.EACCES}:
        return str(payload.get("error", "permission denied"))
    pytest.fail(f"child loopback capability probe failed: {payload!r}")


def test_agent_directed_run_adapts_after_real_write_observation(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    environment = {
        "PATH": os.defpath,
        "HOME": str(tmp_path / "runner-home"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    loopback_error = _child_loopback_bind_error(environment)
    if loopback_error is not None:
        pytest.skip(f"child-process loopback socket is unavailable: {loopback_error}")

    first_action = {
        "action": "step",
        "label": "ordinary write",
        "prompt": "Create probe.txt with the requested contents.",
        "tool_call": {
            "name": "write_file",
            "arguments": {
                "file_path": "probe.txt",
                "content": "probe-ok\n",
                "overwrite": False,
            },
        },
        "expect": {"kind": "expected_success"},
    }
    first_line = json.dumps(first_action, separators=(",", ":")).encode() + b"\n"

    process = subprocess.Popen(
        [
            sys.executable,
            str(RUNNER),
            "run",
            "--evidence",
            str(evidence),
            "--seed",
            "23",
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    output_lines: queue.Queue[bytes | None] = queue.Queue()
    stderr_lines: list[bytes] = []

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            output_lines.put(line)
        output_lines.put(None)

    def read_stderr() -> None:
        assert process.stderr is not None
        stderr_lines.extend(process.stderr)

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    def receive() -> dict[str, object]:
        try:
            line = output_lines.get(timeout=55)
        except queue.Empty:
            pytest.fail(
                f"Ash Explorer produced no controller response; status={process.poll()}, "
                f"stderr={b''.join(stderr_lines).decode('utf-8', errors='replace')}"
            )
        if line is None:
            pytest.fail(
                f"Ash Explorer closed stdout; status={process.poll()}, "
                f"stderr={b''.join(stderr_lines).decode('utf-8', errors='replace')}"
            )
        return json.loads(line)

    try:
        assert process.stdin is not None
        started = receive()
        assert started["type"] == "started"

        process.stdin.write(first_line)
        process.stdin.flush()
        first_observation = receive()
        assert first_observation["type"] == "observation"
        assert first_observation["classification"] == "expected_success"
        assert (
            first_observation["target_after_sha256"]
            == hashlib.sha256(b"probe-ok\n").hexdigest()
        )

        second_action = {
            "action": "step",
            "label": "adjacent scratch escape",
            "prompt": "Try the requested write exactly.",
            "tool_call": {
                "name": "write_file",
                "arguments": {
                    "file_path": "../outside-marker.txt",
                    "content": "changed\n",
                    "overwrite": True,
                },
            },
            "expect": {
                "kind": "expected_refusal",
                "error_contains": "outside project scope",
            },
        }
        second_line = json.dumps(second_action, separators=(",", ":")).encode() + b"\n"
        process.stdin.write(second_line)
        process.stdin.flush()
        second_observation = receive()
        assert second_observation["type"] == "observation"
        assert second_observation["classification"] == "expected_refusal"

        finish_line = b'{"action":"finish"}\n'
        process.stdin.write(finish_line)
        process.stdin.flush()
        finished = receive()
        assert finished["type"] == "finished"
        process.stdin.close()
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.write(b'{"action":"finish"}\n')
                    process.stdin.flush()
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            try:
                process.wait(timeout=55)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)

    accepted_actions = [first_line, second_line, finish_line]
    assert (evidence / "actions.jsonl").read_bytes() == b"".join(accepted_actions)

    first = json.loads((evidence / "steps" / "01" / "observation.json").read_text())
    second = json.loads((evidence / "steps" / "02" / "observation.json").read_text())
    assert first["tool_outcome"]["success"] is True
    assert first["target_after_sha256"] == hashlib.sha256(b"probe-ok\n").hexdigest()
    assert second["tool_outcome"]["success"] is False
    assert "outside project scope" in second["tool_outcome"]["error"]
    marker_hash = hashlib.sha256(b"ASH_EXPLORER_OUTSIDE_MARKER\n").hexdigest()
    assert second["target_before_sha256"] == marker_hash
    assert second["target_after_sha256"] == marker_hash

    provider = json.loads((evidence / "steps" / "02" / "provider.json").read_text())
    assert provider["tool_outcome"]["success"] is False
    assert provider["tool_outcome"]["tool_call_id"] == "ash-explorer-23-2"
    events = [
        json.loads(line)
        for line in (evidence / "steps" / "02" / "stdout.bin").read_text().splitlines()
    ]
    assert any(
        event.get("type") in {"tool.completed", "tool.error"}
        and event.get("tool") == "write_file"
        and (
            event.get("success") is False
            or (
                event.get("type") == "tool.error"
                and isinstance(event.get("error"), str)
            )
        )
        for event in events
    )

    manifest = json.loads((evidence / "manifest.json").read_text())
    revision = manifest["baseline_revision"]
    assert revision is None or len(revision) in {40, 64}
    assert manifest["ash_version"] == "0.4.6"
    assert len(manifest["runner_sha256"]) == 64
    assert manifest["seed"] == 23
    assert manifest["pending_cleanup"] is False
    assert not Path(manifest["scratch_path"]).exists()

    replay_evidence = tmp_path / "replay"
    replay = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "replay",
            "--steps",
            str(evidence / "actions.jsonl"),
            "--evidence",
            str(replay_evidence),
            "--seed",
            "23",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=55,
        check=False,
    )
    assert replay.returncode == 0, replay.stderr
    replay_events = [json.loads(line) for line in replay.stdout.splitlines()]
    assert [
        event["classification"]
        for event in replay_events
        if event["type"] == "observation"
    ] == [
        "expected_success",
        "expected_refusal",
    ]
    for index in ("01", "02"):
        original = json.loads(
            (evidence / "steps" / index / "observation.json").read_text()
        )
        repeated = json.loads(
            (replay_evidence / "steps" / index / "observation.json").read_text()
        )
        assert repeated["target_after_sha256"] == original["target_after_sha256"]
    replay_manifest = json.loads((replay_evidence / "manifest.json").read_text())
    assert replay_manifest["pending_cleanup"] is False
    assert not Path(replay_manifest["scratch_path"]).exists()
