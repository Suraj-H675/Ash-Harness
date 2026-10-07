"""Same-provider API-key rotation without replaying retained model output."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, AsyncGenerator

from ash.core.redaction import redact_text
from ash.providers.base import (
    CompletionStopCategory,
    ProviderABC,
    ProviderIncompleteStreamError,
    ProviderTerminalError,
    StreamChunk,
    completion_stop_category,
    stream_chunk_commits_provider,
)
from ash.providers.capabilities import ProviderCapabilities
from ash.providers.messages import MessageInput
from ash.providers.retry import (
    ProviderFailure,
    ProviderFailureCategory,
    classify_provider_failure,
)


AUTH_COOLDOWN_SECONDS = 60.0
BILLING_COOLDOWN_SECONDS = 300.0
CREDENTIAL_SOURCE_COOLDOWN_SECONDS = 10.0
RATE_LIMIT_COOLDOWN_SECONDS = 60.0
MAX_CREDENTIAL_COOLDOWN_SECONDS = 3600.0

_ROTATABLE_FAILURES = frozenset(
    {
        ProviderFailureCategory.AUTH,
        ProviderFailureCategory.BILLING,
        ProviderFailureCategory.CREDENTIAL_SOURCE,
        ProviderFailureCategory.RATE_LIMIT,
    }
)


class ProviderCredentialPoolCoolingDown(RuntimeError):
    """Raised when every credential is still in a bounded cooldown window."""

    retriable = True
    status_code = 429

    def __init__(self, provider: str, retry_after: float) -> None:
        self.retry_after = max(0.0, retry_after)
        super().__init__(
            f"all configured {provider} credentials are cooling down; "
            f"retry in {self.retry_after:.1f} seconds"
        )


@dataclass
class _CredentialState:
    provider: ProviderABC
    profile_id: str
    cooldown_until: float = 0.0
    cooldown_reason: str = ""


class CredentialPoolProvider(ProviderABC):
    """Rotate ordered credentials for one provider/model before model failover."""

    def __init__(
        self,
        providers: list[ProviderABC],
        profile_ids: list[str],
        *,
        clock=time.monotonic,
    ) -> None:
        if not providers or len(providers) != len(profile_ids):
            raise ValueError("credential providers and profile IDs must align")
        provider_families = {provider.provider_family for provider in providers}
        model_names = {provider.model_name for provider in providers}
        if len(provider_families) != 1 or len(model_names) != 1:
            raise ValueError(
                "credential pool entries must target one provider/model route"
            )
        if len(set(profile_ids)) != len(profile_ids):
            raise ValueError("credential profile IDs must be unique")
        self.providers = providers
        self.profile_ids = tuple(profile_ids)
        self.provider_family = providers[0].provider_family
        self.active_index = 0
        self._sticky_index = 0
        self._clock = clock
        self._states = [
            _CredentialState(provider=provider, profile_id=profile_id)
            for provider, profile_id in zip(providers, profile_ids, strict=True)
        ]
        self._closed_provider_ids: set[int] = set()

    @property
    def model_name(self) -> str:
        return self.providers[self.active_index].model_name

    @property
    def active_profile(self) -> str:
        return self.profile_ids[self.active_index]

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self.providers[self.active_index].capabilities

    def count_tokens(self, text: str) -> int:
        return self.providers[self.active_index].count_tokens(text)

    def configure_reasoning_effort(self, effort: str | None) -> None:
        super().configure_reasoning_effort(effort)
        for provider in self.providers:
            provider.configure_reasoning_effort(effort)

    def configure_max_tokens(self, max_tokens: int) -> None:
        super().configure_max_tokens(max_tokens)
        for provider in self.providers:
            provider.configure_max_tokens(max_tokens)

    async def detect_capabilities(
        self,
        *,
        refresh: bool = False,
    ) -> ProviderCapabilities:
        last_error: Exception | None = None
        for index in self._candidate_indices():
            provider = self.providers[index]
            detect = getattr(provider, "detect_capabilities", None)
            if not callable(detect):
                self._record_success(index)
                return provider.capabilities
            try:
                try:
                    result = await detect(refresh=refresh)
                except TypeError as exc:
                    if "refresh" not in str(exc):
                        raise
                    result = await detect()
            except Exception as exc:  # noqa: BLE001
                failure = classify_provider_failure(exc)
                if failure.category not in _ROTATABLE_FAILURES:
                    raise
                self._record_failure(index, failure)
                last_error = exc
                continue
            self._record_success(index)
            return (
                result
                if isinstance(result, ProviderCapabilities)
                else provider.capabilities
            )
        if last_error is not None:
            raise last_error
        raise self._cooldown_error()

    async def stream_chat(
        self,
        messages: Sequence[MessageInput],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        last_error: Exception | None = None
        candidates = self._candidate_indices()
        if not candidates:
            raise self._cooldown_error()
        for index in candidates:
            provider = self.providers[index]
            self.active_index = index
            emitted_output = False
            exposed_terminal = False
            saw_terminal = False
            try:
                async for chunk in provider.stream_chat(
                    messages,
                    temperature=temperature,
                    tools=tools,
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
                self._record_success(index)
                return
            except Exception as exc:  # noqa: BLE001
                if emitted_output or exposed_terminal:
                    raise
                failure = classify_provider_failure(exc)
                if failure.category not in _ROTATABLE_FAILURES:
                    raise
                self._record_failure(index, failure)
                last_error = exc
                continue
        assert last_error is not None
        raise last_error

    def snapshot(self) -> tuple[dict[str, str | float | bool], ...]:
        now = self._clock()
        return tuple(
            {
                "profile": state.profile_id,
                "active": index == self._sticky_index,
                "cooling_down": state.cooldown_until > now,
                "retry_after": max(0.0, state.cooldown_until - now),
                "reason": state.cooldown_reason,
            }
            for index, state in enumerate(self._states)
        )

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
                    "additional credential provider cleanup failure: "
                    + redact_text(str(failure))
                )
            raise cancellation
        if failures:
            raise RuntimeError(
                f"failed to close {len(failures)} credential provider(s)"
            ) from failures[0]

    def _candidate_indices(self) -> list[int]:
        now = self._clock()
        available = [
            index
            for index, state in enumerate(self._states)
            if state.cooldown_until <= now
        ]
        if self._sticky_index in available:
            available.remove(self._sticky_index)
            available.insert(0, self._sticky_index)
        return available

    def _record_success(self, index: int) -> None:
        state = self._states[index]
        state.cooldown_until = 0.0
        state.cooldown_reason = ""
        self._sticky_index = index
        self.active_index = index

    def _record_failure(self, index: int, failure: ProviderFailure) -> None:
        cooldown = _credential_cooldown_seconds(failure)
        state = self._states[index]
        state.cooldown_until = self._clock() + cooldown
        state.cooldown_reason = failure.category.value

    def _cooldown_error(self) -> ProviderCredentialPoolCoolingDown:
        now = self._clock()
        remaining = min(
            max(0.0, state.cooldown_until - now)
            for state in self._states
        )
        return ProviderCredentialPoolCoolingDown(self.provider_family, remaining)


def _credential_cooldown_seconds(failure: ProviderFailure) -> float:
    if failure.category is ProviderFailureCategory.CREDENTIAL_SOURCE:
        return CREDENTIAL_SOURCE_COOLDOWN_SECONDS
    if failure.category is ProviderFailureCategory.AUTH:
        return AUTH_COOLDOWN_SECONDS
    if failure.category is ProviderFailureCategory.BILLING:
        return BILLING_COOLDOWN_SECONDS
    if failure.category is ProviderFailureCategory.RATE_LIMIT:
        value = (
            failure.retry_after
            if failure.retry_after is not None
            else RATE_LIMIT_COOLDOWN_SECONDS
        )
        return min(MAX_CREDENTIAL_COOLDOWN_SECONDS, max(0.0, value))
    raise ValueError("failure category is not eligible for credential rotation")
