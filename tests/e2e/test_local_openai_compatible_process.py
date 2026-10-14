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
    ("route", "reply_mode", "failure_kind"),
    [
        ("vllm", "native", None),
        ("lmstudio", "native", None),
        ("vllm", "text", "no_native_tool_call"),
        ("vllm", "truncated", "output_truncated"),
        ("vllm", "wrong_args", "invalid_native_tool_call"),
        ("vllm", "invalid_json", "malformed_stream"),
        ("vllm", "rejected", "provider_error"),
    ],
)
def test_provider_tool_protocol_diagnostic_uses_real_cli_without_dispatch(
    tmp_path: Path, route: str, reply_mode: str, failure_kind: str | None
) -> None:
    requests: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:
            if self.path == "/api/v1/models":
                catalog = {"models": [{"key": "local-model", "type": "llm"}]}
            elif self.path == "/v1/models":
                catalog = {"data": [{"id": "local-model"}]}
            else:
                self.send_error(404)
                return
            payload = json.dumps(catalog).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            requests.append(request)
            if request.get("tools") and reply_mode == "rejected":
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                error = b'{"error":{"message":"tool parser unavailable"}}'
                self.send_header("Content-Length", str(len(error)))
                self.end_headers()
                self.wfile.write(error)
                return
            if request.get("tools") and reply_mode in {"native", "wrong_args", "invalid_json"}:
                delta = {
                    "tool_calls": [{
                        "index": 0,
                        "id": "diagnostic-call",
                        "type": "function",
                        "function": {
                            "name": "ash_diagnostic_echo",
                            "arguments": {
                                "invalid_json": '{"value":',
                                "wrong_args": '{"value":"WRONG"}',
                            }.get(reply_mode, '{"value":"ASH_TOOL_OK"}'),
                        },
                    }]
                }
                reason = "tool_calls"
            else:
                delta = {"content": "OK"}
                reason = "length" if request.get("tools") and reply_mode == "truncated" else "stop"
            chunks = [
                {"id": "diag", "object": "chat.completion.chunk", "created": 1,
                 "model": "local-model", "choices": [{"index": 0, "delta": delta,
                                                      "finish_reason": None}]},
                {"id": "diag", "object": "chat.completion.chunk", "created": 1,
                 "model": "local-model", "choices": [{"index": 0, "delta": {},
                                                      "finish_reason": reason}]},
            ]
            body = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                    + "data: [DONE]\n\n").encode()
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
        environment = os.environ.copy()
        environment.update({
            "HOME": str(home),
            "USERPROFILE": str(home),
            "ASH_MODEL": f"{route}/local-model",
            f"{route.upper()}_API_BASE": f"http://127.0.0.1:{server.server_port}/v1",
            "NO_COLOR": "1",
        })
        _route_loopback_directly(environment)
        result = subprocess.run(
            [sys.executable, "-m", "ash", "providers", "test", "--tools", "--json"],
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        native_reply = reply_mode == "native"
        assert result.returncode == (0 if native_reply else 1), result.stderr
        payload = json.loads(result.stdout)
        assert payload["completion_verified"] is True
        assert payload["native_tool_probe_attempted"] is True
        assert payload["native_tool_probe_verified"] is native_reply
        assert payload["native_tool_probe_failure_kind"] == failure_kind
        assert (payload["native_tool_probe_guidance"] is None) is native_reply
        assert payload["ok"] is native_reply
        assert len(requests) == 2
        assert "tools" not in requests[0]
        assert requests[1]["tools"][0]["function"]["name"] == "ash_diagnostic_echo"
        assert list(workspace.iterdir()) == []
        assert all("Authorization" not in str(request) for request in requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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


_BUILTIN_OPENAI_COMPATIBLE_ROUTES = [
    ("deepseek", "DEEPSEEK_API_BASE", "DEEPSEEK_API_KEY"),
    ("groq", "GROQ_API_BASE", "GROQ_API_KEY"),
]


def _builtin_provider_environment(
    home: Path,
    route: str,
    base_variable: str,
    key_variable: str,
    endpoint: str,
) -> dict[str, str]:
    environment = {
        "PATH": os.defpath,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "NO_COLOR": "1",
        "ASH_MODEL": f"{route}/future-model",
        base_variable: endpoint,
        key_variable: f"{route}-local-dummy-key",
    }
    _route_loopback_directly(environment)
    return environment


@pytest.mark.parametrize(
    ("route", "base_variable", "key_variable"),
    _BUILTIN_OPENAI_COMPATIBLE_ROUTES,
)
def test_builtin_provider_incomplete_xml_stream_does_not_write_file(
    tmp_path: Path,
    route: str,
    base_variable: str,
    key_variable: str,
) -> None:
    requests: list[dict[str, object]] = []
    xml_tool_call = (
        '<call_tool name="write_file"><arg name="file_path">stream-repro.txt'
        "</arg><arg name=\"content\">unexpected write</arg></call_tool>"
    )

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
            self._json(
                {
                    "id": "future-model",
                    "active": True,
                    "context_window": 32768,
                    "max_completion_tokens": 1024,
                }
            )

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            request = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            )
            requests.append(request)
            if len(requests) == 1:
                chunks = [
                    {
                        "id": "incomplete",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "future-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": xml_tool_call},
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": "incomplete",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "future-model",
                        "choices": [],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 8},
                    },
                ]
            else:
                chunks = [
                    {
                        "id": "complete",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "future-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "done"},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ]
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

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        workspace = tmp_path / "workspace"
        home = tmp_path / "home"
        workspace.mkdir()
        home.mkdir()
        git_environment = os.environ.copy()
        git_environment["PATH"] = os.defpath
        subprocess.run(
            ["git", "init", "-q"],
            cwd=workspace,
            env=git_environment,
            check=True,
        )
        environment = _builtin_provider_environment(
            home,
            route,
            base_variable,
            key_variable,
            f"http://127.0.0.1:{server.server_port}/v1",
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
                "Create stream-repro.txt using the write_file tool.",
                "--output-format",
                "json",
            ],
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

        assert result.returncode != 0, result.stdout
        payload = json.loads(result.stdout)
        assert "finish_reason" in payload["error"]["message"]
        assert len(requests) == 1
        assert not (workspace / "stream-repro.txt").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("route", "base_variable", "key_variable"),
    [
        *_BUILTIN_OPENAI_COMPATIBLE_ROUTES,
        ("vllm", "VLLM_API_BASE", "VLLM_API_KEY"),
    ],
)
def test_builtin_provider_stream_error_redacts_key_in_cli_json(
    tmp_path: Path,
    route: str,
    base_variable: str,
    key_variable: str,
) -> None:
    api_key = f"{route}-local-dummy-key"

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
            if route == "vllm":
                catalog = {
                    "data": [
                        {
                            "id": "future-model",
                            "max_model_len": 32768,
                            "max_output_tokens": 1024,
                        }
                    ]
                }
            else:
                catalog = {
                    "id": "future-model",
                    "active": True,
                    "context_window": 32768,
                    "max_completion_tokens": 1024,
                }
            self._json(catalog)

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            message = (
                "stream response failed"
                if route == "vllm"
                else f"Stream rejected credential {api_key}"
            )
            events = [
                {
                    "id": "partial",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "future-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "partial"},
                            "finish_reason": None,
                        }
                    ],
                },
                {"error": {"message": message}},
            ]
            body = "".join(
                "data: " + json.dumps(event, separators=(",", ":")) + "\n\n"
                for event in events
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
        environment = _builtin_provider_environment(
            home,
            route,
            base_variable,
            key_variable,
            f"http://127.0.0.1:{server.server_port}/v1",
        )
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
                "Respond briefly.",
                "--output-format",
                "json",
            ],
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

        assert result.returncode == 1, (result.stdout, result.stderr)
        payload = json.loads(result.stdout)
        error = payload["error"]
        assert api_key not in result.stdout + result.stderr
        if route == "vllm":
            assert "stream response failed" in error["message"]
        else:
            assert "[REDACTED]" in error["message"]
        assert error["category"] == "provider"
        assert error["retriable"] is False
        assert "selected model" in error["remedy"]
        assert "local provider process" in error["remedy"]
        assert "report it as an Ash bug" not in error["remedy"]
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
