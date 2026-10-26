"""Signals to the real CLI must settle running commands before exiting."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ash.core.session import SessionStore


@pytest.mark.skipif(os.name == "nt", reason="POSIX process signal contract")
def test_cli_sigterm_cleans_running_command_and_records_unknown_outcome(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    home = tmp_path / "home"
    db_directory = tmp_path / "db"
    for directory in (workspace, home, db_directory):
        directory.mkdir()

    # A child that survives Ash's shutdown would leave a delayed marker.
    # Ignore the soft signal so process-tree cleanup must finish its escalation.
    command = (
        "trap '' TERM; printf running > started.marker; "
        "sleep 3; printf survived > survived.marker"
    )
    tool_request = (
        '<call_tool name="run_command">'
        f'<arg name="command_line">{command}</arg>'
        '<arg name="timeout_seconds">15</arg>'
        "</call_tool>"
    )
    provider_requests = 0

    class LocalProvider(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            data = json.dumps(
                {"object": "list", "data": [{"id": "local-model", "max_model_len": 32768}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            nonlocal provider_requests
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            provider_requests += 1
            response = tool_request if provider_requests == 1 else "RESUMED"
            chunk = {
                "id": "test", "object": "chat.completion.chunk", "created": 1,
                "model": "local-model",
                "choices": [{"index": 0, "delta": {"content": response}, "finish_reason": "stop"}],
            }
            data = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalProvider)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    environment = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(tmp_path),
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "NO_COLOR": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "ASH_MODEL": "vllm/local-model",
        "VLLM_API_BASE": f"http://127.0.0.1:{server.server_port}/v1",
        # The synthetic provider has one fixed tool action in disposable state.
        "ASH_ALLOW_UNSAFE_AUTO_APPROVE": "true",
    }
    process: subprocess.Popen[str] | None = None
    try:
        trust = subprocess.run(
            [sys.executable, "-m", "ash", "trust", "add", str(workspace)],
            cwd=workspace, env=environment, text=True, capture_output=True,
            timeout=10, check=False,
        )
        assert trust.returncode == 0, trust.stderr

        process = subprocess.Popen(
            [
                sys.executable, "-m", "ash", "--db-directory", str(db_directory),
                "--mode", "auto_approve", "-p", "run the fixture command",
                "--output-format", "json",
            ],
            cwd=workspace, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        started = workspace / "started.marker"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not started.exists() and process.poll() is None:
            time.sleep(0.05)
        if not started.exists():
            detail = "CLI did not start the fixture command before its deadline"
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=2)
                detail = f"CLI exited {process.returncode}: {stdout[-500:]} {stderr[-500:]}"
            pytest.fail(detail)
        assert started.read_text() == "running"

        process.terminate()
        _, stderr = process.communicate(timeout=7)
        assert process.returncode == 143, stderr
        time.sleep(3.3)
        assert not (workspace / "survived.marker").exists()

        store = SessionStore(db_directory / "sessions.db")
        sessions = store.list_sessions(project_path=str(workspace), limit=5)
        assert len(sessions) == 1
        session_id = sessions[0].session_id
        calls = [
            call for call in store.load_session(session_id).tool_calls
            if call.tool_name == "run_command"
        ]
        assert len(calls) == 1
        assert calls[0].dispatched is True
        assert store.started_turns(session_id) == []
        reports = store.interrupted_recovery_reports(session_id)
        assert len(reports) == 1
        assert reports[0]["status"] == "needs_attention"
        assert len(reports[0]["unknown_calls"]) == 1
        assert reports[0]["unresolved_files"] == []
        assert store.verify_audit_log(session_id) == []

        resumed = subprocess.run(
            [
                sys.executable, "-m", "ash", "--db-directory", str(db_directory),
                "--session", session_id, "--mode", "auto_edit", "-p", "resume safely",
                "--output-format", "json",
            ],
            cwd=workspace, env=environment, text=True, capture_output=True,
            timeout=15, check=False,
        )
        assert resumed.returncode == 0, resumed.stderr
        result = json.loads(resumed.stdout)
        assert result["response"] == "RESUMED"
        assert result["recovery_attention"] == {
            "needs_attention": True,
            "interrupted_turns": 1,
            "unknown_calls": 1,
            "unresolved_files": 0,
        }

        streamed = subprocess.run(
            [
                sys.executable, "-m", "ash", "--db-directory", str(db_directory),
                "--session", session_id, "--mode", "auto_edit", "-p", "resume safely",
                "--output-format", "stream-json",
            ],
            cwd=workspace, env=environment, text=True, capture_output=True,
            timeout=15, check=False,
        )
        assert streamed.returncode == 0, streamed.stderr
        events = [json.loads(line) for line in streamed.stdout.splitlines()]
        assert any(
            event["type"] == "session.recovery.attention"
            and event["unknown_calls"] == 1
            for event in events
        )
        textual = subprocess.run(
            [
                sys.executable, "-m", "ash", "--db-directory", str(db_directory),
                "--session", session_id, "--mode", "auto_edit", "-p", "resume safely",
            ],
            cwd=workspace, env=environment, text=True, capture_output=True,
            timeout=15, check=False,
        )
        assert textual.returncode == 0, textual.stderr
        assert "RESUMED" in textual.stdout
        assert "1 unknown tool outcome(s)" in textual.stderr
        assert provider_requests == 4  # Never replay the interrupted command.
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.skipif(os.name == "nt", reason="POSIX terminal signal contract")
@pytest.mark.parametrize(
    ("interrupt_signal", "expected_exit_code"),
    [(signal.SIGINT, 130), (signal.SIGTERM, 143)],
)
def test_interactive_cli_signal_cleans_command_and_records_unknown_outcome(
    tmp_path: Path,
    interrupt_signal: signal.Signals,
    expected_exit_code: int,
) -> None:
    import fcntl
    import pty
    import struct
    import termios

    workspace = tmp_path / "project"
    home = tmp_path / "home"
    db_directory = tmp_path / "db"
    for directory in (workspace, home, db_directory):
        directory.mkdir()

    command = (
        "trap '' TERM; printf running > started.marker; "
        "sleep 3; printf survived > survived.marker"
    )
    environment = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(tmp_path),
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "NO_COLOR": "1",
        "TERM": "dumb",
        "ASH_SCREEN_READER_MODE": "true",
        "ASH_WORKSPACE_ROOT": str(workspace),
        "ASH_MODEL": "openai/gpt-4o",
        "OPENAI_API_KEY": "ash-test-placeholder",
        "ASH_ALLOW_UNSAFE_AUTO_APPROVE": "true",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    trust = subprocess.run(
        [sys.executable, "-m", "ash", "trust", "add", str(workspace)],
        cwd=workspace,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert trust.returncode == 0, trust.stderr

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ash",
            "--db-directory",
            str(db_directory),
            "--mode",
            "auto_approve",
        ],
        cwd=workspace,
        env=environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    captured = bytearray()
    try:
        deadline = time.monotonic() + 15
        while b"> " not in captured and process.poll() is None:
            if time.monotonic() >= deadline:
                pytest.fail(f"interactive CLI did not show a prompt: {bytes(captured[-1000:])!r}")
            if select.select([master], [], [], 0.05)[0]:
                try:
                    captured.extend(os.read(master, 65_536))
                except OSError:
                    break
        assert b"> " in captured, (process.poll(), bytes(captured[-1000:]))
        os.write(master, f"! {command}\r".encode())

        started = workspace / "started.marker"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not started.exists() and process.poll() is None:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    captured.extend(os.read(master, 65_536))
                except OSError:
                    break
        assert started.exists(), (process.poll(), bytes(captured[-1000:]))

        process.send_signal(interrupt_signal)
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            while select.select([master], [], [], 0)[0]:
                try:
                    captured.extend(os.read(master, 65_536))
                except OSError:
                    break
            pytest.fail(
                "interactive CLI did not finish cancellation: "
                f"signal={interrupt_signal.name}, output={bytes(captured[-10000:])!r}"
            )
        time.sleep(3.3)
        store = SessionStore(db_directory / "sessions.db")
        sessions = store.list_sessions(project_path=str(workspace), limit=5)
        assert len(sessions) == 1
        session_id = sessions[0].session_id
        calls = [
            call for call in store.load_session(session_id).tool_calls
            if call.tool_name == "run_command"
        ]
        reports = store.interrupted_recovery_reports(session_id)
        survived = (workspace / "survived.marker").exists()
        assert (
            process.returncode == expected_exit_code
            and not survived
            and len(calls) == 1
            and calls[0].dispatched
            and len(reports) == 1
            and len(reports[0]["unknown_calls"]) == 1
        ), (
            f"returncode={process.returncode}, survived={survived}, "
            f"calls={[(call.dispatched, call.executed, call.error) for call in calls]}, "
            f"reports={reports}, output={bytes(captured[-1000:])!r}"
        )
        assert len(calls) == 1
        assert calls[0].dispatched is True
        assert len(reports) == 1
        assert len(reports[0]["unknown_calls"]) == 1
        assert store.verify_audit_log(session_id) == []

        recovery = subprocess.run(
            [
                sys.executable,
                "-m",
                "ash",
                "--db-directory",
                str(db_directory),
                "sessions",
                "recovery",
                "--session",
                session_id,
                "--json",
            ],
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert recovery.returncode == 0, recovery.stderr
        reports_from_cli = json.loads(recovery.stdout)["reports"]
        assert len(reports_from_cli) == 1
        assert len(reports_from_cli[0]["unknown_calls"]) == 1
        calls_after_restart = [
            call
            for call in store.load_session(session_id).tool_calls
            if call.tool_name == "run_command"
        ]
        assert len(calls_after_restart) == 1
        assert store.verify_audit_log(session_id) == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master)
