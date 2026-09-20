from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from ash.providers import readiness
from ash.providers.readiness import verify_provider_connection

from .provider_test_helpers import patch_catalog_client


def _config(model: str) -> SimpleNamespace:
    return SimpleNamespace(model=model, custom_providers={})


def test_verify_provider_connection_uses_resolved_openai_route(
    monkeypatch,
) -> None:
    requests = patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"data": [{"id": "gateway-model"}, {"id": "fast-model"}]},
            request=request,
        ),
    )

    monkeypatch.setenv("OPENAI_API_KEY", "gateway-secret")
    monkeypatch.setenv("OPENAI_API_BASE", "https://gateway.example/v1")

    result = verify_provider_connection(_config("openai/gateway-model"))

    assert result.models == ("gateway-model", "fast-model")
    assert result.selected_model_available is True
    assert len(requests) == 1
    request, timeout = requests[0]
    assert str(request.url) == "https://gateway.example/v1/models"
    assert request.headers["authorization"] == "Bearer gateway-secret"
    assert timeout == 10.0


def test_verify_provider_connection_reports_missing_selected_model(
    monkeypatch,
) -> None:
    patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"data": [{"id": "available-model"}]},
            request=request,
        ),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)

    result = verify_provider_connection(_config("openai/missing-model"))

    assert result.models == ("available-model",)
    assert result.selected_model_available is False


def test_provider_runtime_environment_is_provider_scoped(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("OPENAI_API_BASE", "https://openai.example/v1")
    monkeypatch.setenv("GROQ_API_KEY", "groq-secret")
    monkeypatch.setenv("GROQ_API_BASE", "https://groq.example/v1")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-cross")

    config = SimpleNamespace(
        model="openai/main",
        fallback_models=["groq/fallback"],
        custom_providers={},
    )

    assert readiness.provider_runtime_environment(config) == {
        "GROQ_API_BASE": "https://groq.example/v1",
        "GROQ_API_KEY": "groq-secret",
        "OPENAI_API_BASE": "https://openai.example/v1",
        "OPENAI_API_KEY": "openai-secret",
    }


def test_provider_runtime_environment_includes_google_and_nvidia_routes(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "google-secret")
    monkeypatch.setenv(
        "GOOGLE_API_BASE",
        "https://generativelanguage.googleapis.com/v1beta/openai",
    )
    monkeypatch.setenv("NVIDIA_API_KEY", "nvidia-secret")
    monkeypatch.setenv("NVIDIA_API_BASE", "https://integrate.api.nvidia.com/v1")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-cross")

    config = SimpleNamespace(
        model="google/gemini-test",
        fallback_models=["nvidia/vendor/model"],
        custom_providers={},
    )

    assert readiness.provider_runtime_environment(config) == {
        "GEMINI_API_KEY": "google-secret",
        "GOOGLE_API_BASE": "https://generativelanguage.googleapis.com/v1beta/openai",
        "NVIDIA_API_BASE": "https://integrate.api.nvidia.com/v1",
        "NVIDIA_API_KEY": "nvidia-secret",
    }


def test_provider_runtime_environment_honors_custom_key_env(monkeypatch) -> None:
    monkeypatch.setenv("PRIVATE_GATEWAY_TOKEN", "custom-secret")
    monkeypatch.setenv("OTHER_TOKEN", "must-not-cross")
    config = SimpleNamespace(
        model="private/model",
        fallback_models=[],
        custom_providers={
            "private": {
                "base_url": "https://gateway.example/v1",
                "key_env": "PRIVATE_GATEWAY_TOKEN",
            }
        },
    )

    assert readiness.provider_runtime_environment(config) == {
        "PRIVATE_GATEWAY_TOKEN": "custom-secret"
    }


def test_probe_model_catalog_metadata_preserves_openrouter_capability_fields(
    monkeypatch,
) -> None:
    patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "vendor/text-only",
                        "supported_parameters": ["temperature", "max_tokens"],
                        "architecture": {"input_modalities": ["text"]},
                        "context_length": 128_000,
                        "top_provider": {"max_completion_tokens": 4096},
                    },
                    {
                        "id": "vendor/agent",
                        "supported_parameters": ["tools", "reasoning"],
                        "architecture": {"input_modalities": ["text", "image"]},
                        "context_length": 200_000,
                        "top_provider": {"max_completion_tokens": 8192},
                    },
                ]
            },
            request=request,
        ),
    )

    entries = readiness.probe_model_catalog_metadata(
        "https://openrouter.example/v1/models",
        headers={},
        catalog_format="openai",
    )

    assert entries[0].model_id == "vendor/text-only"
    assert entries[0].supported_parameters == frozenset({"temperature", "max_tokens"})
    assert entries[0].input_modalities == frozenset({"text"})
    assert entries[0].context_window == 128_000
    assert entries[0].max_output_tokens == 4096
    assert entries[1].supported_parameters == frozenset({"tools", "reasoning"})
    assert entries[1].input_modalities == frozenset({"text", "image"})



