from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest

import ash.commands.remote as remote_module
from ash.cli import main
from ash.commands.remote import (
    MAX_REMOTE_RESPONSE_BYTES,
    RemoteAshClient,
    RemoteClientError,
    validate_remote_base_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8765",
        "https://user:secret@example.com",
        "https://example.com/path",
        "https://example.com?token=secret",
        "https://example.com/#fragment",
        "ftp://example.com",
    ],
)
def test_remote_url_rejects_insecure_or_credential_bearing_origins(url: str) -> None:
    with pytest.raises(ValueError):
        validate_remote_base_url(url)


def test_remote_url_accepts_loopback_http_and_remote_https() -> None:
    assert validate_remote_base_url("http://127.0.0.1:8765/") == "http://127.0.0.1:8765"
    assert validate_remote_base_url("http://localhost:8765") == "http://localhost:8765"
    assert validate_remote_base_url("https://ash.example:8765") == "https://ash.example:8765"


@pytest.mark.parametrize(
    "token",
    [
        "0123456789abcde ",
        "0123456789abcde\n",
        "0123456789abcdé",
    ],
)
def test_remote_client_rejects_server_invalid_bearer_tokens(token: str) -> None:
    with pytest.raises(ValueError, match="non-whitespace ASCII"):
        RemoteAshClient("https://ash.example", bearer_token=token)


def test_remote_cli_parses_status_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = {}

    async def fake_run(args) -> int:
        observed["action"] = args.remote_action
        observed["url"] = args.url
        observed["token_env"] = args.token_env
        observed["timeout"] = args.timeout
        observed["json"] = args.json
        return 0

    monkeypatch.setattr("ash.commands.remote.run_remote", fake_run)

    assert (
        main(
            [
                "remote",
                "status",
                "https://ash.example:8765",
                "--token-env",
                "MY_ASH_TOKEN",
                "--timeout",
                "45",
                "--json",
            ]
        )
        == 0
    )
    assert observed == {
        "action": "status",
        "url": "https://ash.example:8765",
        "token_env": "MY_ASH_TOKEN",
        "timeout": 45.0,
        "json": True,
    }


@pytest.mark.asyncio
async def test_remote_client_status_sessions_and_authentication() -> None:
    observed: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        assert request.headers["Authorization"] == "Bearer 0123456789abcdef"
        if request.url.path == "/rpc":
            payload = json.loads(request.content)
            assert payload["method"] == "status"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "model": "fake/model",
                        "session_id": "session-1",
                    },
                },
            )
        if request.url.path == "/v1/sessions":
            assert request.url.params["query"] == "review"
            return httpx.Response(200, json={"sessions": [{"session_id": "session-1"}]})
        raise AssertionError(f"unexpected request: {request.url}")

    async with RemoteAshClient(
        "https://ash.example",
        bearer_token="0123456789abcdef",
        transport=httpx.MockTransport(handler),
    ) as client:
        status = await client.status()
        sessions = await client.sessions(query="review")

    assert status["model"] == "fake/model"
    assert sessions[0]["session_id"] == "session-1"
    assert [request.url.path for request in observed] == ["/rpc", "/v1/sessions"]


@pytest.mark.asyncio
async def test_remote_json_requests_preserve_configured_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        timeout = request.extensions["timeout"]
        assert timeout["connect"] == 15
        assert timeout["read"] == 45
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"model": "fake/model", "session_id": "session-1"},
            },
        )

    async with RemoteAshClient(
        "https://ash.example",
        bearer_token="0123456789abcdef",
        timeout_seconds=45,
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.status()


@pytest.mark.asyncio
async def test_remote_client_streams_bounded_sse_events() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/turn/stream"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'event: assistant.delta\ndata: {"text":"hel"}\n\n'
                b'event: assistant.delta\ndata: {"text":"lo"}\n\n'
                b'event: turn.completed\ndata: {"session_id":"session-1"}\n\n'
            ),
        )

    async with RemoteAshClient(
        "https://ash.example",
        bearer_token="0123456789abcdef",
        transport=httpx.MockTransport(handler),
    ) as client:
        events = [item async for item in client.stream_turn("hello")]

    assert events == [
        ("assistant.delta", {"text": "hel"}),
        ("assistant.delta", {"text": "lo"}),
        ("turn.completed", {"session_id": "session-1"}),
    ]


