from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from ash.providers.vertex import (
    GoogleAdcTokenProvider,
    VertexCredentialError,
    VertexProvider,
    vertex_openai_base_url,
)


class _Credentials:
    def __init__(
        self,
        *,
        token: str | None = None,
        valid: bool = False,
        service_account_email: str | None = None,
    ) -> None:
        self.token = token
        self.valid = valid
        self.service_account_email = service_account_email
        self.expiry = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.refresh_count = 0

    def refresh(self, request) -> None:
        assert request == "request"
        self.refresh_count += 1
        self.token = f"adc-token-{self.refresh_count}"
        self.valid = True
        self.expiry = datetime.now(timezone.utc) + timedelta(hours=1)


def test_vertex_openai_base_url_requires_explicit_safe_scope() -> None:
    assert vertex_openai_base_url("project-123", "us-central1") == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/project-123/"
        "locations/us-central1/endpoints/openapi"
    )
    assert vertex_openai_base_url("project-123", "global") == (
        "https://aiplatform.googleapis.com/v1/projects/project-123/"
        "locations/global/endpoints/openapi"
    )
    with pytest.raises(ValueError, match="safe path segment"):
        vertex_openai_base_url("project-123", "../../global")


def test_vertex_current_gemini_capabilities_are_exact_and_unknowns_conservative() -> None:
    from ash.providers.capabilities import ProviderCapabilities
    from ash.providers.reasoning import ReasoningEffortSpec

    current = VertexProvider(
        "google/gemini-3.8-flash",
        project="project-123",
        location="global",
        token_provider=GoogleAdcTokenProvider(
            credentials=_Credentials(token="token", valid=True),
            request="request",
        ),
        client=SimpleNamespace(),
    )
    unknown = VertexProvider(
        "google/gemini-future",
        project="project-123",
        location="global",
        token_provider=GoogleAdcTokenProvider(
            credentials=_Credentials(token="token", valid=True),
            request="request",
        ),
        client=SimpleNamespace(),
    )

    assert current.capabilities == ProviderCapabilities(
        native_tools=True,
        vision=True,
        reasoning=True,
        max_input_tokens=1_048_576,
        max_output_tokens=65_536,
        reasoning_effort=ReasoningEffortSpec(("low", "medium", "high")),
    )
    assert unknown.capabilities == ProviderCapabilities()


@pytest.mark.asyncio
async def test_vertex_adc_refresh_is_serialized_and_cached() -> None:
    credentials = _Credentials()
    provider = GoogleAdcTokenProvider(
        credentials=credentials,
        request="request",
    )

    tokens = await asyncio.gather(provider(), provider(), provider())

    assert tokens == ["adc-token-1"] * 3
    assert credentials.refresh_count == 1
    assert provider.redaction_secrets() == ("adc-token-1", "adc-token-1")


@pytest.mark.asyncio
async def test_vertex_replay_identity_survives_access_token_rotation_for_same_service_account() -> None:
    credentials = _Credentials(
        token="access-before",
        valid=True,
        service_account_email="agent@project.iam.gserviceaccount.com",
    )
    credentials.expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    token_provider = GoogleAdcTokenProvider(
        credentials=credentials,
        request="request",
    )
    provider = VertexProvider(
        "google/gemini-test",
        project="project-123",
        location="us-central1",
        token_provider=token_provider,
        client=SimpleNamespace(),
    )

    identity_before_refresh = await provider.resolve_replay_credential_identity()
    credentials.token = "access-after-refresh"
    identity_after_refresh = await provider.resolve_replay_credential_identity()

    assert credentials.token != "access-before"
    assert identity_before_refresh is not None
    assert identity_before_refresh == identity_after_refresh
    credentials.service_account_email = "replacement@project.iam.gserviceaccount.com"
    replacement_identity = await provider.resolve_replay_credential_identity()
    assert replacement_identity is not None
    assert replacement_identity != identity_before_refresh
    assert credentials.token not in repr(identity_before_refresh)


