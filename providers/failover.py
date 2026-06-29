"""Ordered provider failover without replaying partial output."""

from __future__ import annotations

from typing import Any, AsyncGenerator

from providers.base import ProviderABC, StreamChunk


class FailoverProvider(ProviderABC):
    def __init__(self, providers: list[ProviderABC]) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self.providers = providers
        self.active_index = 0
        self.failures: list[str] = []

    @property
    def model_name(self) -> str:
        return self.providers[self.active_index].model_name

    @property
    def capabilities(self):
        return self.providers[self.active_index].capabilities

    def count_tokens(self, text: str) -> int:
        return self.providers[self.active_index].count_tokens(text)

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        last_error: Exception | None = None
        for index, provider in enumerate(self.providers):
            emitted = False
            try:
                async for chunk in provider.stream_chat(
                    messages, temperature=temperature, tools=tools
                ):
                    emitted = True
                    self.active_index = index
                    yield chunk
                return
            except Exception as exc:  # noqa: BLE001
                if emitted:
                    raise
                last_error = exc
                self.failures.append(f"{provider.model_name}: {exc}")
        assert last_error is not None
        raise RuntimeError(
            "All configured providers failed: " + "; ".join(self.failures)
        ) from last_error

    async def aclose(self) -> None:
        for provider in self.providers:
            await provider.aclose()
