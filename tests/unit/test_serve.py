import argparse

import pytest

from ash.commands.serve import serve_http
from ash.exceptions import AshError
from ash.install import pipx_install_command


def args(**overrides):
    values = {
        "token_env": "ASH_SERVER_TOKEN",
        "host": "127.0.0.1",
        "port": 8765,
        "rate_limit": 60,
        "approval_timeout": 300.0,
        "allow_remote": False,
        "ssl_certfile": None,
        "ssl_keyfile": None,
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
async def test_serve_requires_tls_for_remote_binding(monkeypatch) -> None:
    monkeypatch.setenv("ASH_SERVER_TOKEN", "0123456789abcdef")

    with pytest.raises(ValueError, match="requires TLS"):
        await serve_http(args(host="0.0.0.0", allow_remote=True))


@pytest.mark.asyncio
async def test_serve_requires_tls_cert_and_key_together(monkeypatch) -> None:
    monkeypatch.setenv("ASH_SERVER_TOKEN", "0123456789abcdef")

    with pytest.raises(ValueError, match="both --ssl-certfile and --ssl-keyfile"):
        await serve_http(args(ssl_certfile="cert.pem"))


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
    with pytest.raises(ValueError, match="Approval timeout"):
        await serve_http(args(approval_timeout=0))


@pytest.mark.asyncio
async def test_serve_reports_missing_optional_dependencies(monkeypatch) -> None:
    monkeypatch.setenv("ASH_SERVER_TOKEN", "0123456789abcdef")
    monkeypatch.setattr("ash.commands.serve.uvicorn", None)

    with pytest.raises(AshError, match="optional HTTP server dependencies") as exc:
        await serve_http(args())

    assert exc.value.exit_code == 2
    assert pipx_install_command("server") in exc.value.remedy
    assert "pipx install" not in exc.value.remedy


@pytest.mark.asyncio
async def test_serve_closes_client_when_server_stops(monkeypatch) -> None:
    monkeypatch.setenv("ASH_SERVER_TOKEN", "0123456789abcdef")
    closed = False

    class Client:
        async def close(self) -> None:
            nonlocal closed
            closed = True

    observed_config = None

    class Server:
        def __init__(self, config) -> None:
            nonlocal observed_config
            self.config = config
            observed_config = config

        async def serve(self) -> None:
            return None

    approval_callback = None

    async def create_client(**kwargs):
        nonlocal approval_callback
        approval_callback = kwargs.get("approval_callback")
        return Client()

    monkeypatch.setattr("ash.commands.serve.AshClient.create", create_client)
    monkeypatch.setattr("ash.commands.serve.uvicorn.Server", Server)

    assert await serve_http(
        args(ssl_certfile="cert.pem", ssl_keyfile="key.pem")
    ) == 0
    assert closed is True
    assert callable(approval_callback)
    assert observed_config.ssl_certfile == "cert.pem"
    assert observed_config.ssl_keyfile == "key.pem"
    assert observed_config.limit_concurrency == 128
    assert observed_config.backlog == 128
    assert observed_config.timeout_keep_alive == 5


@pytest.mark.asyncio
async def test_serve_preserves_server_failure_when_client_close_fails(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ASH_SERVER_TOKEN", "0123456789abcdef")

    class Client:
        async def close(self) -> None:
            raise RuntimeError("client close failure")

    class Server:
        def __init__(self, _config) -> None:
            pass

        async def serve(self) -> None:
            raise RuntimeError("server failure")

    async def create_client(**_kwargs):
        return Client()

    monkeypatch.setattr("ash.commands.serve.AshClient.create", create_client)
    monkeypatch.setattr("ash.commands.serve.uvicorn.Server", Server)

    with pytest.raises(RuntimeError, match="server failure") as captured:
        await serve_http(args())

    assert any("HTTP server client cleanup failed" in note for note in captured.value.__notes__)
