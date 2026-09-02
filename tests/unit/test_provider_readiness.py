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
    monkeypatch.setenv("OPENAI_API_BASE", "http://gateway.example/v1")

    result = verify_provider_connection(_config("openai/gateway-model"))

    assert result.models == ("gateway-model", "fast-model")
    assert result.selected_model_available is True
    assert len(requests) == 1
    request, timeout = requests[0]
    assert str(request.url) == "http://gateway.example/v1/models"
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


def test_resolve_provider_connection_supports_gateway_key_and_endpoint(
    monkeypatch,
) -> None:
    from ash.providers.readiness import resolve_provider_connection

    monkeypatch.setenv("OPENROUTER_API_KEY", "gateway-key")
    result = resolve_provider_connection(_config("openrouter/test-model"))

    assert result.base_url == "https://openrouter.ai/api/v1"
    assert result.catalog_endpoint == "https://openrouter.ai/api/v1/models"
    assert result.headers == {"Authorization": "Bearer gateway-key"}


def test_resolve_local_openai_compatible_provider_never_requires_a_key(
    monkeypatch,
) -> None:
    from ash.providers.readiness import resolve_provider_connection

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = resolve_provider_connection(_config("lmstudio/local-model"))

    assert result.auth_mode == "none"
    assert result.base_url == "http://localhost:1234/v1"
    assert result.headers == {}
