from __future__ import annotations

from typing import Any, AsyncGenerator

import httpx
import pytest

from ash.config import AshConfig
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.failover import FailoverProvider
from ash.providers.registry import (
    ProviderRegistry,
    create_default_provider_registry,
    parse_model_string,
)


class RegistryProvider(ProviderABC):
    provider_family = "registry-test"

    def __init__(self, model_name: str, marker: str = "") -> None:
        self._model_name = model_name
        self.marker = marker

    @property
    def model_name(self) -> str:
        return self._model_name

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        yield StreamChunk(content=self.marker, is_done=True)


@pytest.mark.parametrize(
    "value",
    ["model-only", "/model", "provider/", "Bad Provider/model"],
)
def test_parse_model_string_rejects_invalid_identifiers(value: str) -> None:
    with pytest.raises(ValueError, match="provider/model"):
        parse_model_string(value)


def test_parse_model_string_normalizes_provider_only() -> None:
    assert parse_model_string(" OpenAI/gpt/custom ") == ("openai", "gpt/custom")


def test_registry_registers_and_builds_provider() -> None:
    registry = ProviderRegistry()
    registry.register(
        "example",
        lambda config, model: RegistryProvider(model, marker=str(config.temperature)),
    )

    provider = registry.build(AshConfig(model="example/model", temperature=0.25))

    assert isinstance(provider, RegistryProvider)
    assert provider.model_name == "model"
    assert provider.marker == "0.25"
    assert registry.names() == ("example",)


def test_registry_preserves_provider_owned_family() -> None:
    registry = ProviderRegistry()
    registry.register("example", lambda config, model: RegistryProvider(model))

    provider = registry.build(AshConfig(model="example/model"))

    assert provider.provider_family == "registry-test"


def test_registry_rejects_duplicates_without_explicit_replace() -> None:
    registry = ProviderRegistry()
    registry.register("example", lambda config, model: RegistryProvider(model))

    with pytest.raises(ValueError, match="already registered"):
        registry.register("example", lambda config, model: RegistryProvider(model))

    registry.register(
        "example",
        lambda config, model: RegistryProvider(model, marker="replacement"),
        replace=True,
    )
    provider = registry.build(AshConfig(model="example/model"))
    assert isinstance(provider, RegistryProvider)
    assert provider.marker == "replacement"


def test_registry_builds_fallback_chain_through_same_factories() -> None:
    registry = ProviderRegistry()
    registry.register("one", lambda config, model: RegistryProvider(model, "one"))
    registry.register("two", lambda config, model: RegistryProvider(model, "two"))

    provider = registry.build(
        AshConfig(model="one/primary", fallback_models=["two/backup"])
    )

    assert isinstance(provider, FailoverProvider)
    assert [item.model_name for item in provider.providers] == ["primary", "backup"]


def test_default_registry_exposes_builtins_without_constructing_them() -> None:
    assert create_default_provider_registry().names() == (
        "anthropic",
        "cerebras",
        "deepseek",
        "fireworks",
        "groq",
        "lmstudio",
        "mistral",
        "ollama",
        "openai",
        "openai-compatible",
        "openrouter",
        "together",
        "vllm",
        "xai",
    )


@pytest.mark.parametrize(
    ("provider", "key", "base_url"),
    [
        ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
        ("mistral", "MISTRAL_API_KEY", "https://api.mistral.ai/v1"),
        ("xai", "XAI_API_KEY", "https://api.x.ai/v1"),
        ("together", "TOGETHER_API_KEY", "https://api.together.xyz/v1"),
        ("fireworks", "FIREWORKS_API_KEY", "https://api.fireworks.ai/inference/v1"),
        ("cerebras", "CEREBRAS_API_KEY", "https://api.cerebras.ai/v1"),
    ],
)
def test_openai_compatible_catalog_providers_build_with_their_route(
    provider: str,
    key: str,
    base_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(key, "test-key")
    config = AshConfig(model=f"{provider}/test-model")

    result = create_default_provider_registry().build(config)

    assert result.model_name == "test-model"
    assert result._base_url == base_url


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("lmstudio", "http://localhost:1234/v1"),
        ("vllm", "http://localhost:8000/v1"),
    ],
)
def test_local_openai_compatible_catalog_providers_are_anonymous(
    provider: str,
    base_url: str,
) -> None:
    result = create_default_provider_registry().build(
        AshConfig(model=f"{provider}/local-model")
    )

    assert result.model_name == "local-model"
    assert result._base_url == base_url
    assert result._api_key == ""
    assert result._client.api_key == "ash-anonymous"


@pytest.mark.asyncio
async def test_custom_anonymous_openai_compatible_provider_builds_without_bearer_auth() -> (
    None
):
    config = AshConfig(
        model="local/local-model",
        custom_providers={
            "local": {
                "base_url": "http://127.0.0.1:8000/v1",
                "auth_mode": "none",
            }
        },
    )

    provider = create_default_provider_registry().build(config)

    assert provider.model_name == "local-model"
    request = httpx.Request("POST", "http://127.0.0.1:8000/v1/chat/completions")
    request.headers["Authorization"] = "Bearer should-not-be-sent"
    hooks = provider._client._client.event_hooks["request"]
    assert len(hooks) == 1
    hooks[0](request)
    assert "Authorization" not in request.headers
    await provider.aclose()


def test_custom_bearer_provider_without_its_key_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands.setup import _has_provider_configured

    monkeypatch.delenv("ASH_PROVIDER_LOCAL_API_KEY", raising=False)
    config = AshConfig(
        model="local/local-model",
        custom_providers={
            "local": {
                "base_url": "http://127.0.0.1:8000/v1",
                "auth_mode": "bearer",
                "key_env": "ASH_PROVIDER_LOCAL_API_KEY",
            }
        },
    )

    assert _has_provider_configured(config) is False
