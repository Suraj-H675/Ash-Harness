from __future__ import annotations

import asyncio
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import threading
import time

import httpx
import pytest

from ash.providers.openai import OpenAIProvider


pytestmark = [
    pytest.mark.skipif(
        os.environ.get("ASH_RUN_TOXIPROXY_TESTS") != "1",
        reason="set ASH_RUN_TOXIPROXY_TESTS=1 to run the local fault-injection smoke",
    ),
    pytest.mark.skipif(os.name == "nt", reason="Toxiproxy smoke supports Linux and macOS"),
]

_PROXY_NAME = "ash-openai-smoke"
_SSE_RESPONSE = (
    b'data: {"id":"chatcmpl-local","object":"chat.completion.chunk",'
    b'"created":1700000000,"model":"local-test","choices":[{"index":0,'
    b'"delta":{"role":"assistant","content":"healthy"},'
    b'"finish_reason":null}]}\n\n'
    b'data: {"id":"chatcmpl-local","object":"chat.completion.chunk",'
    b'"created":1700000000,"model":"local-test","choices":[{"index":0,'
    b'"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)


@dataclass(frozen=True)
class _Request:
    path: str
    authorization: str | None
    body: bytes


class _LocalSSEFixture:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self.requests: list[_Request] = []
        self.response_writes: list[int] = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                content_length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(content_length)
                request_number = fixture._record_request(
                    _Request(
                        path=self.path,
                        authorization=self.headers.get("Authorization"),
                        body=body,
                    )
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(_SSE_RESPONSE)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(_SSE_RESPONSE)
                self.wfile.flush()
                fixture._record_response_write(request_number)
                self.close_connection = True

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="ash-local-openai-sse",
            daemon=True,
        )
        try:
            self.thread.start()
        except BaseException:
            self.server.server_close()
            raise

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def _record_request(self, request: _Request) -> int:
        with self._condition:
            self.requests.append(request)
            self._condition.notify_all()
            return len(self.requests)

    def _record_response_write(self, request_number: int) -> None:
        with self._condition:
            self.response_writes.append(request_number)
            self._condition.notify_all()

    def wait_for_requests(self, count: int, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: len(self.requests) >= count, timeout)

    def wait_for_response_writes(self, count: int, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: len(self.response_writes) >= count, timeout
            )

    def request_snapshot(self) -> list[_Request]:
        with self._condition:
            return list(self.requests)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("local OpenAI fixture did not stop")


def _toxiproxy_binary() -> str:
    override = os.environ.get("ASH_TOXIPROXY_SERVER")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override).expanduser())

    system = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    architecture = {
        "x86_64": "amd64",
        "AMD64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(platform.machine())
    if system is not None and architecture is not None:
        candidates.append(
            Path.home()
            / ".local"
            / "share"
            / "ash-dev-tools"
            / "toxiproxy"
            / "v2.12.0"
            / f"toxiproxy-server-{system}-{architecture}"
        )

    found_on_path = shutil.which("toxiproxy-server")
    if found_on_path:
        candidates.append(Path(found_on_path))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    checked = ", ".join(str(candidate) for candidate in candidates) or "no candidates"
    pytest.fail(
        "Toxiproxy was explicitly enabled but no executable server was found. "
        "Set ASH_TOXIPROXY_SERVER to its executable path or add toxiproxy-server "
        f"to PATH. Checked: {checked}",
        pytrace=False,
    )


def _free_loopback_ports(count: int) -> list[int]:
    sockets: list[socket.socket] = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [int(sock.getsockname()[1]) for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _wait_for_toxiproxy(
    process: subprocess.Popen[bytes], admin_url: str, timeout: float = 5
) -> None:
    deadline = time.monotonic() + timeout
    with httpx.Client(trust_env=False, timeout=0.5) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"Toxiproxy exited during startup with status {process.returncode}"
                )
            try:
                response = client.get(f"{admin_url}/proxies")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
    raise TimeoutError("Toxiproxy admin API did not become ready within five seconds")


async def _collect(provider: OpenAIProvider) -> str:
    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [{"role": "user", "content": "say hello"}]
        )
    ]
    return "".join(chunk.content for chunk in chunks)


@pytest.mark.asyncio
async def test_openai_provider_cancels_an_impaired_local_stream_without_retry() -> None:
    server_binary = _toxiproxy_binary()
    fixture = _LocalSSEFixture()
    toxiproxy: subprocess.Popen[bytes] | None = None
    provider: OpenAIProvider | None = None
    impaired_request: asyncio.Task[str] | None = None
    try:
        admin_port, proxy_port = _free_loopback_ports(2)
        admin_url = f"http://127.0.0.1:{admin_port}"
        toxiproxy = subprocess.Popen(
            [
                server_binary,
                "-host=127.0.0.1",
                f"-port={admin_port}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        _wait_for_toxiproxy(toxiproxy, admin_url)

        with httpx.Client(trust_env=False, timeout=1) as admin:
            response = admin.post(
                f"{admin_url}/proxies",
                json={
                    "name": _PROXY_NAME,
                    "listen": f"127.0.0.1:{proxy_port}",
                    "upstream": fixture.base_url.removeprefix("http://"),
                    "enabled": True,
                },
            )
            response.raise_for_status()

        provider = OpenAIProvider(
            model_name="local-test",
            base_url=f"http://127.0.0.1:{proxy_port}/v1",
            allow_anonymous=True,
        )
        assert await asyncio.wait_for(_collect(provider), timeout=5) == "healthy"
        assert fixture.wait_for_requests(1, timeout=2)
        assert fixture.wait_for_response_writes(1, timeout=2)
        assert len(fixture.request_snapshot()) == 1

        with httpx.Client(trust_env=False, timeout=1) as admin:
            response = admin.post(
                f"{admin_url}/proxies/{_PROXY_NAME}/toxics",
                json={
                    "name": "hold_downstream_response",
                    "type": "latency",
                    "stream": "downstream",
                    "toxicity": 1.0,
                    "attributes": {"latency": 15_000, "jitter": 0},
                },
            )
            response.raise_for_status()

        impaired_request = asyncio.create_task(_collect(provider))
        assert await asyncio.to_thread(fixture.wait_for_requests, 2, 5)
        assert await asyncio.to_thread(fixture.wait_for_response_writes, 2, 5)
        assert not impaired_request.done()

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(impaired_request), timeout=0.5)
        impaired_request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(impaired_request, timeout=3)
        assert impaired_request.cancelled()

        requests = fixture.request_snapshot()
        assert len(requests) == 2
        assert all(request.path == "/v1/chat/completions" for request in requests)
        assert all(request.authorization is None for request in requests)
        assert all(
            json.loads(request.body)["model"] == "local-test" for request in requests
        )
    finally:
        try:
            if impaired_request is not None and not impaired_request.done():
                impaired_request.cancel()
                try:
                    await asyncio.wait_for(impaired_request, timeout=3)
                except asyncio.CancelledError:
                    pass
        finally:
            try:
                if provider is not None:
                    await provider.aclose()
            finally:
                try:
                    fixture.close()
                finally:
                    if toxiproxy is not None:
                        _stop_process(toxiproxy)
