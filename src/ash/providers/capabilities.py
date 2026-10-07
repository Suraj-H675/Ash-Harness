"""Dynamic, conservative model capability metadata."""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock
from typing import Callable

from ash.providers.reasoning import ReasoningEffortSpec, reasoning_effort_spec


@dataclass(frozen=True)
class ProviderCapabilities:
    native_tools: bool = False
    vision: bool = False
    reasoning: bool = False
    local: bool = False
    context_window: int | None = None
    max_output_tokens: int | None = None
    reasoning_effort: ReasoningEffortSpec | None = None


CapabilityResolver = Callable[[str], ProviderCapabilities]


class CapabilityRegistry:
    """Resolve model capabilities through provider-owned declarations."""

    def __init__(self) -> None:
        self._resolvers: dict[str, CapabilityResolver] = {}
        self._lock = RLock()

    def register(
        self,
        family: str,
        resolver: CapabilityResolver,
        *,
        replace: bool = False,
    ) -> None:
        normalized = family.strip().casefold()
        if not normalized:
            raise ValueError("provider family cannot be empty")
        if not callable(resolver):
            raise TypeError("capability resolver must be callable")
        with self._lock:
            if normalized in self._resolvers and not replace:
                raise ValueError(
                    f"capability resolver for {normalized!r} is already registered"
                )
            self._resolvers[normalized] = resolver

    def unregister(
        self,
        family: str,
        *,
        resolver: CapabilityResolver | None = None,
    ) -> bool:
        with self._lock:
            normalized = family.strip().casefold()
            current = self._resolvers.get(normalized)
            if current is None or (resolver is not None and current is not resolver):
                return False
            del self._resolvers[normalized]
            return True

    def families(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._resolvers))

    def resolve(self, family: str, model: str) -> ProviderCapabilities:
        with self._lock:
            resolver = self._resolvers.get(family.strip().casefold())
        if resolver is None:
            return ProviderCapabilities()
        capabilities = resolver(model)
        if not isinstance(capabilities, ProviderCapabilities):
            raise TypeError("capability resolver must return ProviderCapabilities")
        return capabilities


_ANTHROPIC_STATIC_CAPABILITIES: dict[str, ProviderCapabilities] = {
    "claude-fable-5-1": ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
    ),
    "claude-opus-5-5": ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
    ),
    "claude-sonnet-5-5": ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
    ),
    "claude-haiku-4-5": ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=200_000,
        max_output_tokens=64_000,
    ),
    "claude-haiku-4-5-20251001": ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=200_000,
        max_output_tokens=64_000,
    ),
    "claude-opus-4-7": ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
    ),
    "claude-sonnet-4-6": ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=1_000_000,
        max_output_tokens=128_000,
    ),
}


def _anthropic(model: str) -> ProviderCapabilities:
    name = model.casefold()
    base = _ANTHROPIC_STATIC_CAPABILITIES.get(name, ProviderCapabilities())
    return replace(
        base,
        reasoning_effort=reasoning_effort_spec("anthropic", name),
    )


_OPENAI_RESPONSES_MODELS = frozenset(
    {
        "gpt-5.6",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-6-astra",
        "gpt-6.1-sol",
        "gpt-6-sol",
        "gpt-6-luna",
    }
)

_OPENAI_STATIC_CAPABILITIES: dict[str, ProviderCapabilities] = {
    **{
        model: ProviderCapabilities(
            native_tools=True,
            vision=True,
            reasoning=True,
            context_window=1_050_000,
            max_output_tokens=128_000,
        )
        for model in _OPENAI_RESPONSES_MODELS
    },
    "gpt-5.2": ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=400_000,
        max_output_tokens=128_000,
    ),
    "gpt-5.2-2025-12-11": ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=400_000,
        max_output_tokens=128_000,
    ),
    "gpt-5.2-codex": ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=400_000,
        max_output_tokens=128_000,
    ),
    "gpt-5-mini": ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=400_000,
        max_output_tokens=128_000,
    ),
    "gpt-5-mini-2025-08-07": ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        context_window=400_000,
        max_output_tokens=128_000,
    ),
    "gpt-4.1": ProviderCapabilities(
        native_tools=True,
        vision=True,
        context_window=1_047_576,
        max_output_tokens=32_768,
    ),
    "gpt-4.1-2025-04-14": ProviderCapabilities(
        native_tools=True,
        vision=True,
        context_window=1_047_576,
        max_output_tokens=32_768,
    ),
}


def openai_uses_responses_api(model: str) -> bool:
    """Return whether Ash's first-party API-key route uses Responses."""

    return model.casefold() in _OPENAI_RESPONSES_MODELS


def _openai(model: str) -> ProviderCapabilities:
    name = model.casefold()
    base = _OPENAI_STATIC_CAPABILITIES.get(name, ProviderCapabilities())
    return replace(
        base,
        reasoning_effort=reasoning_effort_spec("openai", name),
    )


