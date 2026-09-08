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
