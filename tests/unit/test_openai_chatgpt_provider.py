from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ash.providers.base import ProviderIncompleteStreamError, ProviderTerminalError
from ash.providers.openai_chatgpt import (
    ChatGPTResponsesTerminalError,
    OpenAIChatGPTProvider,
    prepare_chatgpt_responses_input,
)


class _AuthSession:
    async def access_token(self) -> str:
        return "oauth-access-token"


class _AsyncEvents:
    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)

    def __aiter__(self):
        self._iterator = iter(self._events)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _Responses:
    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> _AsyncEvents:
        self.kwargs = kwargs
        return _AsyncEvents(self.events)


class _Client:
    def __init__(self, events: list[Any]) -> None:
        self.responses = _Responses(events)


def _event(event_type: str, **kwargs: Any) -> Any:
    return SimpleNamespace(type=event_type, **kwargs)


def test_responses_history_replays_reasoning_calls_and_outputs_in_order() -> None:
    instructions, items = prepare_chatgpt_responses_input(
        [
            {"role": "system", "content": "system rules"},
            {"role": "user", "content": "inspect"},
            {
                "role": "assistant",
                "content": "",
                "provider_state": [
                    {
                        "type": "reasoning",
                        "id": "rs_1",
                        "summary": [],
                        "encrypted_content": "opaque",
                        "status": "completed",
                    }
                ],
                "tool_calls": [
                    {
                        "call_id": "call_1",
                        "name": "read_file",
                        "arguments": {"file_path": "a.py"},
                    }
                ],
            },
            {
                "role": "tool",
                "content": "file contents",
                "tool_call_id": "call_1",
            },
        ]
    )

    assert instructions == "system rules"
    assert items == [
        {"role": "user", "content": "inspect"},
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [],
            "encrypted_content": "opaque",
            "status": "completed",
        },
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": '{"file_path":"a.py"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "file contents",
        },
    ]


@pytest.mark.asyncio
async def test_chatgpt_provider_enforces_preview_request_and_terminal_contract() -> None:
    reasoning = SimpleNamespace(
        type="reasoning",
        id="rs_1",
        encrypted_content="opaque-reasoning",
        summary=[SimpleNamespace(text="brief summary")],
        status="completed",
    )
    function_call = SimpleNamespace(
        type="function_call",
        call_id="call_1",
        name="read_file",
        arguments='{"file_path":"a.py"}',
    )
    usage = SimpleNamespace(
        input_tokens=20,
        output_tokens=5,
        input_tokens_details=SimpleNamespace(cached_tokens=3),
    )
    events = [
        _event("response.output_text.delta", delta="working"),
        _event("response.output_item.done", item=reasoning),
        _event("response.output_item.done", item=function_call),
        _event("response.completed", response=SimpleNamespace(usage=usage)),
    ]
    client = _Client(events)
    tokens: list[str] = []
    provider = OpenAIChatGPTProvider(
        "gpt-test",
        auth_session=_AuthSession(),  # type: ignore[arg-type]
        client_factory=lambda token: tokens.append(token) or client,
    )

    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [
                {"role": "system", "content": "system rules"},
                {"role": "user", "content": "inspect"},
            ],
            temperature=0.9,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file",
                        "parameters": {
                            "type": "object",
                            "properties": {"file_path": {"type": "string"}},
                            "required": ["file_path"],
                        },
                    },
                }
            ],
        )
    ]

    assert tokens == ["oauth-access-token"]
    assert client.responses.kwargs is not None
    request = client.responses.kwargs
    assert request["store"] is False
    assert request["stream"] is True
    assert request["include"] == ["reasoning.encrypted_content"]
    assert request["instructions"] == "system rules"
    assert "temperature" not in request
    assert "max_output_tokens" not in request
    assert request["tools"][0] == {
        "type": "function",
        "name": "read_file",
        "description": "Read a file",
        "parameters": {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        },
        "strict": False,
    }
    assert chunks[0].content == "working"
    terminal = chunks[-1]
    assert terminal.is_done is True
    assert terminal.prompt_tokens == 20
    assert terminal.completion_tokens == 5
    assert terminal.cache_read_tokens == 3
    assert terminal.native_tool_calls is not None
    assert terminal.native_tool_calls[0].call_id == "call_1"
    assert terminal.provider_state == [
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": "brief summary"}],
            "encrypted_content": "opaque-reasoning",
            "status": "completed",
        }
    ]