_GOOGLE_FUNCTION_CALLING_MODELS = frozenset(
    {
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-pro-preview",
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    }
)


def google_capabilities(model: str) -> ProviderCapabilities:
    """Return exact first-party Gemini declarations verified by Ash."""

    name = model.casefold()
    if name not in _GOOGLE_FUNCTION_CALLING_MODELS:
        return ProviderCapabilities()
    effort = reasoning_effort_spec("google", name)
    if name == "gemini-3.8-flash":
        return ProviderCapabilities(
            native_tools=True,
            vision=True,
            reasoning=True,
            context_window=1_000_000,
            max_output_tokens=64_000,
            reasoning_effort=effort,
        )
    return ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        reasoning_effort=effort,
    )


def google_requires_tool_thought_signature(model: str) -> bool:
    """Return whether an exact Gemini 3 tool model requires signature replay."""

    name = model.casefold()
    return name.startswith("gemini-3") and name in _GOOGLE_FUNCTION_CALLING_MODELS


def vertex_google_capabilities(model: str) -> ProviderCapabilities:
    """Return exact Google Gemini capabilities on Vertex OpenAI compatibility."""

    name = model.casefold()
    if not name.startswith("google/"):
        return ProviderCapabilities()
    gemini_model = name.removeprefix("google/")
    base = google_capabilities(gemini_model)
    if base == ProviderCapabilities():
        return base
    effort = reasoning_effort_spec("vertex", name)
    if gemini_model == "gemini-3.8-flash":
        return ProviderCapabilities(
            native_tools=True,
            vision=True,
            reasoning=True,
            context_window=1_048_576,
            max_output_tokens=65_536,
            reasoning_effort=effort,
        )
    return ProviderCapabilities(
        native_tools=base.native_tools,
        vision=base.vision,
        reasoning=base.reasoning,
        reasoning_effort=effort,
    )


def vertex_google_requires_tool_thought_signature(model: str) -> bool:
    """Return whether a verified Vertex Gemini route requires signature replay."""

    name = model.casefold()
    return name.startswith("google/") and google_requires_tool_thought_signature(
        name.removeprefix("google/")
    )


def deepseek_capabilities(model: str) -> ProviderCapabilities:
    name = model.casefold()
    if name in {
        "deepseek-flash",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
    }:
        return ProviderCapabilities(
            native_tools=True,
            vision=True,
            reasoning=True,
            context_window=1_000_000,
            max_output_tokens=384_000,
        )
    if name == "deepseek-v4-pro":
        return ProviderCapabilities(
            native_tools=True,
            reasoning=True,
            context_window=1_000_000,
            max_output_tokens=384_000,
        )
    return ProviderCapabilities()


def groq_capabilities(model: str) -> ProviderCapabilities:
    name = model.casefold()
    if name in {"openai/gpt-oss-120b", "openai/gpt-oss-20b"}:
        return ProviderCapabilities(
            native_tools=True,
            reasoning=True,
            context_window=131_072,
            max_output_tokens=65_536,
        )
    if name == "llama-3.1-8b-instant":
        return ProviderCapabilities(
            native_tools=True,
            context_window=131_072,
            max_output_tokens=131_072,
        )
    if name == "llama-3.3-70b-versatile":
        return ProviderCapabilities(
            native_tools=True,
            context_window=131_072,
            max_output_tokens=32_768,
        )
    if name == "qwen/qwen3.8-27b":
        return ProviderCapabilities(
            native_tools=True,
            vision=True,
            reasoning=True,
            context_window=131_072,
            max_output_tokens=16_384,
        )
    if name == "openai/gpt-oss-safeguard-20b":
        return ProviderCapabilities(
            native_tools=True,
            reasoning=True,
            context_window=131_072,
            max_output_tokens=65_536,
        )
    return ProviderCapabilities()


def _local_conservative(model: str) -> ProviderCapabilities:
    del model
    return ProviderCapabilities(native_tools=False, local=True)


def create_default_capability_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register("anthropic", _anthropic)
    registry.register("openai", _openai)
    registry.register("google", google_capabilities)
    registry.register("deepseek", deepseek_capabilities)
    registry.register("groq", groq_capabilities)
    registry.register("ollama", _local_conservative)
    registry.register("lmstudio", _local_conservative)
    registry.register("vllm", _local_conservative)
    return registry


_DEFAULT_REGISTRY: CapabilityRegistry | None = None
_DEFAULT_REGISTRY_LOCK = RLock()


def get_capability_registry() -> CapabilityRegistry:
    global _DEFAULT_REGISTRY
    with _DEFAULT_REGISTRY_LOCK:
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = create_default_capability_registry()
        return _DEFAULT_REGISTRY


def infer_capabilities(family: str, model: str) -> ProviderCapabilities:
    """Compatibility wrapper around the process-wide capability registry."""

    return get_capability_registry().resolve(family, model)
