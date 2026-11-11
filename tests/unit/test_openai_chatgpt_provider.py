from __future__ import annotations

import json
import traceback
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


@pytest.mark.asyncio
async def test_native_compaction_retains_complete_output_window_without_instructions() -> None:
    from ash.providers.openai_responses import (
        OpenAIResponsesProvider,
        prepare_openai_responses_input,
    )

    class Responses:
        kwargs: dict[str, Any]

        @property
        def with_raw_response(self) -> Responses:
            return self

        async def compact(self, **kwargs: Any) -> Any:
            self.kwargs = kwargs
            output = [
                {
                    "type": "message", "id": "msg-1", "role": "user",
                    "status": "completed",
                    "content": [{"type": "input_text", "text": "Original task"}],
                },
                {
                    "type": "compaction", "id": "cp-1",
                    "encrypted_content": "opaque-checkpoint",
                },
            ]
            return SimpleNamespace(content=json.dumps({
                "object": "response.compaction", "output": output
            }).encode())

    responses = Responses()
    provider = OpenAIResponsesProvider(
        "gpt-6-luna", "synthetic-api-key",
        client=SimpleNamespace(responses=responses),
    )
    history = [
        {"role": "system", "content": "system policy"},
        {"role": "user", "content": "Original task"},
        {"role": "assistant", "content": "I inspected the code"},
    ]
    compacted = await provider.compact_history(history)
    assert responses.kwargs == {
        "model": "gpt-6-luna",
        "input": [
            {"role": "user", "content": "Original task"},
            {"role": "assistant", "content": "I inspected the code"},
        ],
        "timeout": 30,
    }
    assert compacted == [
        {
            "type": "responses_compacted_input",
            "output": [
                {
                    "type": "message", "id": "msg-1", "role": "user",
                    "status": "completed",
                    "content": [{"type": "input_text", "text": "Original task"}],
                },
                {"type": "compaction", "id": "cp-1",
                 "encrypted_content": "opaque-checkpoint"},
            ],
        }
    ]
    instructions, response_input = prepare_openai_responses_input([
        {"role": "system", "content": "system policy"},
        {"role": "assistant", "content": "", "provider_state": compacted},
        {"role": "user", "content": "Continue safely"},
    ])
    assert instructions == "system policy"
    assert response_input == [
        *compacted[0]["output"], {"role": "user", "content": "Continue safely"}
    ]


