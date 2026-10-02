"""Provider factory registry and built-in provider construction."""

from __future__ import annotations

import hashlib
import os
import re
from threading import RLock
from typing import TYPE_CHECKING, Any, Callable

from ash.providers.base import ProviderABC
from ash.providers.capabilities import (
    CapabilityRegistry,
    CapabilityResolver,
    ProviderCapabilities,
    get_capability_registry,
    openai_uses_responses_api,
)
from ash.providers.identifiers import PROVIDER_NAME, parse_model_string
from ash.provider_catalog import BUILTIN_PROVIDER_IDS, get_provider_descriptor

if TYPE_CHECKING:
    from ash.config import AshConfig


ProviderFactory = Callable[["AshConfig", str], ProviderABC]
CredentialProviderFactory = Callable[["AshConfig", str, str], ProviderABC]


def _credential_builder(factory: Callable[..., ProviderABC]) -> CredentialProviderFactory:
    return lambda config, model, api_key: factory(
        config,
        model,
        api_key_override=api_key,
    )


class ProviderRegistry:
    """Resolve ``provider/model`` selections through lazy provider factories."""

    def __init__(self, capability_registry: CapabilityRegistry | None = None) -> None:
        self._factories: dict[str, ProviderFactory] = {}
        self._credential_factories: dict[str, CredentialProviderFactory] = {}
        self._capabilities = capability_registry or get_capability_registry()
        self._owned_capability_resolvers: dict[str, CapabilityResolver] = {}
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
            self._credential_factories.pop(normalized, None)
            if capabilities is not None:
                self._capabilities.register(
                    normalized,
                    capabilities,
                    replace=(
                        replace or normalized in self._owned_capability_resolvers
                    ),
                )
                self._owned_capability_resolvers[normalized] = capabilities
            self._factories[normalized] = factory

    def _enable_api_key_pool(
        self,
        name: str,
        factory: CredentialProviderFactory,
    ) -> None:
        normalized = name.strip().casefold()
        if not callable(factory):
            raise TypeError("credential provider factory must be callable")
        with self._lock:
            if normalized not in self._factories:
                raise ValueError(
                    f"provider {normalized!r} must be registered before enabling pools"
                )
            self._credential_factories[normalized] = factory

    def unregister(self, name: str) -> bool:
        normalized = name.strip().casefold()
        with self._lock:
            removed = self._factories.pop(normalized, None) is not None
            self._credential_factories.pop(normalized, None)
            resolver = self._owned_capability_resolvers.pop(normalized, None)
            if resolver is not None:
                self._capabilities.unregister(normalized, resolver=resolver)
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
            credential_factory = self._credential_factories.get(provider_name)
            resolver = self._owned_capability_resolvers.get(provider_name)
        credential_envs = _configured_api_key_envs(config, provider_name)
        credential_helper = _configured_api_key_helper(config, provider_name)
        if credential_envs or credential_helper is not None:
            if not _pool_auth_mode_allowed(config, provider_name):
                raise ValueError(
                    f"provider {provider_name!r} does not support API-key credential pools"
                )
            if credential_factory is None:
                if factory is not None or provider_name not in config.custom_providers:
                    raise ValueError(
                        f"active provider factory for {provider_name!r} does not "
                        "support Ash API-key credential pools"
                    )
                def custom_credential_factory(
                    cfg: "AshConfig",
                    model: str,
                    api_key: str,
                ) -> ProviderABC:
                    return _build_custom_openai_provider(
                        cfg,
                        provider_name,
                        model,
                        api_key_override=api_key,
                    )

                credential_factory = custom_credential_factory
            return _build_credential_pool(
                config,
                provider_name,
                model_name,
                credential_envs,
                credential_helper=credential_helper,
                credential_factory=credential_factory,
                resolver=resolver,
            )
        if factory is not None:
            provider = factory(config, model_name)
            if provider.provider_family == "custom":
                provider.provider_family = provider_name
            if resolver is not None and provider_name != "ollama":
                capabilities = resolver(model_name)
                if not isinstance(capabilities, ProviderCapabilities):
                    raise TypeError(
                        "capability resolver must return ProviderCapabilities"
                    )
                provider._ash_declared_capabilities = capabilities
            return provider
        if provider_name in config.custom_providers:
            return _build_custom_openai_provider(config, provider_name, model_name)
        raise ValueError(f"Unknown provider in model string: {provider_name!r}")


