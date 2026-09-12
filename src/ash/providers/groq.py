"""Groq chat completion provider for Ash.

API compatible with OpenAI SDK via custom base URL.
Base URL: https://api.groq.com/openai/v1
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, AsyncGenerator

import openai  # type: ignore[import-not-found]

from ash.context.tokens import AnthropicTokenCounter
from ash.providers.base import ProviderABC, StreamChunk, TokenCounterLike
from ash.providers.messages import CanonicalToolCall, MessageInput
from ash.providers.openai import (
    account_openai_compatible_stream_bytes,
    prepare_openai_messages,
)
from ash.providers.readiness import (
    normalize_provider_base_url,
    require_secure_provider_transport,
)


class GroqProvider(ProviderABC):
    provider_family = "groq"

    def __init__(
        self,
        model_name: str = "llama-3.3-70b-versatile",
        api_key: str = "",
        *,
        base_url: str | None = None,
        token_counter: TokenCounterLike | None = None,
    ) -> None:
        if not api_key:
            raise ValueError(
                "Groq API key is required. "
                "Set the GROQ_API_KEY environment variable or pass api_key."
            )
        self._model_name = model_name
        self._api_key = api_key
        self._base_url = normalize_provider_base_url(
            base_url or "https://api.groq.com/openai/v1", provider="groq"
        )
        require_secure_provider_transport(self._base_url, provider="groq")
        self._token_counter = token_counter or AnthropicTokenCounter()
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=self._base_url,
            max_retries=0,
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    def count_tokens(self, text: str) -> int:
        return self._token_counter.count(text)

    def configure_max_tokens(self, max_tokens: int) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self._max_tokens = max_tokens

    async def stream_chat(
        self,
        messages: Sequence[MessageInput],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        kwargs: dict[str, Any] = {
            "model": self._model_name,
            "messages": prepare_openai_messages(messages),
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
        if hasattr(self, "_max_tokens"):
            kwargs["max_tokens"] = self._max_tokens
        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Groq API error: {exc}") from exc

        partials: dict[int, Any] = {}
        completed: list[CanonicalToolCall] = []
        stream_bytes = 0

        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            usage = getattr(chunk, "usage", None)
            if not choices:
                if usage is not None:
                    yield StreamChunk(
                        is_done=True,
                        model=self._model_name,
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=(
                            getattr(usage, "completion_tokens", 0) or 0
                        ),
                        usage_source="provider",
                    )
                continue

            choice = choices[0]
            delta = choice.delta
            content = delta.content or ""
            stream_bytes = account_openai_compatible_stream_bytes(
                stream_bytes, content, provider="Groq"
            )
            is_done = choice.finish_reason is not None
            prompt_tokens = 0
            completion_tokens = 0
            stop_reason = None

            if hasattr(delta, "tool_calls") and delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in partials:
                        partials[idx] = {
                            "id": tc.id or f"call_{idx}",
                            "name": tc.function.name or "",
                            "arguments": "",
                        }
                        stream_bytes = account_openai_compatible_stream_bytes(
                            stream_bytes, partials[idx]["id"], provider="Groq"
                        )
                        stream_bytes = account_openai_compatible_stream_bytes(
                            stream_bytes, partials[idx]["name"], provider="Groq"
                        )
                    if tc.function.arguments:
                        stream_bytes = account_openai_compatible_stream_bytes(
                            stream_bytes,
                            tc.function.arguments,
                            provider="Groq",
                        )
                        partials[idx]["arguments"] += tc.function.arguments

            if is_done:
                if usage is not None:
                    prompt_tokens = usage.prompt_tokens or 0
                    completion_tokens = usage.completion_tokens or 0
                stop_reason = choice.finish_reason
                for partial in partials.values():
                    completed.append(CanonicalToolCall.model_validate(partial))
                partials.clear()

            yield StreamChunk(
                content=content,
                is_done=is_done,
                model=self._model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                usage_source=(
                    "provider"
                    if is_done and usage is not None
                    else "unavailable"
                ),
                stop_reason=stop_reason,
                native_tool_calls=list(completed) if completed else None,
            )
            completed.clear()

    async def aclose(self) -> None:
        await self._client.close()
