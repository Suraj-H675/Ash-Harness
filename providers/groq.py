"""Groq chat completion provider for Ash.

API compatible with OpenAI SDK via custom base URL.
Base URL: https://api.groq.com/openai/v1
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, AsyncGenerator

import openai  # type: ignore[import-not-found]

from context.tokens import AnthropicTokenCounter
from providers.base import ProviderABC, StreamChunk, TokenCounterLike
from providers.messages import MessageInput
from providers.openai import prepare_openai_messages


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
        self._base_url = base_url or "https://api.groq.com/openai/v1"
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
        completed: list[dict[str, Any]] = []

        async for chunk in stream:
            delta = chunk.choices[0].delta
            content = delta.content or ""
            is_done = chunk.choices[0].finish_reason is not None
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
                    if tc.function.arguments:
                        partials[idx]["arguments"] += tc.function.arguments

            if is_done:
                if hasattr(chunk, "usage") and chunk.usage is not None:
                    prompt_tokens = chunk.usage.prompt_tokens or 0
                    completion_tokens = chunk.usage.completion_tokens or 0
                stop_reason = chunk.choices[0].finish_reason
                for partial in partials.values():
                    completed.append(partial)
                partials.clear()

            yield StreamChunk(
                content=content,
                is_done=is_done,
                model=self._model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                usage_source=(
                    "provider"
                    if is_done and getattr(chunk, "usage", None) is not None
                    else "unavailable"
                ),
                stop_reason=stop_reason,
                native_tool_calls=list(completed) if completed else None,
            )
            completed.clear()

    async def aclose(self) -> None:
        await self._client.close()