def _configured_api_key_envs(
    config: "AshConfig",
    provider_name: str,
) -> tuple[str, ...]:
    raw = getattr(config, "provider_api_key_envs", {})
    if not isinstance(raw, dict):
        return ()
    envs = raw.get(provider_name, ())
    return tuple(str(item) for item in envs)


def _configured_api_key_helper(
    config: "AshConfig",
    provider_name: str,
) -> dict[str, Any] | None:
    raw = getattr(config, "provider_api_key_helpers", {})
    if not isinstance(raw, dict):
        return None
    helper = raw.get(provider_name)
    return dict(helper) if isinstance(helper, dict) else None


def _pool_auth_mode_allowed(
    config: "AshConfig",
    provider_name: str,
) -> bool:
    if provider_name == "openai":
        return config.openai_auth_mode == "api_key"
    if provider_name == "azure":
        return config.azure_auth_mode == "api_key"
    custom = config.custom_providers.get(provider_name)
    if not isinstance(custom, dict):
        return provider_name not in {"vertex", "bedrock", "ollama"}
    declared_auth = str(custom.get("auth_mode") or "").strip().casefold()
    if not declared_auth:
        declared_auth = (
            "bearer"
            if custom.get("key_env") or custom.get("api_key")
            else "none"
        )
    return declared_auth == "bearer"


def _build_credential_pool(
    config: "AshConfig",
    provider_name: str,
    model_name: str,
    credential_envs: tuple[str, ...],
    *,
    credential_helper: dict[str, Any] | None,
    credential_factory: CredentialProviderFactory,
    resolver: CapabilityResolver | None,
) -> ProviderABC:
    from ash.providers.credential_helper import (
        CredentialHelperProvider,
        CredentialHelperSource,
    )
    from ash.providers.credential_pool import CredentialPoolProvider

    resolved_keys: list[tuple[str, str]] = []
    for env_name in credential_envs:
        api_key = os.environ.get(env_name, "")
        if not api_key:
            raise ValueError(
                f"configured credential environment variable {env_name!r} "
                f"for provider {provider_name!r} is missing or empty"
            )
        resolved_keys.append((env_name, api_key))

    providers: list[ProviderABC] = []
    profile_ids: list[str] = []

    def build_credential_provider(api_key: str) -> ProviderABC:
        provider = credential_factory(config, model_name, api_key)
        if resolver is not None:
            capabilities = resolver(model_name)
            if not isinstance(capabilities, ProviderCapabilities):
                raise TypeError(
                    "capability resolver must return ProviderCapabilities"
                )
            provider._ash_declared_capabilities = capabilities
        return provider

    if credential_helper is not None:
        prototype = build_credential_provider("ash-credential-helper-placeholder")
        source = CredentialHelperSource(
            credential_helper["command"],
            env_names=credential_helper["env"],
            timeout_seconds=credential_helper["timeout_seconds"],
            ttl_seconds=credential_helper["ttl_seconds"],
            workspace_root=config.workspace_root,
            state_directory=config.db_directory.parent,
        )
        providers.append(
            CredentialHelperProvider(
                prototype,
                build_credential_provider,
                source,
            )
        )
        profile_ids.append(f"helper:{provider_name}")

    for env_name, api_key in resolved_keys:
        providers.append(build_credential_provider(api_key))
        profile_ids.append(env_name)
    return CredentialPoolProvider(providers, profile_ids)


def prompt_cache_key(config: "AshConfig") -> str:
    """Return a stable workspace key without disclosing the workspace path."""

    workspace = str(config.workspace_root.expanduser().resolve()).encode("utf-8")
    digest = hashlib.sha256(workspace).hexdigest()[:24]
    return f"ash-project-{digest}"


def _build_anthropic(
    config: "AshConfig",
    model_name: str,
    *,
    api_key_override: str | None = None,
) -> ProviderABC:
    from ash.providers.anthropic import AnthropicProvider
    from ash.providers.readiness import resolve_provider_connection

    connection = resolve_provider_connection(
        config,
        api_key_override=api_key_override,
    )
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


