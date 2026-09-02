"""Resolve the concrete provider connection used by Ash at runtime.

Provider setup, diagnostics, and provider construction must agree on the
destination that receives credentials.  This module deliberately contains no
network I/O; it turns the selected model plus user-owned configuration into a
validated, immutable connection description.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Mapping
from urllib.parse import urlsplit

import httpx

from ash.providers.identifiers import parse_model_string

if TYPE_CHECKING:
    from ash.config import AshConfig


class ProviderConfigurationError(ValueError):
    """Raised when the selected provider cannot be constructed safely."""


CatalogFormat = Literal["openai", "anthropic", "ollama"]
AuthMode = Literal["bearer", "anthropic", "none"]
MAX_PROVIDER_CATALOG_BYTES = 2_000_000
MAX_PROVIDER_ERROR_BYTES = 64 * 1024


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


class ProviderVerificationError(RuntimeError):
    """Raised when a provider catalog cannot be verified safely."""


@dataclass(frozen=True)
class ProviderVerification:
    connection: ProviderConnection
    models: tuple[str, ...]
    selected_model_available: bool


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
    "openrouter": (
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_BASE",
        "openai",
        "bearer",
    ),
    "mistral": (
        "https://api.mistral.ai/v1",
        "MISTRAL_API_BASE",
        "openai",
        "bearer",
    ),
    "xai": (
        "https://api.x.ai/v1",
        "XAI_API_BASE",
        "openai",
        "bearer",
    ),
    "together": (
        "https://api.together.xyz/v1",
        "TOGETHER_API_BASE",
        "openai",
        "bearer",
    ),
    "fireworks": (
        "https://api.fireworks.ai/inference/v1",
        "FIREWORKS_API_BASE",
        "openai",
        "bearer",
    ),
    "cerebras": (
        "https://api.cerebras.ai/v1",
        "CEREBRAS_API_BASE",
        "openai",
        "bearer",
    ),
    "lmstudio": (
        "http://localhost:1234/v1",
        "LMSTUDIO_API_BASE",
        "openai",
        "none",
    ),
    "vllm": (
        "http://localhost:8000/v1",
        "VLLM_API_BASE",
        "openai",
        "none",
    ),
}

_BUILTIN_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-compatible": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "xai": "XAI_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
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
        key_env = _BUILTIN_KEY_ENV.get(provider)
        if auth_mode == "none":
            api_key = ""
        else:
            assert key_env is not None
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


def _redact_catalog_error(message: str, headers: Mapping[str, str]) -> str:
    redacted = message
    for name, value in headers.items():
        if not value:
            continue
        normalized = name.casefold()
        if normalized == "authorization":
            redacted = redacted.replace(value, "[REDACTED]")
            if value.casefold().startswith("bearer "):
                token = value.split(None, 1)[1]
                redacted = redacted.replace(token, "[REDACTED]")
        elif normalized == "x-api-key":
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def probe_model_catalog(
    endpoint: str,
    *,
    headers: Mapping[str, str],
    catalog_format: CatalogFormat,
    timeout: float = 10.0,
) -> tuple[str, ...]:
    """Fetch and validate a provider model catalog without exposing secrets."""

    try:
        with httpx.Client(timeout=timeout) as client:
            with client.stream("GET", endpoint, headers=dict(headers)) as response:
                status_code = getattr(response, "status_code", None)
                if isinstance(status_code, int) and status_code >= 400:
                    detail = _read_error_detail(response, headers)
                    suffix = f": {detail}" if detail else ""
                    raise ProviderVerificationError(
                        f"provider catalog returned HTTP {status_code}{suffix}"
                    )
                response.raise_for_status()
                raw_payload = _read_bounded_catalog(response)
    except Exception as exc:  # noqa: BLE001 - normalize external request failures
        if isinstance(exc, ProviderVerificationError):
            raise
        raise ProviderVerificationError(
            f"provider catalog request failed ({type(exc).__name__})"
        ) from exc

    try:
        payload = json.loads(raw_payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise ProviderVerificationError(
            f"provider catalog verification failed ({type(exc).__name__})"
        ) from exc

    collection = "models" if catalog_format == "ollama" else "data"
    identifier = "name" if catalog_format == "ollama" else "id"
    if not isinstance(payload, dict) or not isinstance(payload.get(collection), list):
        raise ProviderVerificationError("provider returned an invalid model catalog")

    models: list[str] = []
    for item in payload[collection]:
        if not isinstance(item, dict):
            continue
        model_id = item.get(identifier)
        if isinstance(model_id, str) and model_id and model_id not in models:
            models.append(model_id)
    if not models:
        raise ProviderVerificationError("provider returned no model IDs")
    return tuple(models)


def _read_bounded_catalog(response: httpx.Response) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise ProviderVerificationError(
                "provider catalog returned an invalid Content-Length"
            ) from exc
        if declared_length < 0:
            raise ProviderVerificationError(
                "provider catalog returned an invalid Content-Length"
            )
        if declared_length > MAX_PROVIDER_CATALOG_BYTES:
            raise ProviderVerificationError(
                "provider catalog response is larger than 2 MB"
            )
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_PROVIDER_CATALOG_BYTES:
            raise ProviderVerificationError("provider catalog response is larger than 2 MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _read_error_detail(
    response: httpx.Response,
    headers: Mapping[str, str],
) -> str:
    try:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            declared_length = int(content_length)
            if declared_length < 0 or declared_length > MAX_PROVIDER_ERROR_BYTES:
                return ""
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_PROVIDER_ERROR_BYTES:
                return ""
            chunks.append(chunk)
        return _redact_catalog_error(
            b"".join(chunks).decode("utf-8", errors="replace").strip(),
            headers,
        )
    except (OSError, TypeError, ValueError, httpx.HTTPError):
        return ""


def verify_provider_connection(
    config: "AshConfig",
    *,
    timeout: float = 10.0,
) -> ProviderVerification:
    """Resolve and verify the configured provider route and model catalog."""

    connection = resolve_provider_connection(config)
    models = probe_model_catalog(
        connection.catalog_endpoint,
        headers=connection.headers,
        catalog_format=connection.catalog_format,
        timeout=timeout,
    )
    return ProviderVerification(
        connection=connection,
        models=models,
        selected_model_available=connection.model_name in models,
    )
