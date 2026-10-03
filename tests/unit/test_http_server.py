import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from ash.sdk import AshEvent, AshEventRecord, AshResult
from ash.core.session import SessionLineage
from ash.server.http import (
    HTTPApprovalBroker,
    MAX_HTTP_BODY_BYTES,
    MAX_HTTP_IN_FLIGHT_TURNS,
    MAX_HTTP_RATE_LIMIT_KEYS,
    SlidingWindowLimiter,
    TurnRequest,
    _HTTPBoundaryMiddleware,
    _sse,
    create_app,
)


def test_sse_rejects_non_finite_json_payload() -> None:
    with pytest.raises(ValueError, match="Out of range float values"):
        _sse("metric", {"value": float("nan")})


@pytest.mark.asyncio
async def test_http_approval_broker_redacts_resolves_and_bounds_capacity() -> None:
    broker = HTTPApprovalBroker(timeout_seconds=5, max_pending=1)
    first = asyncio.create_task(
        broker.request(
            "run_command",
            {"command": ["echo", "ok"], "token": "top-secret"},
        )
    )
    await asyncio.sleep(0)

    pending = await broker.list_pending()
    assert len(pending) == 1
    assert pending[0]["tool"] == "run_command"
    assert "top-secret" not in str(pending[0]["arguments"])

    assert await broker.request("write_file", {"path": "x"}) is False
    assert await broker.resolve(pending[0]["id"], approved=True) is True
    assert await first is True
    assert await broker.list_pending() == []


@pytest.mark.asyncio
async def test_http_approval_broker_redacts_tool_declared_sensitive_fields() -> None:
    class TypeTool:
        sensitive_argument_fields = frozenset({"text"})

    class DialogTool:
        sensitive_argument_fields = frozenset({"prompt_text"})

    tools = {
        "browser_type": TypeTool(),
        "browser_dialog": DialogTool(),
    }
    broker = HTTPApprovalBroker(timeout_seconds=5, max_pending=2)
    broker.set_tool_provider(tools.get)
    typed = asyncio.create_task(
        broker.request(
            "browser_type",
            {"ref": "e1", "text": "typed-secret"},
        )
    )
    dialog = asyncio.create_task(
        broker.request(
            "browser_dialog",
            {"accept": True, "prompt_text": "dialog-secret"},
        )
    )
    await asyncio.sleep(0)

    pending = await broker.list_pending()
    assert "typed-secret" not in str(pending)
    assert "dialog-secret" not in str(pending)
    assert all("[REDACTED]" in str(item["arguments"]) for item in pending)
    for item in pending:
        assert await broker.resolve(item["id"], approved=False) is True
    assert await typed is False
    assert await dialog is False


@pytest.mark.asyncio
async def test_http_approval_endpoints_require_auth_and_resolve_pending_request() -> None:
    broker = HTTPApprovalBroker(timeout_seconds=5)
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
        approval_broker=broker,
    )
    pending_task = asyncio.create_task(
        broker.request("write_file", {"path": "README.md", "content": "hello"})
    )
    await asyncio.sleep(0)
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as http:
        assert (await http.get("/v1/approvals")).status_code == 401
        listed = await http.get("/v1/approvals", headers=headers)
        approval = listed.json()["approvals"][0]
        resolved = await http.post(
            f"/v1/approvals/{approval['id']}",
            json={"approved": False},
            headers=headers,
        )

    assert listed.status_code == 200
    assert listed.json()["enabled"] is True
    assert resolved.status_code == 200
    assert resolved.json() == {"resolved": True, "approved": False}
    assert await pending_task is False


@pytest.mark.asyncio
async def test_http_approval_long_poll_wakes_when_request_arrives() -> None:
    broker = HTTPApprovalBroker(timeout_seconds=5)
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
        approval_broker=broker,
    )
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as http:
        waiting = asyncio.create_task(
            http.get(
                "/v1/approvals",
                params={"wait_seconds": 2},
                headers=headers,
            )
        )
        await asyncio.sleep(0)
        pending_task = asyncio.create_task(
            broker.request("write_file", {"path": "README.md"})
        )
        response = await asyncio.wait_for(waiting, timeout=1)
        approval = response.json()["approvals"][0]
        await broker.resolve(approval["id"], approved=True)

    assert response.status_code == 200
    assert approval["tool"] == "write_file"
    assert await pending_task is True


