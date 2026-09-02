from __future__ import annotations

from contextlib import asynccontextmanager
import json

import httpx
import pytest

from ash.providers.anthropic import prepare_anthropic_messages
from ash.providers.base import StreamChunk
from ash.providers.messages import (
    MAX_CANONICAL_MESSAGES,
    CanonicalMessage,
    CanonicalToolCall,
    ImageContentBlock,
    TextContentBlock,
    normalize_messages,
)
from ash.providers.ollama import OllamaProvider
from ash.providers.openai import prepare_openai_messages
from ash.providers.retry import ProviderHTTPError


def test_typed_canonical_messages_round_trip_to_wire_shape() -> None:
    messages = [
        CanonicalMessage(role="system", content="system"),
        CanonicalMessage(
            role="user",
            content=[
                TextContentBlock(text="inspect"),
                ImageContentBlock(media_type="image/png", data="YWJj"),
            ],
        ),
        CanonicalMessage(
            role="assistant",
            tool_calls=[
                CanonicalToolCall(
                    call_id="call-1",
                    name="read_file",
                    arguments={"file_path": "README.md"},
                )
            ],
        ),
        CanonicalMessage(role="tool", tool_call_id="call-1", content="contents"),
    ]

    wire = normalize_messages(messages)

    assert wire[1]["content"][1] == {
        "type": "image",
        "media_type": "image/png",
        "data": "YWJj",
    }
    assert wire[2]["tool_calls"][0]["call_id"] == "call-1"
    assert wire[3]["tool_call_id"] == "call-1"


@pytest.mark.parametrize(
    ("message", "match"),
    [
        ({"role": "tool", "content": "result"}, "require tool_call_id"),
        (
            {"role": "user", "content": "text", "tool_call_id": "call-1"},
            "only on tool messages",
        ),
        (
            {
                "role": "user",
                "content": "text",
                "tool_calls": [
                    {"call_id": "call-1", "name": "read_file", "arguments": {}}
                ],
            },
            "only on assistant messages",
        ),
        (
            {
                "role": "assistant",
                "content": [
                    {"type": "image", "media_type": "image/png", "data": "YWJj"}
                ],
            },
            "only on user messages",
        ),
        (
            {
                "role": "user",
                "content": [
                    {"type": "image", "media_type": "image/svg+xml", "data": "YWJj"}
                ],
            },
            "unsupported image media type",
        ),
        (
            {
                "role": "user",
                "content": [
                    {"type": "image", "media_type": "image/png", "data": "not-base64"}
                ],
            },
            "valid base64",
        ),
    ],
)
def test_canonical_message_rejects_invalid_role_or_content_contracts(
    message, match
) -> None:
    with pytest.raises(ValueError, match=match):
        normalize_messages([message])


def test_canonical_tool_arguments_must_be_strict_json() -> None:
    with pytest.raises(ValueError, match="JSON serializable"):
        CanonicalToolCall(
            call_id="call-1",
            name="tool",
            arguments={"value": float("nan")},
        )


def test_native_tool_calls_normalize_provider_ids_and_json_arguments() -> None:
    chunk = StreamChunk(
        native_tool_calls=[
            {
                "id": "provider-call-1",
                "name": "read_file",
                "arguments": '{"file_path":"README.md"}',
            }
        ]
    )

    assert chunk.native_tool_calls is not None
    assert chunk.native_tool_calls[0].to_wire() == {
        "call_id": "provider-call-1",
        "name": "read_file",
        "arguments": {"file_path": "README.md"},
    }


@pytest.mark.parametrize("arguments", ["not-json", "[]", "null", "1"])
def test_native_tool_calls_reject_non_object_arguments(arguments: str) -> None:
    with pytest.raises(ValueError, match="tool-call arguments"):
        StreamChunk(
            native_tool_calls=[
                {"id": "call-1", "name": "read_file", "arguments": arguments}
            ]
        )


def test_provider_translators_validate_before_encoding() -> None:
    invalid = [{"role": "tool", "content": "orphaned result"}]

    with pytest.raises(ValueError, match="index 0"):
        prepare_openai_messages(invalid)
    with pytest.raises(ValueError, match="index 0"):
        prepare_anthropic_messages(invalid)


def test_message_count_is_bounded() -> None:
    message = CanonicalMessage(role="user", content="x")

    with pytest.raises(ValueError, match="message count exceeds"):
        normalize_messages([message] * (MAX_CANONICAL_MESSAGES + 1))


@pytest.mark.asyncio
async def test_ollama_validates_messages_before_network_io() -> None:
    class FailIfCalled:
        def stream(self, *args, **kwargs):
            raise AssertionError("network must not be reached")

        async def aclose(self):
            return None

    provider = OllamaProvider(
        model_name="test",
        base_url="http://localhost:1",
        client=FailIfCalled(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="tool messages require tool_call_id"):
        async for _ in provider.stream_chat([{"role": "tool", "content": "orphaned"}]):
            pass


@pytest.mark.asyncio
async def test_ollama_forwards_tools_and_emits_native_tool_calls() -> None:
    tool = {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
            },
        },
    }
    client = _FakeOllamaClient(
        json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "read_file",
                                "arguments": {"file_path": "README.md"},
                            }
                        }
                    ],
                },
                "done": True,
                "done_reason": "stop",
            }
        ).encode()
    )
    provider = OllamaProvider(model_name="tool-model", client=client)  # type: ignore[arg-type]

    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [{"role": "user", "content": "read README.md"}],
            tools=[tool],
        )
    ]

    assert client.calls[0][2]["json"]["tools"] == [tool]
    assert chunks[-1].is_done is True
    assert chunks[-1].native_tool_calls is not None
    assert chunks[-1].native_tool_calls[0].to_wire() == {
        "call_id": "call_0",
        "name": "read_file",
        "arguments": {"file_path": "README.md"},
    }


