"""SIGTERM of the real CLI must settle a running command before exiting."""

from __future__ import annotations

import json
import os
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
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            chunk = {
                "id": "test", "object": "chat.completion.chunk", "created": 1,
                "model": "local-model",
                "choices": [{"index": 0, "delta": {"content": tool_request}, "finish_reason": "stop"}],
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
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
