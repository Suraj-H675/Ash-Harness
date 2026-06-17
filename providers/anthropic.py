"""Anthropic Claude provider adapter for Ash.

The real adapter uses the official ``anthropic`` SDK to stream completions.
The SDK is optional at runtime: if it is not importable, the adapter
raises :class:`ProviderBackendUnavailable` on construction so the loop can
surface a clear error to the user instead of crashing mid-turn.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from context.tokens import AnthropicTokenCounter
from providers.base import ProviderABC, StreamChunk, TokenCounterLike


class ProviderBackendUnavailable(ImportError):
    """Raised when the optional ``anthropic`` SDK is not installed."""


class AnthropicProvider(ProviderABC):
    """Streaming adapter for Anthropic Claude models.

    The adapter is constructed with a model name, an API key, and an
    optional pre-built async client. Tests can inject a fake client to
    drive the loop without hitting the network. When ``client`` is
    ``None``, the adapter lazily builds an :class:`anthropic.AsyncAnthropic`
    from ``api_key`` (or the ``ANTHROPIC_API_KEY`` environment variable
    fallback handled by the SDK itself).
    """

    def __init__(
        self,
        model_name: str,
        api_key: str,
        *,
        client: Any | None = None,
        token_counter: TokenCounterLike | None = None,
    ) -> None:
        self._model_name = model_name
        self._api_key = api_key
        self._client = client
        self._owns_client = client is None
        self._token_counter = token_counter or AnthropicTokenCounter()

    @property
    def model_name(self) -> str:
        return self._model_name

    def count_tokens(self, text: str) -> int:
        return self._token_counter.count(text)

    def _resolve_client(self) -> Any:
        if self._client is not None:
            return self._client

        try:
            from anthropic import AsyncAnthropic  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on host env
            raise ProviderBackendUnavailable(
                "The 'anthropic' package is not installed. "
                "Install it with `pip install anthropic` or inject a client."
            ) from exc

        self._client = (
            AsyncAnthropic(api_key=self._api_key) if self._api_key else AsyncAnthropic()
        )
        return self._client

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
    ) -> AsyncGenerator[StreamChunk, None]:
        client = self._resolve_client()

        # The Anthropic API splits system messages from the conversation
        # turn list. Extract any system role up front.
        system_blocks: list[str] = []
        conversation: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content", "")
            if role == "system":
                system_blocks.append(content)
            else:
                conversation.append({"role": role, "content": content})

        request_kwargs: dict[str, Any] = {
            "model": self._model_name,
            "messages": conversation,
            "temperature": temperature,
        }
        if system_blocks:
            request_kwargs["system"] = "\n\n".join(system_blocks)

        # Cap output to keep runaway generations bounded; the spec exposes
        # ``max_completion_tokens`` on the AshConfig for the loop to wire.
        max_tokens = getattr(self, "_max_tokens", None)
        if max_tokens:
            request_kwargs["max_tokens"] = max_tokens

        async with client.messages.stream(**request_kwargs) as stream:
            async for text in stream.text_stream:
                if text:
                    yield StreamChunk(content=text, model=self._model_name)

            final_message = await stream.get_final_message()
            usage = getattr(final_message, "usage", None)
            stop_reason = getattr(final_message, "stop_reason", None)
            yield StreamChunk(
                content="",
                is_done=True,
                prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
                completion_tokens=getattr(usage, "output_tokens", 0) or 0,
                stop_reason=stop_reason,
                model=self._model_name,
            )

    def configure_max_tokens(self, max_tokens: int) -> None:
        """Set the per-completion cap. Call before streaming."""

        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self._max_tokens = max_tokens
