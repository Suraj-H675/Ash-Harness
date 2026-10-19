from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import traceback
import unicodedata
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO, Literal
from xml.sax.saxutils import unescape as xml_unescape


ROOT = Path(__file__).resolve().parents[1]
MAX_STEPS = 8
MAX_ACTION_BYTES = 64 * 1024
MAX_STEPS_FILE_BYTES = 512 * 1024
MAX_LABEL_BYTES = 256
MAX_PROMPT_BYTES = 8 * 1024
MAX_PATH_BYTES = 1024
MAX_CONTENT_BYTES = 16 * 1024
MAX_REFUSAL_TEXT_BYTES = 512
MAX_DIRECTORY_ENTRIES = 64
MAX_GLOB_SCAN_ENTRIES = 128
MAX_GLOB_DEPTH = 4
MAX_GLOB_PATTERN_BYTES = 128
MAX_SEARCH_PATTERN_BYTES = 256
MAX_SEARCH_SCAN_ENTRIES = 128
MAX_SEARCH_DEPTH = 32
MAX_SEARCH_FILE_BYTES = 16 * 1024
MAX_SEARCH_TOTAL_BYTES = 128 * 1024
MAX_SEARCH_MATCHES = 64
MAX_SEARCH_OUTPUT_BYTES = 16 * 1024
MAX_PROVIDER_BODY_BYTES = 2 * 1024 * 1024
MAX_CHILD_OUTPUT_BYTES = 1024 * 1024
MAX_TRACEBACK_CHARS = 8192
CHILD_TIMEOUT_SECONDS = 45
TRUST_TIMEOUT_SECONDS = 15
FIXED_FINAL_RESPONSE = "Ash Explorer fixture completed."


class ExplorerError(ValueError):
    pass


@dataclass(frozen=True)
class ExpectedOutcome:
    kind: Literal["expected_success", "expected_refusal", "observed"]
    error_contains: str | None = None


@dataclass(frozen=True)
class SearchQuery:
    pattern: str
    directory_path: str = "."
    glob: str | None = None
    fixed_strings: bool = True
    case_sensitive: bool = True
    max_results: int = MAX_SEARCH_MATCHES

    def as_arguments(self) -> dict[str, object]:
        return {
            "pattern": self.pattern,
            "directory_path": self.directory_path,
            "glob": self.glob,
            "fixed_strings": self.fixed_strings,
            "case_sensitive": self.case_sensitive,
            "max_results": self.max_results,
        }


@dataclass(frozen=True)
class SearchSnapshot:
    sha256: str | None
    expected_matches: tuple[str, ...] | None
    complete: bool
    detail: str | None = None


@dataclass(frozen=True)
class SearchVerification:
    verified: bool
    evidence_complete: bool
    before_sha256: str | None
    after_sha256: str | None
    expected_match_count: int | None
    actual_match_count: int | None
    expected_output_sha256: str | None
    actual_output_sha256: str | None
    event_truncated: bool | None
    fixture_truncated: bool | None
    detail: str | None = None


@dataclass(frozen=True)
class StepRequest:
    label: str
    prompt: str
    file_path: str
    content: str
    overwrite: bool
    expectation: ExpectedOutcome
    tool_name: Literal[
        "write_file", "read_file", "list_dir", "glob_files", "search_text"
    ] = "write_file"
    search_query: SearchQuery | None = None

    @property
    def tool_arguments(self) -> dict[str, object]:
        if self.tool_name == "read_file":
            return {"file_path": self.file_path}
        if self.tool_name == "list_dir":
            return {
                "directory_path": self.file_path,
                "recursive": False,
                "max_results": MAX_DIRECTORY_ENTRIES,
            }
        if self.tool_name == "glob_files":
            return {
                "pattern": self.file_path,
                "directory_path": ".",
                "max_results": MAX_DIRECTORY_ENTRIES,
            }
        if self.tool_name == "search_text":
            if self.search_query is None:
                raise ExplorerError("search_text request is missing its query")
            return self.search_query.as_arguments()
        return {
            "file_path": self.file_path,
            "content": self.content,
            "overwrite": self.overwrite,
        }


@dataclass(frozen=True)
class StepObservation:
    index: int
    label: str
    classification: str
    process_status: dict[str, object]
    tool_outcome: dict[str, object] | None
    target_before_sha256: str | None
    target_after_sha256: str | None
    artifact_paths: dict[str, str]
    detail: str | None = None
    search_verification: SearchVerification | None = None


@dataclass
class _FixtureState:
    seed: int
    lock: threading.Lock = field(default_factory=threading.Lock)
    index: int = 0
    request_count: int = 0
    tool_call_id: str = ""
    request: StepRequest | None = None
    exchanges: list[dict[str, object]] = field(default_factory=list)
    tool_outcome: dict[str, object] | None = None
    error: str | None = None
    discovery_used: bool = False

    def arm(self, index: int, request: StepRequest) -> None:
        with self.lock:
            self.index = index
            self.request_count = 0
            self.tool_call_id = f"ash-explorer-{self.seed}-{index}"
            self.request = request
            self.exchanges = []
            self.tool_outcome = None
            self.error = None
            self.discovery_used = False

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "step_index": self.index,
                "exchanges": list(self.exchanges),
                "tool_outcome": self.tool_outcome,
                "error": self.error,
                "discovery_used": self.discovery_used,
            }


