from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def test_anthropic_native_route_runs_through_fresh_cli_process(tmp_path: Path) -> None:
    requests: list[tuple[str, str | None, str | None, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length) if length else b""
            requests.append(
                (
                    self.path,
                    self.headers.get("x-api-key"),
                    self.headers.get("anthropic-version"),
                    body,
                )
            )
            if self.path != "/v1/messages":
                self.send_response(404)
                self.end_headers()
                return

            events = [
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg_test",
                            "type": "message",
                            "role": "assistant",
                            "model": "claude-test",
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 3, "output_tokens": 0},
                        },
                    },
                ),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {
                            "type": "text_delta",
                            "text": "ANTHROPIC-ROUTE-OK",
                        },
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 4},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
            payload = "".join(
                f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
                for name, data in events
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        workspace = tmp_path / "workspace"
        home = tmp_path / "home"
        workspace.mkdir()
        home.mkdir()
        port = int(server.server_address[1])
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "ASH_MODEL": "anthropic/claude-test",
                "ANTHROPIC_API_KEY": "test-anthropic-key",
                "ANTHROPIC_API_BASE": f"http://127.0.0.1:{port}",
                "NO_COLOR": "1",
            }
        )

        trust = subprocess.run(
            [sys.executable, "-m", "ash", "trust", "add", str(workspace)],
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        assert trust.returncode == 0, trust.stderr

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ash",
                "--db-directory",
                str(tmp_path / "db"),
                "-p",
                "Reply exactly ANTHROPIC-ROUTE-OK.",
                "--output-format",
                "json",
            ],
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["response"] == "ANTHROPIC-ROUTE-OK"
        assert payload["model"] == "anthropic/claude-test"
        assert payload["usage"]["prompt_tokens"] == 3
        assert payload["usage"]["completion_tokens"] == 4
        assert payload["usage"]["usage_source"] == "provider"

        assert len(requests) == 1
        path, api_key, protocol_version, raw_body = requests[0]
        assert path == "/v1/messages"
        assert api_key == "test-anthropic-key"
        assert protocol_version == "2023-06-01"
        request_payload = json.loads(raw_body)
        assert request_payload["model"] == "claude-test"
        assert request_payload["stream"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
