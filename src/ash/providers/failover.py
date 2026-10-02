"""Ordered provider failover without replaying partial output."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, AsyncGenerator

from ash.core.redaction import redact_text
from ash.providers.base import (
    CompletionStopCategory,
    ProviderABC,
    ProviderCapabilityError,
    ProviderIncompleteStreamError,
    ProviderTerminalError,
    StreamChunk,
    completion_stop_category,
    stream_chunk_commits_provider,
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
        self._closed_provider_ids: set[int] = set()

    @property
    def model_name(self) -> str:
        return self.providers[self.active_index].model_name

    @property
    def capabilities(self) -> ProviderCapabilities:
        capabilities = [provider.capabilities for provider in self.providers]
        context_windows = [item.context_window for item in capabilities]
        output_limits = [item.max_output_tokens for item in capabilities]
        return ProviderCapabilities(
            native_tools=all(item.native_tools for item in capabilities),
            vision=all(item.vision for item in capabilities),
            reasoning=all(item.reasoning for item in capabilities),
            local=all(item.local for item in capabilities),
            context_window=(
                min(value for value in context_windows if value is not None)
                if all(value is not None for value in context_windows)
                else None
            ),
            max_output_tokens=(
                min(value for value in output_limits if value is not None)
                if all(value is not None for value in output_limits)
                else None
            ),
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
            self.active_index = index
            self.provider_family = provider.provider_family
            emitted_output = False
            exposed_terminal = False
            saw_terminal = False
            try:
                async for chunk in provider.stream_chat(
                    messages, temperature=temperature, tools=tools
                ):
                    has_output = stream_chunk_commits_provider(chunk)
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
                failures.append(
                    f"{provider.model_name}: {redact_text(str(exc))}"
                )
        self.failures = failures
        assert last_error is not None
        raise RuntimeError(
            "All configured providers failed: " + "; ".join(failures)
        ) from last_error

    async def aclose(self) -> None:
        pending = [
            provider
            for provider in self.providers
            if id(provider) not in self._closed_provider_ids
        ]
        if not pending:
            return
        outcomes = await asyncio.gather(
            *(provider.aclose() for provider in pending),
            return_exceptions=True,
        )
        failures: list[BaseException] = []
        cancellation: asyncio.CancelledError | None = None
        for provider, outcome in zip(pending, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                failures.append(outcome)
                if isinstance(outcome, asyncio.CancelledError) and cancellation is None:
                    cancellation = outcome
                continue
            self._closed_provider_ids.add(id(provider))
        if cancellation is not None:
            for failure in failures:
                if failure is cancellation:
                    continue
                cancellation.add_note(
                    "additional failover provider cleanup failure: "
                    + redact_text(str(failure))
                )
            raise cancellation
        if failures:
            raise RuntimeError(
                f"failed to close {len(failures)} failover provider(s)"
            ) from failures[0]
