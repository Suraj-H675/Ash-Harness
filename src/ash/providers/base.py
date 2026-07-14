"""Abstract base contract for LLM provider adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, AsyncGenerator, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field
from ash.providers.capabilities import ProviderCapabilities, infer_capabilities
from ash.providers.messages import CanonicalToolCall, MessageInput


class StreamChunk(BaseModel):
    """A single delta in a streaming provider response.

    Providers yield these in real time as model output arrives. Plain text
    deltas populate ``content``; XML-fallback tool fragments populate
    ``tool_call_delta``. Native adapters emit completed ``native_tool_calls``.
    ``is_done`` flips to ``True`` on the terminal chunk so the loop can
    finalize the turn. ``prompt_tokens`` and
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
    native_tool_calls: list[CanonicalToolCall] | None = None


class CompletionStopCategory(StrEnum):
    """Normalized terminal categories shared by every provider adapter."""

    COMPLETE = "complete"
    TRUNCATED = "truncated"
    FILTERED = "filtered"
    ERROR = "error"


class ProviderCompletionError(RuntimeError):
    """Raised when a provider stream cannot produce a safe terminal outcome."""


class CompletionOutcome(BaseModel):
    """Validated provider-neutral result of one complete model request."""

    text: str = ""
    tool_calls: list[CanonicalToolCall] = Field(default_factory=list)
    prompt_tokens: int = Field(0, ge=0)
    completion_tokens: int = Field(0, ge=0)
    cache_read_tokens: int = Field(0, ge=0)
    cache_write_tokens: int = Field(0, ge=0)
    usage_source: Literal["provider", "estimated", "unavailable"] = "unavailable"
    stop_reason: str | None = None


_COMPLETE_STOP_REASONS = frozenset(
    {
        "complete",
        "completed",
        "done",
        "end",
        "end_turn",
        "eos",
        "function_call",
        "stop",
        "stop_sequence",
        "tool_calls",
        "tool_use",
    }
)
_TRUNCATED_STOP_REASONS = frozenset(
    {"length", "max_output_tokens", "max_tokens", "token_limit"}
)
_FILTERED_STOP_REASONS = frozenset(
    {"blocked", "content_filter", "refusal", "safety"}
)
_ERROR_STOP_REASONS = frozenset(
    {"cancelled", "error", "failed", "rate_limit", "timeout"}
)


def completion_stop_category(reason: str | None) -> CompletionStopCategory:
    """Normalize provider terminal reasons, failing closed on unknown values."""

    if reason is None or not reason.strip():
        return CompletionStopCategory.COMPLETE
    normalized = reason.strip().casefold().replace("-", "_")
    if normalized in _COMPLETE_STOP_REASONS:
        return CompletionStopCategory.COMPLETE
    if normalized in _TRUNCATED_STOP_REASONS:
        return CompletionStopCategory.TRUNCATED
    if normalized in _FILTERED_STOP_REASONS:
        return CompletionStopCategory.FILTERED
    if normalized in _ERROR_STOP_REASONS:
        return CompletionStopCategory.ERROR
    return CompletionStopCategory.ERROR


class ProviderTerminalError(ProviderCompletionError):
    """Provider-reported terminal failure that may be safe to replay."""

    def __init__(self, stop_reason: str | None) -> None:
        self.stop_reason = stop_reason or "error"
        normalized = self.stop_reason.strip().casefold().replace("-", "_")
        self.retriable = normalized in {"rate_limit", "timeout"}
        super().__init__(
            "provider reported an unsuccessful terminal outcome: "
            f"{self.stop_reason}"
        )


@runtime_checkable
class TokenCounterLike(Protocol):
    """Anything with a ``count(text) -> int`` method."""

    def count(self, text: str) -> int: ...


class ProviderABC(ABC):
    """Common contract every LLM provider adapter must implement.

    The loop uses :meth:`stream_chat` to receive an async stream of
    :class:`StreamChunk` objects. ``messages`` uses Ash's validated canonical
    shape so the loop does not need provider-specific message encoding.
    Mapping inputs remain supported for compatibility.
    """

    provider_family = "custom"
    _ash_declared_capabilities: ProviderCapabilities | None = None

    @abstractmethod
    async def stream_chat(
        self,
        messages: Sequence[MessageInput],
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

    def configure_max_tokens(self, max_tokens: int) -> None:
        """Apply a per-request completion ceiling when the adapter supports it."""

        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")

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
