from __future__ import annotations

from typing import Any, AsyncGenerator

import pytest

from config import AshConfig
from providers.base import ProviderABC, StreamChunk
from providers.failover import FailoverProvider
from providers.registry import (
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
        "deepseek",
        "groq",
        "ollama",
        "openai",
        "openai-compatible",
    )