@pytest.mark.asyncio
async def test_vertex_user_adc_without_verified_principal_has_no_replay_identity() -> None:
    provider = VertexProvider(
        "google/gemini-test",
        project="project-123",
        location="global",
        token_provider=GoogleAdcTokenProvider(
            credentials=_Credentials(token="opaque-token", valid=True),
            request="request",
        ),
        client=SimpleNamespace(),
    )

    assert await provider.resolve_replay_credential_identity() is None


@pytest.mark.asyncio
async def test_vertex_provider_redacts_recent_adc_token_from_provider_error() -> None:
    credentials = _Credentials(token="sensitive-adc-token", valid=True)
    credentials.expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    token_provider = GoogleAdcTokenProvider(
        credentials=credentials,
        request="request",
    )
    assert await token_provider() == "sensitive-adc-token"

    class FailingCompletions:
        async def create(self, **kwargs):
            del kwargs
            raise RuntimeError("upstream echoed sensitive-adc-token")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=FailingCompletions()),
    )
    provider = VertexProvider(
        "google/gemini-test",
        project="project-123",
        location="us-central1",
        token_provider=token_provider,
        client=client,
    )

    with pytest.raises(RuntimeError) as exc_info:
        _ = [
            chunk
            async for chunk in provider.stream_chat(
                [{"role": "user", "content": "hello"}]
            )
        ]

    message = str(exc_info.value)
    assert "sensitive-adc-token" not in message
    assert "[REDACTED]" in message


@pytest.mark.asyncio
async def test_vertex_seals_and_replays_gemini_thought_signature(tmp_path) -> None:
    from ash.providers.replay_state import ProviderReplayStateCipher

    class Stream:
        def __init__(self, chunks: list[Any]) -> None:
            self.chunks = chunks

        def __aiter__(self):
            async def generate():
                for chunk in self.chunks:
                    yield chunk

            return generate()

    class Completions:
        def __init__(self, chunks: list[Any]) -> None:
            self.chunks = chunks
            self.kwargs: dict[str, Any] = {}

        async def create(self, **kwargs: Any) -> Stream:
            self.kwargs = kwargs
            return Stream(self.chunks)

    signature = "opaque-vertex-thought-signature"
    tool_call = SimpleNamespace(
        index=0,
        id="call-1",
        function=SimpleNamespace(name="read_file", arguments="{}"),
        extra_content={"google": {"thought_signature": signature}},
    )
    terminal = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="", tool_calls=None),
                finish_reason="tool_calls",
            )
        ],
        usage=None,
    )
    first_completions = Completions(
        [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="", tool_calls=[tool_call]),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            terminal,
        ]
    )
    state_dir = tmp_path / "provider-state"
    first = VertexProvider(
        "google/gemini-3.8-flash",
        project="project-123",
        location="global",
        token_provider=GoogleAdcTokenProvider(
            credentials=_Credentials(token="test-token", valid=True),
            request="request",
        ),
        replay_state_cipher=ProviderReplayStateCipher(
            state_dir,
            trusted_root=tmp_path,
        ),
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=first_completions)
        ),
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    first_chunks = [
        chunk
        async for chunk in first.stream_chat(
            [{"role": "user", "content": "read it"}],
            tools=tools,
        )
    ]
    first_terminal = first_chunks[-1]
    assert first_terminal.provider_state is not None
    assert signature not in json.dumps(first_terminal.provider_state)
    assert first_terminal.provider_state[0]["provider"] == "vertex"
    assert first_terminal.native_tool_calls is not None

    second_completions = Completions(
        [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="done", tool_calls=None),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )
        ]
    )
    second = VertexProvider(
        "google/gemini-3.8-flash",
        project="project-123",
        location="global",
        token_provider=GoogleAdcTokenProvider(
            credentials=_Credentials(token="test-token", valid=True),
            request="request",
        ),
        replay_state_cipher=ProviderReplayStateCipher(
            state_dir,
            trusted_root=tmp_path,
        ),
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=second_completions)
        ),
    )
    history = [
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                call.to_wire() for call in first_terminal.native_tool_calls
            ],
            "provider_state": first_terminal.provider_state,
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "file contents"},
    ]

    _ = [chunk async for chunk in second.stream_chat(history, tools=tools)]

    replayed = second_completions.kwargs["messages"][1]
    assert replayed["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": signature}
    }


