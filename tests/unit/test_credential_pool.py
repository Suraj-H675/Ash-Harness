from __future__ import annotations

from collections.abc import Sequence
from typing import Any, AsyncGenerator

import pytest

from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.providers.credential_pool import (
    CredentialPoolProvider,
    ProviderCredentialPoolCoolingDown,
)
from ash.providers.messages import MessageInput


class APIError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int,
        *,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.headers = (
            {"retry-after": retry_after} if retry_after is not None else {}
        )


class FakeProvider(ProviderABC):
    provider_family = "openai"

    def __init__(
        self,
        name: str,
        outcomes: list[BaseException | list[StreamChunk]],
        *,
        capability_outcomes: list[BaseException | ProviderCapabilities] | None = None,
    ) -> None:
        self._model_name = name
        self.outcomes = list(outcomes)
        self.capability_outcomes = list(capability_outcomes or [])
        self.calls = 0
        self.detect_calls = 0
        self.closed = 0

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(native_tools=True, context_window=128_000)

    def count_tokens(self, text: str) -> int:
        return len(text)

    async def detect_capabilities(
        self,
        *,
        refresh: bool = False,
    ) -> ProviderCapabilities:
        del refresh
        self.detect_calls += 1
        if not self.capability_outcomes:
            return self.capabilities
        outcome = self.capability_outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def stream_chat(
        self,
        messages: Sequence[MessageInput],
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        del messages, temperature, tools
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        for chunk in outcome:
            yield chunk

    async def aclose(self) -> None:
        self.closed += 1


def _success(text: str) -> list[StreamChunk]:
    return [
        StreamChunk(content=text),
        StreamChunk(is_done=True, stop_reason="stop"),
    ]


@pytest.mark.asyncio
async def test_auth_failure_rotates_and_success_becomes_sticky() -> None:
    primary = FakeProvider(
        "model-a",
        [APIError("bad key", 401)],
    )
    backup = FakeProvider(
        "model-a",
        [_success("backup-1"), _success("backup-2")],
    )
    pool = CredentialPoolProvider(
        [primary, backup],
        ["OPENAI_PRIMARY", "OPENAI_BACKUP"],
    )

    first = [chunk async for chunk in pool.stream_chat([])]
    second = [chunk async for chunk in pool.stream_chat([])]

    assert first[0].content == "backup-1"
    assert second[0].content == "backup-2"
    assert primary.calls == 1
    assert backup.calls == 2
    assert pool.active_profile == "OPENAI_BACKUP"


@pytest.mark.asyncio
async def test_owned_replay_uses_exact_credential_profile_without_rotation() -> None:
    primary = FakeProvider("model-a", [_success("wrong-profile")])
    owner = FakeProvider("model-a", [_success("owning-profile")])
    pool = CredentialPoolProvider([primary, owner], ["OPENAI_PRIMARY", "OPENAI_BACKUP"])
    history = [
        {
            "role": "assistant",
            "content": "Earlier answer",
            "provider_state": [
                {
                    "type": "reasoning",
                    "id": "rs-owned",
                    "summary": [],
                    "encrypted_content": "profile-owned-state",
                }
            ],
        },
        {"role": "user", "content": "Continue"},
    ]

    chunks = [
        chunk
        async for chunk in pool.stream_chat(history, replay_profile="OPENAI_BACKUP")
    ]

    assert chunks[0].content == "owning-profile"
    assert primary.calls == 0
    assert owner.calls == 1
    assert pool.active_profile == "OPENAI_BACKUP"


@pytest.mark.asyncio
async def test_unowned_replay_refuses_multi_profile_pool_before_dispatch() -> None:
    primary = FakeProvider("model-a", [_success("must-not-run")])
    backup = FakeProvider("model-a", [_success("must-not-run")])
    pool = CredentialPoolProvider(
        [primary, backup], ["OPENAI_PRIMARY", "OPENAI_BACKUP"]
    )
    history = [
        {
            "role": "assistant",
            "content": "Earlier answer",
            "provider_state": [
                {
                    "type": "reasoning",
                    "id": "rs-legacy",
                    "summary": [],
                    "encrypted_content": "unknown-profile-state",
                }
            ],
        },
    ]

    with pytest.raises(RuntimeError, match="/restart-context"):
        _ = [chunk async for chunk in pool.stream_chat(history)]

    assert primary.calls == 0
    assert backup.calls == 0


@pytest.mark.asyncio
async def test_legacy_unowned_replay_refuses_even_with_one_current_profile() -> None:
    """Current pool size cannot prove a legacy state's historical owner."""
    only = FakeProvider("model-a", [_success("must-not-receive-replay")])
    pool = CredentialPoolProvider([only], ["OPENAI_ONLY"])
    history = [
        {
            "role": "assistant",
            "content": "Earlier answer",
            "provider_state": [
                {
                    "type": "reasoning",
                    "id": "rs-single-profile",
                    "summary": [],
                    "encrypted_content": "legacy-single-profile-state",
                }
            ],
        }
    ]

    with pytest.raises(RuntimeError, match="/restart-context"):
        _ = [chunk async for chunk in pool.stream_chat(history)]

    assert only.calls == 0


@pytest.mark.asyncio
async def test_owned_replay_refuses_missing_profile_before_dispatch() -> None:
    primary = FakeProvider("model-a", [_success("must-not-run")])
    backup = FakeProvider("model-a", [_success("must-not-run")])
    pool = CredentialPoolProvider(
        [primary, backup], ["OPENAI_PRIMARY", "OPENAI_BACKUP"]
    )
    history = [
        {
            "role": "assistant",
            "content": "Earlier answer",
            "provider_state": [
                {
                    "type": "reasoning",
                    "id": "rs-missing",
                    "summary": [],
                    "encrypted_content": "missing-profile-state",
                }
            ],
        },
    ]

    with pytest.raises(RuntimeError, match="/restart-context"):
        _ = [
            chunk
            async for chunk in pool.stream_chat(
                history, replay_profile="REMOVED_PROFILE"
            )
        ]

    assert primary.calls == 0
    assert backup.calls == 0


@pytest.mark.asyncio
async def test_owned_profile_failure_never_rotates_replay_to_other_profile() -> None:
    owner = FakeProvider("model-a", [APIError("owner account rejected", 401)])
    other = FakeProvider("model-a", [_success("must-not-receive-replay")])
    pool = CredentialPoolProvider([other, owner], ["OPENAI_PRIMARY", "OPENAI_BACKUP"])
    history = [
        {
            "role": "assistant",
            "content": "Earlier answer",
            "provider_state": [
                {
                    "type": "reasoning",
                    "id": "rs-failed-owner",
                    "summary": [],
                    "encrypted_content": "owner-only-state",
                }
            ],
        },
    ]

    with pytest.raises(RuntimeError, match="/restart-context"):
        _ = [
            chunk
            async for chunk in pool.stream_chat(history, replay_profile="OPENAI_BACKUP")
        ]

    assert owner.calls == 1
    assert other.calls == 0


@pytest.mark.asyncio
async def test_owned_profile_transient_failure_does_not_rotate_replay() -> None:
    owner = FakeProvider("model-a", [APIError("upstream unavailable", 503)])
    other = FakeProvider("model-a", [_success("must-not-receive-replay")])
    pool = CredentialPoolProvider([other, owner], ["PRIMARY", "OWNER"])
    history = [
        {
            "role": "assistant",
            "content": "Earlier answer",
            "provider_state": [
                {
                    "type": "reasoning",
                    "id": "rs-transient-owner",
                    "summary": [],
                    "encrypted_content": "owner-only-state",
                }
            ],
        }
    ]

    with pytest.raises(APIError):
        _ = [
            chunk
            async for chunk in pool.stream_chat(history, replay_profile="OWNER")
        ]

    assert owner.calls == 1
    assert other.calls == 0


@pytest.mark.asyncio
async def test_owned_profile_cooldown_does_not_rotate_replay() -> None:
    now = 100.0
    owner = FakeProvider(
        "model-a",
        [APIError("owner profile rate limited", 429, retry_after="17")],
    )
    other = FakeProvider(
        "model-a",
        [_success("initial fallback"), _success("must-not-receive-replay")],
    )
    pool = CredentialPoolProvider(
        [owner, other],
        ["OPENAI_OWNER", "OPENAI_OTHER"],
        clock=lambda: now,
    )
    history = [
        {
            "role": "assistant",
            "content": "Earlier answer",
            "provider_state": [
                {
                    "type": "reasoning",
                    "id": "rs-cooling-owner",
                    "summary": [],
                    "encrypted_content": "owner-only-state",
                }
            ],
        }
    ]

    initial = [chunk async for chunk in pool.stream_chat([])]
    assert initial[0].content == "initial fallback"

    with pytest.raises(RuntimeError, match="cooling down"):
        _ = [
            chunk
            async for chunk in pool.stream_chat(
                history,
                replay_profile="OPENAI_OWNER",
            )
        ]

    assert owner.calls == 1
    assert other.calls == 1


@pytest.mark.asyncio
async def test_rate_limit_honors_retry_after_and_skips_cooling_profile() -> None:
    now = 100.0
    primary = FakeProvider(
        "model-a",
        [APIError("rate limited", 429, retry_after="17")],
    )
    backup = FakeProvider(
        "model-a",
        [_success("backup"), _success("backup-again")],
    )
    pool = CredentialPoolProvider(
        [primary, backup],
        ["PRIMARY", "BACKUP"],
        clock=lambda: now,
    )

    _ = [chunk async for chunk in pool.stream_chat([])]
    snapshot = pool.snapshot()
    assert snapshot[0]["cooling_down"] is True
    assert snapshot[0]["retry_after"] == 17.0
    assert snapshot[0]["reason"] == "rate_limit"

    _ = [chunk async for chunk in pool.stream_chat([])]
    assert primary.calls == 1
    assert backup.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        APIError("invalid request", 400),
        APIError("server overloaded", 503),
        ConnectionError("connection reset"),
    ],
)
async def test_noncredential_failures_do_not_rotate(error: BaseException) -> None:
    primary = FakeProvider("model-a", [error])
    backup = FakeProvider("model-a", [_success("must-not-run")])
    pool = CredentialPoolProvider([primary, backup], ["PRIMARY", "BACKUP"])

    with pytest.raises(type(error)):
        _ = [chunk async for chunk in pool.stream_chat([])]

    assert primary.calls == 1
    assert backup.calls == 0