@pytest.mark.asyncio
async def test_chatgpt_provider_fails_closed_without_completed_event() -> None:
    client = _Client([_event("response.output_text.delta", delta="partial")])
    provider = OpenAIChatGPTProvider(
        "gpt-test",
        auth_session=_AuthSession(),  # type: ignore[arg-type]
        client_factory=lambda _token: client,
    )

    with pytest.raises(ProviderIncompleteStreamError, match="response.completed"):
        _ = [
            chunk
            async for chunk in provider.stream_chat(
                [{"role": "user", "content": "hello"}]
            )
        ]


@pytest.mark.asyncio
async def test_chatgpt_provider_surfaces_failed_terminal_code() -> None:
    client = _Client(
        [
            _event(
                "response.failed",
                response=SimpleNamespace(
                    error=SimpleNamespace(
                        code="subscription_sharing_usage_limit_exceeded"
                    )
                ),
            )
        ]
    )
    provider = OpenAIChatGPTProvider(
        "gpt-test",
        auth_session=_AuthSession(),  # type: ignore[arg-type]
        client_factory=lambda _token: client,
    )

    with pytest.raises(
        ProviderTerminalError,
        match="subscription_sharing_usage_limit_exceeded",
    ):
        _ = [
            chunk
            async for chunk in provider.stream_chat(
                [{"role": "user", "content": "hello"}]
            )
        ]


@pytest.mark.asyncio
async def test_chatgpt_provider_streams_refusal_text() -> None:
    client = _Client(
        [
            _event("response.refusal.delta", delta="I cannot help with that."),
            _event(
                "response.completed",
                response=SimpleNamespace(usage=None),
            ),
        ]
    )
    provider = OpenAIChatGPTProvider(
        "gpt-6.1-sol",
        auth_session=_AuthSession(),  # type: ignore[arg-type]
        client_factory=lambda _token: client,
    )
    provider.configure_reasoning_effort("high")

    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [{"role": "user", "content": "hello"}]
        )
    ]

    assert chunks[0].content == "I cannot help with that."
    assert chunks[-1].is_done is True
    assert client.responses.kwargs is not None
    assert client.responses.kwargs["reasoning"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_chatgpt_top_level_error_preserves_retryable_code() -> None:
    client = _Client(
        [
            _event(
                "error",
                code="subscription_sharing_user_unavailable",
                message="temporarily unavailable",
                param=None,
            )
        ]
    )
    provider = OpenAIChatGPTProvider(
        "gpt-test",
        auth_session=_AuthSession(),  # type: ignore[arg-type]
        client_factory=lambda _token: client,
    )

    with pytest.raises(ChatGPTResponsesTerminalError) as exc_info:
        _ = [
            chunk
            async for chunk in provider.stream_chat(
                [{"role": "user", "content": "hello"}]
            )
        ]

    assert exc_info.value.stop_reason == "subscription_sharing_user_unavailable"
    assert exc_info.value.retriable is True


def test_chatgpt_response_failure_retry_classification_is_conservative() -> None:
    temporary = ChatGPTResponsesTerminalError(
        "subscription_sharing_usage_unavailable"
    )
    exhausted = ChatGPTResponsesTerminalError(
        "subscription_sharing_usage_limit_exceeded"
    )

    assert temporary.retriable is True
    assert exhausted.retriable is False


def test_stream_chunk_rejects_malformed_provider_replay_state() -> None:
    with pytest.raises(ValueError, match="unsupported field"):
        from ash.providers.base import StreamChunk

        StreamChunk(
            provider_state=[
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [],
                    "encrypted_content": "opaque",
                    "unsafe": "injected",
                }
            ]
        )