@pytest.mark.asyncio
async def test_vertex_gemini3_tool_history_fails_closed_without_signature_state() -> None:
    class Completions:
        async def create(self, **kwargs: Any) -> Any:
            del kwargs
            raise AssertionError("invalid replay must fail before provider request")

    provider = VertexProvider(
        "google/gemini-3.8-flash",
        project="project-123",
        location="global",
        token_provider=GoogleAdcTokenProvider(
            credentials=_Credentials(token="test-token", valid=True),
            request="request",
        ),
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
    )
    history = [
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"call_id": "call-1", "name": "read_file", "arguments": {}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "contents"},
    ]

    with pytest.raises(RuntimeError, match="missing required thought-signature"):
        _ = [chunk async for chunk in provider.stream_chat(history, tools=[])]


@pytest.mark.asyncio
async def test_vertex_verify_credentials_does_not_issue_model_request() -> None:
    credentials = _Credentials()
    token_provider = GoogleAdcTokenProvider(
        credentials=credentials,
        request="request",
    )

    class Completions:
        async def create(self, **kwargs):
            raise AssertionError("model request must not run")

    provider = VertexProvider(
        "google/gemini-test",
        project="project-123",
        location="global",
        token_provider=token_provider,
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
    )

    await provider.verify_credentials()

    assert credentials.refresh_count == 1


@pytest.mark.asyncio
async def test_vertex_adc_rejects_invalid_refreshed_token() -> None:
    class Credentials(_Credentials):
        def refresh(self, request) -> None:
            assert request == "request"
            self.refresh_count += 1
            self.token = ""
            self.valid = True
            self.expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    provider = GoogleAdcTokenProvider(
        credentials=Credentials(),
        request="request",
    )

    with pytest.raises(VertexCredentialError, match="invalid access token"):
        await provider()


@pytest.mark.asyncio
async def test_vertex_adc_refresh_attests_effective_principal_at_token_dispatch(
    tmp_path,
) -> None:
    from ash.providers.replay_affinity import (
        ProviderReplayAffinityError,
        ProviderReplayAttempt,
        ProviderReplayBindingRecord,
        provider_replay_attempt,
    )
    from ash.providers.replay_state import ProviderReplayStateCipher

    class RotatingCredentials(_Credentials):
        replacement_principal = "subject-b@fictional.invalid"

        def refresh(self, request) -> None:
            super().refresh(request)
            self.service_account_email = self.replacement_principal

    credentials = RotatingCredentials(
        token="fictional-token-before", valid=True,
        service_account_email="subject-a@fictional.invalid",
    )
    token_provider = GoogleAdcTokenProvider(
        credentials=credentials, request="request"
    )
    provider = VertexProvider(
        "google/gemini-test", project="project-123", location="global",
        token_provider=token_provider, client=SimpleNamespace(),
    )
    original = await provider.resolve_replay_credential_identity()
    assert original is not None
    state = [{"type": "reasoning", "id": "fixture", "encrypted_content": "OPAQUE"}]
    cipher = ProviderReplayStateCipher(
        tmp_path / "provider-replay-state", trusted_root=tmp_path
    )
    binding = cipher.bind_credential_identity(
        original, session_scope_id="session-root", owner="vertex/google/gemini-test",
        profile=None, state=state,
    )
    record = ProviderReplayBindingRecord(
        owner="vertex/google/gemini-test", profile=None, state=state, binding=binding
    )
    attempt = ProviderReplayAttempt(
        cipher=cipher, session_scope_id="session-root",
        expected_owner="vertex/google/gemini-test", expected_profile=None,
        expected_records=(record,),
    )
    with provider_replay_attempt(attempt):
        with pytest.raises(ProviderReplayAffinityError, match="/restart-context"):
            await provider._api_key()  # SDK's effective token callback
    assert credentials.refresh_count == 1
    assert token_provider.redaction_secrets() == ("adc-token-1",)

    credentials.service_account_email = "subject-a@fictional.invalid"
    credentials.valid = False
    credentials.replacement_principal = "subject-a@fictional.invalid"
    continued = ProviderReplayAttempt(
        cipher=cipher, session_scope_id="session-root",
        expected_owner="vertex/google/gemini-test", expected_profile=None,
        expected_records=(record,),
    )
    with provider_replay_attempt(continued):
        token = await provider._api_key()
    assert token == "adc-token-2"
    assert credentials.refresh_count == 2


