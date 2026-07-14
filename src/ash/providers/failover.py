"""Ordered provider failover without replaying partial output."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, AsyncGenerator

from ash.providers.base import (
    CompletionStopCategory,
    ProviderABC,
    ProviderCompletionError,
    ProviderTerminalError,
    StreamChunk,
    completion_stop_category,
)
from ash.providers.messages import MessageInput


class FailoverProvider(ProviderABC):
    def __init__(self, providers: list[ProviderABC]) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        native_protocols = {
            provider.capabilities.native_tools for provider in providers
        }
        if len(native_protocols) != 1:
            raise ValueError(
                "failover providers must agree on native tool support; "
                "native and XML fallback protocols cannot share one chain"
            )
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
        messages: Sequence[MessageInput],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        last_error: Exception | None = None
        failures: list[str] = []
        for index, provider in enumerate(self.providers):
            emitted_output = False
            exposed_terminal = False
            saw_terminal = False
            try:
                async for chunk in provider.stream_chat(
                    messages, temperature=temperature, tools=tools
                ):
                    has_output = bool(
                        chunk.content
                        or chunk.tool_call_delta
                        or chunk.native_tool_calls
                    )
                    if (
                        chunk.is_done
                        and completion_stop_category(chunk.stop_reason)
                        == CompletionStopCategory.ERROR
                        and not emitted_output
                        and not has_output
                    ):
                        raise ProviderTerminalError(chunk.stop_reason)
                    emitted_output = emitted_output or has_output
                    saw_terminal = saw_terminal or chunk.is_done
                    exposed_terminal = exposed_terminal or chunk.is_done
                    self.active_index = index
                    yield chunk
                if not saw_terminal:
                    raise ProviderCompletionError(
                        f"provider {provider.model_name!r} ended before a terminal chunk"
                    )
                self.failures = failures
                return
            except Exception as exc:  # noqa: BLE001
                if emitted_output or exposed_terminal:
                    self.failures = failures
                    raise
                last_error = exc
                failures.append(f"{provider.model_name}: {exc}")
        self.failures = failures
        assert last_error is not None
        raise RuntimeError(
            "All configured providers failed: " + "; ".join(failures)
        ) from last_error

    async def aclose(self) -> None:
        for provider in self.providers:
            await provider.aclose()
