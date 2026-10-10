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
    def __init__(self, *, token: str | None = None, valid: bool = False) -> None:
        self.token = token
        self.valid = valid
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
