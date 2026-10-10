from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def _route_loopback_directly(environment: dict[str, str]) -> None:
    """Keep host proxy settings from intercepting the fake local provider."""

    for variable in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        environment.pop(variable, None)
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    environment["no_proxy"] = "127.0.0.1,localhost"


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
    request_bodies: list[dict[str, object]] = []

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
                                "max_output_tokens": 64,
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
                        "data": [{
                            "id": "local-model",
                            "max_model_len": 32768,
                            "max_output_tokens": 64,
                        }],
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
                request_bodies.append(json.loads(self.rfile.read(length)))
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
        _route_loopback_directly(environment)
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
        assert request_bodies[0]["max_tokens"] == 64
        assert all(authorization is None for _, _, authorization in seen)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("provider", "base_env", "native_tools"),
    [
        ("lmstudio", "LMSTUDIO_API_BASE", True),
        ("vllm", "VLLM_API_BASE", False),
    ],
)
def test_local_runtime_fresh_process_completes_file_edit_tool_turn(
    tmp_path: Path,
    provider: str,
    base_env: str,
    native_tools: bool,
) -> None:
    requests: list[dict[str, object]] = []

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

        def _stream(self, chunks: list[dict[str, object]]) -> None:
            body = "".join(
                "data: " + json.dumps(chunk, separators=(",", ":")) + "\n\n"
                for chunk in chunks
            )
            body += "data: [DONE]\n\n"
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
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
                                "capabilities": {
                                    "trained_for_tool_use": True,
                                    "vision": False,
                                },
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
            if self.path != "/v1/chat/completions":
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            payload = json.loads(self.rfile.read(length) if length else b"{}")
            requests.append(payload)
            call_number = len(requests)
            if call_number == 1:
                if native_tools:
                    assert payload.get("tools")
                    tool_delta = {
                        "id": "local-tool",
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
                                            "id": "call-local-write",
                                            "type": "function",
                                            "function": {
                                                "name": "write_file",
                                                "arguments": (
                                                    '{"file_path":"local_agent.txt",'
                                                    '"content":"LOCAL_AGENT_OK\\n",'
                                                    '"overwrite":false}'
                                                ),
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                    terminal = {
                        "id": "local-tool",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "local-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "tool_calls",
                            }
                        ],
                    }
                    self._stream([tool_delta, terminal])
                    return
                assert not payload.get("tools")
                self._stream(
                    [
                        {
                            "id": "local-tool",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "local-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "content": (
                                            '<call_tool name="write_file">'
                                            '<arg name="file_path">local_agent.txt</arg>'
                                            '<arg name="content">LOCAL_AGENT_OK\n</arg>'
                                            '<arg name="overwrite">false</arg>'
                                            "</call_tool>"
                                        )
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                    ]
                )
                return
            assert call_number == 2
            if native_tools:
                assert any(
                    message.get("role") == "tool"
                    and message.get("tool_call_id") == "call-local-write"
                    for message in payload.get("messages", [])
                )
            else:
                assert not payload.get("tools")
            self._stream(
                [
                    {
                        "id": "local-final",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "local-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "LOCAL-CODING-OK"},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ]
            )

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
        _route_loopback_directly(environment)
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
                "--mode",
                "auto_edit",
                "-p",
                "Create local_agent.txt containing exactly LOCAL_AGENT_OK and finish.",
                "--output-format",
                "json",
            ],
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            timeout=25,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["response"] == "LOCAL-CODING-OK"
        assert payload["model"] == f"{provider}/local-model"
        assert (workspace / "local_agent.txt").read_text(encoding="utf-8") == (
            "LOCAL_AGENT_OK\n"
        )
        assert len(requests) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_ollama_fresh_process_completes_native_file_edit_tool_turn(
    tmp_path: Path,
) -> None:
    chat_requests: list[dict[str, object]] = []
    show_requests = 0

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

        def _ndjson(self, payload: dict[str, object]) -> None:
            body = (json.dumps(payload, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            assert self.headers.get("Authorization") is None
            if self.path == "/api/tags":
                self._json({"models": [{"name": "local-model"}]})
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            nonlocal show_requests
            assert self.headers.get("Authorization") is None
            length = int(self.headers.get("Content-Length", "0") or 0)
            payload = json.loads(self.rfile.read(length) if length else b"{}")
            if self.path == "/api/show":
                show_requests += 1
                assert payload == {"model": "local-model"}
                self._json(
                    {
                        "capabilities": ["completion", "tools"],
                        "model_info": {"general.context_length": 32768},
                    }
                )
                return
            if self.path != "/api/chat":
                self.send_response(404)
                self.end_headers()
                return
            chat_requests.append(payload)
            call_number = len(chat_requests)
            if call_number == 1:
                assert payload.get("tools")
                self._ndjson(
                    {
                        "model": "local-model",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-local-write",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": {
                                            "file_path": "local_agent.txt",
                                            "content": "LOCAL_AGENT_OK\n",
                                            "overwrite": False,
                                        },
                                    },
                                }
                            ],
                        },
                        "done": True,
                        "done_reason": "tool_calls",
                        "prompt_eval_count": 24,
                        "eval_count": 8,
                    }
                )
                return
            assert call_number == 2
            assert any(
                message.get("role") == "tool"
                and message.get("tool_call_id") == "call-local-write"
                for message in payload.get("messages", [])
            )
            self._ndjson(
                {
                    "model": "local-model",
                    "message": {
                        "role": "assistant",
                        "content": "LOCAL-CODING-OK",
                    },
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 32,
                    "eval_count": 4,
                }
            )

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
                "ASH_MODEL": "ollama/local-model",
                "OLLAMA_API_BASE": f"http://127.0.0.1:{port}",
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
                "--mode",
                "auto_edit",
                "-p",
                "Create local_agent.txt containing exactly LOCAL_AGENT_OK and finish.",
                "--output-format",
                "json",
            ],
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            timeout=25,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["response"] == "LOCAL-CODING-OK"
        assert payload["model"] == "ollama/local-model"
        assert (workspace / "local_agent.txt").read_text(encoding="utf-8") == (
            "LOCAL_AGENT_OK\n"
        )
        assert show_requests >= 1
        assert len(chat_requests) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