def test_probe_model_catalog_metadata_prefers_serving_provider_context_limit(
    monkeypatch,
) -> None:
    patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "vendor/limited",
                        "context_length": 1_310_720,
                        "top_provider": {
                            "context_length": 262_144,
                            "max_completion_tokens": 8192,
                        },
                    }
                ]
            },
            request=request,
        ),
    )

    (entry,) = readiness.probe_model_catalog_metadata(
        "https://openrouter.example/v1/models",
        headers={},
        catalog_format="openai",
    )

    assert entry.context_window == 262_144
    assert entry.max_output_tokens == 8192

def test_probe_model_catalog_rejects_oversized_stream(
    monkeypatch,
) -> None:
    monkeypatch.setattr(readiness, "MAX_PROVIDER_CATALOG_BYTES", 32)
    patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(200, content=b"x" * 33, request=request),
    )

    with pytest.raises(
        readiness.ProviderVerificationError,
        match="larger than 2 MB",
    ):
        readiness.probe_model_catalog(
            "https://gateway.example/v1/models",
            headers={},
            catalog_format="openai",
        )


def test_probe_model_catalog_rejects_invalid_content_length(monkeypatch) -> None:
    patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            content=b'{"data": [{"id": "model"}]}',
            headers={"Content-Length": "not-a-number"},
            request=request,
        ),
    )

    with pytest.raises(
        readiness.ProviderVerificationError,
        match="invalid Content-Length",
    ):
        readiness.probe_model_catalog(
            "https://gateway.example/v1/models",
            headers={},
            catalog_format="openai",
        )


def test_probe_model_catalog_rejects_duplicate_json_keys(monkeypatch) -> None:
    patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            content=(
                b'{"data":[{"id":"first"}],'
                b'"data":[{"id":"second"}]}'
            ),
            request=request,
        ),
    )

    with pytest.raises(readiness.ProviderVerificationError, match="verification failed"):
        readiness.probe_model_catalog(
            "https://gateway.example/v1/models",
            headers={},
            catalog_format="openai",
        )


def test_resolve_provider_connection_supports_gateway_key_and_endpoint(
    monkeypatch,
) -> None:
    from ash.providers.readiness import resolve_provider_connection

    monkeypatch.setenv("OPENROUTER_API_KEY", "gateway-key")
    result = resolve_provider_connection(_config("openrouter/test-model"))

    assert result.base_url == "https://openrouter.ai/api/v1"
    assert result.catalog_endpoint == "https://openrouter.ai/api/v1/models"
    assert result.headers == {"Authorization": "Bearer gateway-key"}


def test_resolve_provider_connection_rejects_plaintext_remote_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.readiness import (
        ProviderConfigurationError,
        resolve_provider_connection,
    )

    monkeypatch.setenv("OPENAI_API_KEY", "gateway-secret")
    monkeypatch.setenv("OPENAI_API_BASE", "http://gateway.example/v1")

    with pytest.raises(ProviderConfigurationError, match="must use HTTPS"):
        resolve_provider_connection(_config("openai/gateway-model"))