@pytest.mark.asyncio
async def test_http_approval_broker_timeout_fails_closed() -> None:
    broker = HTTPApprovalBroker(timeout_seconds=1)
    broker.timeout_seconds = 0.01

    assert await broker.request("write_file", {"path": "README.md"}) is False
    assert await broker.list_pending() == []


@pytest.mark.asyncio
async def test_http_approval_broker_shutdown_fails_pending_closed() -> None:
    broker = HTTPApprovalBroker(timeout_seconds=5)
    pending = asyncio.create_task(
        broker.request("write_file", {"path": "README.md"})
    )
    await asyncio.sleep(0)

    await broker.close()

    assert await pending is False
    assert await broker.list_pending() == []


class FakeClient:
    def __init__(self):
        self.steering_error = None
        self.resume_error = None
        self.tree_error = None

    async def prompt(self, text):
        return AshResult(text.upper(), "session-1", "fake/model", 2)

    def sessions(self, query="", limit=20):
        return []

    def events(
        self,
        session_id=None,
        *,
        after_sequence=0,
        turn_id=None,
        limit=1000,
    ):
        return [
            AshEventRecord(
                after_sequence + 1,
                AshEvent(
                    "turn.completed",
                    {"response": "done", "session_id": session_id},
                ),
            )
        ]

    def session_messages(self, session_id, *, limit=200):
        assert 1 <= limit <= 500
        return [
            {
                "role": "user",
                "content": f"prompt for {session_id}",
                "timestamp": "2026-10-03T00:00:00+00:00",
            },
            {
                "role": "assistant",
                "content": "done",
                "timestamp": "2026-10-03T00:00:01+00:00",
            },
        ]

    async def new_session(self):
        return "session-new"

    async def resume(self, session_id):
        if self.resume_error is not None:
            raise self.resume_error
        return session_id

    async def fork(
        self,
        session_id=None,
        *,
        message_count=None,
        branch_name="",
        branch_summary="",
    ):
        return "session-fork"

    def session_tree(self, session_id=None):
        if self.tree_error is not None:
            raise self.tree_error
        return [
            SessionLineage(
                session_id=session_id or "session-1",
                root_session_id=session_id or "session-1",
                created_at=datetime.now(timezone.utc),
                children=("session-fork",),
            )
        ]

    async def close(self):
        return None

    async def steer(self, text):
        if self.steering_error is not None:
            raise self.steering_error
        return 1

    async def stream_prompt(self, text):
        yield AshEvent("turn.started", {})
        yield AshEvent("assistant.delta", {"text": text[:2]})
        yield AshEvent("assistant.delta", {"text": text[2:]})
        yield AshEvent(
            "turn.completed",
            {
                "response": text,
                "session_id": "session-1",
                "model": "fake/model",
                "context_tokens": 2,
            },
        )


@pytest.mark.asyncio
async def test_http_server_requires_auth_and_runs_turn() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
        requests_per_minute=10,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        health = await http.get("/health")
        assert health.status_code == 200
        assert health.json()["event_schema_version"] == 1
        assert (await http.post("/v1/turn", json={"input": "hello"})).status_code == 401
        response = await http.post(
            "/v1/turn",
            json={"input": "hello"},
            headers={"Authorization": "Bearer 0123456789abcdef"},
        )
    assert response.status_code == 200
    assert response.json()["response"] == "HELLO"
    assert response.json()["usage"]["cache_read_tokens"] == 0


@pytest.mark.asyncio
async def test_http_control_ui_is_public_but_api_remains_authenticated() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as http:
        page = await http.get("/ui")
        script = await http.get("/ui/control.js")
        style = await http.get("/ui/control.css")
        protected = await http.get("/v1/sessions")

    assert page.status_code == 200
    assert '<script src="/ui/control.js" defer></script>' in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-frame-options"] == "DENY"
    assert script.status_code == 200
    assert "localStorage" not in script.text
    assert "sessionStorage" not in script.text
    assert "document.cookie" not in script.text
    assert style.status_code == 200
    assert protected.status_code == 401


