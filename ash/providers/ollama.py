"""Ollama local model provider for Ash."""

from __future__ import annotations

from typing import Any, AsyncGenerator
import httpx

from ash.context.tokens import AnthropicTokenCounter
from ash.providers.base import ProviderABC, StreamChunk, TokenCounterLike


class OllamaProvider(ProviderABC):
    def __init__(
        self,
        model_name: str = "llama3",
        base_url: str = "http://localhost:11434",
        *,
        token_counter: TokenCounterLike | None = None,
    ) -> None:
        self._model_name = model_name
        self._base_url = base_url.rstrip("/")
        self._token_counter = token_counter or AnthropicTokenCounter()
        self._client = httpx.AsyncClient(timeout=60.0)

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
        payload = {
            "model": self._model_name,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        async with self._client.stream(
            "POST", f"{self._base_url}/api/chat", json=payload
        ) as resp:
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                import json

                data = json.loads(line)
                content = data.get("message", {}).get("content", "")
                is_done = data.get("done", False)
                yield StreamChunk(
                    content=content, is_done=is_done, model=self._model_name
                )
