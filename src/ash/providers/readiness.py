"""Resolve the concrete provider connection used by Ash at runtime.

Provider setup, diagnostics, and provider construction must agree on the
destination that receives credentials.  This module deliberately contains no
network I/O; it turns the selected model plus user-owned configuration into a
validated, immutable connection description.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from ash.providers.identifiers import parse_model_string

if TYPE_CHECKING:
    from ash.config import AshConfig


class ProviderConfigurationError(ValueError):
    """Raised when the selected provider cannot be constructed safely."""


CatalogFormat = Literal["openai", "anthropic", "ollama"]
AuthMode = Literal["bearer", "anthropic", "none"]


@dataclass(frozen=True)
class ProviderConnection:
    """The exact endpoint, authentication, and catalog contract for a model."""

    provider: str
    model_name: str
    base_url: str
    catalog_endpoint: str
    catalog_format: CatalogFormat
    auth_mode: AuthMode
    api_key: str = ""
    uses_default_base_url: bool = False

    @property
    def headers(self) -> dict[str, str]:
        if self.auth_mode == "bearer" and self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        if self.auth_mode == "anthropic" and self.api_key:
            return {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            }
        return {}

    @property
    def credential_description(self) -> str:
        if self.auth_mode == "none":
            return "no API key is required"
        return "API key is configured"


_BUILTIN_CONNECTIONS: dict[str, tuple[str, str, CatalogFormat, AuthMode]] = {
    "anthropic": (
        "https://api.anthropic.com",
        "ANTHROPIC_API_BASE",
        "anthropic",
        "anthropic",
    ),
    "openai": (
        "https://api.openai.com/v1",
        "OPENAI_API_BASE",
        "openai",
        "bearer",
    ),
    "openai-compatible": (
        "https://api.openai.com/v1",
        "OPENAI_API_BASE",
        "openai",
        "bearer",
    ),
    "deepseek": (
        "https://api.deepseek.com/v1",
        "DEEPSEEK_API_BASE",
        "openai",
        "bearer",
    ),
    "groq": (
        "https://api.groq.com/openai/v1",
        "GROQ_API_BASE",
        "openai",
        "bearer",
    ),
}

_BUILTIN_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-compatible": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "groq": "GROQ_API_KEY",
}


def _normalize_base_url(value: object, *, provider: str) -> str:
    base_url = str(value or "").strip().rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderConfigurationError(
            f"provider {provider!r} needs an absolute http:// or https:// base URL"
        )
    if parsed.username or parsed.password:
        raise ProviderConfigurationError(
            f"provider {provider!r} base URL must not contain credentials"
        )
    if parsed.query or parsed.fragment:
        raise ProviderConfigurationError(
            f"provider {provider!r} base URL must not contain a query or fragment"
        )
    return base_url


def _catalog_endpoint(base_url: str, catalog_format: CatalogFormat) -> str:
    if catalog_format == "ollama":
        return f"{base_url}/api/tags"
    if catalog_format == "anthropic":
        return (
            f"{base_url}/models"
            if base_url.endswith("/v1")
            else f"{base_url}/v1/models"
        )
    return f"{base_url}/models"


def _require_key(provider: str, key_env: str, key: str) -> str:
    if key:
        return key
    raise ProviderConfigurationError(
        f"provider {provider!r} requires {key_env}; run 'ash setup'"
    )


def resolve_provider_connection(config: "AshConfig") -> ProviderConnection:
    """Resolve the provider route used for construction and connectivity checks.

    Custom endpoints declare authentication explicitly.  Older entries that
    have a key environment variable retain bearer authentication; entries
    without one are treated as intentionally anonymous.
    """

    provider, model_name = parse_model_string(config.model)
    if provider == "ollama":
        default = "http://localhost:11434"
        supplied = os.environ.get("OLLAMA_API_BASE")
        base_url = _normalize_base_url(supplied or default, provider=provider)
        return ProviderConnection(
            provider=provider,
            model_name=model_name,
            base_url=base_url,
            catalog_endpoint=_catalog_endpoint(base_url, "ollama"),
            catalog_format="ollama",
            auth_mode="none",
            uses_default_base_url=not bool(supplied),
        )

    builtin = _BUILTIN_CONNECTIONS.get(provider)
    if builtin is not None:
        default, base_env, catalog_format, auth_mode = builtin
        supplied = os.environ.get(base_env)
        base_url = _normalize_base_url(supplied or default, provider=provider)
        key_env = _BUILTIN_KEY_ENV[provider]
        api_key = _require_key(provider, key_env, os.environ.get(key_env, ""))
        return ProviderConnection(
            provider=provider,
            model_name=model_name,
            base_url=base_url,
            catalog_endpoint=_catalog_endpoint(base_url, catalog_format),
            catalog_format=catalog_format,
            auth_mode=auth_mode,
            api_key=api_key,
            uses_default_base_url=not bool(supplied),
        )

    custom_providers = getattr(config, "custom_providers", {})
    custom = (
        custom_providers.get(provider) if isinstance(custom_providers, dict) else None
    )
    if not isinstance(custom, dict):
        raise ProviderConfigurationError(f"unknown provider {provider!r}")
    base_url = _normalize_base_url(custom.get("base_url"), provider=provider)
    key_env = str(custom.get("key_env") or "").strip()
    inline_key = str(custom.get("api_key") or "")
    declared_auth = str(custom.get("auth_mode") or "").strip().casefold()
    if not declared_auth:
        declared_auth = "bearer" if key_env or inline_key else "none"
    if declared_auth not in {"bearer", "none"}:
        raise ProviderConfigurationError(
            f"custom provider {provider!r} auth_mode must be 'bearer' or 'none'"
        )
    if declared_auth == "bearer":
        custom_auth_mode: AuthMode = "bearer"
        source = key_env or "configured API key"
        api_key = _require_key(
            provider, source, os.environ.get(key_env, "") or inline_key
        )
    else:
        custom_auth_mode = "none"
        api_key = ""
    return ProviderConnection(
        provider=provider,
        model_name=model_name,
        base_url=base_url,
        catalog_endpoint=_catalog_endpoint(base_url, "openai"),
        catalog_format="openai",
        auth_mode=custom_auth_mode,
        api_key=api_key,
    )