@pytest.mark.asyncio
async def test_http_bounds_direct_turns_and_keeps_steering_available() -> None:
    assert MAX_HTTP_IN_FLIGHT_TURNS >= 2

    class BlockingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.prompt_calls = 0
            self.started = 0
            self.two_started = asyncio.Event()
            self.release = asyncio.Event()

        async def prompt(self, text):
            self.prompt_calls += 1
            self.started += 1
            if self.started == 2:
                self.two_started.set()
            await self.release.wait()
            return AshResult(text.upper(), "session-1", "fake/model", 2)

    client = BlockingClient()
    app = create_app(
        client,  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
        requests_per_minute=20,
        max_in_flight_turns=2,
    )
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        first = asyncio.create_task(
            http.post("/v1/turn", json={"input": "first"}, headers=headers)
        )
        second = asyncio.create_task(
            http.post("/v1/turn", json={"input": "second"}, headers=headers)
        )
        try:
            await asyncio.wait_for(client.two_started.wait(), timeout=1)

            busy = await http.post(
                "/v1/turn",
                json={"input": "third"},
                headers=headers,
            )
            assert busy.status_code == 503
            assert busy.headers["retry-after"] == "1"
            assert client.prompt_calls == 2

            steer = await http.post(
                "/v1/turn/steer",
                json={"input": "change direction"},
                headers=headers,
            )
            assert steer.status_code == 200

            client.release.set()
            completed = await asyncio.gather(first, second)
            assert [response.status_code for response in completed] == [200, 200]

            after = await http.post(
                "/v1/turn",
                json={"input": "after"},
                headers=headers,
            )
            assert after.status_code == 200
            assert client.prompt_calls == 3
        finally:
            client.release.set()
            await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
async def test_http_unstarted_stream_response_does_not_consume_turn_capacity() -> None:
    client = FakeClient()
    app = create_app(
        client,  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
        max_in_flight_turns=1,
    )
    stream_endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/v1/turn/stream"
    )
    turn_endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/v1/turn"
    )

    response = await stream_endpoint(TurnRequest(input="never-started"))
    close = getattr(response.body_iterator, "aclose", None)
    if close is not None:
        await close()

    result = await turn_endpoint(TurnRequest(input="after"))

    assert result["response"] == "AFTER"


@pytest.mark.asyncio
async def test_http_stream_disconnect_releases_turn_capacity() -> None:
    started = asyncio.Event()

    class BlockingStreamClient(FakeClient):
        async def stream_prompt(self, text):
            started.set()
            yield AshEvent("turn.started", {})
            await asyncio.Event().wait()

    client = BlockingStreamClient()
    app = create_app(
        client,  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
        max_in_flight_turns=1,
    )
    stream_endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/v1/turn/stream"
    )
    turn_endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/v1/turn"
    )
    response = await stream_endpoint(TurnRequest(input="stream"))
    disconnect = asyncio.Event()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("more_body"):
            disconnect.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/turn/stream",
        "raw_path": b"/v1/turn/stream",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    await asyncio.wait_for(response(scope, receive, send), timeout=1)
    assert started.is_set()

    result = await turn_endpoint(TurnRequest(input="after-disconnect"))

    assert result["response"] == "AFTER-DISCONNECT"


@pytest.mark.asyncio
async def test_http_server_rejects_duplicate_authorization_headers() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
        requests_per_minute=10,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        for headers in (
            [
                ("Authorization", "Bearer 0123456789abcdef"),
                ("Authorization", "Bearer wrongwrongwrongwrong"),
            ],
            [
                ("Authorization", "Bearer wrongwrongwrongwrong"),
                ("Authorization", "Bearer 0123456789abcdef"),
            ],
            [
                ("Authorization", "Bearer 0123456789abcdef"),
                ("authorization", "Bearer 0123456789abcdef"),
            ],
        ):
            response = await http.post(
                "/v1/turn",
                json={"input": "hello"},
                headers=headers,
            )
            assert response.status_code == 401
            assert response.json()["detail"] == "Invalid bearer token"


