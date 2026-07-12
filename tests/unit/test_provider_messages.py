from __future__ import annotations

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
    provider = OllamaProvider(model_name="test", base_url="http://localhost:1")

    class FailIfCalled:
        def stream(self, *args, **kwargs):
            raise AssertionError("network must not be reached")

        async def aclose(self):
            return None

    await provider._client.aclose()
    provider._client = FailIfCalled()  # type: ignore[assignment]

    with pytest.raises(ValueError, match="tool messages require tool_call_id"):
        async for _ in provider.stream_chat([{"role": "tool", "content": "orphaned"}]):
            pass
