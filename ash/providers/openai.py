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
        stream = await self._client.chat.completions.create(
            model=self._model_name,
            messages=messages,
            temperature=temperature,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta
            content = delta.content or ""
            is_done = chunk.choices[0].finish_reason is not None
            yield StreamChunk(content=content, is_done=is_done, model=self._model_name)
