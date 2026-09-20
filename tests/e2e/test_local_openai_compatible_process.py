from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("provider", "base_env", "catalog_path"),
    [
        ("lmstudio", "LMSTUDIO_API_BASE", "/api/v1/models"),
        ("vllm", "VLLM_API_BASE", "/v1/models"),
    ],
)
def test_local_openai_compatible_route_runs_through_fresh_cli_process(
    tmp_path: Path,
    provider: str,
    base_env: str,
    catalog_path: str,
) -> None:
    seen: list[tuple[str, str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _json(self, payload: object) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            seen.append(("GET", self.path, self.headers.get("Authorization")))
            if self.path == "/api/v1/models":
                self._json(
                    {
                        "models": [
                            {
                                "key": "local-model",
                                "type": "llm",
                                "loaded_instances": [
                                    {"config": {"context_length": 32768}}
                                ],
                            }
                        ]
                    }
                )
                return
            if self.path == "/v1/models":
                self._json(
                    {
                        "object": "list",
                        "data": [{"id": "local-model", "max_model_len": 32768}],
                    }
                )
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            seen.append(("POST", self.path, self.headers.get("Authorization")))
            if self.path != "/v1/chat/completions":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length:
                self.rfile.read(length)
            chunk = {
                "id": "local",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "local-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "LOCAL-ROUTE-OK"},
                        "finish_reason": "stop",
                    }
                ],
            }
            body = (
                "data: "
                + json.dumps(chunk, separators=(",", ":"))
                + "\n\ndata: [DONE]\n\n"
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
        port = int(server.server_address[1])
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "ASH_MODEL": f"{provider}/local-model",
                base_env: f"http://127.0.0.1:{port}/v1",
                "NO_COLOR": "1",
            }
        )
        for other in {"LMSTUDIO_API_BASE", "VLLM_API_BASE"} - {base_env}:
            environment.pop(other, None)

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
                "Reply exactly LOCAL-ROUTE-OK.",
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
        assert payload["response"] == "LOCAL-ROUTE-OK"
        assert payload["model"] == f"{provider}/local-model"
        assert ("GET", catalog_path, None) in seen
        assert ("POST", "/v1/chat/completions", None) in seen
        assert all(authorization is None for _, _, authorization in seen)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
