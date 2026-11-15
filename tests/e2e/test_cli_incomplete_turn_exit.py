"""One-shot CLI exit status reflects an unfinished turn, not just a clean shutdown."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ash.core.session import SessionStore


@pytest.mark.parametrize(
    ("output_format", "tool_turn", "override", "finish_on_request"),
    [
        ("json", False, None, 1),
        ("stream-json", False, None, 1),
        ("text", False, None, 1),
        ("json", True, None, None),
        ("stream-json", True, None, None),
        ("text", True, None, None),
        ("json", True, 2, 2),
        ("stream-json", True, 2, 2),
        ("text", True, 2, 2),
        ("stream-json", True, None, 12),
        ("stream-json", True, 12, 12),
    ],
)
def test_one_shot_cli_respects_iteration_limit_and_explicit_override(
    tmp_path: Path,
    output_format: str,
    tool_turn: bool,
    override: int | None,
    finish_on_request: int | None,
) -> None:
    requests: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:
            if self.path != "/api/v1/models":
                self.send_error(404)
                return
            self.send_json(
                {
                    "models": [
                        {
                            "key": "local-model",
                            "type": "llm",
                            "loaded_instances": [
                                {"config": {"context_length": 32768}}
                            ],
                            "capabilities": {"trained_for_tool_use": True},
                        }
                    ]
                }
            )

        def send_json(self, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            request = json.loads(
                self.rfile.read(int(self.headers["Content-Length"]))
            )
            requests.append(request)
            if tool_turn and (
                finish_on_request is None or len(requests) < finish_on_request
            ):
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"read-call-{len(requests)}",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"file_path": "README.md"}),
                            },
                        }
                    ]
                }
                stop = "tool_calls"
            else:
                delta = {"content": "TASK-ACK"}
                stop = "stop"
            chunks = [
                {
                    "id": "exit-check",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "local-model",
                    "choices": [
                        {"index": 0, "delta": delta, "finish_reason": None}
                    ],
                },
                {
                    "id": "exit-check",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "local-model",
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": stop}
                    ],
                },
            ]
            body = (
                "".join("data: " + json.dumps(item) + "\n\n" for item in chunks)
                + "data: [DONE]\n\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        workspace = tmp_path / "workspace"
        home = tmp_path / "home"
        workspace.mkdir()
        home.mkdir()
        (workspace / "README.md").write_text("# Disposable workspace\n")
        configured_limit = 10 if finish_on_request == 12 else 1
        environment = {
            "PATH": os.defpath,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "ASH_MODEL": "lmstudio/local-model",
            "LMSTUDIO_API_BASE": f"http://127.0.0.1:{server.server_port}/v1",
            "ASH_MAX_TURN_ITERATIONS": str(configured_limit),
            "NO_COLOR": "1",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "NO_PROXY": "127.0.0.1,localhost",
        }
        trust = subprocess.run(
            [sys.executable, "-m", "ash", "trust", "add", str(workspace)],
            cwd=workspace, env=environment, capture_output=True, text=True,
            timeout=15, check=False,
        )
        assert trust.returncode == 0, trust.stderr
        arguments = [
            sys.executable, "-m", "ash",
            "--db-directory", str(tmp_path / "db"),
            "--mode", "auto_edit",
            "-p", "Read README.md and complete the task.",
            "--output-format", output_format,
        ]
        if override is not None:
            arguments.extend(("--max-turn-iterations", str(override)))
        result = subprocess.run(
            arguments,
            cwd=workspace, env=environment, capture_output=True, text=True,
            timeout=25, check=False,
        )
        effective_limit = override or configured_limit
        incomplete = tool_turn and (
            finish_on_request is None or finish_on_request > effective_limit
        )
        assert result.returncode == (1 if incomplete else 0), (
            result.stdout[-800:], result.stderr[-800:]
        )
        expected_requests = (
            min(finish_on_request or effective_limit, effective_limit)
            if tool_turn else 1
        )
        assert len(requests) == expected_requests
        if output_format == "text":
            assert ("max iterations" in result.stdout) is incomplete
        else:
            events = [json.loads(line) for line in result.stdout.splitlines()]
            completions = [
                event for event in events if event["type"] == "turn.completed"
            ]
            assert len(completions) == 1
            assert completions[0]["stop_reason"] == (
                "iteration_limit" if incomplete else "completed"
            )
            assert completions[0]["iteration_limit"] == effective_limit
        store = SessionStore(tmp_path / "db" / "sessions.db")
        sessions = store.list_sessions(project_path=str(workspace))
        assert len(sessions) == 1
        assert len(store.load_session(sessions[0].session_id).tool_calls) == (
            expected_requests if incomplete else expected_requests - 1
        )
        assert store.verify_audit_log(sessions[0].session_id) == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("args", "diagnostic"),
    [
        (["-p", "fix this", "--max-turn-iterations", "0"], "between 1 and 100"),
        (["-p", "fix this", "--max-turn-iterations", "101"], "between 1 and 100"),
        (["-p", "fix this", "--max-turn-iterations", "abc"], "invalid int value"),
        (["--max-turn-iterations", "12"], "requires a one-shot prompt"),
        (["--max-turn-iterations", "12", "trust", "status"],
         "requires a one-shot prompt"),
    ],
)
def test_iteration_override_rejects_invalid_or_non_one_shot_uses(
    tmp_path: Path, args: list[str], diagnostic: str,
) -> None:
    environment = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
    }
    result = subprocess.run(
        [sys.executable, "-m", "ash", *args],
        cwd=tmp_path, env=environment, input="", capture_output=True, text=True,
        timeout=15, check=False,
    )
    assert result.returncode == 2
    assert diagnostic in result.stderr
    assert not (tmp_path / ".ash").exists()
