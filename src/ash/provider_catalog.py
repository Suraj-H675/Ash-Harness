"""Declarative built-in provider catalog shared by setup and runtime.

This module intentionally lives at the package root. Importing configuration or
the lightweight CLI must not import the eager ``ash.providers`` compatibility
package and every provider SDK just to read provider identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderDescriptor:
    """The user-facing and connection metadata for one built-in provider."""

    id: str
    name: str
    category: str
    description: str
    base_url: str
    key_env: str | None = None
    protocol: str = "openai"
    local: bool = False

    @property
    def key_required(self) -> bool:
        return self.key_env is not None


# Keep this list intentionally descriptor-only. Providers that speak the same
# OpenAI wire protocol use the same adapter; adding a provider must not require
# a new SDK or another copy of setup/readiness logic.
BUILTIN_PROVIDERS: tuple[ProviderDescriptor, ...] = (
    ProviderDescriptor(
        "anthropic",
        "Anthropic",
        "Cloud API",
        "Claude models via the Anthropic Messages API",
        "https://api.anthropic.com",
        "ANTHROPIC_API_KEY",
        protocol="anthropic",
    ),
    ProviderDescriptor(
        "openai",
        "OpenAI",
        "Cloud API",
        "GPT models via the OpenAI Chat Completions API",
        "https://api.openai.com/v1",
        "OPENAI_API_KEY",
    ),
    ProviderDescriptor(
        "openrouter",
        "OpenRouter",
        "Gateway",
        "Multi-provider routing with one OpenAI-compatible endpoint",
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
    ),
    ProviderDescriptor(
        "deepseek",
        "DeepSeek",
        "Cloud API",
        "DeepSeek chat and reasoning models",
        "https://api.deepseek.com/v1",
        "DEEPSEEK_API_KEY",
    ),
    ProviderDescriptor(
        "groq",
        "Groq",
        "Cloud API",
        "Fast hosted open models",
        "https://api.groq.com/openai/v1",
        "GROQ_API_KEY",
    ),
    ProviderDescriptor(
        "mistral",
        "Mistral",
        "Cloud API",
        "Mistral models via an OpenAI-compatible endpoint",
        "https://api.mistral.ai/v1",
        "MISTRAL_API_KEY",
    ),
    ProviderDescriptor(
        "xai",
        "xAI",
        "Cloud API",
        "Grok models via an OpenAI-compatible endpoint",
        "https://api.x.ai/v1",
        "XAI_API_KEY",
    ),
    ProviderDescriptor(
        "together",
        "Together AI",
        "Cloud API",
        "Hosted open models via an OpenAI-compatible endpoint",
        "https://api.together.xyz/v1",
        "TOGETHER_API_KEY",
    ),
    ProviderDescriptor(
        "fireworks",
        "Fireworks AI",
        "Cloud API",
        "Hosted inference via an OpenAI-compatible endpoint",
        "https://api.fireworks.ai/inference/v1",
        "FIREWORKS_API_KEY",
    ),
    ProviderDescriptor(
        "cerebras",
        "Cerebras",
        "Cloud API",
        "Fast hosted models via an OpenAI-compatible endpoint",
        "https://api.cerebras.ai/v1",
        "CEREBRAS_API_KEY",
    ),
    ProviderDescriptor(
        "ollama",
        "Ollama",
        "Local runtime",
        "Local models; no API key required",
        "http://localhost:11434",
        local=True,
    ),
    ProviderDescriptor(
        "lmstudio",
        "LM Studio",
        "Local runtime",
        "Local models exposed through an OpenAI-compatible server",
        "http://localhost:1234/v1",
        local=True,
    ),
    ProviderDescriptor(
        "vllm",
        "vLLM",
        "Local runtime",
        "Self-hosted models exposed through an OpenAI-compatible server",
        "http://localhost:8000/v1",
        local=True,
    ),
)

BUILTIN_PROVIDER_IDS = frozenset(provider.id for provider in BUILTIN_PROVIDERS)
BUILTIN_PROVIDER_BY_ID = {provider.id: provider for provider in BUILTIN_PROVIDERS}


def get_provider_descriptor(provider_id: str) -> ProviderDescriptor | None:
    """Return a normalized descriptor, or ``None`` for a user-defined route."""

    return BUILTIN_PROVIDER_BY_ID.get(provider_id.strip().casefold())