@pytest.mark.parametrize(
    "base_url",
    [
        "https://gateway.example:not-a-port/v1",
        "https://gateway.example:99999/v1",
        "https://gateway.example:0/v1",
    ],
)
def test_resolve_provider_connection_rejects_invalid_ports(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    from ash.providers.readiness import (
        ProviderConfigurationError,
        resolve_provider_connection,
    )

    monkeypatch.setenv("OPENAI_API_KEY", "gateway-secret")
    monkeypatch.setenv("OPENAI_API_BASE", base_url)

    with pytest.raises(ProviderConfigurationError, match="base URL"):
        resolve_provider_connection(_config("openai/gateway-model"))


def test_resolve_provider_connection_allows_loopback_http_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.readiness import resolve_provider_connection

    monkeypatch.setenv("OPENAI_API_KEY", "local-secret")
    monkeypatch.setenv("OPENAI_API_BASE", "http://127.0.0.1:8080/v1")

    result = resolve_provider_connection(_config("openai/local-model"))

    assert result.base_url == "http://127.0.0.1:8080/v1"
    assert result.headers == {"Authorization": "Bearer local-secret"}


def test_probe_model_catalog_refuses_plaintext_credentials_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_client(*args, **kwargs):
        raise AssertionError("network client must not be constructed")

    monkeypatch.setattr(readiness.httpx, "Client", fail_client)

    with pytest.raises(readiness.ProviderVerificationError, match="must use HTTPS"):
        readiness.probe_model_catalog(
            "http://gateway.example/v1/models",
            headers={"Authorization": "Bearer gateway-secret"},
            catalog_format="openai",
        )


def test_resolve_local_openai_compatible_provider_never_requires_a_key(
    monkeypatch,
) -> None:
    from ash.providers.readiness import resolve_provider_connection

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = resolve_provider_connection(_config("lmstudio/local-model"))

    assert result.auth_mode == "none"
    assert result.base_url == "http://localhost:1234/v1"
    assert result.headers == {}


def test_probe_model_catalog_metadata_preserves_mistral_capabilities(monkeypatch) -> None:
    patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"data": [{
                "id": "mistral-agent-2609",
                "aliases": ["mistral-agent", "mistral-agent-latest"],
                "capabilities": {"function_calling": True, "vision": True},
                "max_context_length": 262_144,
            }]},
            request=request,
        ),
    )

    (entry,) = readiness.probe_model_catalog_metadata(
        "https://api.mistral.ai/v1/models",
        headers={},
        catalog_format="openai",
    )

    assert entry.native_tools is True
    assert entry.vision is True
    assert entry.reasoning is None
    assert entry.context_window == 262_144


def test_probe_cerebras_public_catalog_preserves_capabilities(monkeypatch) -> None:
    patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "gpt-oss-120b",
                        "capabilities": {
                            "function_calling": True,
                            "vision": False,
                            "reasoning": True,
                            "tools": True,
                        },
                        "limits": {
                            "max_context_length": 131_072,
                            "max_completion_tokens": 40_960,
                        },
                    }
                ]
            },
            request=request,
        ),
    )

    (entry,) = readiness.probe_model_catalog_metadata(
        "https://api.cerebras.ai/public/v1/models",
        headers={},
        catalog_format="openai",
    )

    assert entry.model_id == "gpt-oss-120b"
    assert entry.native_tools is True
    assert entry.vision is False
    assert entry.reasoning is True
    assert entry.context_window == 131_072
    assert entry.max_output_tokens == 40_960


def test_probe_together_catalog_preserves_context_length(monkeypatch) -> None:
    patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json=[
                {
                    "id": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
                    "type": "chat",
                    "context_length": 131_072,
                }
            ],
            request=request,
        ),
    )

    (entry,) = readiness.probe_model_catalog_metadata(
        "https://api.together.xyz/v1/models",
        headers={},
        catalog_format="together",
    )

    assert entry.model_id == "meta-llama/Llama-3.3-70B-Instruct-Turbo"
    assert entry.context_window == 131_072
    assert entry.native_tools is None
    assert entry.vision is None


