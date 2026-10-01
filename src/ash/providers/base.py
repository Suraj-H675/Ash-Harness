"""Abstract base contract for LLM provider adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import StrEnum
import json
from typing import Any, AsyncGenerator, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator
from ash.providers.capabilities import ProviderCapabilities, infer_capabilities
from ash.providers.messages import CanonicalMessage, CanonicalToolCall, MessageInput


MAX_PROVIDER_USAGE_TOKENS = 2**53 - 1
MAX_PROVIDER_STOP_REASON_CHARS = 256
MAX_PROVIDER_NATIVE_TOOL_CALLS_PER_CHUNK = 64
MAX_PROVIDER_REASONING_BLOCKS_PER_CHUNK = 4096
MAX_PROVIDER_CHUNK_TEXT_BYTES = 16 * 1024 * 1024
MAX_PROVIDER_CHUNK_STRUCTURED_BYTES = 16 * 1024 * 1024


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
    prompt_tokens: int = Field(default=0, ge=0, le=MAX_PROVIDER_USAGE_TOKENS)
    completion_tokens: int = Field(default=0, ge=0, le=MAX_PROVIDER_USAGE_TOKENS)
    cache_read_tokens: int = Field(default=0, ge=0, le=MAX_PROVIDER_USAGE_TOKENS)
    cache_write_tokens: int = Field(default=0, ge=0, le=MAX_PROVIDER_USAGE_TOKENS)
    usage_source: Literal["provider", "estimated", "unavailable"] = "unavailable"
    stop_reason: str | None = Field(
        default=None,
        max_length=MAX_PROVIDER_STOP_REASON_CHARS,
    )
    reasoning_blocks: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=MAX_PROVIDER_REASONING_BLOCKS_PER_CHUNK,
    )
    model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Fully-formed tool calls from providers that support native
    # OpenAI tool_calls streaming (includes real id for tool_call_id).
    native_tool_calls: list[CanonicalToolCall] | None = Field(
        default=None,
        max_length=MAX_PROVIDER_NATIVE_TOOL_CALLS_PER_CHUNK,
    )
    reasoning: list[dict[str, Any]] | None = Field(
        default=None,
        max_length=MAX_PROVIDER_REASONING_BLOCKS_PER_CHUNK,
    )
    provider_state: list[dict[str, Any]] | None = Field(default=None, max_length=64)

    @field_validator(
        "native_tool_calls",
        "reasoning",
        "reasoning_blocks",
        mode="before",
    )
    @classmethod
    def validate_structured_stream_payload(cls, value: Any) -> Any:
        if value is None:
            return value
        serializable = value
        if isinstance(value, list):
            serializable = [
                item.to_wire() if isinstance(item, CanonicalToolCall) else item
                for item in value
            ]
        try:
            encoded = json.dumps(
                serializable,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("provider structured stream data must be serializable") from exc
        if len(encoded) > MAX_PROVIDER_CHUNK_STRUCTURED_BYTES:
            raise ValueError(
                "provider structured stream data exceeds "
                f"{MAX_PROVIDER_CHUNK_STRUCTURED_BYTES} UTF-8 bytes"
            )
        return value

    @field_validator("provider_state", mode="before")
    @classmethod
    def validate_provider_replay_state(cls, value: Any) -> Any:
        if value is None:
            return value
        validated = CanonicalMessage.model_validate(
            {
                "role": "assistant",
                "content": "",
                "provider_state": value,
            }
        )
        return validated.provider_state

    @field_validator("content", "tool_call_delta")
    @classmethod
    def validate_stream_text(cls, value: str) -> str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("provider stream text must be valid UTF-8") from exc
        if size > MAX_PROVIDER_CHUNK_TEXT_BYTES:
            raise ValueError(
                "provider stream text exceeds "
                f"{MAX_PROVIDER_CHUNK_TEXT_BYTES} UTF-8 bytes"
            )
        return value


class CompletionStopCategory(StrEnum):
    """Normalized terminal categories shared by every provider adapter."""

    COMPLETE = "complete"
    TRUNCATED = "truncated"
    FILTERED = "filtered"
    ERROR = "error"


class ProviderCompletionError(RuntimeError):
    """Raised when a provider stream cannot produce a safe terminal outcome."""


class ProviderIncompleteStreamError(ProviderCompletionError):
    """Provider stream ended before a terminal chunk and is safe to retry."""

    retriable = True


class CompletionOutcome(BaseModel):
    """Validated provider-neutral result of one complete model request."""

    text: str = ""
    tool_calls: list[CanonicalToolCall] = Field(default_factory=list)
    prompt_tokens: int = Field(0, ge=0, le=MAX_PROVIDER_USAGE_TOKENS)
    completion_tokens: int = Field(0, ge=0, le=MAX_PROVIDER_USAGE_TOKENS)
    cache_read_tokens: int = Field(0, ge=0, le=MAX_PROVIDER_USAGE_TOKENS)
    cache_write_tokens: int = Field(0, ge=0, le=MAX_PROVIDER_USAGE_TOKENS)
    usage_source: Literal["provider", "estimated", "unavailable"] = "unavailable"
    stop_reason: str | None = Field(None, max_length=MAX_PROVIDER_STOP_REASON_CHARS)
    reasoning_blocks: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=MAX_PROVIDER_REASONING_BLOCKS_PER_CHUNK,
    )
    provider_state: list[dict[str, Any]] = Field(default_factory=list, max_length=64)

    @field_validator("provider_state", mode="before")
    @classmethod
    def validate_provider_replay_state(cls, value: Any) -> Any:
        if value is None:
            return []
        validated = CanonicalMessage.model_validate(
            {
                "role": "assistant",
                "content": "",
                "provider_state": value,
            }
        )
        return validated.provider_state or []


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


class ProviderCapabilityError(RuntimeError):
    """Raised when runtime capability negotiation cannot preserve protocol safety."""


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
