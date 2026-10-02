"""Dynamic, conservative model capability metadata."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Callable


@dataclass(frozen=True)
class ProviderCapabilities:
    native_tools: bool = False
    vision: bool = False
    reasoning: bool = False
    local: bool = False
    context_window: int | None = None
    max_output_tokens: int | None = None


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


def _anthropic(model: str) -> ProviderCapabilities:
    name = model.casefold()
    if "opus-4-7" in name:
        return ProviderCapabilities(True, True, True, False, 1_000_000, 128_000)
    if "sonnet-4-6" in name:
        return ProviderCapabilities(True, True, True, False, 1_000_000, 64_000)
    if "haiku-4-5" in name:
        return ProviderCapabilities(True, True, True, False, 200_000, 64_000)
    return ProviderCapabilities(native_tools=True, vision=True)


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
    return _OPENAI_STATIC_CAPABILITIES.get(model.casefold(), ProviderCapabilities())


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