@pytest.mark.asyncio
async def test_pool_never_rotates_after_reasoning_or_provider_state() -> None:
    for committed in (
        StreamChunk(reasoning=[{"type": "thinking", "thinking": "partial"}]),
        StreamChunk(
            provider_state=[
                {
                    "type": "reasoning",
                    "id": "state-1",
                    "summary": [],
                    "encrypted_content": "opaque",
                }
            ]
        ),
    ):
        class PartialProvider(FakeProvider):
            async def stream_chat(
                self,
                messages: Sequence[MessageInput],
                temperature: float = 0.0,
                tools: list[dict[str, Any]] | None = None,
            ) -> AsyncGenerator[StreamChunk, None]:
                del messages, temperature, tools
                self.calls += 1
                yield committed
                raise APIError("late auth failure", 401)

        primary = PartialProvider("model-a", [])
        backup = FakeProvider("model-a", [_success("must-not-run")])
        pool = CredentialPoolProvider([primary, backup], ["PRIMARY", "BACKUP"])
        seen: list[StreamChunk] = []

        with pytest.raises(APIError, match="late auth failure"):
            async for chunk in pool.stream_chat([]):
                seen.append(chunk)

        assert seen == [committed]
        assert backup.calls == 0


@pytest.mark.asyncio
async def test_all_cooling_profiles_raise_bounded_retry_signal() -> None:
    now = 100.0
    first = FakeProvider("model-a", [APIError("bad key", 401)])
    second = FakeProvider("model-a", [APIError("no credits", 402)])
    pool = CredentialPoolProvider(
        [first, second],
        ["PRIMARY", "BACKUP"],
        clock=lambda: now,
    )

    with pytest.raises(APIError, match="no credits"):
        _ = [chunk async for chunk in pool.stream_chat([])]

    with pytest.raises(ProviderCredentialPoolCoolingDown) as exc_info:
        _ = [chunk async for chunk in pool.stream_chat([])]

    assert exc_info.value.retry_after == 60.0


