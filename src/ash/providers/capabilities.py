"""Dynamic, conservative model capability metadata."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Callable


@dataclass(frozen=True)
class ProviderCapabilities:
    native_tools: bool = True
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

    def unregister(self, family: str) -> bool:
        with self._lock:
            return self._resolvers.pop(family.strip().casefold(), None) is not None

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


def _openai(model: str) -> ProviderCapabilities:
    name = model.casefold()
    return ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=any(token in name for token in ("gpt-5", "o1", "o3", "o4")),
    )


def _reasoning_by_name(model: str) -> ProviderCapabilities:
    return ProviderCapabilities(
        native_tools=True,
        reasoning="reason" in model.casefold(),
    )


def _ollama(model: str) -> ProviderCapabilities:
    del model
    return ProviderCapabilities(native_tools=False, local=True)


def create_default_capability_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.register("anthropic", _anthropic)
    registry.register("openai", _openai)
    registry.register("deepseek", _reasoning_by_name)
    registry.register("groq", _reasoning_by_name)
    registry.register("ollama", _ollama)
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
