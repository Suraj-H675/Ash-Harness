from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from ash.providers.readiness import verify_provider_connection


def _config(model: str) -> SimpleNamespace:
    return SimpleNamespace(model=model, custom_providers={})


def test_verify_provider_connection_uses_resolved_openai_route(
    monkeypatch,
) -> None:
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "data": [{"id": "gateway-model"}, {"id": "fast-model"}]
    }
    requests: list[tuple[str, dict[str, str], float]] = []

    def get(endpoint, *, headers, timeout):
        requests.append((endpoint, headers, timeout))
        return response

    monkeypatch.setenv("OPENAI_API_KEY", "gateway-secret")
    monkeypatch.setenv("OPENAI_API_BASE", "http://gateway.example/v1")
    monkeypatch.setattr("ash.providers.readiness.httpx.get", get)

    result = verify_provider_connection(_config("openai/gateway-model"))

    assert result.models == ("gateway-model", "fast-model")
    assert result.selected_model_available is True
    assert requests == [
        (
            "http://gateway.example/v1/models",
            {"Authorization": "Bearer gateway-secret"},
            10.0,
        )
    ]


def test_verify_provider_connection_reports_missing_selected_model(
    monkeypatch,
) -> None:
    response = MagicMock(status_code=200)
    response.json.return_value = {"data": [{"id": "available-model"}]}
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.setattr(
        "ash.providers.readiness.httpx.get",
        lambda endpoint, *, headers, timeout: response,
    )

    result = verify_provider_connection(_config("openai/missing-model"))

    assert result.models == ("available-model",)
    assert result.selected_model_available is False


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
