"""Credential identity binding for durable opaque provider replay state."""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

import pytest

from ash.providers.openai import OpenAIProvider
from ash.providers.base import CompletionOutcome
from ash.providers.replay_affinity import (
    ProviderReplayAffinityError,
    ProviderReplayAttempt,
    ProviderReplayBindingRecord,
    provider_replay_attempt,
)
from ash.providers.replay_state import ProviderReplayStateCipher


def test_api_key_material_is_bound_across_fresh_cipher_instances(
    tmp_path: Path,
) -> None:
    secret_a = secrets.token_urlsafe(48)
    secret_b = secrets.token_urlsafe(48)
    provider_a = OpenAIProvider(model_name="gpt-test", api_key=secret_a)
    identity_method = getattr(provider_a, "replay_credential_identity", None)
    assert callable(identity_method), (
        "the active OpenAI API key must expose a private replay identity"
    )
    identity_a = identity_method()
    assert identity_a is not None

    state = [
        {
            "type": "reasoning",
            "id": "rs-affinity-test",
            "summary": [],
            "encrypted_content": "opaque-state-fixture",
        }
    ]
    directory = tmp_path / "provider-replay-state"
    cipher_a = ProviderReplayStateCipher(directory, trusted_root=tmp_path)
    binding = cipher_a.bind_credential_identity(
        identity_a,
        session_scope_id="session-affinity-test",
        owner="openai/gpt-test",
        profile="OPENAI_API_KEY",
        state=state,
    )

    same_provider = OpenAIProvider(model_name="gpt-test", api_key=secret_a)
    changed_provider = OpenAIProvider(model_name="gpt-test", api_key=secret_b)
    changed_route_provider = OpenAIProvider(
        model_name="gpt-test",
        api_key=secret_a,
        base_url="https://other-provider.invalid/v1",
    )
    identity_same = same_provider.replay_credential_identity()
    identity_changed = changed_provider.replay_credential_identity()
    identity_changed_route = changed_route_provider.replay_credential_identity()
    assert identity_same is not None
    assert identity_changed is not None
    assert identity_changed_route is not None

    cipher_after_restart = ProviderReplayStateCipher(
        directory,
        trusted_root=tmp_path,
    )
    assert cipher_after_restart.verify_credential_identity(
        binding,
        identity_same,
        session_scope_id="session-affinity-test",
        owner="openai/gpt-test",
        profile="OPENAI_API_KEY",
        state=state,
    )
    assert not cipher_after_restart.verify_credential_identity(
        binding,
        identity_changed,
        session_scope_id="session-affinity-test",
        owner="openai/gpt-test",
        profile="OPENAI_API_KEY",
        state=state,
    )
    assert not cipher_after_restart.verify_credential_identity(
        binding,
        identity_changed_route,
        session_scope_id="session-affinity-test",
        owner="openai/gpt-test",
        profile="OPENAI_API_KEY",
        state=state,
    )
    for context in (
        {
            "session_scope_id": "other-session",
            "owner": "openai/gpt-test",
            "profile": "OPENAI_API_KEY",
            "state": state,
        },
        {
            "session_scope_id": "session-affinity-test",
            "owner": "openai/other-model",
            "profile": "OPENAI_API_KEY",
            "state": state,
        },
        {
            "session_scope_id": "session-affinity-test",
            "owner": "openai/gpt-test",
            "profile": "OTHER_SLOT",
            "state": state,
        },
        {
            "session_scope_id": "session-affinity-test",
            "owner": "openai/gpt-test",
            "profile": "OPENAI_API_KEY",
            "state": [{**state[0], "encrypted_content": "tampered-state"}],
        },
    ):
        assert not cipher_after_restart.verify_credential_identity(
            binding,
            identity_same,
            **context,
        )
    tampered_binding = {**binding, "tag": "A" * len(binding["tag"])}
    assert not cipher_after_restart.verify_credential_identity(
        tampered_binding,
        identity_same,
        session_scope_id="session-affinity-test",
        owner="openai/gpt-test",
        profile="OPENAI_API_KEY",
        state=state,
    )
    missing_key_directory = tmp_path / "missing-key"
    missing_key_cipher = ProviderReplayStateCipher(
        missing_key_directory,
        trusted_root=tmp_path,
    )
    assert not missing_key_cipher.verify_credential_identity(
        binding,
        identity_same,
        session_scope_id="session-affinity-test",
        owner="openai/gpt-test",
        profile="OPENAI_API_KEY",
        state=state,
    )
    assert not missing_key_directory.exists()
    assert secret_a not in repr(identity_a)
    assert secret_a not in json.dumps(binding)
    completion = CompletionOutcome()
    completion._provider_state_owner = "openai/gpt-test"
    completion._provider_state_credential_profile = "OPENAI_API_KEY"
    completion._provider_state_credential_binding = binding
    assert "provider_state_credential_binding" not in completion.model_dump()
    assert "provider_state_owner" not in completion.model_dump()
    assert "provider_state_credential_profile" not in completion.model_dump()
    assert binding["tag"] not in repr(completion)


