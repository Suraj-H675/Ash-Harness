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
    assert result.provider_family == provider
    if provider != "openrouter":
        assert result.capabilities.native_tools is False
        assert result.capabilities.vision is False
        assert callable(getattr(result, "detect_capabilities", None))
    assert result._base_url == base_url


def test_openrouter_capabilities_are_not_assumed_from_openai_wire_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    provider = create_default_provider_registry().build(
        AshConfig(model="openrouter/inference-net/schematron-v2-turbo")
    )

    assert provider.provider_family == "openrouter"
    assert provider.capabilities.native_tools is False
    assert provider.capabilities.vision is False
    assert callable(getattr(provider, "detect_capabilities", None))


@pytest.mark.asyncio
async def test_openrouter_negotiates_model_capabilities_from_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.readiness import ProviderModelMetadata

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    provider = create_default_provider_registry().build(
        AshConfig(model="openrouter/vendor/agent")
    )
    snapshots = [
        (
            ProviderModelMetadata(
                model_id="vendor/agent",
                supported_parameters=frozenset({"tools", "reasoning"}),
                input_modalities=frozenset({"text", "image"}),
                context_window=200_000,
                max_output_tokens=8192,
            ),
        ),
        (
            ProviderModelMetadata(
                model_id="vendor/agent",
                supported_parameters=frozenset({"temperature"}),
                input_modalities=frozenset({"text"}),
                context_window=128_000,
                max_output_tokens=4096,
            ),
        ),
    ]
    calls = 0

    def probe(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        result = snapshots[min(calls, len(snapshots) - 1)]
        calls += 1
        return result

    monkeypatch.setattr("ash.providers.openrouter.probe_model_catalog_metadata", probe)

    first = await provider.detect_capabilities()
    cached = await provider.detect_capabilities()
    refreshed = await provider.detect_capabilities(refresh=True)

    assert first == ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=200_000,
        max_output_tokens=8192,
    )
    assert cached == first
    assert refreshed == ProviderCapabilities(
        context_window=128_000,
        max_output_tokens=4096,
    )
    assert calls == 2
    await provider.aclose()


@pytest.mark.asyncio
async def test_openrouter_capability_probe_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.readiness import ProviderVerificationError

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    provider = create_default_provider_registry().build(
        AshConfig(model="openrouter/vendor/unknown")
    )

    def fail(*args, **kwargs):
        del args, kwargs
        raise ProviderVerificationError("catalog offline")

    monkeypatch.setattr("ash.providers.openrouter.probe_model_catalog_metadata", fail)

    with pytest.raises(ProviderVerificationError, match="catalog offline"):
        await provider.detect_capabilities()
    assert provider.capabilities == ProviderCapabilities()
    await provider.aclose()


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
    assert result.provider_family == provider
    assert result.capabilities.native_tools is False
    assert result.capabilities.vision is False
    assert result.capabilities.local is True
    assert callable(getattr(result, "detect_capabilities", None))
    assert result._base_url == base_url
    assert result._api_key == ""
    assert result._client.api_key == "ash-anonymous"


@pytest.mark.asyncio
async def test_vllm_supported_tools_parameter_does_not_enable_native_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.readiness import ProviderModelMetadata

    provider = create_default_provider_registry().build(
        AshConfig(model="vllm/local-model")
    )
    monkeypatch.setattr(
        "ash.providers.openai_compatible.probe_model_catalog_metadata",
        lambda *args, **kwargs: (
            ProviderModelMetadata(
                model_id="local-model",
                supported_parameters=frozenset({"tools"}),
                context_window=32_768,
            ),
        ),
    )

    assert await provider.detect_capabilities() == ProviderCapabilities(
        local=True,
        context_window=32_768,
    )
    await provider.aclose()