def test_probe_xai_language_catalog_preserves_modalities(monkeypatch) -> None:
    patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "models": [
                    {
                        "id": "grok-4.3",
                        "aliases": ["latest"],
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                    }
                ]
            },
            request=request,
        ),
    )

    (entry,) = readiness.probe_model_catalog_metadata(
        "https://api.x.ai/v1/language-models",
        headers={},
        catalog_format="xai",
    )

    assert entry.model_id == "grok-4.3"
    assert entry.aliases == frozenset({"latest"})
    assert entry.input_modalities == frozenset({"text", "image"})


def test_probe_fireworks_model_preserves_capabilities(monkeypatch) -> None:
    patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "name": "accounts/fireworks/models/kimi-k2-instruct",
                "contextLength": 131_072,
                "supportsImageInput": True,
                "supportsTools": True,
            },
            request=request,
        ),
    )

    (entry,) = readiness.probe_model_catalog_metadata(
        "https://api.fireworks.ai/v1/accounts/fireworks/models/kimi-k2-instruct",
        headers={},
        catalog_format="fireworks",
    )

    assert entry.model_id == "accounts/fireworks/models/kimi-k2-instruct"
    assert entry.native_tools is True
    assert entry.vision is True
    assert entry.context_window == 131_072


def test_probe_lmstudio_catalog_preserves_model_capabilities(monkeypatch) -> None:
    patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"models": [
                {
                    "type": "llm",
                    "key": "local-agent",
                    "loaded_instances": [
                        {"id": "a", "config": {"context_length": 8192}},
                        {"id": "b", "config": {"context_length": 4096}},
                    ],
                    "max_context_length": 131_072,
                    "capabilities": {
                        "vision": True,
                        "trained_for_tool_use": True,
                        "reasoning": {
                            "allowed_options": ["off", "low", "high"],
                            "default": "low",
                        },
                    },
                },
                {
                    "type": "embedding",
                    "key": "embedding-model",
                    "max_context_length": 2048,
                },
            ]},
            request=request,
        ),
    )

    (entry,) = readiness.probe_model_catalog_metadata(
        "http://localhost:1234/api/v1/models",
        headers={},
        catalog_format="lmstudio",
    )

    assert entry.model_id == "local-agent"
    assert entry.native_tools is True
    assert entry.vision is True
    assert entry.reasoning is True
    assert entry.context_window == 4096


def test_probe_vllm_catalog_keeps_tools_unknown_but_served_context(monkeypatch) -> None:
    patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"data": [{"id": "served-model", "max_model_len": 32_768}]},
            request=request,
        ),
    )

    (entry,) = readiness.probe_model_catalog_metadata(
        "http://localhost:8000/v1/models",
        headers={},
        catalog_format="openai",
    )

    assert entry.native_tools is None
    assert entry.vision is None
    assert entry.reasoning is None
    assert entry.context_window == 32_768


def test_together_connection_uses_native_catalog_shape(monkeypatch) -> None:
    monkeypatch.setenv("TOGETHER_API_KEY", "test-key")

    connection = readiness.resolve_provider_connection(
        _config("together/meta-llama/Llama-3.3-70B-Instruct-Turbo")
    )

    assert connection.catalog_format == "together"
    assert connection.catalog_endpoint == "https://api.together.xyz/v1/models"


