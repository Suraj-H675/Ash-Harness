"""Exercise Ctrl-C cancellation in a real rich-mode approval chooser."""

from __future__ import annotations

import json
import os
import pty
import select
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ash.core.session import SessionStore


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_rich_approval_ctrl_c_cancels_turn_and_keeps_repl_usable(
    tmp_path: Path,
) -> None:
    import fcntl
    import termios

    workspace = tmp_path / "project"
    home = tmp_path / "home"
    db_directory = tmp_path / "db"
    for directory in (workspace, home, db_directory):
        directory.mkdir()

    requests: list[dict[str, object]] = []
    second_request = threading.Event()
    tool_request = (
        '<call_tool name="run_command">'
        '<arg name="command_line">touch denied.marker</arg>'
        '<arg name="timeout_seconds">15</arg>'
        "</call_tool>"
    )

    class LocalProvider(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def send_json(self, payload: object) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path != "/v1/models":
                self.send_error(404)
                return
            self.send_json(
                {
                    "object": "list",
                    "data": [{"id": "local-model", "max_model_len": 32768}],
                }
            )

        def do_POST(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            requests.append(json.loads(self.rfile.read(size)))
            if len(requests) == 1:
                text = tool_request
            else:
                second_request.set()
                text = "NEXT_TURN_OK"
            chunk = {
                "id": "local",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "local-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": text},
                        "finish_reason": "stop",
                    }
                ],
            }
            body = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalProvider)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    environment = {
        "PATH": os.defpath,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "NO_COLOR": "1",
        "TERM": "xterm-256color",
        "ASH_MODEL": "vllm/local-model",
        "VLLM_API_BASE": f"http://127.0.0.1:{server.server_port}/v1",
        "ASH_SCREEN_READER_MODE": "false",
    }
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    process: subprocess.Popen[bytes] | None = None
    slave_open = True
    captured = bytearray()

    def wait_for(marker: bytes, timeout: float = 15) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker in captured:
                return
            if process is not None and process.poll() is not None:
                break
            if select.select([master_fd], [], [], 0.05)[0]:
                try:
                    captured.extend(os.read(master_fd, 65_536))
                except OSError:
                    break
        raise AssertionError(
            f"missing {marker!r}; exit={process.poll() if process else None}; "
            f"output={captured[-3000:]!r}"
        )

    try:
        trust = subprocess.run(
            [sys.executable, "-m", "ash", "trust", "add", str(workspace)],
            cwd=workspace,
            env=environment,
            capture_output=True,
            timeout=12,
            check=False,
        )
        assert trust.returncode == 0, trust.stderr
        process = subprocess.Popen(
            [sys.executable, "-m", "ash", "--db-directory", str(db_directory)],
            cwd=workspace,
            env=environment,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_open = False

        wait_for(b"Ready", timeout=25)
        os.write(master_fd, b"please run the fixture command\r")
        wait_for(b"Allow once", timeout=25)
        os.write(master_fd, b"\x03")
        assert not second_request.wait(timeout=2), (
            "the provider received another request after Ctrl-C in approval"
        )
        wait_for(b"Turn cancelled.", timeout=15)

        assert len(requests) == 1
        assert not (workspace / "denied.marker").exists()

        os.write(master_fd, b"/recovery\r")
        wait_for(b"Recovery 1:", timeout=10)
        wait_for(b"interrupted", timeout=5)
        assert len(requests) == 1

        os.write(master_fd, b"continue safely\r")
        wait_for(b"NEXT_TURN_OK", timeout=20)
        assert second_request.is_set()
        assert len(requests) == 2
        os.write(master_fd, b"/exit\r")
        process.wait(timeout=15)
        assert process.returncode == 0

        store = SessionStore(db_directory / "sessions.db")
        sessions = store.list_sessions(project_path=str(workspace), limit=5)
        assert len(sessions) == 1
        session_id = sessions[0].session_id
        calls = [
            call
            for call in store.load_session(session_id).tool_calls
            if call.tool_name == "run_command"
        ]
        assert len(calls) == 1
        assert calls[0].approved is False
        assert calls[0].dispatched is False
        reports = store.interrupted_recovery_reports(session_id)
        assert len(reports) == 1
        assert reports[0]["status"] == "interrupted"
        assert reports[0]["unknown_calls"] == []
        print(
            "provider_requests=2 (one cancelled, one fresh turn); "
            "cancelled_turn_recovered=True; dispatched=False; "
            "denied_effect_absent=True; next_turn_completed=True"
        )
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if slave_open:
            os.close(slave_fd)
        os.close(master_fd)
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
