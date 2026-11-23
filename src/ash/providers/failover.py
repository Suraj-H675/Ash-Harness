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
    ProviderMalformedToolArgumentsError,
    ProviderTerminalError,
    StreamChunk,
    completion_stop_category,
    managed_async_stream,
    stream_chunk_commits_provider,
)
from ash.providers.capabilities import ProviderCapabilities
from ash.providers.messages import MessageInput, normalize_messages
from ash.providers.replay_affinity import (
    resolve_provider_replay_identity,
    verify_provider_replay_identity,
)
from ash.providers.reasoning import ReasoningEffortSpec


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
        input_limits = [item.max_input_tokens for item in capabilities]
        output_limits = [item.max_output_tokens for item in capabilities]
        effort_supports = [item.reasoning_effort for item in capabilities]
        effort_support = None
        if all(item is not None for item in effort_supports):
            known_efforts = [item for item in effort_supports if item is not None]
            levels = tuple(
                level
                for level in known_efforts[0].supported
                if all(level in item.supported for item in known_efforts)
            )
            defaults = {item.default for item in known_efforts}
            default = known_efforts[0].default if len(defaults) == 1 else None
            if levels:
                effort_support = ReasoningEffortSpec(
                    levels,
                    default if default in levels else None,
                )
        return ProviderCapabilities(
            native_tools=all(item.native_tools for item in capabilities),
            vision=all(item.vision for item in capabilities),
            reasoning=all(item.reasoning for item in capabilities),
            local=all(item.local for item in capabilities),
            reasoning_effort=effort_support,
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
            max_input_tokens=(
                min(value for value in input_limits if value is not None)
                if all(value is not None for value in input_limits)
                else None
            ),
        )

    def count_tokens(self, text: str) -> int:
        return max(provider.count_tokens(text) for provider in self.providers)

    def configure_reasoning_effort(self, effort: str | None) -> None:
        super().configure_reasoning_effort(effort)
        for provider in self.providers:
            provider.configure_reasoning_effort(effort)

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
        *,
        replay_owner: str | None = None,
        replay_profile: str | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        last_error: Exception | None = None
        failures: list[str] = []
        historical_state = any(
            item.get("role") == "assistant" and item.get("provider_state")
            for item in normalize_messages(messages)
        )
        if historical_state and replay_owner is None:
            raise RuntimeError(
                "Opaque reasoning replay has no verified source model. "
                "Use /restart-context with an explicit handoff before "
                "continuing, or supply verified replay ownership."
            )
        if replay_profile is not None and replay_owner is None:
            raise RuntimeError(
                "Opaque reasoning replay has no verified model owner for its "
                "credential profile; use /restart-context with an explicit handoff."
            )
        if replay_owner is not None:
            matches = [
                index
                for index, candidate in enumerate(self.providers)
                if f"{candidate.provider_family}/{candidate.model_name}" == replay_owner
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    "The provider that owns retained reasoning state is not "
                    "uniquely available. Use /restart-context with an explicit "
                    "handoff before continuing."
                )
            candidate_indexes = matches
        else:
            candidate_indexes = list(range(len(self.providers)))
        for index in candidate_indexes:
            provider = self.providers[index]
            self.active_index = index
            self.provider_family = provider.provider_family
            emitted_output = False
            exposed_terminal = False
            saw_terminal = False
            try:
                if replay_profile is not None:
                    from ash.providers.credential_pool import CredentialPoolProvider

                    if not isinstance(provider, CredentialPoolProvider):
                        raise RuntimeError(
                            "The credential profile owning retained opaque "
                            "reasoning state is not available on the selected "
                            "model route; use /restart-context with an explicit "
                            "handoff before continuing."
                        )
                    child_stream = provider.stream_chat(
                        messages,
                        temperature=temperature,
                        tools=tools,
                        replay_profile=replay_profile,
                    )
                else:
                    identity = await resolve_provider_replay_identity(provider)
                    verify_provider_replay_identity(
                        identity,
                        provider_family=provider.provider_family,
                        model_name=provider.model_name,
                    )
                    child_stream = provider.stream_chat(
                        messages, temperature=temperature, tools=tools
                    )
                async with managed_async_stream(
                    child_stream,
                    label="failover child",
                ) as child_stream:
                    async for chunk in child_stream:
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
                if (
                    emitted_output
                    or exposed_terminal
                    or isinstance(exc, ProviderMalformedToolArgumentsError)
                ):
                    # Invalid model-generated arguments are not a transport
                    # outage. Preserve the typed error for one bounded retry
                    # in the outer loop, rather than multiplying requests
                    # across providers and losing the safe CLI remedy.
                    self.failures = failures
                    raise
                last_error = exc
                failures.append(
                    f"{provider.model_name}: {redact_text(str(exc))}"
                )
        self.failures = failures
        assert last_error is not None
        if replay_owner is not None:
            if replay_profile is not None:
                raise RuntimeError(
                    "The model and credential profile owning retained opaque "
                    "reasoning state are unavailable; restore that exact route "
                    "or use /restart-context with an explicit handoff."
                ) from last_error
            raise RuntimeError(
                "The model owning retained opaque reasoning state is unavailable; "
                "use /restart-context with an explicit handoff or restore the "
                "original provider before retrying."
            ) from last_error
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
