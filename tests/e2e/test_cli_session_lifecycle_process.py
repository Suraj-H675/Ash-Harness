"""Exercise durable session continuity through separate real CLI processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ash.core.session import SessionStore


def test_populated_session_continue_resume_and_fork_across_cli_processes(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, Any]] = []
    authorizations: list[str | None] = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path != "/v1/models":
                self.send_error(404)
                return
            self._json({"data": [{"id": "local-model", "max_model_len": 32768}]})

        def _json(self, payload: object) -> None:
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            request = json.loads(raw)
            requests.append(request)
            authorizations.append(self.headers.get("Authorization"))
            text = f"ANSWER-{len(requests)}"
            chunk = {
                "id": "local",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "local-model",
                "choices": [
                    {"index": 0, "delta": {"content": text}, "finish_reason": "stop"}
                ],
            }
            body = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    other_workspace = tmp_path / "other-workspace"
    for path in (workspace, home, other_workspace):
        path.mkdir()
    database = tmp_path / "db"
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
        "ASH_MODEL": "vllm/local-model",
        "VLLM_API_BASE": f"http://127.0.0.1:{server.server_port}/v1",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }

    def cli(*args: str, project: Path = workspace) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "ash",
                "--db-directory",
                str(database),
                *args,
            ],
            cwd=project,
            env=environment,
            text=True,
            capture_output=True,
            timeout=25,
            check=False,
        )

    def completed(*args: str) -> dict[str, Any]:
        result = cli(*args, "--output-format", "json")
        assert result.returncode == 0, (result.stdout, result.stderr)
        return json.loads(result.stdout)

    try:
        first = completed("-p", "first question")
        root_id = first["session_id"]
        assert first["response"] == "ANSWER-1"
        second = completed("--continue", "-p", "follow-up question")
        assert second["session_id"] == root_id
        assert second["response"] == "ANSWER-2"

        store = SessionStore(database / "sessions.db")
        root_before_fork = store.load_session(root_id)
        fork = completed("--resume", root_id, "--fork-session", "-p", "branch question")
        branch_id = fork["session_id"]
        assert branch_id != root_id
        assert fork["response"] == "ANSWER-3"
        assert store.load_session(root_id).messages == root_before_fork.messages

        fourth = completed("--resume", branch_id, "-p", "branch follow-up")
        assert fourth["session_id"] == branch_id
        assert fourth["response"] == "ANSWER-4"
        fifth = completed("--continue", "-p", "latest branch question")
        assert fifth["session_id"] == branch_id
        assert fifth["response"] == "ANSWER-5"
        assert store.load_session(root_id).messages == root_before_fork.messages
        assert len(store.load_session(branch_id).messages) > len(
            root_before_fork.messages
        )

        listing = cli("sessions", "list", "--json")
        assert listing.returncode == 0, listing.stderr
        assert {
            row["session_id"] for row in json.loads(listing.stdout)["sessions"]
        } == {root_id, branch_id}
        tree = cli("sessions", "tree", "--session", branch_id, "--json")
        assert tree.returncode == 0, tree.stderr
        lineage = json.loads(tree.stdout)["sessions"]
        assert [row["session_id"] for row in lineage] == [root_id, branch_id]
        assert lineage[0]["parent_session_id"] is None
        assert lineage[1]["parent_session_id"] == root_id
        assert lineage[1]["root_session_id"] == root_id
        assert lineage[1]["fork_message_count"] == len(root_before_fork.messages)

        search = cli("sessions", "search", "--query", "branch question", "--json")
        assert search.returncode == 0, search.stderr
        assert {
            match["session_id"] for match in json.loads(search.stdout)["matches"]
        } == {branch_id}

        foreign = cli(
            "--resume",
            branch_id,
            "-p",
            "not your project",
            "--output-format",
            "json",
            project=other_workspace,
        )
        assert foreign.returncode != 0
        assert json.loads(foreign.stdout)["type"] == "error"
        assert len(requests) == 5
        assert authorizations == [None] * 5
        assert any(
            message.get("content") == "follow-up question"
            for message in requests[2]["messages"]
        )
        assert any(
            message.get("content") == "branch question"
            for message in requests[3]["messages"]
        )
        assert any(
            message.get("content") == "branch follow-up"
            for message in requests[4]["messages"]
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
