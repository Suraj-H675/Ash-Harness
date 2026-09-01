from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


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
    )

    rendered = render_provider_test(verification, json_output=True)

    assert "secret-value" not in rendered
    assert json.loads(rendered)["ok"] is True


def test_main_lists_provider_catalog_without_loading_runtime_config(capsys) -> None:
    from ash.cli import main

    assert main(["providers", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(item["id"] == "openrouter" for item in payload["providers"])


def test_test_provider_rejects_non_positive_timeout() -> None:
    from ash.commands.providers import test_provider

    with pytest.raises(ValueError, match="timeout must be positive"):
        test_provider(SimpleNamespace(), timeout=0)