@pytest.mark.asyncio
async def test_remote_client_bounds_delimiter_free_sse_before_line_buffer_growth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(remote_module, "MAX_REMOTE_SSE_EVENT_BYTES", 64)

    class ChunkedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 40
            yield b"y" * 40

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkedStream(),
        )

    async with RemoteAshClient(
        "https://ash.example",
        bearer_token="0123456789abcdef",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RemoteClientError, match="SSE event exceeds"):
            _ = [item async for item in client.stream_turn("hello")]


@pytest.mark.asyncio
async def test_remote_client_rejects_oversized_response_before_buffering() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(MAX_REMOTE_RESPONSE_BYTES + 1)},
            content=b"{}",
        )

    async with RemoteAshClient(
        "https://ash.example",
        bearer_token="0123456789abcdef",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RemoteClientError, match="exceeds the client limit"):
            await client.status()


@pytest.mark.asyncio
async def test_remote_client_reports_http_failure_without_following_redirect() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            307,
            headers={"location": "https://attacker.example/"},
            json={"detail": "moved"},
        )

    async with RemoteAshClient(
        "https://ash.example",
        bearer_token="0123456789abcdef",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RemoteClientError, match="HTTP 307"):
            await client.status()

    assert calls == 1


@pytest.mark.asyncio
async def test_remote_client_approval_lifecycle() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.params["wait_seconds"] == "2"
            return httpx.Response(
                200,
                json={
                    "enabled": True,
                    "approvals": [
                        {
                            "id": "approval-1",
                            "tool": "write_file",
                            "arguments": {"path": "README.md"},
                        }
                    ],
                },
            )
        assert request.url.path == "/v1/approvals/approval-1"
        assert json.loads(request.content) == {"approved": True}
        return httpx.Response(200, json={"resolved": True, "approved": True})

    async with RemoteAshClient(
        "https://ash.example",
        bearer_token="0123456789abcdef",
        transport=httpx.MockTransport(handler),
    ) as client:
        approvals = await client.approvals(wait_seconds=2)
        await client.resolve_approval("approval-1", approved=True)

    assert approvals[0]["tool"] == "write_file"


def test_remote_prompt_returns_failure_for_terminal_error_event(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailedRemoteClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def status(self):
            return {"session_id": "session-1"}

        async def stream_turn(
            self,
            _text: str,
        ) -> AsyncIterator[tuple[str, dict[str, object]]]:
            yield "turn.error", {"error": "remote failure"}

    monkeypatch.setenv("ASH_SERVER_TOKEN", "0123456789abcdef")
    monkeypatch.setattr("ash.commands.remote.RemoteAshClient", FailedRemoteClient)

    assert main(["remote", "prompt", "https://ash.example", "hello"]) == 1
    assert "remote failure" in capsys.readouterr().err


def test_remote_prompt_returns_failure_when_stream_ends_without_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class TruncatedRemoteClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def status(self):
            return {"session_id": "session-1"}

        async def stream_turn(
            self,
            _text: str,
        ) -> AsyncIterator[tuple[str, dict[str, object]]]:
            yield "assistant.delta", {"text": "partial"}

    monkeypatch.setenv("ASH_SERVER_TOKEN", "0123456789abcdef")
    monkeypatch.setattr("ash.commands.remote.RemoteAshClient", TruncatedRemoteClient)

    assert main(["remote", "prompt", "https://ash.example", "hello"]) == 1
    assert "without a terminal event" in capsys.readouterr().err
