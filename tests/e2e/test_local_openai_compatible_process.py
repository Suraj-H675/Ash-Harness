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
    """Exercise failing test feedback, repair, and verification through a real CLI."""
    requests: list[dict[str, object]] = []
    actions: list[tuple[str, dict[str, object]]] = [
        (
            "write_file",
            {"file_path": "local_agent.txt", "content": "BROKEN\n", "overwrite": False},
        ),
        (
            "run_command",
            {
                "command_line": "python3 -B test_local_agent.py",
                "cwd": ".",
                "timeout_seconds": 10,
            },
        ),
        (
            "write_file",
            {"file_path": "local_agent.txt", "content": "LOCAL_AGENT_OK\n", "overwrite": True},
        ),
        (
            "run_command",
            {
                "command_line": "python3 -B test_local_agent.py",
                "cwd": ".",
                "timeout_seconds": 10,
            },
        ),
    ]

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
            if call_number <= len(actions):
                name, arguments = actions[call_number - 1]
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
                                            "id": f"call-local-{call_number}",
                                            "type": "function",
                                            "function": {
                                                "name": name,
                                                "arguments": json.dumps(arguments),
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
                                            f'<call_tool name="{name}">'
                                            + "".join(
                                                f'<arg name="{key}">'
                                                f"{str(value).lower() if isinstance(value, bool) else value}"
                                                "</arg>"
                                                for key, value in arguments.items()
                                            )
                                            + "</call_tool>"
                                        )
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                    ]
                )
                return
            assert call_number == len(actions) + 1
            if native_tools:
                assert any(
                    message.get("role") == "tool"
                    and message.get("tool_call_id") == f"call-local-{len(actions)}"
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
        (workspace / "test_local_agent.py").write_text(
            "from pathlib import Path\n"
            "assert Path('local_agent.txt').read_text() == 'LOCAL_AGENT_OK\\n'\n"
        )
        (workspace / "unrelated.txt").write_text("preserve unrelated work\n")
        port = int(server.server_address[1])
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "ASH_MODEL": f"{provider}/local-model",
                base_env: f"http://127.0.0.1:{port}/v1",
                "NO_COLOR": "1",
                "ASH_ALLOW_UNSAFE_AUTO_APPROVE": "true",
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
                "auto_approve",
                "-p",
                "Create local_agent.txt, run its test, repair the failure, and verify.",
                "--output-format",
                "json",
            ],
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["response"] == "LOCAL-CODING-OK"
        assert payload["model"] == f"{provider}/local-model"
        assert (workspace / "local_agent.txt").read_text(encoding="utf-8") == (
            "LOCAL_AGENT_OK\n"
        )
        assert (workspace / "unrelated.txt").read_text() == "preserve unrelated work\n"
        assert (
            subprocess.run(
                [sys.executable, "-B", "test_local_agent.py"],
                cwd=workspace,
                check=False,
                timeout=5,
            ).returncode
            == 0
        )
        assert len(requests) == len(actions) + 1
        messages_after_failure = json.dumps(requests[2]["messages"])
        assert "AssertionError" in messages_after_failure
        tool_messages = [
            message for message in requests[-1]["messages"]
            if message.get("role") == "tool"
        ]
        assert tool_messages
        assert '"success": true' in str(tool_messages[-1]["content"])

        store = SessionStore(tmp_path / "db" / "sessions.db")
        sessions = store.list_sessions(project_path=str(workspace))
        assert len(sessions) == 1
        calls = store.load_session(sessions[0].session_id).tool_calls
        assert [call.tool_name for call in calls] == [name for name, _ in actions]
        assert all(call.executed and call.approved for call in calls)
        assert calls[1].error is not None and "AssertionError" in calls[1].error
        assert calls[3].error is None
        assert store.verify_audit_log(sessions[0].session_id) == []
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
