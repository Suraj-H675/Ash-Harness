"""Abstract base contract for LLM provider adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field
from providers.capabilities import ProviderCapabilities, infer_capabilities


class StreamChunk(BaseModel):
    """A single delta in a streaming provider response.

    Providers yield these in real time as model output arrives. Plain text
    deltas populate ``content``; mid-flight tool-call fragments populate
    ``tool_call_delta``. ``is_done`` flips to ``True`` on the terminal
    chunk so the loop can finalize the turn. ``prompt_tokens`` and
    ``completion_tokens`` are best-effort usage figures. Cache reads and
    writes are included in ``prompt_tokens`` and also exposed separately.
    """

    content: str = ""
    tool_call_delta: str = ""
    is_done: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usage_source: Literal["provider", "estimated", "unavailable"] = "unavailable"
    stop_reason: str | None = None
    model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Fully-formed tool calls from providers that support native
    # OpenAI tool_calls streaming (includes real id for tool_call_id).
    native_tool_calls: list[dict[str, Any]] | None = None


@runtime_checkable
class TokenCounterLike(Protocol):
    """Anything with a ``count(text) -> int`` method."""

    def count(self, text: str) -> int: ...


class ProviderABC(ABC):
    """Common contract every LLM provider adapter must implement.

    The loop uses :meth:`stream_chat` to receive an async stream of
    :class:`StreamChunk` objects. ``messages`` is a list of dicts in the
    standard ``{"role": ..., "content": ...}`` shape so the loop does not
    have to know provider-specific message encoding.
    """

    provider_family = "custom"
    _ash_declared_capabilities: ProviderCapabilities | None = None

    @abstractmethod
    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        """Yield provider response deltas as the model emits them."""

        # An async generator *must* yield, so we use ``return`` followed by
        # ``yield`` in subclasses; this stub raises to fail loud if a
        # subclass forgets to implement streaming.
        raise NotImplementedError
        yield  # pragma: no cover - makes this a generator

    @abstractmethod
    def count_tokens(self, text: str) -> int:
        """Return the token footprint of ``text`` under this provider."""

        raise NotImplementedError

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Identifier of the model this adapter is bound to."""

        raise NotImplementedError

    async def aclose(self) -> None:
        """Release provider resources. Stateless providers may do nothing."""

    @property
    def capabilities(self) -> ProviderCapabilities:
        declared = self._ash_declared_capabilities
        if isinstance(declared, ProviderCapabilities):
            return declared
        return infer_capabilities(self.provider_family, self.model_name)