@pytest.mark.asyncio
async def test_ollama_rejects_an_oversized_stream_line(monkeypatch) -> None:
    monkeypatch.setattr("ash.providers.ollama.MAX_OLLAMA_STREAM_LINE_BYTES", 8)
    provider = OllamaProvider(
        model_name="test",
        client=_FakeOllamaClient(b"xxxxxxxxx"),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="stream line exceeded"):
        _ = [chunk async for chunk in provider.stream_chat([])]


@pytest.mark.asyncio
async def test_ollama_bounds_error_response_text(monkeypatch) -> None:
    monkeypatch.setattr("ash.providers.ollama.MAX_OLLAMA_ERROR_BYTES", 8)
    provider = OllamaProvider(
        model_name="test",
        client=_FakeOllamaClient(b"x" * 9, status_code=500),  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderHTTPError) as raised:
        _ = [chunk async for chunk in provider.stream_chat([])]

    assert "x" * 9 not in str(raised.value)


@pytest.mark.asyncio
async def test_ollama_rejects_non_object_stream_messages() -> None:
    provider = OllamaProvider(
        model_name="test",
        client=_FakeOllamaClient(b"[]\n"),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="message must be an object"):
        _ = [chunk async for chunk in provider.stream_chat([])]


@pytest.mark.asyncio
async def test_ollama_stream_chat_handles_chunked_ndjson_and_usage() -> None:
    lines = [
        b'{"message":{"content":"hello"},"done":false}\n',
        b'{"message":{"content":" world"},"done":false}\n',
        (
            b'{"message":{"content":""},"done":true,'
            b'"prompt_eval_count":3,"eval_count":2,"done_reason":"stop"}\n'
        ),
    ]
    body = b"".join(lines)
    split_chunks = [body[:17], body[17:43], body[43:]]
    client = _FakeOllamaClient(b"", chunks=split_chunks)
    provider = OllamaProvider(model_name="test", client=client)  # type: ignore[arg-type]

    chunks = [chunk async for chunk in provider.stream_chat([])]

    assert "".join(chunk.content for chunk in chunks) == "hello world"
    assert chunks[-1].is_done is True
    assert chunks[-1].prompt_tokens == 3
    assert chunks[-1].completion_tokens == 2
    assert chunks[-1].usage_source == "provider"
    assert chunks[-1].stop_reason == "stop"


@pytest.mark.asyncio
async def test_ollama_rejects_an_oversized_stream(monkeypatch) -> None:
    monkeypatch.setattr("ash.providers.ollama.MAX_OLLAMA_STREAM_BYTES", 8)
    provider = OllamaProvider(
        model_name="test",
        client=_FakeOllamaClient(b"123456789"),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="stream exceeded"):
        _ = [chunk async for chunk in provider.stream_chat([])]


class _FakeOllamaClient:
    def __init__(
        self,
        body: bytes,
        status_code: int = 200,
        *,
        chunks: list[bytes] | None = None,
    ) -> None:
        self._chunks = tuple(chunks) if chunks is not None else (body,)
        self._status_code = status_code
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    @asynccontextmanager
    async def stream(self, method: str, url: str, **kwargs: object):
        self.calls.append((method, url, kwargs))
        request = httpx.Request(method, url)
        yield httpx.Response(
            self._status_code,
            stream=_ChunkedAsyncStream(self._chunks),
            request=request,
        )

    async def aclose(self) -> None:
        return None


class _ChunkedAsyncStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_ollama_detects_tools_and_context_from_model_metadata():
    provider = OllamaProvider(model_name="tool-model", client=_StaticAsyncClient())  # type: ignore[arg-type]

    assert provider.capabilities.native_tools is False
    capabilities = await provider.detect_capabilities()
    again = await provider.detect_capabilities()

    assert capabilities.native_tools is True
    assert capabilities.local is True
    assert capabilities.context_window == 32768
    assert again is capabilities
    assert provider.capabilities.native_tools is True
    assert provider.capabilities.context_window == 32768


class _StaticAsyncClient:
    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        yield httpx.Response(
            200,
            json={
                "details": {"families": ["tools"]},
                "template": "Use {{ .ToolCall }}",
                "model_info": {"general.context_length": 32768},
            },
        )

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_ollama_capability_probe_fails_closed_to_local_only():
    class BrokenClient:
        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            raise RuntimeError("offline")
            yield

    provider = OllamaProvider(
        model_name="local-only",
        client=BrokenClient(),  # type: ignore[arg-type]
    )

    capabilities = await provider.detect_capabilities()

    assert capabilities.native_tools is False
    assert capabilities.local is True


@pytest.mark.asyncio
async def test_ollama_capability_refresh_forces_new_probe():
    class ChangingClient:
        def __init__(self):
            self.calls = 0

        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            self.calls += 1
            yield httpx.Response(
                200,
                json={
                    "details": {"families": ["tools"] if self.calls > 1 else []},
                    "model_info": {"general.context_length": 8192},
                },
            )

    client = ChangingClient()
    provider = OllamaProvider(
        model_name="changing", client=client  # type: ignore[arg-type]
    )

    initial = await provider.detect_capabilities()
    cached = await provider.detect_capabilities()
    refreshed = await provider.detect_capabilities(refresh=True)

    assert client.calls == 2
    assert initial.native_tools is False
    assert cached is initial
    assert refreshed.native_tools is True
    assert refreshed.context_window == 8192