@pytest.mark.asyncio
async def test_http_rejects_unauthenticated_rest_before_reading_body() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    consumed: list[int] = []

    async def body():
        for index in range(3):
            consumed.append(index)
            yield b"x" * 1024

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        response = await http.post(
            "/v1/turn",
            content=body(),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert consumed == []


@pytest.mark.asyncio
async def test_http_auth_precedes_rest_content_length_rejection() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    consumed = False

    async def body():
        nonlocal consumed
        consumed = True
        yield b"should-not-be-read"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        response = await http.post(
            "/v1/turn",
            content=body(),
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(MAX_HTTP_BODY_BYTES + 1),
            },
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert consumed is False


@pytest.mark.asyncio
async def test_http_rejects_unauthenticated_jsonrpc_before_reading_body() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    consumed: list[int] = []

    async def body():
        for index in range(3):
            consumed.append(index)
            yield b"x" * 1024

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        response = await http.post(
            "/rpc",
            content=body(),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert consumed == []


@pytest.mark.asyncio
async def test_http_rate_limit_precedes_rest_body_reading() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
        requests_per_minute=1,
    )
    headers = {
        "Authorization": "Bearer 0123456789abcdef",
        "Content-Type": "application/json",
    }
    consumed = False

    async def body():
        nonlocal consumed
        consumed = True
        yield b"x" * 1024

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        first = await http.get("/v1/sessions", headers=headers)
        limited = await http.post("/v1/turn", content=body(), headers=headers)

    assert first.status_code == 200
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert consumed is False


@pytest.mark.asyncio
async def test_http_framework_and_unknown_routes_default_to_authenticated() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        docs = await http.get("/docs")
        schema = await http.get("/openapi.json")
        missing = await http.get("/not-an-ash-route")
        authenticated_docs = await http.get("/docs", headers=headers)
        authenticated_schema = await http.get("/openapi.json", headers=headers)
        authenticated_missing = await http.get("/not-an-ash-route", headers=headers)

    assert docs.status_code == 401
    assert schema.status_code == 401
    assert missing.status_code == 401
    assert authenticated_docs.status_code == 404
    assert authenticated_schema.status_code == 404
    assert authenticated_missing.status_code == 404


@pytest.mark.asyncio
async def test_http_auth_rejects_non_ascii_bearer_token_as_unauthorized() -> None:
    app_called = False

    async def app(scope, receive, send) -> None:
        del scope, receive, send
        nonlocal app_called
        app_called = True

    middleware = _HTTPBoundaryMiddleware(
        app,
        bearer_token="0123456789abcdef",
        requests_per_minute=100,
        max_bytes=MAX_HTTP_BODY_BYTES,
    )
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/turn",
            "headers": [(b"authorization", b"Bearer \xff")],
            "client": ("127.0.0.1", 12345),
        },
        receive,
        send,
    )

    assert app_called is False
    assert sent[0]["status"] == 401


@pytest.mark.asyncio
async def test_http_pre_authentication_attempts_are_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ash.server.http.MAX_HTTP_PREAUTH_REQUESTS_PER_MINUTE", 2)
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
        requests_per_minute=1,
    )
    transport = httpx.ASGITransport(app=app, client=("198.51.100.10", 12345))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as http:
        responses = [
            await http.get(
                "/v1/sessions",
                headers={"Authorization": "Bearer wrong-token-value"},
            )
            for _ in range(5)
        ]

    assert [response.status_code for response in responses[:4]] == [401] * 4
    assert responses[4].status_code == 429
    assert responses[4].json()["detail"] == "Too many authentication attempts"