@pytest.mark.asyncio
async def test_capability_detection_rotates_auth_failure_and_sticks() -> None:
    primary = FakeProvider(
        "model-a",
        [],
        capability_outcomes=[APIError("bad key", 401)],
    )
    backup_capabilities = ProviderCapabilities(
        native_tools=True,
        context_window=64_000,
    )
    backup = FakeProvider(
        "model-a",
        [],
        capability_outcomes=[backup_capabilities],
    )
    pool = CredentialPoolProvider(
        [primary, backup],
        ["PRIMARY", "BACKUP"],
    )

    assert await pool.detect_capabilities() == backup_capabilities
    assert primary.detect_calls == 1
    assert backup.detect_calls == 1
    assert pool.active_profile == "BACKUP"


@pytest.mark.asyncio
async def test_pool_closes_every_child_once() -> None:
    first = FakeProvider("model-a", [])
    second = FakeProvider("model-a", [])
    pool = CredentialPoolProvider([first, second], ["PRIMARY", "BACKUP"])

    await pool.aclose()
    await pool.aclose()

    assert first.closed == 1
    assert second.closed == 1


def test_snapshot_contains_profile_ids_not_credentials() -> None:
    first = FakeProvider("model-a", [])
    second = FakeProvider("model-a", [])
    pool = CredentialPoolProvider(
        [first, second],
        ["OPENAI_PRIMARY", "OPENAI_BACKUP"],
    )

    rendered = repr(pool.snapshot())

    assert "OPENAI_PRIMARY" in rendered
    assert "OPENAI_BACKUP" in rendered
    assert "secret" not in rendered.casefold()