def test_changed_api_key_is_refused_before_provider_dispatch(tmp_path: Path) -> None:
    secret_a = secrets.token_urlsafe(48)
    secret_b = secrets.token_urlsafe(48)
    owner = "openai/gpt-test"
    state = [
        {
            "type": "reasoning",
            "id": "rs-pre-dispatch-test",
            "summary": [],
            "encrypted_content": "opaque-state-fixture",
        }
    ]
    cipher = ProviderReplayStateCipher(
        tmp_path / "provider-replay-state",
        trusted_root=tmp_path,
    )
    source = OpenAIProvider(model_name="gpt-test", api_key=secret_a)
    source_identity = source.replay_credential_identity()
    assert source_identity is not None
    binding = cipher.bind_credential_identity(
        source_identity,
        session_scope_id="session-pre-dispatch-test",
        owner=owner,
        profile=None,
        state=state,
    )
    record = ProviderReplayBindingRecord(
        owner=owner,
        profile=None,
        state=state,
        binding=binding,
    )

    class Client:
        def __init__(self) -> None:
            self.calls = 0
            self.chat = self
            self.completions = self

        async def create(self, **_kwargs: Any) -> Any:
            self.calls += 1
            raise AssertionError("mismatched credentials reached dispatch")

    client = Client()
    replacement = OpenAIProvider(
        model_name="gpt-test",
        api_key=secret_b,
        client=client,
    )
    attempt = ProviderReplayAttempt(
        cipher=cipher,
        session_scope_id="session-pre-dispatch-test",
        expected_owner=owner,
        expected_profile=None,
        expected_records=(record,),
    )

    async def dispatch() -> None:
        async for _chunk in replacement.stream_chat(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "provider_state": state,
                }
            ]
        ):
            pass

    async def verify_before_dispatch() -> None:
        with provider_replay_attempt(attempt):
            with pytest.raises(ProviderReplayAffinityError, match="/restart-context"):
                await dispatch()

    asyncio.run(verify_before_dispatch())
    assert client.calls == 0


def test_sdk_effective_base_url_is_bound_for_openai_and_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers.openai_responses import OpenAIResponsesProvider

    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_ORG_ID", raising=False)
    monkeypatch.delenv("OPENAI_PROJECT_ID", raising=False)
    for provider_type in (OpenAIProvider, OpenAIResponsesProvider):
        previous = provider_type(model_name="gpt-fixture", api_key="FICTIONAL_KEY")
        first = previous.replay_credential_identity()
        assert first is not None
        monkeypatch.setenv("OPENAI_BASE_URL", "https://other-origin.invalid/v1")
        later = provider_type(model_name="gpt-fixture", api_key="FICTIONAL_KEY")
        second = later.replay_credential_identity()
        assert second is not None
        assert first._value != second._value, (
            "The effective SDK base URL must be in credential replay identity"
        )
        assert "other-origin.invalid" in str(later._resolve_client().base_url)
        # The live SDK client retains its original routing if env later changes.
        monkeypatch.setenv("OPENAI_BASE_URL", "https://third-origin.invalid/v1")
        assert second._value == later.replay_credential_identity()._value
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


def test_sdk_organization_project_are_bound_for_openai_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Compatible(OpenAIProvider):
        provider_family = "openai-compatible"

    monkeypatch.setenv("OPENAI_ORG_ID", "org-fictional-A")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-fictional-A")
    original = Compatible(
        model_name="gpt-fixture", api_key="FICTIONAL_KEY",
        base_url="https://provider-fixture.invalid/v1",
    )
    previous = original.replay_credential_identity()
    assert previous is not None
    monkeypatch.setenv("OPENAI_ORG_ID", "org-fictional-B")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-fictional-B")
    replacement = Compatible(
        model_name="gpt-fixture", api_key="FICTIONAL_KEY",
        base_url="https://provider-fixture.invalid/v1",
    )
    current = replacement.replay_credential_identity()
    assert current is not None
    assert previous._value != current._value, (
        "Effective SDK organization/project must be in replay identity"
    )
    assert replacement._resolve_client().organization == "org-fictional-B"
    assert replacement._resolve_client().project == "proj-fictional-B"
    assert previous._value == original.replay_credential_identity()._value