@pytest.mark.parametrize(
    ("model", "key_env", "base_url"),
    [
        (
            "google/gemini-test",
            "GEMINI_API_KEY",
            "https://generativelanguage.googleapis.com/v1beta/openai",
        ),
        (
            "nvidia/nvidia/test-model",
            "NVIDIA_API_KEY",
            "https://integrate.api.nvidia.com/v1",
        ),
    ],
)
def test_new_openai_compatible_provider_connections_use_bearer_catalog(
    model: str,
    key_env: str,
    base_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(key_env, "test-secret")

    connection = readiness.resolve_provider_connection(_config(model))

    assert connection.base_url == base_url
    assert connection.catalog_endpoint == f"{base_url}/models"
    assert connection.catalog_format == "openai"
    assert connection.auth_mode == "bearer"
    expected_headers = {"Authorization": "Bearer test-secret"}
    if model.startswith("google/"):
        expected_headers["x-goog-api-client"] = readiness.GOOGLE_API_CLIENT_HEADER
    assert connection.headers == expected_headers


def test_google_catalog_request_identifies_ash(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"data": [{"id": "gemini-test"}]},
            request=request,
        ),
    )
    monkeypatch.setenv("GEMINI_API_KEY", "google-secret")

    result = verify_provider_connection(_config("google/gemini-test"))

    assert result.selected_model_available is True
    assert len(requests) == 1
    request, _ = requests[0]
    assert request.headers["authorization"] == "Bearer google-secret"
    assert (
        request.headers["x-goog-api-client"]
        == readiness.GOOGLE_API_CLIENT_HEADER
    )


@pytest.mark.parametrize(
    ("model", "key_env", "base_env"),
    [
        ("google/gemini-test", "GEMINI_API_KEY", "GOOGLE_API_BASE"),
        ("nvidia/nvidia/test-model", "NVIDIA_API_KEY", "NVIDIA_API_BASE"),
    ],
)
def test_new_openai_compatible_provider_base_url_overrides(
    model: str,
    key_env: str,
    base_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(key_env, "test-secret")
    monkeypatch.setenv(base_env, "https://gateway.example/v1")

    connection = readiness.resolve_provider_connection(_config(model))

    assert connection.base_url == "https://gateway.example/v1"
    assert connection.catalog_endpoint == "https://gateway.example/v1/models"
    assert connection.uses_default_base_url is False


def test_lmstudio_connection_uses_native_capability_catalog() -> None:
    connection = readiness.resolve_provider_connection(_config("lmstudio/local-model"))

    assert connection.catalog_format == "lmstudio"
    assert connection.catalog_endpoint == "http://localhost:1234/api/v1/models"


def test_catalog_metadata_does_not_promote_malformed_capability_values(monkeypatch) -> None:
    patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"data": [{
                "id": "malformed",
                "capabilities": {
                    "function_calling": "true",
                    "vision": 1,
                    "reasoning": "yes",
                },
                "supported_parameters": {"tools": "yes", "temperature": True},
            }]},
            request=request,
        ),
    )

    (entry,) = readiness.probe_model_catalog_metadata(
        "https://provider.example/v1/models", headers={}, catalog_format="openai"
    )
    assert entry.native_tools is None
    assert entry.vision is None
    assert entry.reasoning is None
    assert entry.supported_parameters == frozenset({"temperature"})



def test_select_provider_model_metadata_prefers_exact_id_over_alias_collision() -> None:
    exact = readiness.ProviderModelMetadata(model_id="model-a")
    alias_collision = readiness.ProviderModelMetadata(
        model_id="model-b",
        aliases=frozenset({"model-a"}),
    )

    selected = readiness.select_provider_model_metadata(
        (alias_collision, exact),
        "model-a",
    )

    assert selected is exact


def test_select_provider_model_metadata_rejects_ambiguous_alias() -> None:
    catalog = (
        readiness.ProviderModelMetadata(
            model_id="model-a", aliases=frozenset({"latest"})
        ),
        readiness.ProviderModelMetadata(
            model_id="model-b", aliases=frozenset({"latest"})
        ),
    )

    assert readiness.select_provider_model_metadata(catalog, "latest") is None


def test_verify_provider_connection_accepts_unique_model_alias(monkeypatch) -> None:
    patch_catalog_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"data": [{
                "id": "mistral-large-2609",
                "aliases": ["mistral-large-latest"],
            }]},
            request=request,
        ),
    )
    monkeypatch.setenv("MISTRAL_API_KEY", "test-key")

    result = readiness.verify_provider_connection(
        _config("mistral/mistral-large-latest")
    )

    assert result.models == ("mistral-large-2609",)
    assert result.selected_model_available is True
