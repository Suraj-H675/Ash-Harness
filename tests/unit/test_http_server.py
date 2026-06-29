import httpx
import pytest

from ash.sdk import AshEvent, AshResult
from server.http import create_app


class FakeClient:
    def __init__(self):
        self.steering_error = None

    async def prompt(self, text):
        return AshResult(text.upper(), "session-1", "fake/model", 2)

    def sessions(self, query="", limit=20):
        return []

    async def new_session(self):
        return "session-new"

    async def resume(self, session_id):
        return session_id

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
        assert (await http.get("/health")).status_code == 200
        assert (await http.post("/v1/turn", json={"input": "hello"})).status_code == 401
        response = await http.post(
            "/v1/turn",
            json={"input": "hello"},
            headers={"Authorization": "Bearer 0123456789abcdef"},
        )
        assert response.status_code == 200
        assert response.json()["response"] == "HELLO"


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
        assert 'data: {"text":"he"}' in body
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