@pytest.mark.asyncio
async def test_vllm_explicit_tool_capability_enables_native_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.readiness import ProviderModelMetadata

    provider = create_default_provider_registry().build(
        AshConfig(model="vllm/local-model")
    )
    monkeypatch.setattr(
        "ash.providers.openai_compatible.probe_model_catalog_metadata",
        lambda *args, **kwargs: (
            ProviderModelMetadata(
                model_id="local-model",
                native_tools=True,
                context_window=32_768,
            ),
        ),
    )

    assert await provider.detect_capabilities() == ProviderCapabilities(
        native_tools=True,
        local=True,
        context_window=32_768,
    )
    await provider.aclose()


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
    assert provider.provider_family == "local"
    assert provider.capabilities.native_tools is False
    assert provider.capabilities.vision is False

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert "Authorization" not in request.headers
        body = (
            b'data: {"id":"local","object":"chat.completion.chunk",'
            b'"choices":[{"index":0,"delta":{"content":"works"},'
            b'"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
            request=request,
        )

    # Keep the provider's real AsyncClient and request-hook path, replacing
    # only the network transport with a deterministic local response.
    provider._client._client._transport = httpx.MockTransport(handler)
    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [{"role": "user", "content": "hello"}]
        )
    ]
    assert [chunk.content for chunk in chunks] == ["works"]
    await provider.aclose()


def test_custom_openai_compatible_provider_uses_explicit_model_capabilities() -> None:
    config = AshConfig(
        model="custom/agent-model",
        custom_providers={
            "custom": {
                "base_url": "http://127.0.0.1:8000/v1",
                "auth_mode": "none",
                "model_capabilities": {
                    "agent-model": {
                        "native_tools": True,
                        "vision": True,
                        "reasoning": True,
                        "context_window": 128_000,
                        "max_output_tokens": 8192,
                    }
                },
            }
        },
    )

    provider = create_default_provider_registry().build(config)

    assert provider.capabilities.native_tools is True
    assert provider.capabilities.vision is True
    assert provider.capabilities.reasoning is True
    assert provider.capabilities.context_window == 128_000
    assert provider.capabilities.max_output_tokens == 8192


def test_custom_openai_compatible_provider_rejects_invalid_capability_claim() -> None:
    config = AshConfig(
        model="custom/agent-model",
        custom_providers={
            "custom": {
                "base_url": "http://127.0.0.1:8000/v1",
                "auth_mode": "none",
                "model_capabilities": {
                    "agent-model": {"native_tools": "yes"}
                },
            }
        },
    )

    with pytest.raises(ValueError, match="native_tools.*must be boolean"):
        create_default_provider_registry().build(config)


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


@pytest.mark.asyncio
async def test_mistral_negotiates_capabilities_from_provider_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.readiness import ProviderModelMetadata

    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    provider = create_default_provider_registry().build(
        AshConfig(model="mistral/agent-model-latest")
    )
    monkeypatch.setattr(
        "ash.providers.openai_compatible.probe_model_catalog_metadata",
        lambda *args, **kwargs: (
            ProviderModelMetadata(
                model_id="agent-model-2609",
                aliases=frozenset({"agent-model-latest"}),
                native_tools=True,
                vision=True,
                context_window=131_072,
            ),
        ),
    )

    assert await provider.detect_capabilities() == ProviderCapabilities(
        native_tools=True,
        vision=True,
        context_window=131_072,
    )
    await provider.aclose()


@pytest.mark.asyncio
async def test_xai_combines_context_and_language_modalities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.readiness import ProviderModelMetadata

    monkeypatch.setenv("XAI_API_KEY", "test-key")
    provider = create_default_provider_registry().build(AshConfig(model="xai/latest"))
    calls: list[tuple[str, str]] = []

    def probe(endpoint, *, catalog_format, **kwargs):
        del kwargs
        calls.append((endpoint, catalog_format))
        if catalog_format == "openai":
            return (
                ProviderModelMetadata(
                    model_id="grok-4.3",
                    context_window=131_072,
                ),
            )
        return (
            ProviderModelMetadata(
                model_id="grok-4.3",
                aliases=frozenset({"latest"}),
                input_modalities=frozenset({"text", "image"}),
            ),
        )

    monkeypatch.setattr(
        "ash.providers.openai_compatible.probe_model_catalog_metadata", probe
    )

    assert await provider.detect_capabilities() == ProviderCapabilities(
        vision=True,
        context_window=131_072,
    )
    assert calls == [
        ("https://api.x.ai/v1/models", "openai"),
        ("https://api.x.ai/v1/language-models", "xai"),
    ]
    await provider.aclose()


