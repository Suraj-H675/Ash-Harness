"""Provider factory registry and built-in provider construction."""

from __future__ import annotations

import hashlib
from threading import RLock
from typing import TYPE_CHECKING, Callable

from ash.providers.base import ProviderABC
from ash.providers.capabilities import (
    CapabilityRegistry,
    CapabilityResolver,
    ProviderCapabilities,
    get_capability_registry,
)
from ash.providers.identifiers import PROVIDER_NAME, parse_model_string
from ash.provider_catalog import BUILTIN_PROVIDER_IDS

if TYPE_CHECKING:
    from ash.config import AshConfig


ProviderFactory = Callable[["AshConfig", str], ProviderABC]


class ProviderRegistry:
    """Resolve ``provider/model`` selections through lazy provider factories."""

    def __init__(self, capability_registry: CapabilityRegistry | None = None) -> None:
        self._factories: dict[str, ProviderFactory] = {}
        self._capabilities = capability_registry or get_capability_registry()
        self._owned_capability_families: set[str] = set()
        self._lock = RLock()

    def register(
        self,
        name: str,
        factory: ProviderFactory,
        *,
        replace: bool = False,
        capabilities: CapabilityResolver | None = None,
    ) -> None:
        normalized = name.strip().casefold()
        if not PROVIDER_NAME.fullmatch(normalized):
            raise ValueError("provider name must be a lowercase path-safe identifier")
        if not callable(factory):
            raise TypeError("provider factory must be callable")
        with self._lock:
            if normalized in self._factories and not replace:
                raise ValueError(f"provider {normalized!r} is already registered")
            if capabilities is not None:
                self._capabilities.register(
                    normalized,
                    capabilities,
                    replace=(replace or normalized in self._owned_capability_families),
                )
                self._owned_capability_families.add(normalized)
            self._factories[normalized] = factory

    def unregister(self, name: str) -> bool:
        normalized = name.strip().casefold()
        with self._lock:
            removed = self._factories.pop(normalized, None) is not None
            if normalized in self._owned_capability_families:
                self._capabilities.unregister(normalized)
                self._owned_capability_families.discard(normalized)
            return removed

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._factories))

    def build(self, config: "AshConfig") -> ProviderABC:
        fallback_models = list(config.fallback_models)
        if fallback_models:
            from ash.providers.failover import FailoverProvider

            models = [config.model, *fallback_models]
            return FailoverProvider(
                [
                    self.build(
                        config.model_copy(
                            update={"model": model, "fallback_models": []}
                        )
                    )
                    for model in models
                ]
            )

        provider_name, model_name = parse_model_string(config.model)
        with self._lock:
            factory = self._factories.get(provider_name)
        if factory is not None:
            provider = factory(config, model_name)
            if provider.provider_family == "custom":
                provider.provider_family = provider_name
            if provider_name in self._owned_capability_families and provider_name != "ollama":
                provider._ash_declared_capabilities = self._capabilities.resolve(
                    provider_name, model_name
                )
            return provider
        if provider_name in config.custom_providers:
            return _build_custom_openai_provider(config, provider_name, model_name)
        raise ValueError(f"Unknown provider in model string: {provider_name!r}")


def prompt_cache_key(config: "AshConfig") -> str:
    """Return a stable workspace key without disclosing the workspace path."""

    workspace = str(config.workspace_root.expanduser().resolve()).encode("utf-8")
    digest = hashlib.sha256(workspace).hexdigest()[:24]
    return f"ash-project-{digest}"


