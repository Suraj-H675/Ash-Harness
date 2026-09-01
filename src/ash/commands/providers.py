"""Provider catalog and connectivity commands."""

from __future__ import annotations

import json
import os
from io import StringIO
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.table import Table

from ash.provider_catalog import BUILTIN_PROVIDERS
from ash.providers.readiness import (
    ProviderConfigurationError,
    ProviderVerification,
    ProviderVerificationError,
    verify_provider_connection,
)

if TYPE_CHECKING:
    from ash.config import AshConfig


def provider_catalog_payload() -> dict[str, Any]:
    """Return the safe, declarative provider catalog for scripts and UIs."""

    return {
        "providers": [
            {
                "id": descriptor.id,
                "name": descriptor.name,
                "category": descriptor.category,
                "description": descriptor.description,
                "protocol": descriptor.protocol,
                "base_url": descriptor.base_url,
                "auth": "none" if descriptor.local else "api-key",
                "key_env": descriptor.key_env,
                "local": descriptor.local,
            }
            for descriptor in BUILTIN_PROVIDERS
        ]
    }


def render_provider_catalog(*, json_output: bool = False) -> str:
    """Render provider choices without making network requests or loading keys."""

    payload = provider_catalog_payload()
    if json_output:
        return json.dumps(payload, indent=2, sort_keys=True)

    buffer = StringIO()
    console = Console(
        file=buffer,
        no_color=bool(os.environ.get("NO_COLOR") or os.environ.get("ASH_NO_COLOR")),
        soft_wrap=True,
    )
    table = Table(title="Ash provider catalog", header_style="bold cyan")
    table.add_column("Provider", style="bold")
    table.add_column("Route")
    table.add_column("Protocol")
    table.add_column("Authentication")
    table.add_column("Default endpoint")
    for descriptor in BUILTIN_PROVIDERS:
        table.add_row(
            descriptor.id,
            descriptor.category,
            descriptor.protocol,
            "none" if descriptor.local else descriptor.key_env or "api key",
            descriptor.base_url,
        )
    console.print(table)
    return buffer.getvalue().rstrip()


def provider_test_payload(verification: ProviderVerification) -> dict[str, Any]:
    """Return a secret-free connectivity result."""

    connection = verification.connection
    return {
        "provider": connection.provider,
        "model": connection.model_name,
        "endpoint": connection.base_url,
        "catalog_endpoint": connection.catalog_endpoint,
        "authentication": connection.credential_description,
        "discovered_model_count": len(verification.models),
        "discovered_models": list(verification.models),
        "selected_model_available": verification.selected_model_available,
        "ok": verification.selected_model_available,
    }


def render_provider_test(
    verification: ProviderVerification,
    *,
    json_output: bool = False,
) -> str:
    """Render a successful provider verification."""

    payload = provider_test_payload(verification)
    if json_output:
        return json.dumps(payload, indent=2, sort_keys=True)
    selected = "available" if verification.selected_model_available else "not returned"
    connection = verification.connection
    return "\n".join(
        [
            f"Provider: {connection.provider}/{connection.model_name}",
            f"Endpoint: {connection.base_url}",
            f"Authentication: {connection.credential_description}",
            f"Catalog: {len(verification.models)} model(s); selected model {selected}",
            (
                "Result: ready to use"
                if verification.selected_model_available
                else "Result: endpoint is reachable, but the selected model is unavailable"
            ),
        ]
    )


def test_provider(
    config: "AshConfig",
    *,
    model: str | None = None,
    timeout: float = 10.0,
) -> ProviderVerification:
    """Verify one route, optionally overriding the active model for the probe."""

    if timeout <= 0:
        raise ValueError("provider test timeout must be positive")
    test_config = (
        config.model_copy(update={"model": model, "fallback_models": []})
        if model
        else config.model_copy(update={"fallback_models": []})
    )
    return verify_provider_connection(test_config, timeout=timeout)


def provider_test_error(exc: Exception, *, json_output: bool = False) -> str:
    """Render a stable, secret-free provider test failure."""

    message = str(exc)
    if json_output:
        return json.dumps({"ok": False, "error": message}, sort_keys=True)
    return f"Provider test failed: {message}"


__all__ = [
    "ProviderConfigurationError",
    "ProviderVerificationError",
    "provider_catalog_payload",
    "provider_test_error",
    "render_provider_catalog",
    "render_provider_test",
    "test_provider",
]
