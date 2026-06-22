from fastapi.testclient import TestClient

from ash.sdk import AshResult
from server.http import create_app


class FakeClient:
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


def test_http_server_requires_auth_and_runs_turn() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
        requests_per_minute=10,
    )
    with TestClient(app) as http:
        assert http.get("/health").status_code == 200
        assert http.post("/v1/turn", json={"input": "hello"}).status_code == 401
        response = http.post(
            "/v1/turn",
            json={"input": "hello"},
            headers={"Authorization": "Bearer 0123456789abcdef"},
        )
        assert response.status_code == 200
        assert response.json()["response"] == "HELLO"


def test_http_server_rate_limits_authenticated_requests() -> None:
    app = create_app(
        FakeClient(),  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
        requests_per_minute=1,
    )
    headers = {"Authorization": "Bearer 0123456789abcdef"}
    with TestClient(app) as http:
        assert http.get("/v1/sessions", headers=headers).status_code == 200
        assert http.get("/v1/sessions", headers=headers).status_code == 429