def _build_openai(
    config: "AshConfig",
    model_name: str,
    *,
    api_key_override: str | None = None,
) -> ProviderABC:
    provider: ProviderABC
    if config.openai_auth_mode == "chatgpt":
        if api_key_override is not None:
            raise ValueError("OpenAI ChatGPT auth does not accept API-key profiles")
        from ash.providers.openai_chatgpt import OpenAIChatGPTProvider

        provider = OpenAIChatGPTProvider(model_name=model_name)
        provider.configure_max_tokens(config.max_completion_tokens)
        provider.configure_prompt_cache(
            enabled=False,
            cache_key="",
            retention=config.prompt_cache_retention,
        )
        return provider

    from ash.providers.readiness import resolve_provider_connection

    connection = resolve_provider_connection(
        config,
        api_key_override=api_key_override,
    )
    if connection.uses_default_base_url and openai_uses_responses_api(model_name):
        from ash.providers.openai_responses import OpenAIResponsesProvider

        provider = OpenAIResponsesProvider(
            model_name=model_name,
            api_key=connection.api_key,
        )
        provider.configure_max_tokens(config.max_completion_tokens)
        provider.configure_prompt_cache(
            enabled=config.prompt_cache_enabled,
            cache_key=prompt_cache_key(config),
            retention=config.prompt_cache_retention,
        )
        return provider

    from ash.providers.openai import OpenAIProvider

    provider = OpenAIProvider(
        model_name=model_name,
        api_key=connection.api_key,
        base_url=None if connection.uses_default_base_url else connection.base_url,
    )
    if not connection.uses_default_base_url and openai_uses_responses_api(model_name):
        provider._ash_declared_capabilities = ProviderCapabilities()
    provider.configure_max_tokens(config.max_completion_tokens)
    provider.configure_prompt_cache(
        enabled=config.prompt_cache_enabled and connection.uses_default_base_url,
        cache_key=prompt_cache_key(config),
        retention=config.prompt_cache_retention,
    )
    return provider


_FIREWORKS_MODEL_RESOURCE = re.compile(
    r"^accounts/[A-Za-z0-9._-]+/models/[A-Za-z0-9._-]+$"
)


def _fireworks_capability_endpoint(model_name: str) -> str | None:
    if not _FIREWORKS_MODEL_RESOURCE.fullmatch(model_name):
        return None
    return f"https://api.fireworks.ai/v1/{model_name}"


def _build_openai_compatible(
    config: "AshConfig",
    model_name: str,
    *,
    api_key_override: str | None = None,
) -> ProviderABC:
    from ash.providers.openai import OpenAIProvider
    from ash.providers.readiness import CatalogFormat, resolve_provider_connection

    connection = resolve_provider_connection(
        config,
        api_key_override=api_key_override,
    )
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
    elif connection.provider in {
        "mistral",
        "lmstudio",
        "vllm",
        "openai-compatible",
        "google",
        "huggingface",
        "vercel",
        "nvidia",
        "xai",
        "together",
        "fireworks",
        "cerebras",
    }:
        from ash.providers.openai_compatible import CatalogOpenAIProvider

        catalog_endpoint = connection.catalog_endpoint
        catalog_format = connection.catalog_format
        catalog_headers = connection.headers
        additional_catalog_sources: tuple[
            tuple[str, CatalogFormat, dict[str, str]], ...
        ] = ()
        if connection.provider == "xai" and connection.uses_default_base_url:
            additional_catalog_sources = (
                (
                    f"{connection.base_url}/language-models",
                    "xai",
                    connection.headers,
                ),
            )
        elif connection.provider == "cerebras" and connection.uses_default_base_url:
            catalog_endpoint = "https://api.cerebras.ai/public/v1/models"
            catalog_headers = {}
        elif connection.provider == "fireworks" and connection.uses_default_base_url:
            fireworks_endpoint = _fireworks_capability_endpoint(model_name)
            if fireworks_endpoint is not None:
                catalog_endpoint = fireworks_endpoint
                catalog_format = "fireworks"

        provider = CatalogOpenAIProvider(
            model_name=model_name,
            api_key=connection.api_key,
            provider_family=connection.provider,
            base_url=connection.base_url,
            catalog_endpoint=catalog_endpoint,
            catalog_format=catalog_format,
            catalog_headers=catalog_headers,
            additional_catalog_sources=additional_catalog_sources,
            allow_anonymous=connection.auth_mode == "none",
            local=connection.provider in {"lmstudio", "vllm"},
            default_headers=connection.client_headers,
        )
    else:
        provider = OpenAIProvider(
            model_name=model_name,
            api_key=connection.api_key,
            base_url=connection.base_url,
            allow_anonymous=connection.auth_mode == "none",
        )
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