@pytest.mark.asyncio
async def test_http_rest_stops_reading_after_payload_limit() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    consumed: list[int] = []
    chunk_size = 4 * 1024 * 1024

    async def oversized_body():
        yield b'{"input":"'
        for index in range(8):
            consumed.append(index)
            yield b"x" * chunk_size
        yield b'"}'

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        response = await http.post(
            "/v1/turn",
            content=oversized_body(),
            headers={
                "Authorization": "Bearer 0123456789abcdef",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 413
    assert consumed == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_http_rest_rejects_oversized_content_length_without_reading_body() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    consumed = False

    async def oversized_body():
        nonlocal consumed
        consumed = True
        yield b"should-not-be-read"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        response = await http.post(
            "/v1/turn",
            content=oversized_body(),
            headers={
                "Authorization": "Bearer 0123456789abcdef",
                "Content-Type": "application/json",
                "Content-Length": str(MAX_HTTP_BODY_BYTES + 1),
            },
        )

    assert response.status_code == 413
    assert consumed is False


@pytest.mark.asyncio
async def test_http_resume_normalizes_missing_and_cross_workspace_sessions() -> None:
    client = FakeClient()
    app = create_app(
        client,  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        client.resume_error = KeyError("Session not found: missing")
        missing = await http.post(
            "/v1/sessions/resume",
            json={"session_id": "missing"},
            headers=headers,
        )
        client.resume_error = ValueError("session belongs to a different workspace")
        wrong_workspace = await http.post(
            "/v1/sessions/resume",
            json={"session_id": "foreign"},
            headers=headers,
        )

    assert missing.status_code == 404
    assert missing.json()["detail"] == "'Session not found: missing'"
    assert wrong_workspace.status_code == 422
    assert wrong_workspace.json()["detail"] == "session belongs to a different workspace"


@pytest.mark.asyncio
async def test_http_server_redacts_secrets_from_error_details() -> None:
    client = FakeClient()
    secret = "sk-proj-abcdefghijklmnop"
    client.steering_error = RuntimeError(f"provider failed token={secret}")
    app = create_app(
        client,  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        response = await http.post(
            "/v1/turn/steer",
            json={"input": "hello"},
            headers={"Authorization": "Bearer 0123456789abcdef"},
        )

    assert response.status_code == 409
    assert secret not in response.text
    assert "[REDACTED]" in response.json()["detail"]


@pytest.mark.asyncio
async def test_http_stream_redacts_secrets_from_event_payloads() -> None:
    client = FakeClient()
    secret = "verylongpasswordvalue"

    async def stream_with_secret(_text):
        yield AshEvent("turn.error", {"error": f"provider failed password={secret}"})

    client.stream_prompt = stream_with_secret
    app = create_app(
        client,  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        response = await http.post(
            "/v1/turn/stream",
            json={"input": "hello"},
            headers={"Authorization": "Bearer 0123456789abcdef"},
        )

    assert response.status_code == 200
    assert secret not in response.text
    assert "[REDACTED]" in response.text


@pytest.mark.asyncio
async def test_http_jsonrpc_runs_requests_and_returns_notification_ack() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    result_usage = (await FakeClient().prompt("hello")).usage  # type: ignore[arg-type]
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        request = await http.post(
            "/rpc",
            json={
                "jsonrpc": "2.0",
                "id": "rpc-1",
                "method": "turn/run",
                "params": {"input": "hello"},
            },
            headers=headers,
        )
        notification = await http.post(
            "/rpc",
            json={"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": 1}},
            headers=headers,
        )

    assert request.status_code == 200
    assert request.json() == {
        "jsonrpc": "2.0",
        "id": "rpc-1",
        "result": {
            "response": "HELLO",
            "session_id": "session-1",
            "model": "fake/model",
            "context_tokens": 2,
            "usage": result_usage,
        },
    }
    assert notification.status_code == 204
    assert notification.content == b""


@pytest.mark.asyncio
async def test_http_jsonrpc_returns_explicit_null_id_responses() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        notification = await http.post(
            "/rpc",
            json={"jsonrpc": "2.0", "method": "initialize"},
            headers=headers,
        )
        explicit_null = await http.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": None, "method": "initialize"},
            headers=headers,
        )
        missing = await http.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": None, "method": "missing"},
            headers=headers,
        )

    assert notification.status_code == 204
    assert explicit_null.status_code == 200
    assert explicit_null.json()["id"] is None
    assert "result" in explicit_null.json()
    assert missing.status_code == 200
    assert missing.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32601, "message": "Method not found: missing"},
    }


