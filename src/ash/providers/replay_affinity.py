"""Request-scoped credential checks for durable opaque provider state."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
import inspect
from typing import Any, Iterator, Mapping

from ash.providers.replay_state import (
    ProviderReplayCredentialIdentity,
    ProviderReplayStateCipher,
)


_RESTART_GUIDANCE = (
    "Opaque provider history has no verified credential binding, so its "
    "credential cannot be verified. This turn cannot complete safely. Use "
    "/restart-context <handoff-for-next-turn> in the "
    "interactive terminal or ash sessions restart-context --session SESSION "
    "--handoff TEXT from a shell, then resume the child session after checking "
    "the parent's recovery state. Earlier actions and approvals are not copied."
)


class ProviderReplayAffinityError(RuntimeError):
    """Opaque history cannot be safely dispatched with the current credential."""

    retriable = False

    def __init__(self) -> None:
        super().__init__(_RESTART_GUIDANCE)


@dataclass(frozen=True)
class ProviderReplayBindingRecord:
    """One durable state value and its authenticated, non-secret binding."""

    owner: str
    profile: str | None
    state: list[dict[str, Any]]
    binding: Mapping[str, Any]


@dataclass
class ProviderReplayAttempt:
    """Credential provenance for exactly one provider dispatch attempt."""

    cipher: ProviderReplayStateCipher | None
    session_scope_id: str | None
    expected_owner: str | None = None
    expected_profile: str | None = None
    expected_records: tuple[ProviderReplayBindingRecord, ...] = ()
    selected_profile: str | None = None
    owner: str | None = None
    identity: ProviderReplayCredentialIdentity | None = field(
        default=None,
        repr=False,
    )

    def select_profile(self, profile: str) -> None:
        self.selected_profile = profile

    def verify_identity(
        self,
        identity: ProviderReplayCredentialIdentity | None,
        *,
        owner: str,
    ) -> None:
        self.owner = owner
        self.identity = identity
        if not self.expected_records:
            return
        if (
            identity is None
            or owner != self.expected_owner
            or self.selected_profile != self.expected_profile
        ):
            raise ProviderReplayAffinityError
        for record in self.expected_records:
            if (
                record.owner != owner
                or record.profile != self.selected_profile
                or self.cipher is None
                or not self.cipher.verify_credential_identity(
                    record.binding,
                    identity,
                    session_scope_id=self.session_scope_id or "",
                    owner=owner,
                    profile=self.selected_profile,
                    state=record.state,
                )
            ):
                raise ProviderReplayAffinityError

    def output_binding(self, state: list[dict[str, Any]]) -> dict[str, Any] | None:
        if (
            not state
            or self.session_scope_id is None
            or self.owner is None
            or self.identity is None
            or self.cipher is None
        ):
            return None
        return self.cipher.bind_credential_identity(
            self.identity,
            session_scope_id=self.session_scope_id,
            owner=self.owner,
            profile=self.selected_profile,
            state=state,
        )


_current_attempt: ContextVar[ProviderReplayAttempt | None] = ContextVar(
    "ash_provider_replay_attempt",
    default=None,
)


@contextmanager
def provider_replay_attempt(
    attempt: ProviderReplayAttempt,
) -> Iterator[ProviderReplayAttempt]:
    """Bind credential provenance to this task until its stream completes."""

    token = enter_provider_replay_attempt(attempt)
    try:
        yield attempt
    finally:
        leave_provider_replay_attempt(token)


def enter_provider_replay_attempt(
    attempt: ProviderReplayAttempt,
) -> Token[ProviderReplayAttempt | None]:
    return _current_attempt.set(attempt)


def leave_provider_replay_attempt(
    token: Token[ProviderReplayAttempt | None],
) -> None:
    _current_attempt.reset(token)


def select_provider_replay_profile(profile: str) -> None:
    attempt = _current_attempt.get()
    if attempt is not None:
        attempt.select_profile(profile)


async def resolve_provider_replay_identity(
    provider: Any,
) -> ProviderReplayCredentialIdentity | None:
    """Ask a provider for the identity of the credential it will dispatch."""

    getter = getattr(provider, "resolve_replay_credential_identity", None)
    if not callable(getter):
        getter = getattr(provider, "replay_credential_identity", None)
    if not callable(getter):
        return None
    identity = getter()
    if inspect.isawaitable(identity):
        identity = await identity
    return (
        identity
        if isinstance(identity, ProviderReplayCredentialIdentity)
        else None
    )


def verify_provider_replay_identity(
    identity: ProviderReplayCredentialIdentity | None,
    *,
    provider_family: str,
    model_name: str,
) -> None:
    attempt = _current_attempt.get()
    if attempt is None:
        return
    attempt.verify_identity(
        identity,
        owner=f"{provider_family}/{model_name}",
    )