def _build_deepseek(
    config: "AshConfig",
    model_name: str,
    *,
    api_key_override: str | None = None,
) -> ProviderABC:
    from ash.providers.deepseek import DeepSeekProvider
    from ash.providers.readiness import resolve_provider_connection

    connection = resolve_provider_connection(
        config,
        api_key_override=api_key_override,
    )
    provider = DeepSeekProvider(
        model_name=model_name,
        api_key=connection.api_key,
        base_url=None if connection.uses_default_base_url else connection.base_url,
        replay_state_directory=config.db_directory / "provider-replay-state",
        replay_state_trusted_root=config.db_directory.parent,
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    return provider


def _build_groq(
    config: "AshConfig",
    model_name: str,
    *,
    api_key_override: str | None = None,
) -> ProviderABC:
    from ash.providers.groq import GroqProvider
    from ash.providers.readiness import resolve_provider_connection

    connection = resolve_provider_connection(
        config,
        api_key_override=api_key_override,
    )
    provider = GroqProvider(
        model_name=model_name,
        api_key=connection.api_key,
        base_url=None if connection.uses_default_base_url else connection.base_url,
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    return provider


def _build_vertex(config: "AshConfig", model_name: str) -> ProviderABC:
    from ash.providers.vertex import VertexProvider

    project = (
        str(getattr(config, "vertex_project", "") or "").strip()
        or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    )
    location = (
        str(getattr(config, "vertex_location", "") or "").strip()
        or os.environ.get("GOOGLE_CLOUD_LOCATION", "").strip()
    )
    provider = VertexProvider(
        model_name=model_name,
        project=project,
        location=location,
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    return provider


def _build_bedrock(config: "AshConfig", model_name: str) -> ProviderABC:
    from ash.providers.bedrock import BedrockProvider

    region = (
        str(getattr(config, "bedrock_region", "") or "").strip()
        or os.environ.get("AWS_REGION", "").strip()
        or os.environ.get("AWS_DEFAULT_REGION", "").strip()
    )
    profile = (
        str(getattr(config, "bedrock_profile", "") or "").strip()
        or os.environ.get("AWS_PROFILE", "").strip()
    )
    provider = BedrockProvider(
        model_name=model_name,
        region=region,
        profile=profile,
    )
    provider.configure_max_tokens(config.max_completion_tokens)
    return provider


def _build_azure(
    config: "AshConfig",
    model_name: str,
    *,
    api_key_override: str | None = None,
) -> ProviderABC:
    from ash.providers.azure import AzureProvider
    from ash.providers.readiness import resolve_provider_connection

    connection = resolve_provider_connection(
        config,
        api_key_override=api_key_override,
    )
    provider = AzureProvider(
        model_name=model_name,
        base_url=connection.base_url,
        auth_mode=str(getattr(config, "azure_auth_mode", "entra") or "entra"),
        api_key=connection.api_key,
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
    *,
    api_key_override: str | None = None,
) -> ProviderABC:
    from ash.providers.openai import OpenAIProvider
    from ash.providers.readiness import resolve_provider_connection

    connection = resolve_provider_connection(
        config,
        api_key_override=api_key_override,
    )
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



def create_default_provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register("anthropic", _build_anthropic)
    registry._enable_api_key_pool(
        "anthropic",
        _credential_builder(_build_anthropic),
    )
    registry.register("openai", _build_openai)
    registry._enable_api_key_pool(
        "openai",
        _credential_builder(_build_openai),
    )
    registry.register("openai-compatible", _build_openai_compatible)
    registry._enable_api_key_pool(
        "openai-compatible",
        _credential_builder(_build_openai_compatible),
    )
    registry.register("ollama", _build_ollama)
    registry.register("deepseek", _build_deepseek)
    registry._enable_api_key_pool(
        "deepseek",
        _credential_builder(_build_deepseek),
    )
    registry.register("groq", _build_groq)
    registry._enable_api_key_pool(
        "groq",
        _credential_builder(_build_groq),
    )
    registry.register("vertex", _build_vertex)
    registry.register("bedrock", _build_bedrock)
    registry.register("azure", _build_azure)
    registry._enable_api_key_pool(
        "azure",
        _credential_builder(_build_azure),
    )
    for provider_id in sorted(
        BUILTIN_PROVIDER_IDS
        - {
            "anthropic",
            "openai",
            "deepseek",
            "groq",
            "ollama",
            "vertex",
            "bedrock",
            "azure",
        }
    ):
        registry.register(provider_id, _build_openai_compatible)
        descriptor = get_provider_descriptor(provider_id)
        if descriptor is not None and descriptor.key_envs:
            registry._enable_api_key_pool(
                provider_id,
                _credential_builder(_build_openai_compatible),
            )
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
