import argparse

import pytest

from ash.commands.serve import serve_http
from ash.exceptions import AshError


def args(**overrides):
    values = {
        "token_env": "ASH_SERVER_TOKEN",
        "host": "127.0.0.1",
        "port": 8765,
        "rate_limit": 60,
        "allow_remote": False,
        "log_level": "info",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.mark.asyncio
async def test_serve_requires_token(monkeypatch) -> None:
    monkeypatch.delenv("ASH_SERVER_TOKEN", raising=False)
    with pytest.raises(ValueError, match="ASH_SERVER_TOKEN"):
        await serve_http(args())


@pytest.mark.asyncio
async def test_serve_requires_remote_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("ASH_SERVER_TOKEN", "0123456789abcdef")
    with pytest.raises(ValueError, match="allow-remote"):
        await serve_http(args(host="0.0.0.0"))


@pytest.mark.asyncio
async def test_serve_validates_arguments_before_creating_client(monkeypatch) -> None:
    monkeypatch.setenv("ASH_SERVER_TOKEN", "0123456789abcdef")

    async def fail_create():
        pytest.fail("client must not be created for invalid arguments")

    monkeypatch.setattr("ash.commands.serve.AshClient.create", fail_create)
    with pytest.raises(ValueError, match="Port"):
        await serve_http(args(port=0))
    with pytest.raises(ValueError, match="Rate limit"):
        await serve_http(args(rate_limit=0))


@pytest.mark.asyncio
async def test_serve_reports_missing_optional_dependencies(monkeypatch) -> None:
    monkeypatch.setenv("ASH_SERVER_TOKEN", "0123456789abcdef")
    monkeypatch.setattr("ash.commands.serve.uvicorn", None)

    with pytest.raises(AshError, match="optional HTTP server dependencies") as exc:
        await serve_http(args())

    assert exc.value.exit_code == 2
    assert "ash-ai[server]" in exc.value.remedy


@pytest.mark.asyncio
async def test_serve_closes_client_when_server_stops(monkeypatch) -> None:
    monkeypatch.setenv("ASH_SERVER_TOKEN", "0123456789abcdef")
    closed = False

    class Client:
        async def close(self) -> None:
            nonlocal closed
            closed = True

    class Server:
        def __init__(self, config) -> None:
            self.config = config

        async def serve(self) -> None:
            return None

    async def create_client():
        return Client()

    monkeypatch.setattr("ash.commands.serve.AshClient.create", create_client)
    monkeypatch.setattr("ash.commands.serve.uvicorn.Server", Server)

    assert await serve_http(args()) == 0
    assert closed is True