@pytest.mark.asyncio
async def test_native_compaction_uses_real_sdk_raw_http_response_without_coercion() -> None:
    import httpx
    import openai

    from ash.providers.openai_responses import compact_openai_responses_history

    output = [
        {
            "type": "message", "id": "msg-2", "role": "user",
            "status": "completed",
            "content": [{"type": "input_text", "text": "Original task"}],
        },
        {"type": "compaction", "id": "cp-1", "encrypted_content": "opaque"},
    ]
    response = {
        "id": "resp-synthetic", "object": "response.compaction",
        "created_at": 1764967971,
        "output": output,
        "usage": {
            "input_tokens": 1, "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 1, "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 2,
        },
    }
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        async with openai.AsyncOpenAI(
            api_key="synthetic-only", http_client=http
        ) as client:
            compacted = await compact_openai_responses_history(
                client,
                model_name="gpt-6-luna",
                messages=[{"role": "user", "content": "Original task"}],
            )
    assert requests == ["/v1/responses/compact"]
    assert compacted == [{"type": "responses_compacted_input", "output": output}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_body", "error_pattern"),
    [
        (b'{"output": [', "invalid JSON"),
        (
            b'{"object":"response.compaction","output":[{"type":"compaction",'
            b'"encrypted_content":"first","encrypted_content":"second"}]}',
            "invalid JSON",
        ),
        (
            b'{"object":"response","output":[{"type":"compaction",'
            b'"encrypted_content":"opaque"}]}',
            "unexpected object type",
        ),
        (
            b'{"output":[{"type":"compaction","encrypted_content":"opaque"}]}',
            "unexpected object type",
        ),
        (b'x' * (4 * 1024 * 1024 + 1), "oversized response"),
        (
            json.dumps({
                "object": "response.compaction",
                "output": [
                    {
                        "type": "message", "role": "user",
                        "content": [{"type": "function_call", "name": "shell"}],
                    },
                    {"type": "compaction", "encrypted_content": "opaque"},
                ],
            }).encode(),
            "malformed or oversized checkpoint",
        ),
    ],
)
async def test_native_compaction_rejects_unsafe_raw_http_responses(
    raw_body: bytes, error_pattern: str
) -> None:
    from ash.providers.openai_responses import compact_openai_responses_history

    class Responses:
        @property
        def with_raw_response(self) -> Responses:
            return self

        async def compact(self, **kwargs: Any) -> Any:
            return SimpleNamespace(content=raw_body)

    with pytest.raises(RuntimeError, match=error_pattern):
        await compact_openai_responses_history(
            SimpleNamespace(responses=Responses()),
            model_name="gpt-6-luna",
            messages=[{"role": "user", "content": "Synthetic request"}],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("item", [
    {"type": "reasoning", "id": "rs", "encrypted_content": "opaque"},
    {"type": "compaction", "id": "cp", "encrypted_content": ""},
    {"type": "compaction", "id": 123, "encrypted_content": "opaque"},
    {"type": "function_call", "call_id": "oops", "name": "write_file"},
])
async def test_native_compaction_rejects_unsupported_or_invalid_results(
    item: Any,
) -> None:
    from ash.providers.openai_responses import compact_openai_responses_history

    class Responses:
        @property
        def with_raw_response(self) -> Responses:
            return self

        async def compact(self, **kwargs: Any) -> Any:
            return SimpleNamespace(content=json.dumps({
                "object": "response.compaction", "output": [item]
            }).encode())

    with pytest.raises(RuntimeError):
        await compact_openai_responses_history(
            SimpleNamespace(responses=Responses()),
            model_name="gpt-6-luna",
            messages=[{"role": "user", "content": "minimal synthetic"}],
        )


@pytest.mark.asyncio
async def test_native_compaction_provider_errors_redact_api_credentials() -> None:
    from ash.providers.openai_responses import OpenAIResponsesProvider

    class Responses:
        @property
        def with_raw_response(self) -> Responses:
            return self

        async def compact(self, **kwargs: Any) -> Any:
            raise RuntimeError("Transport failed with synthetic-secret-key")

    provider = OpenAIResponsesProvider(
        "gpt-6-luna", "synthetic-secret-key",
        client=SimpleNamespace(responses=Responses()),
    )
    with pytest.raises(RuntimeError) as error:
        await provider.compact_history(
            [{"role": "user", "content": "synthetic input"}]
        )
    assert "synthetic-secret-key" not in str(error.value)
    assert "synthetic-secret-key" not in "".join(
        traceback.format_exception(error.value)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("response_body", [
    b'{"object":"response.compaction","output":[{"type":"compaction",'
    b'"encrypted_content":"synthetic-sensitive-untrusted-item",'
    b'"encrypted_content":"duplicate"}]}',
    json.dumps({
        "object": "response.compaction",
        "output": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "synthetic-sensitive-untrusted-item",
                          "untrusted_extra": "reject"}]},
            {"type": "compaction", "encrypted_content": "synthetic-opaque"},
        ],
    }).encode("utf-8"),
])
async def test_native_compaction_rejected_raw_payload_never_leaks_into_traceback(
    response_body: bytes,
) -> None:
    from ash.providers.openai_responses import compact_openai_responses_history

    class Responses:
        @property
        def with_raw_response(self) -> Responses:
            return self

        async def compact(self, **kwargs: Any) -> Any:
            return SimpleNamespace(content=response_body)

    with pytest.raises(RuntimeError) as error:
        await compact_openai_responses_history(
            SimpleNamespace(responses=Responses()),
            model_name="gpt-6-luna",
            messages=[{"role": "user", "content": "synthetic"}],
        )
    assert "synthetic-sensitive-untrusted-item" not in "".join(
        traceback.format_exception(error.value)
    )
