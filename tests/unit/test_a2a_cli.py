import asyncio
import io
import sys
from types import SimpleNamespace

import httpx
import pytest
from a2a.utils.errors import InternalError

from ash.commands.a2a import _local_public_url, _remote_url, serve_a2a

from ash.cli import main


def test_a2a_check_reports_protocol_readiness(capsys) -> None:
    assert main(["a2a", "check"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Ash A2A protocol v1.0 is ready.\n"
    assert captured.err == ""


def test_a2a_serve_requires_an_operator_token(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ASH_A2A_TOKEN", raising=False)

    assert main(["a2a", "serve"]) == 2
    assert "Set ASH_A2A_TOKEN" in capsys.readouterr().err


def test_a2a_serve_requires_tls_for_remote_binding(monkeypatch, capsys) -> None:
    monkeypatch.setenv("ASH_A2A_TOKEN", "0123456789abcdef")

    assert (
        main(
            [
                "a2a",
                "serve",
                "--host",
                "0.0.0.0",
                "--allow-remote",
                "--public-url",
                "https://agent.example.com",
            ]
        )
        == 2
    )
    assert "requires TLS" in capsys.readouterr().err


def test_a2a_serve_requires_tls_cert_and_key_together(monkeypatch, capsys) -> None:
    monkeypatch.setenv("ASH_A2A_TOKEN", "0123456789abcdef")

    assert main(["a2a", "serve", "--ssl-certfile", "cert.pem"]) == 2
    assert "both --ssl-certfile and --ssl-keyfile" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a2a_serve_bounds_uvicorn_admission(monkeypatch) -> None:
    monkeypatch.setenv("ASH_A2A_TOKEN", "0123456789abcdef")
    observed_config = None

    class Server:
        def __init__(self, config) -> None:
            nonlocal observed_config
            observed_config = config

        async def serve(self) -> None:
            return None

    monkeypatch.setattr("ash.commands.a2a.AshConfig.load", lambda: object())
    monkeypatch.setattr("ash.commands.a2a.create_a2a_app", lambda *args, **kwargs: object())
    monkeypatch.setattr("ash.commands.a2a.uvicorn.Server", Server)
    args = SimpleNamespace(
        token_env="ASH_A2A_TOKEN",
        host="127.0.0.1",
        port=8766,
        rate_limit=60,
        allow_remote=False,
        ssl_certfile=None,
        ssl_keyfile=None,
        public_url=None,
        log_level="info",
    )

    assert await serve_a2a(args) == 0
    assert observed_config.limit_concurrency == 128
    assert observed_config.backlog == 128
    assert observed_config.timeout_keep_alive == 5


def test_a2a_client_network_failure_has_stable_cli_error(monkeypatch, capsys) -> None:
    async def fail(args) -> int:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("ash.commands.a2a.inspect_a2a", fail)

    assert main(["a2a", "inspect", "https://agent.example.com"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "A2A operation failed: connection refused" in captured.err


def test_a2a_inspect_redacts_signed_url_from_agent_card(monkeypatch, capsys) -> None:
    from ash.commands import a2a as a2a_commands

    marker = "inspect-signed-marker"

    class FakeResolver:
        def __init__(self, http, url) -> None:
            del http, url

        async def get_agent_card(self):
            return object()

    monkeypatch.setattr(a2a_commands, "A2ACardResolver", FakeResolver)
    monkeypatch.setattr(
        a2a_commands,
        "agent_card_to_dict",
        lambda card: {
            "name": "Remote",
            "supportedInterfaces": [
                {
                    "url": (
                        "https://agent.example/a2a?"
                        f"X-Amz-Signature={marker}&view=full"
                    )
                }
            ],
        },
    )

    assert main(["a2a", "inspect", "https://agent.example.com"]) == 0
    captured = capsys.readouterr()
    assert marker not in captured.out
    assert "X-Amz-Signature=[REDACTED]" in captured.out


def test_a2a_cli_catches_and_redacts_standard_protocol_error(
    monkeypatch,
    capsys,
) -> None:
    marker = "actual-sdk-signed-marker"

    async def fail(args) -> int:
        del args
        raise InternalError(
            "remote failed at https://agent.example/cb?"
            f"X-Amz-Signature={marker}&view=full"
        )

    monkeypatch.setattr("ash.commands.a2a.send_a2a", fail)

    assert main(["a2a", "send", "https://agent.example.com", "hello"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert marker not in captured.err
    assert "X-Amz-Signature=[REDACTED]" in captured.err


def test_a2a_stdin_prompt_is_bounded_before_network_use(monkeypatch) -> None:
    from ash.commands.a2a import MAX_A2A_CLIENT_INPUT_BYTES, send_a2a

    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO("x" * (MAX_A2A_CLIENT_INPUT_BYTES + 1)),
    )
    args = type(
        "Args",
        (),
        {
            "url": "https://agent.example.com",
            "prompt": "-",
            "context_id": None,
            "token_env": "ASH_A2A_TOKEN",
            "timeout": 30.0,
            "json": False,
        },
    )()

    with pytest.raises(ValueError, match="A2A prompt exceeds"):
        asyncio.run(send_a2a(args))


def test_a2a_cli_remote_url_rejects_plaintext_non_loopback() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        _remote_url("http://agent.example.com")
    assert _remote_url("http://localhost:8765") == "http://localhost:8765"


def test_a2a_local_public_url_matches_tls_transport() -> None:
    assert _local_public_url("127.0.0.1", 8770) == "http://127.0.0.1:8770"
    assert (
        _local_public_url("::1", 8770, secure=True) == "https://[::1]:8770"
    )


def test_a2a_json_event_accumulator_rejects_large_payload() -> None:
    from ash.commands.a2a import _append_json_event

    events: list[dict] = []

    with pytest.raises(RuntimeError, match="A2A response exceeded"):
        _append_json_event(events, {"text": "x" * 1_000_001}, 0, 0)

    assert events == []


def test_a2a_json_event_accumulator_redacts_signed_url() -> None:
    from ash.commands.a2a import _append_json_event

    marker = "cli-json-signature-marker"
    events: list[dict] = []
    event = {
        "statusUpdate": {
            "status": {
                "message": {
                    "parts": [
                        {
                            "text": (
                                "failed at https://agent.example/cb?"
                                f"X-Amz-Signature={marker}&view=full"
                            )
                        }
                    ]
                }
            }
        }
    }

    _append_json_event(events, event, 0, 0)

    rendered = str(events)
    assert marker not in rendered
    assert "X-Amz-Signature=[REDACTED]" in rendered


def test_a2a_json_event_accumulator_tracks_cumulative_raw_bytes(
    monkeypatch,
) -> None:
    from ash.commands import a2a as a2a_commands

    monkeypatch.setattr(a2a_commands, "MAX_A2A_CLIENT_OUTPUT_BYTES", 1_000)
    events: list[dict] = []
    current_raw = 0
    current_rendered = 0
    current_raw, current_rendered = a2a_commands._append_json_event(
        events,
        {
            "text": (
                "https://agent.example/cb?X-Amz-Signature=" + "a" * 600
            )
        },
        current_raw,
        current_rendered,
    )

    with pytest.raises(RuntimeError, match="A2A response exceeded"):
        a2a_commands._append_json_event(
            events,
            {"text": "b" * 500},
            current_raw,
            current_rendered,
        )


def test_a2a_status_text_redacts_signed_url() -> None:
    from a2a.types.a2a_pb2 import Part

    from ash.commands.a2a import _bounded_text_parts

    marker = "cli-status-signature-marker"
    text = _bounded_text_parts(
        [
            Part(
                text=(
                    "failed at https://agent.example/cb?"
                    f"X-Amz-Signature={marker}&view=full"
                )
            )
        ],
        64 * 1024,
        "A2A status message",
    )

    assert marker not in text
    assert "X-Amz-Signature=[REDACTED]" in text


@pytest.mark.asyncio
async def test_a2a_send_preserves_remote_error_when_client_close_fails(
    monkeypatch,
) -> None:
    from ash.commands import a2a as a2a_commands

    class FakeHTTP:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class FakeResolver:
        def __init__(self, _http, _url) -> None:
            pass

        async def get_agent_card(self):
            return SimpleNamespace(supported_interfaces=[])

    class FakeClient:
        async def send_message(self, _request):
            raise RuntimeError("primary send failure")
            yield

        async def close(self) -> None:
            raise RuntimeError("client close failure")

    class FakeFactory:
        def __init__(self, _config) -> None:
            pass

        def create(self, _card):
            return FakeClient()

    http = FakeHTTP()
    monkeypatch.setattr(a2a_commands, "_remote_http_client", lambda _args: http)
    monkeypatch.setattr(a2a_commands, "A2ACardResolver", FakeResolver)
    monkeypatch.setattr(a2a_commands, "ClientConfig", lambda **_kwargs: object())
    monkeypatch.setattr(a2a_commands, "ClientFactory", FakeFactory)
    monkeypatch.setattr(a2a_commands, "validate_agent_card_origins", lambda *_: None)
    args = SimpleNamespace(
        url="https://agent.example.com",
        prompt="hello",
        context_id=None,
        token_env="ASH_A2A_TOKEN",
        timeout=30.0,
        json=False,
    )

    with pytest.raises(RuntimeError, match="primary send failure") as captured:
        await a2a_commands.send_a2a(args)

    assert http.closed is True
    assert any(
        "A2A client cleanup failed" in note for note in captured.value.__notes__
    )


@pytest.mark.asyncio
async def test_a2a_send_settles_client_close_before_repeated_cancellation(
    monkeypatch,
) -> None:
    from a2a.types.a2a_pb2 import Part
    from ash.commands import a2a as a2a_commands

    close_started = asyncio.Event()
    close_release = asyncio.Event()
    close_finished = asyncio.Event()

    class FakeHTTP:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class FakeResolver:
        def __init__(self, _http, _url) -> None:
            pass

        async def get_agent_card(self):
            return SimpleNamespace(supported_interfaces=[])

    class FakeEvent:
        message = SimpleNamespace(parts=[Part(text="ok")])

        def HasField(self, field: str) -> bool:
            return field == "message"

    class FakeClient:
        async def send_message(self, _request):
            yield FakeEvent()

        async def close(self) -> None:
            close_started.set()
            await close_release.wait()
            close_finished.set()

    class FakeFactory:
        def __init__(self, _config) -> None:
            pass

        def create(self, _card):
            return FakeClient()

    http = FakeHTTP()
    monkeypatch.setattr(a2a_commands, "_remote_http_client", lambda _args: http)
    monkeypatch.setattr(a2a_commands, "A2ACardResolver", FakeResolver)
    monkeypatch.setattr(a2a_commands, "ClientConfig", lambda **_kwargs: object())
    monkeypatch.setattr(a2a_commands, "ClientFactory", FakeFactory)
    monkeypatch.setattr(a2a_commands, "validate_agent_card_origins", lambda *_: None)
    args = SimpleNamespace(
        url="https://agent.example.com",
        prompt="hello",
        context_id=None,
        token_env="ASH_A2A_TOKEN",
        timeout=30.0,
        json=False,
    )

    call = asyncio.create_task(a2a_commands.send_a2a(args))
    await asyncio.wait_for(close_started.wait(), timeout=1)
    call.cancel()
    call.cancel()
    await asyncio.sleep(0)
    assert not call.done()
    assert not close_finished.is_set()

    close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call, timeout=1)

    assert close_finished.is_set()
    assert http.closed is True
