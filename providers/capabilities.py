"""Conservative model capability metadata used by the runtime."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapabilities:
    native_tools: bool = True
    vision: bool = False
    reasoning: bool = False
    local: bool = False
    context_window: int | None = None
    max_output_tokens: int | None = None


def infer_capabilities(family: str, model: str) -> ProviderCapabilities:
    name = model.casefold()
    if family == "anthropic":
        if "opus-4-7" in name:
            return ProviderCapabilities(True, True, True, False, 1_000_000, 128_000)
        if "sonnet-4-6" in name:
            return ProviderCapabilities(True, True, True, False, 1_000_000, 64_000)
        if "haiku-4-5" in name:
            return ProviderCapabilities(True, True, True, False, 200_000, 64_000)
        return ProviderCapabilities(native_tools=True, vision=True)
    if family == "openai":
        return ProviderCapabilities(
            native_tools=True,
            vision=True,
            reasoning=any(token in name for token in ("gpt-5", "o1", "o3", "o4")),
        )
    if family in {"deepseek", "groq"}:
        return ProviderCapabilities(
            native_tools=True,
            reasoning="reason" in name,
        )
    if family == "ollama":
        # Ollama model manifests vary; Ash currently uses its chat endpoint
        # without native tool schemas, so XML fallback is the safe contract.
        return ProviderCapabilities(native_tools=False, local=True)
    return ProviderCapabilities(native_tools=True)