class _FixtureServer:
    def __init__(self, seed: int) -> None:
        state = _FixtureState(seed=seed)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *args: object) -> None:
                del args

            def _reply(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, payload: object) -> None:
                self._reply(
                    status,
                    json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                    "application/json",
                )

            def do_GET(self) -> None:
                if self.path not in {"/api/v1/models", "/v1/models"}:
                    self._json(404, {"error": {"message": "fixture route not found"}})
                    return
                with state.lock:
                    state.exchanges.append({"method": "GET", "path": self.path})
                if self.path == "/api/v1/models":
                    payload: object = {
                        "models": [
                            {
                                "key": "local-model",
                                "type": "llm",
                                "capabilities": {
                                    "trained_for_tool_use": True,
                                    "vision": False,
                                },
                                "loaded_instances": [
                                    {"config": {"context_length": 32768}}
                                ],
                            }
                        ]
                    }
                else:
                    payload = {"object": "list", "data": [{"id": "local-model"}]}
                self._json(200, payload)

            def do_POST(self) -> None:
                if self.path != "/v1/chat/completions":
                    self._json(404, {"error": {"message": "fixture route not found"}})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._json(400, {"error": {"message": "invalid content length"}})
                    return
                if length < 0 or length > MAX_PROVIDER_BODY_BYTES:
                    self._json(413, {"error": {"message": "request too large"}})
                    return
                try:
                    raw = self.rfile.read(length)
                    request_payload = _strict_json_loads(raw)
                except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                    self._json(400, {"error": {"message": "invalid JSON request"}})
                    return
                if not isinstance(request_payload, dict):
                    self._json(400, {"error": {"message": "invalid request shape"}})
                    return

                with state.lock:
                    state.request_count += 1
                    number = state.request_count
                    state.exchanges.append(
                        {
                            "method": "POST",
                            "path": self.path,
                            "request": request_payload,
                        }
                    )
                    action = state.request
                    call_id = state.tool_call_id
                    if number > (3 if state.discovery_used else 2):
                        state.error = (
                            "fixture received more than three completion requests"
                        )
                        self._json(
                            502,
                            {"error": {"message": "fixture request limit exceeded"}},
                        )
                        return

                if action is None:
                    with state.lock:
                        state.error = (
                            "fixture received a request before a step was armed"
                        )
                    self._json(502, {"error": {"message": "fixture is not armed"}})
                    return

                if number == 1:
                    if not _request_offers_tool(request_payload, action.tool_name):
                        if action.tool_name in {
                            "glob_files",
                            "search_text",
                        } and _request_offers_tool(request_payload, "search_tools"):
                            with state.lock:
                                state.discovery_used = True
                            query = (
                                "glob_files find workspace files by glob pattern"
                                if action.tool_name == "glob_files"
                                else "search_text find literal text in workspace files"
                            )
                            self._emit_tool_call(
                                state.seed,
                                state.index,
                                call_id + "-discover",
                                "search_tools",
                                {
                                    "query": query,
                                    "limit": 8,
                                },
                            )
                            return
                        with state.lock:
                            state.error = (
                                f"Ash request did not offer the {action.tool_name} tool"
                            )
                        self._json(
                            502,
                            {
                                "error": {
                                    "message": f"{action.tool_name} tool unavailable"
                                }
                            },
                        )
                        return
                    self._emit_tool_call(
                        state.seed,
                        state.index,
                        call_id,
                        action.tool_name,
                        action.tool_arguments,
                    )
                    return

                with state.lock:
                    discovery_used = state.discovery_used
                if number == 2 and discovery_used:
                    discovery_outcome = _extract_tool_outcome(
                        request_payload, call_id + "-discover", "search_tools"
                    )
                    activated = _request_offers_tool(request_payload, action.tool_name)
                    if action.tool_name == "search_text":
                        activated = (
                            activated
                            and discovery_outcome is not None
                            and discovery_outcome.get("success") is True
                            and discovery_outcome.get("truncated") is False
                            and _search_discovery_matches_offered_schema(
                                discovery_outcome.get("output"), request_payload
                            )
                        )
                    else:
                        activated = (
                            activated
                            and discovery_outcome is not None
                            and discovery_outcome.get("success") is True
                        )
                    if not activated:
                        with state.lock:
                            state.error = f"{action.tool_name} discovery did not activate its schema"
                        self._json(502, {"error": {"message": state.error}})
                        return
                    self._emit_tool_call(
                        state.seed,
                        state.index,
                        call_id,
                        action.tool_name,
                        action.tool_arguments,
                    )
                    return

                if (number == 2 and not discovery_used) or (
                    number == 3 and discovery_used
                ):
                    outcome = _extract_tool_outcome(
                        request_payload, call_id, action.tool_name
                    )
                    with state.lock:
                        state.tool_outcome = outcome
                        if outcome is None:
                            state.error = (
                                "Ash follow-up request omitted the fixture tool result"
                            )
                    self._stream(
                        [
                            {
                                "id": f"explorer-final-{state.seed}-{state.index}",
                                "object": "chat.completion.chunk",
                                "created": 1,
                                "model": "local-model",
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"content": FIXED_FINAL_RESPONSE},
                                        "finish_reason": "stop",
                                    }
                                ],
                            }
                        ]
                    )
                    return

                with state.lock:
                    state.error = "fixture received an invalid completion sequence"
                self._json(502, {"error": {"message": "invalid completion sequence"}})

            def _emit_tool_call(
                self,
                seed: int,
                index: int,
                call_id: str,
                tool_name: str,
                arguments: dict[str, object],
            ) -> None:
                self._stream(
                    [
                        {
                            "id": f"explorer-{seed}-{index}",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "local-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": call_id,
                                                "type": "function",
                                                "function": {
                                                    "name": tool_name,
                                                    "arguments": json.dumps(
                                                        arguments,
                                                        ensure_ascii=False,
                                                        separators=(",", ":"),
                                                    ),
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        },
                        {
                            "id": f"explorer-{seed}-{index}",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "local-model",
                            "choices": [
                                {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                            ],
                        },
                    ]
                )

            def _stream(self, chunks: list[dict[str, object]]) -> None:
                body = "".join(
                    "data: " + json.dumps(chunk, separators=(",", ":")) + "\n\n"
                    for chunk in chunks
                )
                encoded = (body + "data: [DONE]\n\n").encode("utf-8")
                self._reply(200, encoded, "text/event-stream")

        self.state = state
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@dataclass
class RunState:
    evidence: Path
    scratch: Path
    project: Path
    home: Path
    database: Path
    owner_id: str
    seed: int
    baseline_revision: str | None
    ash_version: str
    fixture: _FixtureServer
    step_index: int = 0
    session_id: str | None = None
    lifecycle: Literal["open", "closed"] = "open"
    pending_cleanup: bool = True
    observations: list[StepObservation] = field(default_factory=list)
    action_accepted: bool = False

    def write_manifest(self, *, in_flight: dict[str, object] | None = None) -> None:
        _write_json(
            self.evidence / "manifest.json",
            {
                "schema_version": 1,
                "baseline_revision": self.baseline_revision,
                "ash_version": self.ash_version,
                "runner_sha256": _runner_sha256(),
                "python_executable": sys.executable,
                "python_version": sys.version.split()[0],
                "seed": self.seed,
                "scratch_path": str(self.scratch),
                "pending_cleanup": self.pending_cleanup,
                "cleanup_state": "pending" if self.pending_cleanup else "removed",
                "lifecycle": self.lifecycle,
                "step_index": self.step_index,
                "session_id": self.session_id,
                "owner_id": self.owner_id,
                "child_environment_keys": _child_environment_keys(),
                "fixture_base_url": self.fixture.base_url,
                "in_flight": in_flight,
            },
        )

    def append_accepted_line(self, raw_line: bytes) -> None:
        with (self.evidence / "actions.jsonl").open("ab") as stream:
            stream.write(raw_line)
            if not raw_line.endswith(b"\n"):
                stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())

    def run_step(self, request: StepRequest, raw_line: bytes) -> StepObservation:
        self.action_accepted = False
        if self.lifecycle != "open":
            raise ExplorerError("run is already closed")
        if self.step_index >= MAX_STEPS:
            raise ExplorerError(f"run limit is {MAX_STEPS} steps")

        index = self.step_index + 1
        target = _target_path(
            self.scratch,
            self.project,
            "."
            if request.tool_name in {"glob_files", "search_text"}
            else request.file_path,
        )
        search_before: SearchSnapshot | None = None
        if request.tool_name == "glob_files":
            before_hash, _ = _glob_snapshot(target, request.file_path)
        elif request.tool_name == "list_dir":
            before_hash, _ = _directory_snapshot(target)
        elif request.tool_name == "search_text":
            assert request.search_query is not None
            search_before = _search_snapshot(self.project, request.search_query)
            before_hash = search_before.sha256
        else:
            before_hash = _hash_target(target)
        step_dir = self.evidence / "steps" / f"{index:02d}"
        step_dir.mkdir(parents=True, mode=0o700)
        self.append_accepted_line(raw_line)
        self.action_accepted = True
        _write_json(
            step_dir / "target-before.json",
            {"path": str(target), "sha256": before_hash},
        )
        self.fixture.state.arm(index, request)
        self.write_manifest(
            in_flight={
                "step_index": index,
                "label": request.label,
                "target_path": str(target),
                "dispatch_pending": True,
            }
        )

        command = [
            sys.executable,
            "-m",
            "ash",
            "--db-directory",
            str(self.database),
            "--mode",
            "auto_edit",
        ]
        if self.session_id is not None:
            command.extend(["--session", self.session_id])
        command.extend(
            [
                "-p",
                request.prompt,
                "--output-format",
                "stream-json",
            ]
        )
        _write_json(
            step_dir / "command.json", {"argv": command, "cwd": str(self.project)}
        )

        status, stdout, stderr = _run_bounded_process(
            command,
            cwd=self.project,
            environment=_child_environment(self, provider=True),
            timeout=CHILD_TIMEOUT_SECONDS,
            output_limit=MAX_CHILD_OUTPUT_BYTES,
        )
        (step_dir / "stdout.bin").write_bytes(stdout.data)
        (step_dir / "stderr.bin").write_bytes(stderr.data)
        events, malformed_lines = _parse_stream_events(bytes(stdout.data))
        fixture_snapshot = self.fixture.state.snapshot()
        _write_json(step_dir / "provider.json", fixture_snapshot)
        search_after: SearchSnapshot | None = None
        if request.tool_name == "glob_files":
            after_hash, directory_listing = _glob_snapshot(target, request.file_path)
        elif request.tool_name == "list_dir":
            after_hash, directory_listing = _directory_snapshot(target)
        elif request.tool_name == "search_text":
            assert request.search_query is not None
            search_after = _search_snapshot(self.project, request.search_query)
            after_hash = search_after.sha256
            directory_listing = None
        else:
            after_hash = _hash_target(target)
            directory_listing = None

        tool_events = [
            event
            for event in events
            if event.get("type") in {"tool.completed", "tool.error", "tool.denied"}
            and event.get("tool") == request.tool_name
        ]
        tool_outcome = _tool_outcome(tool_events, fixture_snapshot)
        search_verification = (
            _verify_search(
                search_before,
                search_after,
                tool_outcome,
                len(tool_events),
            )
            if request.tool_name == "search_text"
            else None
        )
        result_event = next(
            (
                event
                for event in reversed(events)
                if event.get("type") == "turn.completed"
            ),
            None,
        )
        received_session_id = bool(
            result_event is not None and isinstance(result_event.get("session_id"), str)
        )
        status["session_id_in_result"] = received_session_id
        if received_session_id and result_event is not None:
            self.session_id = str(result_event["session_id"])
        classification, detail = _classify(
            request,
            status,
            tool_outcome,
            before_hash,
            after_hash,
            malformed_lines,
            fixture_snapshot,
            directory_listing=directory_listing,
            search_verification=search_verification,
        )

        self.step_index = index
        observation = StepObservation(
            index=index,
            label=request.label,
            classification=classification,
            process_status=status,
            tool_outcome=tool_outcome,
            target_before_sha256=before_hash,
            target_after_sha256=after_hash,
            artifact_paths={
                "actions": "actions.jsonl",
                "command": str((step_dir / "command.json").relative_to(self.evidence)),
                "stdout": str((step_dir / "stdout.bin").relative_to(self.evidence)),
                "stderr": str((step_dir / "stderr.bin").relative_to(self.evidence)),
                "provider": str(
                    (step_dir / "provider.json").relative_to(self.evidence)
                ),
            },
            detail=detail,
            search_verification=search_verification,
        )
        self.observations.append(observation)
        _write_json(step_dir / "observation.json", asdict(observation))
        self.write_manifest()
        return observation

    def close(self, *, reason: str) -> dict[str, object]:
        if self.lifecycle == "closed":
            return _summary(self, reason)
        fixture_error: str | None = None
        try:
            self.fixture.close()
        except (OSError, RuntimeError) as exc:
            fixture_error = f"fixture cleanup failed: {exc}"

        marker = self.scratch / ".ash-explorer-owner.json"
        expected = {"schema_version": 1, "owner_id": self.owner_id}
        try:
            marker_payload = _strict_json_loads(marker.read_bytes())
        except (OSError, ValueError, json.JSONDecodeError):
            marker_payload = None
        if marker_payload != expected:
            self.lifecycle = "closed"
            self.pending_cleanup = True
            self.write_manifest()
            raise ExplorerError("owned scratch marker changed; scratch was preserved")

        try:
            shutil.rmtree(self.scratch)
            self.pending_cleanup = False
        except OSError as exc:
            _write_json(
                self.evidence / "cleanup-error.json",
                {"error_type": type(exc).__name__, "error": str(exc)},
            )
            self.pending_cleanup = True
            self.lifecycle = "closed"
            self.write_manifest()
            raise ExplorerError(f"scratch cleanup failed: {exc}") from exc
        self.lifecycle = "closed"
        self.write_manifest()
        summary = _summary(self, reason)
        if fixture_error is not None:
            summary["fixture_cleanup_error"] = fixture_error
        _write_json(self.evidence / "report.json", summary)
        if fixture_error is not None:
            raise ExplorerError(fixture_error)
        return summary


@dataclass
class _BoundedCapture:
    limit: int
    data: bytearray = field(default_factory=bytearray)
    overflow: threading.Event = field(default_factory=threading.Event)

    def drain(self, stream: BinaryIO) -> None:
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                available = self.limit - len(self.data)
                if available > 0:
                    self.data.extend(chunk[:available])
                if len(chunk) > max(available, 0):
                    self.overflow.set()
        finally:
            stream.close()


def _run_bounded_process(
    argv: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: int,
    output_limit: int,
) -> tuple[dict[str, object], _BoundedCapture, _BoundedCapture]:
    stdout = _BoundedCapture(output_limit)
    stderr = _BoundedCapture(output_limit)
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except OSError as exc:
        return (
            {"exit_code": None, "timed_out": False, "launch_error": str(exc)},
            stdout,
            stderr,
        )
    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(target=stdout.drain, args=(process.stdout,), daemon=True),
        threading.Thread(target=stderr.drain, args=(process.stderr,), daemon=True),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    output_limited = False
    deadline = started + timeout
    try:
        while process.poll() is None:
            if stdout.overflow.is_set() or stderr.overflow.is_set():
                output_limited = True
                _stop_process(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _stop_process(process)
                break
            time.sleep(0.05)
        process.wait(timeout=2)
    except BaseException:
        _stop_process(process)
        raise
    finally:
        for reader in readers:
            reader.join(timeout=2)
    return (
        {
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "output_limited": output_limited,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout_truncated": stdout.overflow.is_set(),
            "stderr_truncated": stderr.overflow.is_set(),
        },
        stdout,
        stderr,
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _child_environment(run: RunState, *, provider: bool) -> dict[str, str]:
    home = str(run.home)
    environment = {
        "PATH": os.defpath,
        "HOME": home,
        "USERPROFILE": home,
        "APPDATA": str(run.home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(run.home / "AppData" / "Local"),
        "XDG_CONFIG_HOME": str(run.home / ".config"),
        "XDG_DATA_HOME": str(run.home / ".local" / "share"),
        "XDG_STATE_HOME": str(run.home / ".local" / "state"),
        "XDG_CACHE_HOME": str(run.home / ".cache"),
        "TMPDIR": str(run.scratch / "tmp"),
        "TMP": str(run.scratch / "tmp"),
        "TEMP": str(run.scratch / "tmp"),
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
        "TERM": "dumb",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "ASH_DB_DIRECTORY": str(run.database),
    }
    for name in ("SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    if provider:
        environment.update(
            {
                "ASH_MODEL": "lmstudio/local-model",
                "LMSTUDIO_API_BASE": run.fixture.base_url,
            }
        )
    Path(environment["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    return environment


def _child_environment_keys() -> list[str]:
    return sorted(
        {
            "PATH",
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
            "TMPDIR",
            "TMP",
            "TEMP",
            "PYTHONPATH",
            "PYTHONNOUSERSITE",
            "PYTHONDONTWRITEBYTECODE",
            "NO_COLOR",
            "TERM",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_TERMINAL_PROMPT",
            "ASH_DB_DIRECTORY",
            "ASH_MODEL",
            "LMSTUDIO_API_BASE",
            "SYSTEMROOT",
            "WINDIR",
        }
    )


def _create_run(evidence_path: Path, seed: int) -> RunState:
    evidence = _prepare_evidence_path(evidence_path)
    scratch = Path(tempfile.mkdtemp(prefix="ash-explorer-")).resolve()
    owner_id = str(uuid.uuid4())
    project = scratch / "project"
    home = scratch / "home"
    database = scratch / "db"
    for directory in (project, home, database, scratch / "tmp"):
        directory.mkdir(mode=0o700)
    (scratch / "outside-marker.txt").write_bytes(b"ASH_EXPLORER_OUTSIDE_MARKER\n")
    _write_json(
        scratch / ".ash-explorer-owner.json",
        {"schema_version": 1, "owner_id": owner_id},
    )
    baseline_revision = _baseline_revision()
    ash_version = _ash_version()
    _write_json(
        evidence / "manifest.json",
        {
            "schema_version": 1,
            "baseline_revision": baseline_revision,
            "ash_version": ash_version,
            "runner_sha256": _runner_sha256(),
            "python_executable": sys.executable,
            "python_version": sys.version.split()[0],
            "seed": seed,
            "scratch_path": str(scratch),
            "pending_cleanup": True,
            "cleanup_state": "pending",
            "lifecycle": "starting",
            "step_index": 0,
            "session_id": None,
            "owner_id": owner_id,
            "fixture_base_url": None,
        },
    )
    fixture: _FixtureServer | None = None
    try:
        try:
            fixture = _FixtureServer(seed)
        except OSError as exc:
            raise ExplorerError(f"could not start loopback fixture: {exc}") from exc
        run = RunState(
            evidence=evidence,
            scratch=scratch,
            project=project,
            home=home,
            database=database,
            owner_id=owner_id,
            seed=seed,
            baseline_revision=baseline_revision,
            ash_version=ash_version,
            fixture=fixture,
        )
        run.write_manifest()
        environment = _child_environment(run, provider=False)
        command = [
            sys.executable,
            "-m",
            "ash",
            "--db-directory",
            str(database),
            "trust",
            "add",
            str(project),
        ]
        _write_json(
            evidence / "trust-command.json", {"argv": command, "cwd": str(project)}
        )
        run.write_manifest(
            in_flight={"operation": "trust_add", "dispatch_pending": True}
        )
        status, stdout, stderr = _run_bounded_process(
            command,
            cwd=project,
            environment=environment,
            timeout=TRUST_TIMEOUT_SECONDS,
            output_limit=MAX_CHILD_OUTPUT_BYTES,
        )
        (evidence / "trust-stdout.bin").write_bytes(stdout.data)
        (evidence / "trust-stderr.bin").write_bytes(stderr.data)
        _write_json(evidence / "trust-status.json", status)
        run.write_manifest()
        if status.get("exit_code") != 0 or status.get("timed_out"):
            raise ExplorerError("Ash could not trust the disposable project")
        (evidence / "actions.jsonl").touch(mode=0o600)
        (evidence / "steps").mkdir(mode=0o700)
        return run
    except BaseException as exc:
        cleanup_error: str | None = None
        if fixture is not None:
            try:
                fixture.close()
            except (OSError, RuntimeError) as cleanup_exc:
                cleanup_error = f"fixture cleanup failed: {cleanup_exc}"
        marker = scratch / ".ash-explorer-owner.json"
        cleaned = False
        try:
            owner = _strict_json_loads(marker.read_bytes())
            if owner == {"schema_version": 1, "owner_id": owner_id}:
                try:
                    shutil.rmtree(scratch)
                    cleaned = True
                except OSError as cleanup_exc:
                    cleanup_error = f"scratch cleanup failed: {cleanup_exc}"
            else:
                cleanup_error = "owned scratch marker changed; scratch was preserved"
        except (OSError, ValueError, json.JSONDecodeError) as cleanup_exc:
            cleanup_error = f"could not verify owned scratch marker: {cleanup_exc}"
        _write_json(
            evidence / "manifest.json",
            {
                "schema_version": 1,
                "baseline_revision": baseline_revision,
                "ash_version": ash_version,
                "runner_sha256": _runner_sha256(),
                "python_executable": sys.executable,
                "python_version": sys.version.split()[0],
                "seed": seed,
                "scratch_path": str(scratch),
                "pending_cleanup": not cleaned,
                "cleanup_state": "removed" if cleaned else "preserved_pending",
                "lifecycle": "startup_failed",
                "step_index": 0,
                "session_id": None,
                "owner_id": owner_id,
            },
        )
        _write_json(
            evidence / "startup-error.json",
            {
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": "".join(traceback.format_exception(exc))[
                    -MAX_TRACEBACK_CHARS:
                ],
                "cleanup_error": cleanup_error,
            },
        )
        raise


def _prepare_evidence_path(requested: Path) -> Path:
    raw = requested.expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    if raw.exists() or raw.is_symlink():
        raise ExplorerError("evidence directory already exists")
    target = raw.resolve(strict=False)
    root = ROOT.resolve()
    if target == root or root in target.parents:
        raise ExplorerError("evidence directory must be outside the checkout")
    if target.exists() or target.is_symlink():
        raise ExplorerError("evidence directory already exists")
    if not target.parent.is_dir():
        raise ExplorerError("evidence parent must be an existing directory")
    target.mkdir(mode=0o700)
    return target


def _parse_action(raw_line: bytes) -> tuple[str, StepRequest | None]:
    if len(raw_line) > MAX_ACTION_BYTES:
        raise ExplorerError(f"JSONL action exceeds {MAX_ACTION_BYTES} bytes")
    text = raw_line.decode("utf-8").rstrip("\r\n")
    if not text:
        raise ExplorerError("blank JSONL lines are not accepted")
    payload = _strict_json_loads(text)
    if not isinstance(payload, dict):
        raise ExplorerError("each JSONL line must contain one JSON object")
    action = payload.get("action")
    if action == "finish":
        if set(payload) != {"action"}:
            raise ExplorerError("finish accepts only the action field")
        return "finish", None
    if action != "step":
        raise ExplorerError("action must be 'step' or 'finish'")
    if set(payload) != {"action", "label", "prompt", "tool_call", "expect"}:
        raise ExplorerError(
            "step fields must be action, label, prompt, tool_call, expect"
        )

    label = _bounded_string(payload.get("label"), "label", MAX_LABEL_BYTES)
    prompt = _bounded_string(payload.get("prompt"), "prompt", MAX_PROMPT_BYTES)
    tool_call = payload.get("tool_call")
    if not isinstance(tool_call, dict) or set(tool_call) != {"name", "arguments"}:
        raise ExplorerError("tool_call must contain only name and arguments")
    tool_name = tool_call.get("name")
    if tool_name not in {
        "write_file",
        "read_file",
        "list_dir",
        "glob_files",
        "search_text",
    }:
        raise ExplorerError(
            "only write_file, read_file, list_dir, glob_files, and search_text are supported"
        )
    arguments = tool_call.get("arguments")
    if not isinstance(arguments, dict):
        raise ExplorerError("tool arguments must be an object")
    search_query: SearchQuery | None = None
    if tool_name == "write_file":
        if set(arguments) != {"file_path", "content", "overwrite"}:
            raise ExplorerError(
                "write_file arguments must be file_path, content, overwrite"
            )
    elif tool_name == "search_text":
        required = {
            "pattern",
            "directory_path",
            "glob",
            "fixed_strings",
            "case_sensitive",
            "max_results",
        }
        if set(arguments) != required:
            raise ExplorerError(
                "search_text arguments must be pattern, directory_path, glob, "
                "fixed_strings, case_sensitive, max_results"
            )
        pattern = _bounded_string(
            arguments.get("pattern"), "pattern", MAX_SEARCH_PATTERN_BYTES
        )
        if not pattern:
            raise ExplorerError("search_text pattern must not be empty")
        if _has_control_chars(pattern):
            raise ExplorerError("search_text pattern must not contain controls")
        if arguments.get("directory_path") != ".":
            raise ExplorerError("search_text directory_path must be '.'")
        if arguments.get("fixed_strings") is not True:
            raise ExplorerError("search_text requires fixed_strings=true")
        if arguments.get("case_sensitive") is not True:
            raise ExplorerError("search_text requires case_sensitive=true")
        if (
            type(arguments.get("max_results")) is not int
            or arguments["max_results"] != MAX_SEARCH_MATCHES
        ):
            raise ExplorerError(
                f"search_text requires max_results={MAX_SEARCH_MATCHES}"
            )
        glob_value = arguments.get("glob")
        if glob_value is not None:
            glob_value = _bounded_string(glob_value, "glob", MAX_GLOB_PATTERN_BYTES)
            if (
                _has_control_chars(glob_value)
                or ".." in Path(glob_value).parts
                or ".." in PureWindowsPath(glob_value).parts
            ):
                raise ExplorerError("search_text glob must be bounded and relative")
            _validate_relative_path_syntax(glob_value)
        search_query = SearchQuery(pattern=pattern, glob=glob_value)
    elif tool_name == "list_dir":
        if (
            set(arguments) != {"directory_path", "recursive", "max_results"}
            or arguments.get("recursive") is not False
            or type(arguments.get("max_results")) is not int
            or arguments["max_results"] != MAX_DIRECTORY_ENTRIES
        ):
            raise ExplorerError(
                "list_dir requires directory_path, recursive=false, max_results=64"
            )
    elif tool_name == "glob_files":
        if (
            set(arguments) != {"pattern", "directory_path", "max_results"}
            or arguments.get("directory_path") != "."
            or type(arguments.get("max_results")) is not int
            or arguments["max_results"] != MAX_DIRECTORY_ENTRIES
        ):
            raise ExplorerError(
                "glob_files requires pattern, directory_path='.', max_results=64"
            )
    elif set(arguments) != {"file_path"}:
        raise ExplorerError("read_file arguments must contain only file_path")
    if tool_name == "search_text":
        file_path = "."
    else:
        file_path = _bounded_string(
            arguments.get(
                "directory_path"
                if tool_name == "list_dir"
                else "pattern"
                if tool_name == "glob_files"
                else "file_path"
            ),
            "file_path",
            MAX_PATH_BYTES,
        )
        _validate_relative_path_syntax(file_path)
        if tool_name == "glob_files" and (
            len(file_path.encode("utf-8")) > MAX_GLOB_PATTERN_BYTES
            or _has_control_chars(file_path)
            or ".." in Path(file_path).parts
            or ".." in PureWindowsPath(file_path).parts
        ):
            raise ExplorerError("glob_files pattern must be bounded and relative")
    content = (
        _bounded_string(arguments.get("content"), "content", MAX_CONTENT_BYTES)
        if tool_name == "write_file"
        else ""
    )
    overwrite = arguments.get("overwrite", False)
    if type(overwrite) is not bool:
        raise ExplorerError("overwrite must be a JSON boolean")

    expect = payload.get("expect")
    if not isinstance(expect, dict) or "kind" not in expect:
        raise ExplorerError("expect must contain a kind")
    kind = expect.get("kind")
    if kind == "expected_success":
        if set(expect) != {"kind"}:
            raise ExplorerError("expected_success accepts only kind")
        expectation = ExpectedOutcome("expected_success")
    elif kind == "expected_refusal":
        if set(expect) != {"kind", "error_contains"}:
            raise ExplorerError("expected_refusal requires error_contains")
        refusal = _bounded_string(
            expect.get("error_contains"), "error_contains", MAX_REFUSAL_TEXT_BYTES
        )
        if not refusal.strip():
            raise ExplorerError("error_contains must not be empty")
        expectation = ExpectedOutcome("expected_refusal", refusal)
    elif kind == "observed":
        if set(expect) != {"kind"}:
            raise ExplorerError("observed accepts only kind")
        expectation = ExpectedOutcome("observed")
    else:
        raise ExplorerError(
            "expect.kind must be expected_success, expected_refusal, or observed"
        )
    return (
        "step",
        StepRequest(
            label,
            prompt,
            file_path,
            content,
            overwrite,
            expectation,
            tool_name,
            search_query,
        ),
    )


def _validate_relative_path_syntax(value: str) -> None:
    if not value or "\x00" in value:
        raise ExplorerError("file_path must be a non-empty path without NUL")
    if Path(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise ExplorerError("absolute file_path values are not accepted")
    if PureWindowsPath(value).drive:
        raise ExplorerError("drive-qualified file_path values are not accepted")
    if value.startswith(("/", "\\")):
        raise ExplorerError("rooted file_path values are not accepted")


def _has_control_chars(value: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in value)


def _target_path(scratch: Path, project: Path, file_path: str) -> Path:
    target = (project / file_path).resolve(strict=False)
    scratch_resolved = scratch.resolve()
    if target == scratch_resolved or scratch_resolved not in target.parents:
        raise ExplorerError(
            "write target must remain inside this run's disposable scratch"
        )
    return target


def _hash_target(path: Path) -> str | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    if not path.is_file() or path.is_symlink():
        raise ExplorerError("write target exists but is not a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_snapshot(path: Path) -> tuple[str | None, str | None]:
    if not path.exists():
        return None, None
    if path.is_symlink():
        raise ExplorerError("directory target must not be a symlink")
    if not path.is_dir():
        return _hash_target(path), None
    try:
        with os.scandir(path) as entries:
            names = []
            for entry in entries:
                if entry.is_symlink():
                    raise ExplorerError("directory snapshot cannot contain symlinks")
                names.append(
                    entry.name + ("/" if entry.is_dir(follow_symlinks=False) else "")
                )
                if len(names) > MAX_DIRECTORY_ENTRIES:
                    raise ExplorerError(
                        "directory snapshot exceeds Explorer entry limit"
                    )
    except OSError as exc:
        raise ExplorerError("directory snapshot failed") from exc
    listing = "\n".join(sorted(names))
    digest = hashlib.sha256(b"list_dir\x00" + listing.encode("utf-8")).hexdigest()
    return digest, listing


def _glob_snapshot(root: Path, pattern: str) -> tuple[str | None, str | None]:
    if not root.is_dir() or root.is_symlink():
        return None, None
    matches: list[str] = []
    scanned = 0
    directories: list[tuple[Path, int]] = [(root, 0)]
    try:
        while directories:
            directory, depth = directories.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > MAX_GLOB_SCAN_ENTRIES:
                        raise ExplorerError("glob snapshot exceeds scan limit")
                    if entry.is_symlink():
                        raise ExplorerError("glob snapshot cannot contain symlinks")
                    if entry.is_dir(follow_symlinks=False):
                        if depth >= MAX_GLOB_DEPTH:
                            raise ExplorerError("glob snapshot exceeds depth limit")
                        directories.append((Path(entry.path), depth + 1))
                    elif entry.is_file(follow_symlinks=False):
                        relative = Path(entry.path).relative_to(root).as_posix()
                        if fnmatch.fnmatch(relative, pattern):
                            matches.append(relative)
                            if len(matches) > MAX_DIRECTORY_ENTRIES:
                                raise ExplorerError("glob snapshot exceeds match limit")
                    else:
                        raise ExplorerError(
                            "glob snapshot contains an unsupported entry"
                        )
    except OSError as exc:
        raise ExplorerError("glob snapshot failed") from exc
    listing = "\n".join(sorted(matches))
    digest = hashlib.sha256(
        b"glob_files\x00" + pattern.encode("utf-8") + b"\x00" + listing.encode("utf-8")
    ).hexdigest()
    return digest, listing


def _search_snapshot(root: Path, query: SearchQuery) -> SearchSnapshot:
    """Build a bounded, independent proof of the fixed-string search corpus."""
    from ash.safety.guard import SafetyGuard, SafetyViolation
    from ash.safety.scoped_io import (
        list_scoped_directory,
        read_scoped_bytes,
        stat_scoped_path,
    )

    entries: list[tuple[str, str, tuple[int, ...]]] = []
    selected_files: list[tuple[str, Path, tuple[int, ...]]] = []
    directories: list[tuple[Path, tuple[int, ...]]] = []
    scanned = 0
    total_bytes = 0

    def incomplete(detail: str) -> SearchSnapshot:
        return SearchSnapshot(None, None, False, detail)

    try:
        root_stat = os.lstat(root)
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            return incomplete("search root is not a regular directory")
        guard = SafetyGuard(root)
        pending: list[tuple[Path, str, int, tuple[int, ...] | None]] = [
            (root, "", 0, None)
        ]
        while pending:
            directory, relative_directory, depth, expected_directory = pending.pop()
            _, directory_stat = stat_scoped_path(directory, guard)
            if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_ISLNK(
                directory_stat.st_mode
            ):
                return incomplete("search directory changed to a link or special entry")
            before_directory = _search_stat_key(directory_stat)
            if (
                expected_directory is not None
                and before_directory != expected_directory
            ):
                return incomplete("search directory changed before scanning")
            directories.append((directory, before_directory))
            _, children = list_scoped_directory(directory, guard)
            for name, is_directory in children:
                scanned += 1
                if scanned > MAX_SEARCH_SCAN_ENTRIES:
                    return incomplete("search scan exceeds entry limit")
                relative = (
                    f"{relative_directory}/{name}" if relative_directory else name
                )
                try:
                    relative_bytes = relative.encode("utf-8", errors="strict")
                except UnicodeEncodeError:
                    return incomplete("search path is not valid UTF-8")
                if len(relative_bytes) > MAX_PATH_BYTES or _has_control_chars(relative):
                    return incomplete("search path exceeds the safe path bound")
                path = directory / name
                _, metadata = stat_scoped_path(path, guard)
                metadata_key = _search_stat_key(metadata)
                actual_directory = stat.S_ISDIR(metadata.st_mode)
                if actual_directory != is_directory:
                    return incomplete("search entry changed during directory listing")
                if actual_directory:
                    entries.append((relative, "directory", metadata_key))
                    child_depth = depth + 1
                    if child_depth >= MAX_SEARCH_DEPTH:
                        return incomplete("search tree reaches the depth limit")
                    pending.append((path, relative, child_depth, metadata_key))
                elif stat.S_ISREG(metadata.st_mode):
                    entries.append((relative, "file", metadata_key))
                    if query.glob is None or fnmatch.fnmatch(relative, query.glob):
                        selected_files.append((relative, path, metadata_key))
                else:
                    return incomplete("search tree contains a special entry")
            _, after_directory = stat_scoped_path(directory, guard)
            if _search_stat_key(after_directory) != before_directory:
                return incomplete("search directory changed during snapshot")

        file_hashes: dict[str, str] = {}
        expected_matches: list[str] = []
        expected_output_bytes = 0
        for relative, path, expected_metadata in sorted(selected_files):
            if expected_metadata[3] > MAX_SEARCH_FILE_BYTES:
                return incomplete("selected search file exceeds the file limit")
            _, before_file = stat_scoped_path(path, guard)
            if _search_stat_key(before_file) != expected_metadata:
                return incomplete("selected search file changed before reading")
            _, content = read_scoped_bytes(
                path,
                guard,
                max_bytes=MAX_SEARCH_FILE_BYTES,
            )
            _, after_file = stat_scoped_path(path, guard)
            if _search_stat_key(after_file) != expected_metadata:
                return incomplete("selected search file changed during snapshot")
            if len(content) > MAX_SEARCH_FILE_BYTES:
                return incomplete("selected search file exceeds the file limit")
            total_bytes += len(content)
            if total_bytes > MAX_SEARCH_TOTAL_BYTES:
                return incomplete("selected search files exceed the aggregate limit")
            file_hashes[relative] = hashlib.sha256(content).hexdigest()
            with io.TextIOWrapper(
                io.BytesIO(content), encoding="utf-8", errors="strict"
            ) as handle:
                for line_number, physical_line in enumerate(handle, start=1):
                    text = physical_line.rstrip("\r\n")
                    if query.pattern not in text:
                        continue
                    rendered = f"{relative}:{line_number}:{text}"
                    rendered_size = len(rendered.encode("utf-8", errors="strict"))
                    separator_size = 1 if expected_matches else 0
                    if len(expected_matches) >= MAX_SEARCH_MATCHES:
                        return incomplete(
                            "search result count exceeds the result limit"
                        )
                    if (
                        expected_output_bytes + separator_size + rendered_size
                        > MAX_SEARCH_OUTPUT_BYTES
                    ):
                        return incomplete("search output exceeds the rendered limit")
                    expected_matches.append(rendered)
                    expected_output_bytes += separator_size + rendered_size

        for directory, expected_metadata in directories:
            _, current = stat_scoped_path(directory, guard)
            if _search_stat_key(current) != expected_metadata:
                return incomplete("search directory changed during snapshot")
        for relative, kind, expected_metadata in entries:
            if kind == "file":
                path = root / relative
                _, current = stat_scoped_path(path, guard)
                if _search_stat_key(current) != expected_metadata:
                    return incomplete("search entry changed during snapshot")

        digest = hashlib.sha256(b"ash-explorer-search-snapshot-v1\x00")
        digest.update(
            json.dumps(
                {
                    "pattern": query.pattern,
                    "directory_path": query.directory_path,
                    "glob": query.glob,
                    "fixed_strings": True,
                    "case_sensitive": True,
                    "max_results": MAX_SEARCH_MATCHES,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8", errors="strict")
        )
        for relative, kind, _metadata in sorted(entries):
            digest.update(b"\x00entry\x00")
            digest.update(kind.encode("ascii"))
            digest.update(b"\x00")
            digest.update(relative.encode("utf-8", errors="strict"))
            digest.update(b"\x00")
            content_hash = file_hashes.get(relative)
            if content_hash is not None:
                digest.update(b"content\x00")
                digest.update(content_hash.encode("ascii"))
        return SearchSnapshot(digest.hexdigest(), tuple(expected_matches), True, None)
    except (OSError, SafetyViolation, UnicodeError, ValueError):
        return incomplete("search snapshot could not read a stable workspace")


def _search_stat_key(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _search_output_digest(output: str) -> str:
    return hashlib.sha256(output.encode("utf-8", errors="strict")).hexdigest()


def _verify_search(
    before: SearchSnapshot | None,
    after: SearchSnapshot | None,
    outcome: dict[str, object] | None,
    event_count: int,
) -> SearchVerification:
    event_truncated: bool | None = None
    fixture_truncated: bool | None = None
    actual_count: int | None = None
    actual_digest: str | None = None
    expected_count = (
        len(before.expected_matches)
        if before is not None and before.expected_matches is not None
        else None
    )
    expected_digest = (
        _search_output_digest("\n".join(before.expected_matches))
        if before is not None and before.expected_matches is not None
        else None
    )

    def make(
        verified: bool, evidence_complete: bool, detail: str | None
    ) -> SearchVerification:
        return SearchVerification(
            verified=verified,
            evidence_complete=evidence_complete,
            before_sha256=before.sha256 if before is not None else None,
            after_sha256=after.sha256 if after is not None else None,
            expected_match_count=expected_count,
            actual_match_count=actual_count,
            expected_output_sha256=expected_digest,
            actual_output_sha256=actual_digest,
            event_truncated=event_truncated,
            fixture_truncated=fixture_truncated,
            detail=detail,
        )

    if before is None or not before.complete:
        return make(
            False,
            False,
            before.detail if before is not None else "missing pre-snapshot",
        )
    if after is None or not after.complete:
        return make(
            False,
            False,
            after.detail if after is not None else "missing post-snapshot",
        )
    if event_count != 1 or outcome is None:
        return make(False, False, "missing or multiple search tool events")
    fixture_result = outcome.get("fixture_result")
    if not isinstance(fixture_result, dict):
        return make(False, False, "missing fixture tool result")

    raw_event_truncated = outcome.get("truncated")
    raw_fixture_truncated = outcome.get("fixture_truncated")
    parsed_fixture_truncated = fixture_result.get("truncated")
    if type(raw_event_truncated) is bool:
        event_truncated = raw_event_truncated
    if type(raw_fixture_truncated) is bool:
        fixture_truncated = raw_fixture_truncated
    if (
        type(raw_event_truncated) is not bool
        or type(raw_fixture_truncated) is not bool
        or type(parsed_fixture_truncated) is not bool
    ):
        return make(False, False, "truncation metadata is missing or invalid")
    if raw_fixture_truncated != parsed_fixture_truncated:
        return make(False, True, "captured fixture truncation is inconsistent")
    if outcome.get("success") is not True or fixture_result.get("success") is not True:
        return make(False, True, "search tool did not report explicit success")
    if outcome.get("ambiguous") is not False:
        return make(False, False, "search outcome is not explicitly unambiguous")
    if raw_event_truncated != raw_fixture_truncated:
        return make(False, True, "tool event and fixture truncation disagree")
    if raw_event_truncated is not False:
        return make(False, True, "search result was truncated")
    if fixture_result.get("output") != outcome.get("output") or fixture_result.get(
        "error"
    ) != outcome.get("error"):
        return make(False, True, "tool event and fixture result disagree")
    if before.sha256 != after.sha256:
        return make(False, True, "workspace changed during search dispatch")
    output = outcome.get("output")
    if not isinstance(output, str):
        return make(False, False, "search output is missing")
    try:
        if len(output.encode("utf-8", errors="strict")) > MAX_SEARCH_OUTPUT_BYTES:
            return make(False, True, "search output exceeds the rendered limit")
        actual_digest = _search_output_digest(output)
    except UnicodeEncodeError:
        return make(False, True, "search output is not valid UTF-8")
    actual_records = [] if output == "" else output.split("\n")
    actual_count = len(actual_records)
    expected_records = before.expected_matches
    assert expected_records is not None
    if Counter(actual_records) != Counter(expected_records):
        return make(False, True, "search output does not match workspace contents")
    return make(True, True, None)


def _classify(
    request: StepRequest,
    process_status: dict[str, object],
    tool_outcome: dict[str, object] | None,
    before_hash: str | None,
    after_hash: str | None,
    malformed_lines: list[int],
    fixture: dict[str, object],
    *,
    directory_listing: str | None = None,
    search_verification: SearchVerification | None = None,
) -> tuple[str, str | None]:
    if (
        request.tool_name == "search_text"
        and tool_outcome is not None
        and tool_outcome.get("ambiguous") is True
    ):
        return "ambiguous_outcome", "tool dispatch outcome is ambiguous"
    fixture_error = fixture.get("error")
    output_truncated = bool(
        process_status.get("stdout_truncated") or process_status.get("stderr_truncated")
    )
    if (
        process_status.get("launch_error")
        or process_status.get("timed_out")
        or process_status.get("output_limited")
        or output_truncated
        or fixture_error
        or malformed_lines
    ):
        details = []
        if process_status.get("launch_error"):
            details.append(f"launch: {process_status['launch_error']}")
        if process_status.get("timed_out"):
            details.append("Ash process timed out")
        if process_status.get("output_limited"):
            details.append("Ash process exceeded output limit")
        elif output_truncated:
            details.append("Ash process output was truncated")
        if fixture_error:
            details.append(str(fixture_error))
        if malformed_lines:
            details.append(f"malformed stream-json lines: {malformed_lines}")
        return "driver_failure", "; ".join(details)

    if process_status.get("exit_code") != 0:
        return (
            "unexpected_failure",
            f"Ash exited with {process_status.get('exit_code')}",
        )
    if tool_outcome is not None and tool_outcome.get("ambiguous") is True:
        return "ambiguous_outcome", "tool dispatch outcome is ambiguous"
    if request.expectation.kind == "observed":
        return "observed", None
    if process_status.get("session_id_in_result") is not True:
        return "driver_failure", "Ash completion omitted its session id"
    if tool_outcome is None:
        return (
            "unexpected_failure",
            f"no {request.tool_name} outcome was present in Ash evidence",
        )

    succeeded = tool_outcome.get("success") is True
    error = tool_outcome.get("error")
    if request.expectation.kind == "expected_success":
        if request.tool_name == "search_text":
            if not succeeded:
                return (
                    "unexpected_failure",
                    "search tool did not report explicit success",
                )
            if search_verification is None:
                return "observed", "unverifiable: search evidence is missing"
            if search_verification.verified:
                return "expected_success", None
            if not search_verification.evidence_complete:
                reason = search_verification.detail or "search evidence is incomplete"
                return "observed", f"unverifiable: {reason}"
            return (
                "unexpected_failure",
                search_verification.detail or "search verification failed",
            )
        if request.tool_name in {"list_dir", "glob_files"}:
            if (
                succeeded
                and directory_listing is not None
                and before_hash is not None
                and before_hash == after_hash
                and tool_outcome.get("output") == directory_listing
                and hashlib.sha256(
                    (
                        b"list_dir\x00"
                        if request.tool_name == "list_dir"
                        else b"glob_files\x00"
                        + request.file_path.encode("utf-8")
                        + b"\x00"
                    )
                    + directory_listing.encode("utf-8")
                ).hexdigest()
                == after_hash
            ):
                return "expected_success", None
            return (
                "unexpected_failure",
                "directory listing did not match stable workspace entries",
            )
        if request.tool_name == "read_file":
            output = tool_outcome.get("output")
            if (
                succeeded
                and before_hash is not None
                and before_hash == after_hash
                and isinstance(output, str)
                and output.startswith("[read_file metadata: ")
                and f"; sha256={before_hash};" in output.split("\n", 1)[0]
            ):
                return "expected_success", None
            return (
                "unexpected_failure",
                "read did not return unchanged target file metadata",
            )
        expected_bytes_hash = hashlib.sha256(
            request.content.encode("utf-8")
        ).hexdigest()
        if succeeded and after_hash == expected_bytes_hash:
            return "expected_success", None
        return "unexpected_failure", "write did not produce the requested target bytes"

    refusal_text = request.expectation.error_contains or ""
    if succeeded:
        return "unexpected_success", "write succeeded where a refusal was expected"
    if not isinstance(error, str) or refusal_text not in error:
        return (
            "unexpected_failure",
            "tool error did not match the declared refusal text",
        )
    if request.tool_name == "search_text":
        if tool_outcome.get("ambiguous") is not False:
            return (
                "unexpected_failure",
                "search refusal was not explicitly unambiguous",
            )
        if (
            search_verification is None
            or search_verification.before_sha256 is None
            or search_verification.after_sha256 is None
        ):
            return (
                "unexpected_failure",
                "search workspace stability could not be verified during refusal",
            )
        if search_verification.before_sha256 != search_verification.after_sha256:
            return "unexpected_failure", "workspace changed during the expected refusal"
        return "expected_refusal", None
    if before_hash != after_hash:
        return "unexpected_failure", "target changed during the expected refusal"
    return "expected_refusal", None


def _tool_outcome(
    events: list[dict[str, Any]], fixture: dict[str, object]
) -> dict[str, object] | None:
    if len(events) != 1:
        return None
    event = events[0]
    kind = event.get("type")
    if kind == "tool.completed" and type(event.get("success")) is bool:
        succeeded = event["success"]
    elif kind == "tool.error" and isinstance(event.get("error"), str):
        succeeded = False
    elif kind == "tool.denied" and isinstance(event.get("reason"), str):
        succeeded = False
    else:
        return None
    error = event.get("reason") if kind == "tool.denied" else event.get("error")
    output = event.get("output")
    fixture_outcome = fixture.get("tool_outcome")
    fixture_truncated = (
        fixture_outcome.get("truncated") if isinstance(fixture_outcome, dict) else None
    )
    return {
        "event_type": event.get("type"),
        "success": succeeded,
        "error": error if isinstance(error, str) else None,
        "output": output if isinstance(output, str) else None,
        "truncated": event.get("truncated"),
        "fixture_truncated": fixture_truncated,
        "dispatched": False if kind == "tool.denied" else event.get("dispatched"),
        "ambiguous": False if kind == "tool.denied" else event.get("ambiguous"),
        "fixture_result": fixture_outcome,
    }


def _parse_stream_events(raw: bytes) -> tuple[list[dict[str, Any]], list[int]]:
    events: list[dict[str, Any]] = []
    malformed: list[int] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line:
            continue
        try:
            payload = _strict_json_loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            malformed.append(number)
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            malformed.append(number)
            continue
        events.append(payload)
    return events, malformed


def _request_offers_tool(request: dict[str, Any], name: str) -> bool:
    tools = request.get("tools")
    if not isinstance(tools, list):
        return False
    return any(
        isinstance(tool, dict)
        and isinstance(tool.get("function"), dict)
        and tool["function"].get("name") == name
        for tool in tools
    )


def _offered_tool_parameters(request: dict[str, Any], name: str) -> object | None:
    tools = request.get("tools")
    if not isinstance(tools, list):
        return None
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name") == name:
            return function.get("parameters")
    return None


def _valid_search_text_schema(schema: object) -> bool:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    properties = schema.get("properties")
    if not isinstance(properties, dict) or set(properties) != {
        "pattern",
        "directory_path",
        "glob",
        "fixed_strings",
        "case_sensitive",
        "max_results",
    }:
        return False
    expected_types = {
        "pattern": "string",
        "directory_path": "string",
        "fixed_strings": "boolean",
        "case_sensitive": "boolean",
        "max_results": "integer",
    }
    for name, expected_type in expected_types.items():
        value = properties.get(name)
        if not isinstance(value, dict) or value.get("type") != expected_type:
            return False
    glob_schema = properties.get("glob")
    if not isinstance(glob_schema, dict):
        return False
    glob_types = glob_schema.get("type")
    glob_any_of = glob_schema.get("anyOf")
    if not (
        glob_types == "string"
        or glob_types == ["string", "null"]
        or glob_types == ["null", "string"]
        or (
            isinstance(glob_any_of, list)
            and {item.get("type") for item in glob_any_of if isinstance(item, dict)}
            == {"string", "null"}
        )
    ):
        return False
    required = schema.get("required", [])
    return isinstance(required, list) and set(required) == {"pattern"}


def _search_discovery_matches_offered_schema(
    discovery_output: object, request: dict[str, Any]
) -> bool:
    if not isinstance(discovery_output, str):
        return False
    try:
        payload = _strict_json_loads(discovery_output)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("tools"), list):
        return False
    discovered_schema = None
    for tool in payload["tools"]:
        if not isinstance(tool, dict) or tool.get("name") != "search_text":
            continue
        if tool.get("schema_truncated") is True:
            return False
        discovered_schema = tool.get("input_schema")
        break
    offered_schema = _offered_tool_parameters(request, "search_text")
    return (
        discovered_schema is not None
        and _valid_search_text_schema(discovered_schema)
        and discovered_schema == offered_schema
    )


def _extract_tool_outcome(
    request: dict[str, Any], call_id: str, tool_name: str = "write_file"
) -> dict[str, object] | None:
    messages = request.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        if message.get("tool_call_id") != call_id:
            continue
        content = message.get("content")
        content_limit = (
            MAX_SEARCH_OUTPUT_BYTES * 5 + 4 * 1024
            if tool_name == "search_text"
            else MAX_CONTENT_BYTES * 2
        )
        if not isinstance(content, str) or len(content.encode("utf-8")) > content_limit:
            return {"tool_call_id": call_id, "content": str(content)[:2048]}
        prefix = f'<tool_response name="{tool_name}" call_id="{call_id}">\n'
        suffix = "\n</tool_response>"
        wrapped = content.startswith(prefix) and content.endswith(suffix)
        body = content[len(prefix) : -len(suffix)] if wrapped else content
        if wrapped:
            # Ash XML-escapes the serialized JSON body. Decode one XML layer
            # only after isolating the exact tool response wrapper so ordinary
            # matched text such as "<tag> & value" remains JSON string data.
            body = xml_unescape(body)
        try:
            payload = _strict_json_loads(body)
        except (ValueError, json.JSONDecodeError):
            return {"tool_call_id": call_id, "content": content}
        if not isinstance(payload, dict):
            return {"tool_call_id": call_id, "content": content}
        return {
            "tool_call_id": call_id,
            "success": payload.get("success"),
            "output": payload.get("output"),
            "error": payload.get("error"),
            "truncated": payload.get("truncated"),
            "content": content,
        }
    return None


def _strict_json_loads(value: bytes | str) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ExplorerError(f"duplicate JSON field: {key}")
            result[key] = item
        return result

    def reject_constant(value: str) -> None:
        raise ExplorerError(f"invalid JSON constant: {value}")

    return json.loads(
        value,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )


def _bounded_string(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ExplorerError(f"{name} must be a string")
    if len(value.encode("utf-8")) > limit:
        raise ExplorerError(f"{name} exceeds {limit} UTF-8 bytes")
    if "\x00" in value:
        raise ExplorerError(f"{name} must not contain NUL")
    return value


def _baseline_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            timeout=2,
            text=True,
            env={
                "PATH": os.defpath,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if re.fullmatch(r"[0-9a-f]{40,64}", value) else None


def _ash_version() -> str:
    try:
        payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        value = payload["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return "unknown"
    return str(value) if isinstance(value, str) else "unknown"


def _runner_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    encoded = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _summary(run: RunState, reason: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "seed": run.seed,
        "baseline_revision": run.baseline_revision,
        "ash_version": run.ash_version,
        "runner_sha256": _runner_sha256(),
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "evidence_path": str(run.evidence),
        "scratch_path": str(run.scratch),
        "finish_reason": reason,
        "classifications": [asdict(item) for item in run.observations],
        "pending_cleanup": run.pending_cleanup,
        "lifecycle": run.lifecycle,
    }


def _read_live_line(stream: BinaryIO) -> bytes | None:
    line = stream.readline(MAX_ACTION_BYTES + 1)
    if not line:
        return None
    if len(line) > MAX_ACTION_BYTES and not line.endswith(b"\n"):
        raise ExplorerError(f"JSONL action exceeds {MAX_ACTION_BYTES} bytes")
    if len(line) > MAX_ACTION_BYTES:
        raise ExplorerError(f"JSONL action exceeds {MAX_ACTION_BYTES} bytes")
    return line


def _execute_step(
    run: RunState, request: StepRequest, raw_line: bytes
) -> tuple[StepObservation, bool]:
    try:
        return run.run_step(request, raw_line), False
    except KeyboardInterrupt as exc:
        return _record_driver_failure(run, request, raw_line, exc), True
    except ExplorerError:
        raise
    except Exception as exc:
        return _record_driver_failure(run, request, raw_line, exc), False


def _run_live(args: argparse.Namespace) -> int:
    run = _create_run(Path(args.evidence), args.seed)
    reason = "eof"
    exit_code = 0
    print(
        json.dumps(
            {
                "type": "started",
                "evidence_path": str(run.evidence),
                "seed": run.seed,
                "max_steps": MAX_STEPS,
                "scratch_path": str(run.scratch),
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    try:
        while True:
            raw_line = _read_live_line(sys.stdin.buffer)
            if raw_line is None:
                break
            action, request = _parse_action(raw_line)
            if action == "finish":
                run.append_accepted_line(raw_line)
                reason = "finish_action"
                break
            assert request is not None
            observation, interrupted = _execute_step(run, request, raw_line)
            if interrupted:
                reason = "interrupted"
                exit_code = 130
            print(
                json.dumps(
                    {"type": "observation", **asdict(observation)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                flush=True,
            )
            if interrupted:
                break
            if observation.classification == "driver_failure":
                reason = "driver_failure"
                exit_code = 2
                break
    except KeyboardInterrupt:
        reason = "interrupted"
        exit_code = 130
    except (ExplorerError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        reason = "input_error"
        exit_code = 2
        print(json.dumps({"type": "error", "error": str(exc)}), flush=True)
        _write_json(run.evidence / "input-error.json", {"error": str(exc)})
    finally:
        try:
            summary = run.close(reason=reason)
            print(
                json.dumps({"type": "finished", **summary}, separators=(",", ":")),
                flush=True,
            )
        except ExplorerError as exc:
            exit_code = 2
            print(json.dumps({"type": "error", "error": str(exc)}), flush=True)
    return exit_code


def _record_driver_failure(
    run: RunState, request: StepRequest, raw_line: bytes, exc: BaseException
) -> StepObservation:
    index = run.step_index + 1
    step_dir = run.evidence / "steps" / f"{index:02d}"
    step_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not run.action_accepted:
        run.append_accepted_line(raw_line)
        run.action_accepted = True
    if request.tool_name == "search_text":
        # A search targets the project tree, not one hashable file. Preserve an
        # unknown result without trying to hash the directory as a file.
        current_hash = None
        before_hash = None
    else:
        target = _target_path(run.scratch, run.project, request.file_path)
        current_hash = _hash_target(target)
    if request.tool_name != "search_text":
        try:
            before_payload = _strict_json_loads(
                (step_dir / "target-before.json").read_bytes()
            )
        except (OSError, ValueError, json.JSONDecodeError):
            before_payload = None
        before_hash = (
            before_payload.get("sha256")
            if isinstance(before_payload, dict)
            else current_hash
        )
    run.write_manifest(
        in_flight={
            "step_index": index,
            "label": request.label,
            "dispatch_pending": False,
            "outcome_unknown": True,
        }
    )
    detail = f"driver error: {type(exc).__name__}: {exc}\n"
    detail += "".join(traceback.format_exception(exc))[-MAX_TRACEBACK_CHARS:]
    observation = StepObservation(
        index=index,
        label=request.label,
        classification="driver_failure",
        process_status={
            "exit_code": None,
            "timed_out": False,
            "outcome_unknown": True,
        },
        tool_outcome=None,
        target_before_sha256=before_hash,
        target_after_sha256=current_hash,
        artifact_paths={"actions": "actions.jsonl"},
        detail=detail,
    )
    _write_json(step_dir / "observation.json", asdict(observation))
    run.step_index = index
    run.observations.append(observation)
    run.write_manifest()
    return observation


def _load_replay_steps(path: Path) -> list[tuple[bytes, StepRequest]]:
    if path.is_symlink() or not path.is_file():
        raise ExplorerError("steps path must be a regular file, not a symlink")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ExplorerError(f"cannot read steps file: {exc}") from exc
    if len(raw) > MAX_STEPS_FILE_BYTES:
        raise ExplorerError(f"steps file exceeds {MAX_STEPS_FILE_BYTES} bytes")
    chunks = raw.splitlines(keepends=True)
    if not chunks and raw:
        chunks = [raw]
    result: list[tuple[bytes, StepRequest]] = []
    for position, line in enumerate(chunks):
        if not line.strip():
            raise ExplorerError("blank lines are not accepted in steps file")
        action, request = _parse_action(line)
        if action == "finish" and position == len(chunks) - 1:
            break
        if action != "step" or request is None:
            raise ExplorerError("replay steps file may contain only step actions")
        result.append((line, request))
        if len(result) > MAX_STEPS:
            raise ExplorerError(f"steps file exceeds the {MAX_STEPS}-step run limit")
    return result


def _run_replay(args: argparse.Namespace) -> int:
    steps = _load_replay_steps(Path(args.steps).expanduser())
    run = _create_run(Path(args.evidence), args.seed)
    exit_code = 0
    reason = "replay_complete"
    print(
        json.dumps(
            {"type": "started", "evidence_path": str(run.evidence), "seed": run.seed},
            separators=(",", ":"),
        ),
        flush=True,
    )
    try:
        for raw_line, request in steps:
            observation, interrupted = _execute_step(run, request, raw_line)
            if interrupted:
                reason = "interrupted"
                exit_code = 130
            print(
                json.dumps(
                    {"type": "observation", **asdict(observation)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                flush=True,
            )
            if interrupted:
                break
            if observation.classification == "driver_failure":
                reason = "driver_failure"
                exit_code = 2
                break
    except KeyboardInterrupt:
        reason = "interrupted"
        exit_code = 130
    except Exception as exc:
        reason = "replay_error"
        exit_code = 2
        _write_json(
            run.evidence / "replay-error.json",
            {
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": "".join(traceback.format_exception(exc))[
                    -MAX_TRACEBACK_CHARS:
                ],
            },
        )
        print(json.dumps({"type": "error", "error": str(exc)}), flush=True)
    finally:
        try:
            summary = run.close(reason=reason)
            print(
                json.dumps({"type": "finished", **summary}, separators=(",", ":")),
                flush=True,
            )
        except ExplorerError as exc:
            exit_code = 2
            print(json.dumps({"type": "error", "error": str(exc)}), flush=True)
    return exit_code


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Exercise a real Ash CLI process in disposable state. The loopback model "
            "fixture is scripted; the caller chooses each next action."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser(
        "run", help="read adaptive step/finish actions from JSONL stdin"
    )
    run_parser.add_argument("--evidence", required=True, type=Path)
    run_parser.add_argument("--seed", required=True, type=int)
    replay_parser = subparsers.add_parser(
        "replay", help="replay step JSONL in a new disposable run"
    )
    replay_parser.add_argument("--steps", required=True, type=Path)
    replay_parser.add_argument("--evidence", required=True, type=Path)
    replay_parser.add_argument("--seed", required=True, type=int)
    return parser


def _handle_termination(signum: int, _frame: object) -> None:
    raise KeyboardInterrupt(f"received signal {signum}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.seed < 0 or args.seed > 2**32 - 1:
        parser.error("--seed must be in the inclusive range 0..4294967295")
    previous_sigterm = signal.signal(signal.SIGTERM, _handle_termination)
    try:
        try:
            if args.command == "run":
                return _run_live(args)
            return _run_replay(args)
        except KeyboardInterrupt:
            return 130
    except ExplorerError as exc:
        print(json.dumps({"type": "error", "error": str(exc)}), file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