@pytest.mark.asyncio
async def test_vertex_sdk_token_callback_rejects_switched_principal_before_transport(
    tmp_path,
) -> None:
    import httpx2
    import openai
    from ash.providers.replay_affinity import (
        ProviderReplayAttempt, ProviderReplayBindingRecord, provider_replay_attempt,
    )
    from ash.providers.replay_state import ProviderReplayStateCipher
    from ash.providers.retry import classify_provider_failure

    class ReplacementCredentials(_Credentials):
        def refresh(self, request) -> None:
            super().refresh(request)
            self.service_account_email = "different-owner@fictional.invalid"

    credentials = ReplacementCredentials(
        token="fictional-original", valid=True,
        service_account_email="original-owner@fictional.invalid",
    )
    provider = VertexProvider(
        "google/gemini-fixture", project="fixture-project", location="global",
        token_provider=GoogleAdcTokenProvider(
            credentials=credentials, request="request",
        ),
    )
    observed_requests: list[Any] = []

    def fake_transport(request: Any) -> Any:
        observed_requests.append(request)
        return httpx2.Response(200, json={"choices": []})

    provider._client = openai.AsyncOpenAI(
        api_key=provider._scoped_request_token,
        base_url=vertex_openai_base_url(provider.project, provider.location),
        max_retries=0,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(fake_transport), trust_env=False,
        ),
    )
    assert provider._owns_client is True
    identity = await provider.resolve_replay_credential_identity()
    assert identity is not None
    state = [{
        "type": "reasoning", "id": "fictional", "summary": [],
        "encrypted_content": "FICTIONAL_SOURCE_A",
    }]
    cipher = ProviderReplayStateCipher(
        tmp_path / "replay-state", trusted_root=tmp_path,
    )
    owner = "vertex/" + provider.model_name
    binding = cipher.bind_credential_identity(
        identity, session_scope_id="root-a", owner=owner,
        profile=None, state=state,
    )
    attempt = ProviderReplayAttempt(
        cipher=cipher, session_scope_id="root-a",
        expected_owner=owner, expected_profile=None,
        expected_records=(ProviderReplayBindingRecord(
            owner=owner, profile=None, state=state, binding=binding,
        ),),
    )
    try:
        with provider_replay_attempt(attempt):
            with pytest.raises(Exception) as exc_info:
                _ = [
                    chunk async for chunk in provider.stream_chat([{
                        "role": "assistant", "content": "", "provider_state": state,
                    }])
                ]
        assert not classify_provider_failure(exc_info.value).retriable
        chain = []
        current: BaseException | None = exc_info.value
        while current is not None and len(chain) < 8:
            chain.append(type(current).__name__)
            current = (
                current.__cause__
                if current.__cause__ is not None else current.__context__
            )
        assert "ProviderReplayAffinityError" in chain
        assert observed_requests == []
    finally:
        await provider.aclose()
