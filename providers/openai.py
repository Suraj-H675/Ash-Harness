"""OpenAI GPT provider adapter for Ash."""

from __future__ import annotations

from typing import Any, AsyncGenerator
import openai  # type: ignore[import-not-found]

from ash.context.tokens import OpenAITokenCounter
from ash.providers.base import ProviderABC, StreamChunk, TokenCounterLike


class OpenAIProvider(ProviderABC):
    def __init__(
        self,
        model_name: str = "gpt-4o",
        api_key: str | None = None,
        *,
        token_counter: TokenCounterLike | None = None,
    ) -> None:
        self._model_name = model_name
        self._api_key = api_key
        self._token_counter = token_counter or OpenAITokenCounter(model_name)
        self._client = openai.AsyncOpenAI(api_key=api_key)

    @property
    def model_name(self) -> str:
        return self._model_name

    def count_tokens(self, text: str) -> int:
        return self._token_counter.count(text)

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
    ) -> AsyncGenerator[StreamChunk, None]:
        try:
            stream = await self._client.chat.completions.create(
                model=self._model_name,
                messages=messages,
                temperature=temperature,
                stream=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"OpenAI API error: {exc}") from exc
        async for chunk in stream:
            delta = chunk.choices[0].delta
            content = delta.content or ""
            is_done = chunk.choices[0].finish_reason is not None
            # Extract token usage from the final chunk's usage dict.
            prompt_tokens = 0
            completion_tokens = 0
            stop_reason = None
            if hasattr(chunk, "usage") and chunk.usage is not None:
                prompt_tokens = chunk.usage.prompt_tokens or 0
                completion_tokens = chunk.usage.completion_tokens or 0
            if is_done:
                stop_reason = chunk.choices[0].finish_reason
            yield StreamChunk(
                content=content,
                is_done=is_done,
                model=self._model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                stop_reason=stop_reason,
            )
