"""Provider factory registry and built-in provider construction."""

from __future__ import annotations

import hashlib
import os
import re
from threading import RLock
from typing import TYPE_CHECKING, Any, Callable

from providers.base import ProviderABC
from providers.capabilities import (
    CapabilityRegistry,
    CapabilityResolver,
    get_capability_registry,
)

if TYPE_CHECKING:
    from config import AshConfig


PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
ProviderFactory = Callable[["AshConfig", str], ProviderABC]


class ProviderRegistry:
    """Resolve ``provider/model`` selections through lazy provider factories."""

    def __init__(
        self, capability_registry: CapabilityRegistry | None = None
    ) -> None:
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
            from providers.failover import FailoverProvider

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
            if provider_name in self._owned_capability_families:
                provider._ash_declared_capabilities = self._capabilities.resolve(
                    provider_name, model_name
                )
            return provider
        if provider_name in config.custom_providers:
            return _build_custom_openai_provider(config, provider_name, model_name)
        raise ValueError(f"Unknown provider in model string: {provider_name!r}")


def parse_model_string(model: str) -> tuple[str, str]:
    """Split and validate a canonical ``provider/model`` identifier."""

    provider, separator, model_name = model.strip().partition("/")
    provider = provider.casefold()
    if not separator or not PROVIDER_NAME.fullmatch(provider) or not model_name.strip():
        raise ValueError(
            f"Model string must be in 'provider/model' format, got: {model!r}"
        )
    return provider, model_name.strip()


def prompt_cache_key(config: "AshConfig") -> str:
    """Return a stable workspace key without disclosing the workspace path."""

    workspace = str(config.workspace_root.expanduser().resolve()).encode("utf-8")
    digest = hashlib.sha256(workspace).hexdigest()[:24]
    return f"ash-project-{digest}"


def _build_anthropic(config: "AshConfig", model_name: str) -> ProviderABC:
    from providers.anthropic import AnthropicProvider

    base_url = os.environ.get("ANTHROPIC_API_BASE") or None
    provider = AnthropicProvider(
        model_name=model_name,
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        base_url=base_url,
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    provider.configure_prompt_cache(
        enabled=config.prompt_cache_enabled and base_url is None,
        retention=config.prompt_cache_retention,
    )
    return provider


def _build_openai(config: "AshConfig", model_name: str) -> ProviderABC:
    from providers.openai import OpenAIProvider

    base_url = os.environ.get("OPENAI_API_BASE") or None
    provider = OpenAIProvider(
        model_name=model_name,
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        base_url=base_url,
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    provider.configure_prompt_cache(
        enabled=config.prompt_cache_enabled and base_url is None,
        cache_key=prompt_cache_key(config),
        retention=config.prompt_cache_retention,
    )
    return provider


def _build_openai_compatible(config: "AshConfig", model_name: str) -> ProviderABC:
    from providers.openai import OpenAIProvider

    provider = OpenAIProvider(
        model_name=model_name,
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        base_url=os.environ.get("OPENAI_API_BASE") or None,
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    return provider


def _build_ollama(config: "AshConfig", model_name: str) -> ProviderABC:
    from providers.ollama import OllamaProvider

    provider = OllamaProvider(
        model_name=model_name,
        base_url=os.environ.get("OLLAMA_API_BASE", "http://localhost:11434"),
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    return provider


def _build_deepseek(config: "AshConfig", model_name: str) -> ProviderABC:
    from providers.deepseek import DeepSeekProvider

    provider = DeepSeekProvider(
        model_name=model_name,
        api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        base_url=os.environ.get("DEEPSEEK_API_BASE") or None,
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    return provider


def _build_groq(config: "AshConfig", model_name: str) -> ProviderABC:
    from providers.groq import GroqProvider

    provider = GroqProvider(
        model_name=model_name,
        api_key=os.environ.get("GROQ_API_KEY", ""),
        base_url=os.environ.get("GROQ_API_BASE") or None,
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    return provider


def _build_custom_openai_provider(
    config: "AshConfig",
    provider_name: str,
    model_name: str,
) -> ProviderABC:
    from providers.openai import OpenAIProvider

    custom: dict[str, Any] = config.custom_providers[provider_name]
    key_env = str(custom.get("key_env", ""))
    provider = OpenAIProvider(
        model_name=model_name,
        api_key=(
            os.environ.get(key_env, "") if key_env else str(custom.get("api_key", ""))
        ),
        base_url=custom.get("base_url"),
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    return provider


def create_default_provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register("anthropic", _build_anthropic)
    registry.register("openai", _build_openai)
    registry.register("openai-compatible", _build_openai_compatible)
    registry.register("ollama", _build_ollama)
    registry.register("deepseek", _build_deepseek)
    registry.register("groq", _build_groq)
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
