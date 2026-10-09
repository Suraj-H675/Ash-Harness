"""Exercise recovery after the real Ash CLI dies between provider requests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ash.core.session import SessionStore


def test_cli_restart_preserves_completed_write_without_replay(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    home = tmp_path / "home"
    db_dir = tmp_path / "db"
    for path in (workspace, home, db_dir):
        path.mkdir()

    second_request = threading.Event()
    release_second = threading.Event()
    requests: list[dict[str, object]] = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path != "/v1/models":
                self.send_error(404)
                return
            self.send_json({"object": "list", "data": [{"id": "local-model", "max_model_len": 32768}]})

        def send_json(self, payload: object) -> None:
            content = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            size = int(self.headers.get("Content-Length", "0"))
            requests.append(json.loads(self.rfile.read(size)))
            number = len(requests)
            if number == 2:
                second_request.set()
                release_second.wait(timeout=25)
                return
            if number == 1:
                text = (
                    '<call_tool name="write_file">'
                    '<arg name="file_path">kept.txt</arg>'
                    '<arg name="content">written-before-crash\n</arg>'
                    '<arg name="overwrite">false</arg>'
                    "</call_tool>"
                )
            else:
                text = "RESTART-OK"
            chunk = {
                "id": "local", "object": "chat.completion.chunk", "created": 1,
                "model": "local-model",
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}],
            }
            payload = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
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
        "GIT_TERMINAL_PROMPT": "0",
        "NO_COLOR": "1",
        "ASH_MODEL": "vllm/local-model",
        "VLLM_API_BASE": f"http://127.0.0.1:{server.server_port}/v1",
    }

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "ash", "--db-directory", str(db_dir), *args],
            cwd=workspace, env=environment, capture_output=True, text=True,
            timeout=15, check=False,
        )

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=workspace, env=environment,
            capture_output=True, text=True, timeout=8, check=True,
        ).stdout.strip()

    process: subprocess.Popen[str] | None = None
    try:
        git("init", "-q")
        git("config", "user.name", "Ash Test")
        git("config", "user.email", "ash@test")
        (workspace / "README.md").write_text("# fixture\n")
        git("add", "README.md")
        git("commit", "-qm", "seed")
        trust = cli("trust", "add", str(workspace))
        assert trust.returncode == 0, trust.stderr

        process = subprocess.Popen(
            [
                sys.executable, "-m", "ash", "--db-directory", str(db_dir),
                "--mode", "auto_edit", "-p", "create kept.txt",
                "--output-format", "json",
            ],
            cwd=workspace, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert second_request.wait(timeout=15), (
            f"CLI never reached second provider request; exit={process.poll()}"
        )
        assert (workspace / "kept.txt").read_bytes() == b"written-before-crash\n"

        # Model an abrupt process death, not the graceful SIGTERM shutdown.
        process.kill()
        process.communicate(timeout=5)
        assert process.returncode is not None and process.returncode != 0
        release_second.set()

        store = SessionStore(db_dir / "sessions.db")
        sessions = store.list_sessions(project_path=str(workspace), limit=5)
        assert len(sessions) == 1
        session_id = sessions[0].session_id
        assert len([call for call in store.load_session(session_id).tool_calls if call.tool_name == "write_file"]) == 1
        assert len(store.started_turns(session_id)) == 1

        resumed = cli(
            "--session", session_id, "--mode", "auto_edit", "-p", "resume safely",
            "--output-format", "json",
        )
        assert resumed.returncode == 0, resumed.stderr
        assert json.loads(resumed.stdout)["response"] == "RESTART-OK"

        recovery = cli("sessions", "recovery", "--session", session_id, "--json")
        assert recovery.returncode == 0, recovery.stderr
        reports = json.loads(recovery.stdout)["reports"]
        assert len(reports) == 1
        assert reports[0]["status"] == "interrupted"
        assert reports[0]["unknown_calls"] == []
        assert reports[0]["unresolved_files"] == []

        audit = cli("audit", "verify", "--session", session_id, "--json")
        assert audit.returncode == 0, audit.stderr
        assert json.loads(audit.stdout)["ok"] is True
        write_calls = [
            call for call in SessionStore(db_dir / "sessions.db").load_session(session_id).tool_calls
            if call.tool_name == "write_file"
        ]
        assert len(write_calls) == 1
        assert write_calls[0].executed is True and write_calls[0].error is None
        assert len(requests) == 3
        assert (workspace / "kept.txt").read_bytes() == b"written-before-crash\n"
        assert git("status", "--short", "--untracked-files=all") == "?? kept.txt"
        assert len(git("log", "--oneline").splitlines()) == 1
    finally:
        release_second.set()
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
