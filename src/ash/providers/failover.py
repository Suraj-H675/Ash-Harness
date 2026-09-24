"""Ordered provider failover without replaying partial output."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, AsyncGenerator

from ash.providers.base import (
    CompletionStopCategory,
    ProviderABC,
    ProviderCapabilityError,
    ProviderIncompleteStreamError,
    ProviderTerminalError,
    StreamChunk,
    completion_stop_category,
)
from ash.providers.capabilities import ProviderCapabilities
from ash.providers.messages import MessageInput


class FailoverProvider(ProviderABC):
    def __init__(self, providers: list[ProviderABC]) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        dynamic = [
            callable(getattr(provider, "detect_capabilities", None))
            for provider in providers
        ]
        static_protocols = {
            provider.capabilities.native_tools
            for provider, is_dynamic in zip(providers, dynamic, strict=True)
            if not is_dynamic
        }
        if len(static_protocols) > 1:
            raise ValueError(
                "failover providers must agree on native tool support; "
                "native and XML fallback protocols cannot share one chain"
            )
        self.providers = providers
        self.active_index = 0
        self.provider_family = providers[0].provider_family
        self.failures: list[str] = []

    @property
    def model_name(self) -> str:
        return self.providers[self.active_index].model_name

    @property
    def capabilities(self) -> ProviderCapabilities:
        capabilities = [provider.capabilities for provider in self.providers]
        context_windows = [
            item.context_window for item in capabilities if item.context_window is not None
        ]
        output_limits = [
            item.max_output_tokens
            for item in capabilities
            if item.max_output_tokens is not None
        ]
        return ProviderCapabilities(
            native_tools=all(item.native_tools for item in capabilities),
            vision=all(item.vision for item in capabilities),
            reasoning=all(item.reasoning for item in capabilities),
            local=all(item.local for item in capabilities),
            context_window=min(context_windows) if context_windows else None,
            max_output_tokens=min(output_limits) if output_limits else None,
        )

    def count_tokens(self, text: str) -> int:
        return max(provider.count_tokens(text) for provider in self.providers)

    def configure_max_tokens(self, max_tokens: int) -> None:
        super().configure_max_tokens(max_tokens)
        for provider in self.providers:
            provider.configure_max_tokens(max_tokens)

    async def detect_capabilities(
        self, *, refresh: bool = False
    ):
        """Negotiate dynamic children before validating replay protocol parity."""

        for provider in self.providers:
            detect = getattr(provider, "detect_capabilities", None)
            if not callable(detect):
                continue
            try:
                await detect(refresh=refresh)
            except TypeError as exc:
                if "refresh" not in str(exc):
                    continue
                try:
                    await detect()
                except ProviderCapabilityError:
                    raise
                except Exception:  # noqa: BLE001 - child remains conservative
                    continue
            except ProviderCapabilityError:
                raise
            except Exception:  # noqa: BLE001 - child remains conservative
                continue
        native_protocols = {
            provider.capabilities.native_tools for provider in self.providers
        }
        if len(native_protocols) != 1:
            raise ProviderCapabilityError(
                "failover providers must agree on native tool support; "
                "native and XML fallback protocols cannot share one chain"
            )
        return self.capabilities

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
                    self.provider_family = provider.provider_family
                    yield chunk
                if not saw_terminal:
                    raise ProviderIncompleteStreamError(
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