def _build_anthropic(config: "AshConfig", model_name: str) -> ProviderABC:
    from ash.providers.anthropic import AnthropicProvider
    from ash.providers.readiness import resolve_provider_connection

    connection = resolve_provider_connection(config)
    provider = AnthropicProvider(
        model_name=model_name,
        api_key=connection.api_key,
        base_url=None if connection.uses_default_base_url else connection.base_url,
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    provider.configure_prompt_cache(
        enabled=config.prompt_cache_enabled and connection.uses_default_base_url,
        retention=config.prompt_cache_retention,
    )
    return provider


def _build_openai(config: "AshConfig", model_name: str) -> ProviderABC:
    from ash.providers.openai import OpenAIProvider
    from ash.providers.readiness import resolve_provider_connection

    connection = resolve_provider_connection(config)
    provider = OpenAIProvider(
        model_name=model_name,
        api_key=connection.api_key,
        base_url=None if connection.uses_default_base_url else connection.base_url,
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    provider.configure_prompt_cache(
        enabled=config.prompt_cache_enabled and connection.uses_default_base_url,
        cache_key=prompt_cache_key(config),
        retention=config.prompt_cache_retention,
    )
    return provider


def _build_openai_compatible(config: "AshConfig", model_name: str) -> ProviderABC:
    from ash.providers.openai import OpenAIProvider
    from ash.providers.readiness import resolve_provider_connection

    connection = resolve_provider_connection(config)
    provider: ProviderABC
    if connection.provider == "openrouter":
        from ash.providers.openrouter import OpenRouterProvider

        provider = OpenRouterProvider(
            model_name=model_name,
            api_key=connection.api_key,
            base_url=connection.base_url,
            catalog_endpoint=connection.catalog_endpoint,
            catalog_headers=connection.headers,
        )
    elif connection.provider in {"mistral", "lmstudio", "vllm", "openai-compatible"}:
        from ash.providers.openai_compatible import CatalogOpenAIProvider

        provider = CatalogOpenAIProvider(
            model_name=model_name,
            api_key=connection.api_key,
            provider_family=connection.provider,
            base_url=connection.base_url,
            catalog_endpoint=connection.catalog_endpoint,
            catalog_format=connection.catalog_format,
            catalog_headers=connection.headers,
            allow_anonymous=connection.auth_mode == "none",
            local=connection.provider in {"lmstudio", "vllm"},
        )
    else:
        provider = OpenAIProvider(
            model_name=model_name,
            api_key=connection.api_key,
            base_url=connection.base_url,
            allow_anonymous=connection.auth_mode == "none",
        )
        _assign_openai_wire_route_identity(provider, connection.provider)
    provider.configure_max_tokens(config.max_completion_tokens)
    return provider


def _build_ollama(config: "AshConfig", model_name: str) -> ProviderABC:
    from ash.providers.ollama import OllamaProvider
    from ash.providers.readiness import resolve_provider_connection

    connection = resolve_provider_connection(config)
    provider = OllamaProvider(
        model_name=model_name,
        base_url=connection.base_url,
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    return provider


def _build_deepseek(config: "AshConfig", model_name: str) -> ProviderABC:
    from ash.providers.deepseek import DeepSeekProvider
    from ash.providers.readiness import resolve_provider_connection

    connection = resolve_provider_connection(config)
    provider = DeepSeekProvider(
        model_name=model_name,
        api_key=connection.api_key,
        base_url=None if connection.uses_default_base_url else connection.base_url,
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    return provider


def _build_groq(config: "AshConfig", model_name: str) -> ProviderABC:
    from ash.providers.groq import GroqProvider
    from ash.providers.readiness import resolve_provider_connection

    connection = resolve_provider_connection(config)
    provider = GroqProvider(
        model_name=model_name,
        api_key=connection.api_key,
        base_url=None if connection.uses_default_base_url else connection.base_url,
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    return provider


def _custom_model_capabilities(
    config: "AshConfig",
    provider_name: str,
    model_name: str,
) -> ProviderCapabilities:
    """Resolve explicit per-model capabilities for a custom wire-compatible route."""

    provider_config = config.custom_providers.get(provider_name, {})
    raw_models = provider_config.get("model_capabilities")
    if raw_models is None:
        return ProviderCapabilities()
    if not isinstance(raw_models, dict):
        raise ValueError(
            f"custom provider {provider_name!r} model_capabilities must be a table"
        )
    raw = raw_models.get(model_name)
    if raw is None:
        return ProviderCapabilities()
    if not isinstance(raw, dict):
        raise ValueError(
            f"custom provider {provider_name!r} capabilities for {model_name!r} "
            "must be a table"
        )

    allowed = {
        "native_tools",
        "vision",
        "reasoning",
        "context_window",
        "max_output_tokens",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(
            f"custom provider {provider_name!r} capabilities for {model_name!r} "
            "contain unknown field(s): " + ", ".join(sorted(unknown))
        )

    boolean_values: dict[str, bool] = {}
    for field in ("native_tools", "vision", "reasoning"):
        value = raw.get(field, False)
        if not isinstance(value, bool):
            raise ValueError(
                f"custom provider {provider_name!r} capability {field!r} for "
                f"{model_name!r} must be boolean"
            )
        boolean_values[field] = value

    integer_values: dict[str, int | None] = {}
    for field in ("context_window", "max_output_tokens"):
        value = raw.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError(
                f"custom provider {provider_name!r} capability {field!r} for "
                f"{model_name!r} must be a positive integer"
            )
        integer_values[field] = value

    return ProviderCapabilities(
        native_tools=boolean_values["native_tools"],
        vision=boolean_values["vision"],
        reasoning=boolean_values["reasoning"],
        context_window=integer_values["context_window"],
        max_output_tokens=integer_values["max_output_tokens"],
    )


def configured_model_capabilities(
    config: "AshConfig",
    model_string: str,
) -> ProviderCapabilities:
    """Resolve trusted static capability metadata without network I/O."""

    provider_name, model_name = parse_model_string(model_string)
    if provider_name in config.custom_providers:
        return _custom_model_capabilities(config, provider_name, model_name)
    return get_capability_registry().resolve(provider_name, model_name)


def _build_custom_openai_provider(
    config: "AshConfig",
    provider_name: str,
    model_name: str,
) -> ProviderABC:
    from ash.providers.openai import OpenAIProvider
    from ash.providers.readiness import resolve_provider_connection

    connection = resolve_provider_connection(config)
    provider = OpenAIProvider(
        model_name=model_name,
        api_key=connection.api_key or None,
        base_url=connection.base_url,
        allow_anonymous=connection.auth_mode == "none",
    )
    provider.provider_family = provider_name
    provider._ash_declared_capabilities = _custom_model_capabilities(
        config, provider_name, model_name
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    return provider



def _assign_openai_wire_route_identity(provider: ProviderABC, family: str) -> None:
    """Keep adapter capabilities while exposing the route that owns the request."""

    capabilities = provider.capabilities
    provider.provider_family = family
    provider._ash_declared_capabilities = capabilities

def create_default_provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register("anthropic", _build_anthropic)
    registry.register("openai", _build_openai)
    registry.register("openai-compatible", _build_openai_compatible)
    registry.register("ollama", _build_ollama)
    registry.register("deepseek", _build_deepseek)
    registry.register("groq", _build_groq)
    for provider_id in sorted(
        BUILTIN_PROVIDER_IDS - {"anthropic", "openai", "deepseek", "groq", "ollama"}
    ):
        registry.register(provider_id, _build_openai_compatible)
    return registry


_DEFAULT_REGISTRY: ProviderRegistry | None = None
_DEFAULT_REGISTRY_LOCK = RLock()


def get_provider_registry() -> ProviderRegistry:
    """Return the process-wide registry used by CLI, SDK, and extensions."""

    global _DEFAULT_REGISTRY
    with _DEFAULT_REGISTRY_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = create_default_provider_registry()
        return _DEFAULT_REGISTRY
