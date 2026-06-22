"""Ollama local model provider for Ash."""

from __future__ import annotations

from typing import Any, AsyncGenerator
import httpx

from context.tokens import AnthropicTokenCounter
from providers.base import ProviderABC, StreamChunk, TokenCounterLike


class OllamaProvider(ProviderABC):
    provider_family = "ollama"
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

    def configure_max_tokens(self, max_tokens: int) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self._max_tokens = max_tokens

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        options: dict[str, Any] = {"temperature": temperature}
        if hasattr(self, "_max_tokens"):
            options["num_predict"] = self._max_tokens
        payload = {
            "model": self._model_name,
            "messages": messages,
            "stream": True,
            "options": options,
        }
        try:
            async with self._client.stream(
                "POST", f"{self._base_url}/api/chat", json=payload
            ) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    text = resp.text
                    raise RuntimeError(f"Ollama API error {resp.status_code}: {text}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    import json

                    data = json.loads(line)
                    content = data.get("message", {}).get("content", "")
                    is_done = data.get("done", False)
                    # Extract token usage from final chunk.
                    prompt_tokens = 0
                    completion_tokens = 0
                    stop_reason = None
                    if is_done:
                        prompt_tokens = data.get("prompt_eval_count", 0)
                        completion_tokens = data.get("eval_count", 0)
                        done_reason = data.get("done_reason", "")
                        if done_reason:
                            stop_reason = done_reason
                    yield StreamChunk(
                        content=content,
                        is_done=is_done,
                        model=self._model_name,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        stop_reason=stop_reason,
                    )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Ollama connection error: {exc}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