@pytest.mark.asyncio
async def test_http_lifespan_cancels_jsonrpc_notifications_without_owning_client() -> None:
    client = FakeClient()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    closed = False

    async def slow_prompt(_text: str) -> AshResult:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def close() -> None:
        nonlocal closed
        closed = True

    client.prompt = slow_prompt
    client.close = close
    app = create_app(
        client,  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
        close_client_on_shutdown=False,
    )
    headers = {"Authorization": "Bearer 0123456789abcdef"}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            response = await http.post(
                "/rpc",
                json={
                    "jsonrpc": "2.0",
                    "method": "turn/run",
                    "params": {"input": "wait"},
                },
                headers=headers,
            )
            assert response.status_code == 204
            await started.wait()

    assert cancelled.is_set()
    assert closed is False


@pytest.mark.asyncio
async def test_http_lifespan_preserves_body_failure_when_shutdown_fails() -> None:
    class FailingCloseClient(FakeClient):
        async def close(self) -> None:
            raise RuntimeError("client close failure")

    app = create_app(
        FailingCloseClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
        close_client_on_shutdown=True,
    )

    with pytest.raises(RuntimeError, match="lifespan body failure") as captured:
        async with app.router.lifespan_context(app):
            raise RuntimeError("lifespan body failure")

    assert any(
        "HTTP JSON-RPC shutdown cleanup failed" in note
        for note in captured.value.__notes__
    )


@pytest.mark.asyncio
async def test_http_jsonrpc_batch_and_protocol_errors() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        batch = await http.post(
            "/rpc",
            json=[
                {"jsonrpc": "2.0", "id": 1, "method": "status"},
                {"jsonrpc": "2.0", "method": "status"},
                {"jsonrpc": "1.0", "id": 2, "method": "status"},
            ],
            headers=headers,
        )
        parse_error = await http.post(
            "/rpc",
            content=b"{bad",
            headers={**headers, "Content-Type": "application/json"},
        )
        duplicate_keys = await http.post(
            "/rpc",
            content=b'{"jsonrpc":"2.0","id":1,"id":2,"method":"status"}',
            headers={**headers, "Content-Type": "application/json"},
        )
        oversized = await http.post(
            "/rpc",
            content=b"0" * 1048577,
            headers={**headers, "Content-Type": "application/json"},
        )

    assert batch.status_code == 200
    responses = batch.json()
    assert len(responses) == 2
    assert responses[0]["id"] == 1
    assert responses[1]["error"]["code"] == -32600
    assert parse_error.status_code == 400
    assert duplicate_keys.status_code == 400
    assert oversized.status_code == 413


@pytest.mark.asyncio
async def test_http_jsonrpc_stops_reading_after_payload_limit() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    headers = {
        "Authorization": "Bearer 0123456789abcdef",
        "Content-Type": "application/json",
    }
    consumed: list[int] = []

    async def oversized_body():
        for index in range(6):
            consumed.append(index)
            yield b"x" * 600_000

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        response = await http.post(
            "/rpc",
            content=oversized_body(),
            headers=headers,
        )

    assert response.status_code == 413
    assert consumed == [0, 1]


@pytest.mark.asyncio
async def test_http_jsonrpc_batch_rejects_non_object_members() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        response = await http.post(
            "/rpc",
            json=[
                1,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "turn/run",
                    "params": {"input": "hello"},
                },
            ],
            headers=headers,
        )

    assert response.status_code == 200
    responses = response.json()
    assert responses[0] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    assert responses[1]["jsonrpc"] == "2.0"
    assert responses[1]["id"] == 2
    assert responses[1]["result"]["response"] == "HELLO"


@pytest.mark.asyncio
async def test_http_jsonrpc_rejects_non_finite_request_ids() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    headers = {
        "Authorization": "Bearer 0123456789abcdef",
        "Content-Type": "application/json",
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        responses = []
        for identifier in ("1e400", "-1e400"):
            response = await http.post(
                "/rpc",
                content=(
                    '{"jsonrpc":"2.0","id":'
                    f"{identifier}"
                    ',"method":"status"}'
                ).encode(),
                headers=headers,
            )
            responses.append(response)

    for response in responses:
        assert response.status_code == 200
        assert response.json() == {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }


@pytest.mark.asyncio
async def test_http_server_rate_limits_authenticated_requests() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
        requests_per_minute=1,
    )
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        assert (await http.get("/v1/sessions", headers=headers)).status_code == 200
        assert (await http.get("/v1/sessions", headers=headers)).status_code == 429


