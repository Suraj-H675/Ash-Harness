from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


_DYNAMIC_CATALOG_PROVIDERS = {
    "google",
    "openrouter",
    "mistral",
    "xai",
    "together",
    "fireworks",
    "cerebras",
    "nvidia",
}


@pytest.mark.parametrize(
    ("provider", "key_env", "base_env"),
    [
        ("openai", "OPENAI_API_KEY", "OPENAI_API_BASE"),
        ("google", "GOOGLE_API_KEY", "GOOGLE_API_BASE"),
        ("openrouter", "OPENROUTER_API_KEY", "OPENROUTER_API_BASE"),
        ("deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE"),
        ("groq", "GROQ_API_KEY", "GROQ_API_BASE"),
        ("mistral", "MISTRAL_API_KEY", "MISTRAL_API_BASE"),
        ("xai", "XAI_API_KEY", "XAI_API_BASE"),
        ("together", "TOGETHER_API_KEY", "TOGETHER_API_BASE"),
        ("fireworks", "FIREWORKS_API_KEY", "FIREWORKS_API_BASE"),
        ("cerebras", "CEREBRAS_API_KEY", "CEREBRAS_API_BASE"),
        ("nvidia", "NVIDIA_API_KEY", "NVIDIA_API_BASE"),
    ],
)
def test_openai_wire_builtin_runs_through_fresh_cli_process(
    tmp_path: Path,
    provider: str,
    key_env: str,
    base_env: str,
) -> None:
    requests: list[tuple[str, str, str | None, str | None]] = []
    expected_auth = f"Bearer test-key-{provider}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _json(self, payload: object) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            authorization = self.headers.get("Authorization")
            requests.append(
                (
                    "GET",
                    self.path,
                    authorization,
                    self.headers.get("x-goog-api-client"),
                )
            )
            if self.path != "/v1/models":
                self.send_response(404)
                self.end_headers()
                return
            model = {
                "id": "test-model",
                "supported_parameters": [],
                "context_length": 32768,
            }
            if provider == "together":
                self._json([model])
            else:
                self._json({"object": "list", "data": [model]})

        def do_POST(self) -> None:
            authorization = self.headers.get("Authorization")
            requests.append(
                (
                    "POST",
                    self.path,
                    authorization,
                    self.headers.get("x-goog-api-client"),
                )
            )
            if self.path != "/v1/chat/completions":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length:
                self.rfile.read(length)
            chunk = {
                "id": "cloud",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "CLOUD-ROUTE-OK"},
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
                "ASH_MODEL": f"{provider}/test-model",
                key_env: f"test-key-{provider}",
                base_env: f"http://127.0.0.1:{port}/v1",
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
                "Reply exactly CLOUD-ROUTE-OK.",
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
        assert payload["response"] == "CLOUD-ROUTE-OK"
        assert payload["model"] == f"{provider}/test-model"
        assert (
            "POST",
            "/v1/chat/completions",
            expected_auth,
        ) in {(method, path, auth) for method, path, auth, _ in requests}
        if provider in _DYNAMIC_CATALOG_PROVIDERS:
            assert (
                "GET",
                "/v1/models",
                expected_auth,
            ) in {(method, path, auth) for method, path, auth, _ in requests}
        if provider == "google":
            google_requests = [
                client_header
                for _, _, auth, client_header in requests
                if auth == expected_auth
            ]
            assert google_requests
            assert all(google_requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
