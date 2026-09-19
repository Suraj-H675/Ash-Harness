from __future__ import annotations

import asyncio
import json
import math
from types import SimpleNamespace

import pytest

from ash.providers.base import CanonicalToolCall, ProviderABC, StreamChunk
from ash.providers.readiness import ProviderConnection, ProviderVerification


def _provider_command_config() -> SimpleNamespace:
    config = SimpleNamespace(
        model="openrouter/test-model",
        fallback_models=[],
    )
    config.model_copy = lambda *, update: SimpleNamespace(
        **{
            **config.__dict__,
            **update,
            "model_copy": config.model_copy,
        }
    )
    return config


def _provider_verification(*, selected: bool = True) -> ProviderVerification:
    return ProviderVerification(
        connection=ProviderConnection(
            provider="openrouter",
            model_name="test-model",
            base_url="https://openrouter.ai/api/v1",
            catalog_endpoint="https://openrouter.ai/api/v1/models",
            catalog_format="openai",
            auth_mode="bearer",
            api_key="secret-value",
        ),
        models=("test-model",) if selected else ("other-model",),
        selected_model_available=selected,
    )


class _ScriptedProbeProvider(ProviderABC):
    model_name = "test-model"

    def __init__(
        self,
        *,
        chunks: list[StreamChunk] | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.chunks = list(chunks or [])
        self.error = error
        self.delay = delay
        self.max_tokens: int | None = None
        self.closed = False
        self.messages = None
        self.tools = None

    def configure_max_tokens(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.messages = messages
        self.tools = tools
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _install_probe_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider: ProviderABC,
) -> None:
    class Registry:
        def build(self, _config):
            return provider

    monkeypatch.setattr(
        "ash.providers.registry.get_provider_registry",
        lambda: Registry(),
    )


def test_provider_catalog_is_secret_free_and_includes_local_and_gateway_routes() -> None:
    from ash.commands.providers import provider_catalog_payload, render_provider_catalog

    payload = provider_catalog_payload()
    provider_ids = {item["id"] for item in payload["providers"]}

    assert {"openrouter", "lmstudio", "vllm"} <= provider_ids
    assert all("API_KEY" not in json.dumps(item) or item["key_env"] for item in payload["providers"])
    rendered = render_provider_catalog()
    assert "Ash provider catalog" in rendered
    assert "openrouter" in rendered
    assert "lmstudio" in rendered


def test_provider_test_rendering_never_includes_credentials() -> None:
    from ash.commands.providers import render_provider_test
    from ash.providers.readiness import ProviderConnection, ProviderVerification

    verification = ProviderVerification(
        connection=ProviderConnection(
            provider="openrouter",
            model_name="test-model",
            base_url="https://openrouter.ai/api/v1",
            catalog_endpoint="https://openrouter.ai/api/v1/models",
            catalog_format="openai",
            auth_mode="bearer",
            api_key="secret-value",
        ),
        models=("test-model",),
        selected_model_available=True,
        completion_attempted=True,
        completion_verified=True,
    )

    rendered = render_provider_test(verification, json_output=True)

    assert "secret-value" not in rendered
    assert json.loads(rendered)["ok"] is True


def test_provider_test_does_not_report_ready_when_completion_probe_fails(
    monkeypatch,
) -> None:
    from ash.commands import providers

    verification = _provider_verification()
    monkeypatch.setattr(
        providers,
        "verify_provider_connection",
        lambda _config, *, timeout: verification,
    )

    provider = _ScriptedProbeProvider(
        error=RuntimeError("completion endpoint rejected request")
    )
    _install_probe_provider(monkeypatch, provider)
    config = _provider_command_config()

    result = providers.test_provider(config, timeout=1.0)
    payload = providers.provider_test_payload(result)

    assert result.completion_attempted is True
    assert result.completion_verified is False
    assert "completion endpoint rejected request" in (result.completion_error or "")
    assert payload["ok"] is False
    assert provider.max_tokens == 8
    assert provider.closed is True


def test_provider_test_reports_ready_after_real_completion_probe(monkeypatch) -> None:
    from ash.commands import providers

    verification = _provider_verification()
    monkeypatch.setattr(
        providers,
        "verify_provider_connection",
        lambda _config, *, timeout: verification,
    )

    provider = _ScriptedProbeProvider(
        chunks=[
            StreamChunk(content="OK"),
            StreamChunk(is_done=True, stop_reason="stop"),
        ]
    )
    _install_probe_provider(monkeypatch, provider)

    result = providers.test_provider(_provider_command_config(), timeout=1.0)

    assert result.completion_attempted is True
    assert result.completion_verified is True
    assert result.completion_error is None
    assert result.ready_to_use is True
    assert providers.provider_test_payload(result)["ok"] is True
    assert provider.max_tokens == 8
    assert provider.closed is True
    assert provider.tools is None
    assert provider.messages == [{"role": "user", "content": "Reply with exactly OK."}]


def test_provider_test_skips_completion_when_selected_model_is_missing(
    monkeypatch,
) -> None:
    from ash.commands import providers

    verification = _provider_verification(selected=False)
    monkeypatch.setattr(
        providers,
        "verify_provider_connection",
        lambda _config, *, timeout: verification,
    )

    def fail_registry():
        raise AssertionError("completion provider must not be built")

    monkeypatch.setattr(
        "ash.providers.registry.get_provider_registry",
        fail_registry,
    )

    result = providers.test_provider(_provider_command_config(), timeout=1.0)

    assert result.selected_model_available is False
    assert result.completion_attempted is False
    assert result.completion_verified is False
    assert result.ready_to_use is False


@pytest.mark.parametrize(
    ("chunks", "error_fragment"),
    [
        ([StreamChunk(content="OK"), StreamChunk(is_done=True, stop_reason="length")], "length"),
        ([StreamChunk(is_done=True, stop_reason="content_filter")], "content_filter"),
        ([StreamChunk(content="partial")], "before a terminal completion"),
        (
            [
                StreamChunk(content="OK", is_done=True, stop_reason="stop"),
                StreamChunk(content="late output"),
            ],
            "output after its terminal completion",
        ),
        (
            [
                StreamChunk(
                    native_tool_calls=[
                        CanonicalToolCall(
                            call_id="unexpected-call",
                            name="read_file",
                            arguments={"file_path": "x"},
                        )
                    ]
                )
            ],
            "unexpected tool call",
        ),
    ],
)
def test_provider_test_rejects_nonfinal_or_tool_completion_streams(
    monkeypatch,
    chunks: list[StreamChunk],
    error_fragment: str,
) -> None:
    from ash.commands import providers

    verification = _provider_verification()
    monkeypatch.setattr(
        providers,
        "verify_provider_connection",
        lambda _config, *, timeout: verification,
    )

    provider = _ScriptedProbeProvider(chunks=chunks)
    _install_probe_provider(monkeypatch, provider)

    result = providers.test_provider(_provider_command_config(), timeout=1.0)

    assert result.completion_attempted is True
    assert result.completion_verified is False
    assert error_fragment in (result.completion_error or "")
    assert result.ready_to_use is False


def test_provider_completion_probe_obeys_timeout_and_closes(monkeypatch) -> None:
    from ash.commands import providers

    verification = _provider_verification()
    monkeypatch.setattr(
        providers,
        "verify_provider_connection",
        lambda _config, *, timeout: verification,
    )
    provider = _ScriptedProbeProvider(delay=0.1)
    _install_probe_provider(monkeypatch, provider)

    result = providers.test_provider(_provider_command_config(), timeout=0.01)

    assert result.completion_attempted is True
    assert result.completion_verified is False
    assert result.completion_error == "provider completion probe timed out"
    assert provider.closed is True


def test_provider_completion_error_redacts_connection_secret(monkeypatch) -> None:
    from ash.commands import providers

    verification = _provider_verification()
    monkeypatch.setattr(
        providers,
        "verify_provider_connection",
        lambda _config, *, timeout: verification,
    )

    provider = _ScriptedProbeProvider(
        error=RuntimeError("upstream echoed secret-value")
    )
    _install_probe_provider(monkeypatch, provider)

    result = providers.test_provider(_provider_command_config(), timeout=1.0)
    rendered = providers.render_provider_test(result, json_output=True)

    assert "secret-value" not in (result.completion_error or "")
    assert "secret-value" not in rendered
    assert "[REDACTED]" in rendered


def test_provider_test_human_rendering_distinguishes_completion_failure() -> None:
    from ash.commands.providers import render_provider_test

    verification = ProviderVerification(
        **{
            **_provider_verification().__dict__,
            "completion_attempted": True,
            "completion_verified": False,
            "completion_error": "chat endpoint failed",
        }
    )

    rendered = render_provider_test(verification)

    assert "Catalog: 1 model(s); selected model available" in rendered
    assert "Completion: failed: chat endpoint failed" in rendered
    assert "model is available, but a completion could not be verified" in rendered


@pytest.mark.parametrize("json_output", [False, True])
def test_provider_test_error_redacts_credentials(json_output: bool) -> None:
    from ash.commands.providers import provider_test_error

    secret = "verylongprovidersecret"
    rendered = provider_test_error(
        RuntimeError(f"upstream failed api_key={secret}"),
        json_output=json_output,
    )

    assert secret not in rendered
    assert "[REDACTED]" in rendered


def test_provider_test_error_sanitizes_human_terminal_controls() -> None:
    from ash.commands.providers import provider_test_error

    rendered = provider_test_error(
        RuntimeError("bad\nerror\x1b[2J\u202ehidden\u202c"),
        json_output=False,
    )

    assert "bad\\x0aerror\\x1b[2J\\u202ehidden\\u202c" in rendered
    assert "bad\nerror" not in rendered
    assert "\x1b[2J" not in rendered
    assert "\u202e" not in rendered


def test_main_lists_provider_catalog_without_loading_runtime_config(capsys) -> None:
    from ash.cli import main

    assert main(["providers", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(item["id"] == "openrouter" for item in payload["providers"])


@pytest.mark.parametrize(
    ("completion_verified", "expected_exit"),
    [(False, 1), (True, 0)],
)
def test_main_provider_test_exit_requires_verified_completion(
    monkeypatch,
    capsys,
    completion_verified: bool,
    expected_exit: int,
) -> None:
    import ash.cli as cli
    from ash.commands import providers

    verification = ProviderVerification(
        **{
            **_provider_verification().__dict__,
            "completion_attempted": True,
            "completion_verified": completion_verified,
            "completion_error": None if completion_verified else "chat failed",
        }
    )
    monkeypatch.setattr(
        cli,
        "_load_config_or_report",
        lambda **_kwargs: (_provider_command_config(), 0),
    )
    monkeypatch.setattr(
        providers,
        "test_provider",
        lambda _config, *, model=None, timeout=10.0: verification,
    )

    assert cli.main(["providers", "test", "--json"]) == expected_exit
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_model_available"] is True
    assert payload["completion_verified"] is completion_verified
    assert payload["ok"] is completion_verified


@pytest.mark.parametrize("timeout", [0.0, math.nan, math.inf, -math.inf])
def test_test_provider_rejects_invalid_timeout(timeout: float) -> None:
    from ash.commands.providers import test_provider

    with pytest.raises(ValueError, match="timeout must be positive and finite"):
        test_provider(SimpleNamespace(), timeout=timeout)
