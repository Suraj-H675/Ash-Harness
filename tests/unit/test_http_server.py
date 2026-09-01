from datetime import datetime, timezone

import httpx
import pytest

from ash.sdk import AshEvent, AshEventRecord, AshResult
from ash.core.session import SessionLineage
from ash.server.http import create_app


class FakeClient:
    def __init__(self):
        self.steering_error = None

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

    async def new_session(self):
        return "session-new"

    async def resume(self, session_id):
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