@pytest.mark.asyncio
async def test_xai_keeps_verified_context_when_language_catalog_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.readiness import ProviderModelMetadata, ProviderVerificationError

    monkeypatch.setenv("XAI_API_KEY", "test-key")
    provider = create_default_provider_registry().build(AshConfig(model="xai/latest"))

    def probe(endpoint, *, catalog_format, **kwargs):
        del endpoint, kwargs
        if catalog_format == "xai":
            raise ProviderVerificationError("language catalog unavailable")
        return (
            ProviderModelMetadata(
                model_id="latest",
                context_window=131_072,
            ),
        )

    monkeypatch.setattr(
        "ash.providers.openai_compatible.probe_model_catalog_metadata", probe
    )

    assert await provider.detect_capabilities() == ProviderCapabilities(
        context_window=131_072
    )
    await provider.aclose()


@pytest.mark.asyncio
async def test_catalog_source_conflict_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.readiness import ProviderModelMetadata

    monkeypatch.setenv("XAI_API_KEY", "test-key")
    provider = create_default_provider_registry().build(AshConfig(model="xai/latest"))
    calls = 0

    def probe(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        return (
            ProviderModelMetadata(
                model_id="latest",
                native_tools=calls == 1,
                supported_parameters=frozenset({"tools"}),
            ),
        )

    monkeypatch.setattr(
        "ash.providers.openai_compatible.probe_model_catalog_metadata", probe
    )

    assert await provider.detect_capabilities() == ProviderCapabilities()
    await provider.aclose()


@pytest.mark.asyncio
async def test_catalog_source_alias_identity_conflict_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.readiness import ProviderModelMetadata

    monkeypatch.setenv("XAI_API_KEY", "test-key")
    provider = create_default_provider_registry().build(AshConfig(model="xai/latest"))
    calls = 0

    def probe(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        if calls == 1:
            return (
                ProviderModelMetadata(
                    model_id="grok-4-a",
                    aliases=frozenset({"latest"}),
                    native_tools=True,
                    context_window=131_072,
                ),
            )
        return (
            ProviderModelMetadata(
                model_id="grok-4-b",
                aliases=frozenset({"latest"}),
                vision=True,
                context_window=65_536,
            ),
        )

    monkeypatch.setattr(
        "ash.providers.openai_compatible.probe_model_catalog_metadata", probe
    )

    assert await provider.detect_capabilities() == ProviderCapabilities()
    assert provider.capabilities == ProviderCapabilities()
    await provider.aclose()


@pytest.mark.asyncio
async def test_catalog_followup_alias_identity_conflict_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.readiness import ProviderModelMetadata

    monkeypatch.setenv("XAI_API_KEY", "test-key")
    provider = create_default_provider_registry().build(AshConfig(model="xai/latest"))
    calls = 0

    def probe(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        if calls == 1:
            return (
                ProviderModelMetadata(
                    model_id="grok-4-a",
                    aliases=frozenset({"latest"}),
                    native_tools=True,
                    context_window=131_072,
                ),
            )
        return (
            ProviderModelMetadata(
                model_id="grok-4-b",
                aliases=frozenset({"grok-4-a"}),
                vision=True,
                max_output_tokens=8192,
            ),
        )

    monkeypatch.setattr(
        "ash.providers.openai_compatible.probe_model_catalog_metadata", probe
    )

    assert await provider.detect_capabilities() == ProviderCapabilities()
    assert provider.capabilities == ProviderCapabilities()
    await provider.aclose()


@pytest.mark.asyncio
async def test_together_negotiates_context_without_assuming_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.readiness import ProviderModelMetadata

    monkeypatch.setenv("TOGETHER_API_KEY", "test-key")
    provider = create_default_provider_registry().build(
        AshConfig(model="together/meta-llama/Llama-3.3-70B-Instruct-Turbo")
    )
    monkeypatch.setattr(
        "ash.providers.openai_compatible.probe_model_catalog_metadata",
        lambda *args, **kwargs: (
            ProviderModelMetadata(
                model_id="meta-llama/Llama-3.3-70B-Instruct-Turbo",
                context_window=131_072,
            ),
        ),
    )

    assert await provider.detect_capabilities() == ProviderCapabilities(
        context_window=131_072
    )
    await provider.aclose()


def test_fireworks_noncanonical_model_does_not_build_management_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-key")
    provider = create_default_provider_registry().build(
        AshConfig(model="fireworks/not/a/canonical/resource/path")
    )

    assert provider.capabilities.native_tools is False
    assert provider.capabilities.vision is False
    assert provider._catalog_endpoint == "https://api.fireworks.ai/inference/v1/models"
    assert provider._catalog_format == "openai"


@pytest.mark.asyncio
async def test_cerebras_uses_public_capability_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.readiness import ProviderModelMetadata

    monkeypatch.setenv("CEREBRAS_API_KEY", "test-key")
    provider = create_default_provider_registry().build(
        AshConfig(model="cerebras/gpt-oss-120b")
    )
    seen: list[tuple[str, dict[str, str]]] = []

    def probe(endpoint, *, headers, **kwargs):
        del kwargs
        seen.append((endpoint, dict(headers)))
        return (
            ProviderModelMetadata(
                model_id="gpt-oss-120b",
                native_tools=True,
                vision=False,
                reasoning=True,
                context_window=131_072,
                max_output_tokens=40_960,
            ),
        )

    monkeypatch.setattr(
        "ash.providers.openai_compatible.probe_model_catalog_metadata", probe
    )

    assert await provider.detect_capabilities() == ProviderCapabilities(
        native_tools=True,
        reasoning=True,
        context_window=131_072,
        max_output_tokens=40_960,
    )
    assert seen == [("https://api.cerebras.ai/public/v1/models", {})]
    await provider.aclose()


@pytest.mark.asyncio
async def test_fireworks_uses_selected_model_management_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.readiness import ProviderModelMetadata

    monkeypatch.setenv("FIREWORKS_API_KEY", "test-key")
    model = "accounts/fireworks/models/kimi-k2-instruct"
    provider = create_default_provider_registry().build(
        AshConfig(model=f"fireworks/{model}")
    )
    seen: list[tuple[str, str]] = []

    def probe(endpoint, *, catalog_format, **kwargs):
        del kwargs
        seen.append((endpoint, catalog_format))
        return (
            ProviderModelMetadata(
                model_id=model,
                native_tools=True,
                vision=True,
                context_window=131_072,
            ),
        )

    monkeypatch.setattr(
        "ash.providers.openai_compatible.probe_model_catalog_metadata", probe
    )

    assert await provider.detect_capabilities() == ProviderCapabilities(
        native_tools=True,
        vision=True,
        context_window=131_072,
    )
    assert seen == [
        (
            "https://api.fireworks.ai/v1/accounts/fireworks/models/kimi-k2-instruct",
            "fireworks",
        )
    ]
    await provider.aclose()


@pytest.mark.asyncio
async def test_catalog_capabilities_do_not_leak_from_another_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.readiness import ProviderModelMetadata

    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
    provider = create_default_provider_registry().build(
        AshConfig(model="mistral/selected-model")
    )
    monkeypatch.setattr(
        "ash.providers.openai_compatible.probe_model_catalog_metadata",
        lambda *args, **kwargs: (
            ProviderModelMetadata(
                model_id="different-model",
                native_tools=True,
                vision=True,
            ),
        ),
    )

    assert await provider.detect_capabilities() == ProviderCapabilities()
    assert provider.capabilities == ProviderCapabilities()
    await provider.aclose()