@pytest.mark.asyncio
async def test_http_rate_limiter_bounds_client_buckets_and_reclaims_stale_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    monkeypatch.setattr("ash.server.http.time.monotonic", lambda: now)
    limiter = SlidingWindowLimiter(1)

    for index in range(MAX_HTTP_RATE_LIMIT_KEYS):
        assert await limiter.allow(f"client-{index}") is True

    assert await limiter.allow("overflow") is False
    assert len(limiter._requests) == MAX_HTTP_RATE_LIMIT_KEYS

    now = 61.0
    assert await limiter.allow("replacement") is True
    assert list(limiter._requests) == ["replacement"]


@pytest.mark.asyncio
async def test_http_server_replays_events_with_cursor() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        response = await http.get(
            "/v1/sessions/session-1/events?after_sequence=4", headers=headers
        )

    assert response.status_code == 200
    assert response.json()["schema_version"] == 1
    assert response.json()["events"][0]["sequence"] == 5
    assert response.json()["next_sequence"] == 5


@pytest.mark.asyncio
async def test_http_server_returns_bounded_session_transcript() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer 0123456789abcdef"}

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as http:
        response = await http.get(
            "/v1/sessions/session-1/messages",
            params={"limit": 50},
            headers=headers,
        )

    assert response.status_code == 200
    assert response.json()["messages"][0] == {
        "role": "user",
        "content": "prompt for session-1",
        "timestamp": "2026-10-03T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_http_server_rejects_invalid_event_cursors_and_limits() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        responses = [
            await http.get(
                "/v1/sessions/session-1/events?after_sequence=-1",
                headers=headers,
            ),
            await http.get(
                "/v1/sessions/session-1/events?limit=0",
                headers=headers,
            ),
            await http.get(
                "/v1/sessions/session-1/events?limit=10001",
                headers=headers,
            ),
        ]

    assert [response.status_code for response in responses] == [422, 422, 422]


@pytest.mark.asyncio
async def test_http_server_forks_and_returns_session_tree() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        forked = await http.post(
            "/v1/sessions/session-1/fork",
            json={"message_count": 2, "branch_name": "alternate"},
            headers=headers,
        )
        tree = await http.get("/v1/sessions/session-1/tree", headers=headers)

    assert forked.status_code == 200
    assert forked.json() == {"session_id": "session-fork"}
    assert tree.status_code == 200
    assert tree.json()["sessions"][0]["children"] == ["session-fork"]


@pytest.mark.asyncio
async def test_http_session_tree_rejects_cross_workspace_session() -> None:
    client = FakeClient()
    client.tree_error = ValueError("session belongs to a different workspace")
    app = create_app(
        client,  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        response = await http.get("/v1/sessions/foreign/tree", headers=headers)

    assert response.status_code == 422
    assert response.json()["detail"] == "session belongs to a different workspace"


@pytest.mark.asyncio
async def test_http_server_rejects_boolean_fork_message_count() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        response = await http.post(
            "/v1/sessions/session-1/fork",
            json={"message_count": True},
            headers=headers,
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_http_server_forwards_live_sse_events() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        async with http.stream(
            "POST", "/v1/turn/stream", json={"input": "hello"}, headers=headers
        ) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])
        assert response.status_code == 200
        assert "event: turn.started" in body
        assert body.count("event: assistant.delta") == 2
        assert '"schema_version":1' in body
        assert '"text":"he"' in body
        assert "event: turn.completed" in body


@pytest.mark.asyncio
async def test_http_server_queues_turn_steering_and_reports_conflicts() -> None:
    client = FakeClient()
    app = create_app(
        client,  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
    )
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        accepted = await http.post(
            "/v1/turn/steer",
            json={"input": "change direction"},
            headers=headers,
        )
        assert accepted.status_code == 200
        assert accepted.json() == {"pending": 1}

        client.steering_error = RuntimeError("no turn is currently running")
        idle = await http.post(
            "/v1/turn/steer",
            json={"input": "too late"},
            headers=headers,
        )
        assert idle.status_code == 409

        client.steering_error = OverflowError("steering queue is full")
        full = await http.post(
            "/v1/turn/steer",
            json={"input": "one more"},
            headers=headers,
        )
        assert full.status_code == 429
