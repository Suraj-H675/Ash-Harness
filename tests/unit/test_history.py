import pytest

from context.history import (
    IMAGE_TOKEN_ESTIMATE,
    ContextBudgetAllocator,
    HistoryCompactor,
)


def count_words(text: str) -> int:
    return len(text.split())


def test_history_below_budget_is_unchanged() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    result = HistoryCompactor(
        max_context_tokens=100,
        completion_reserve=10,
    ).compact(messages, count_tokens=count_words)
    assert result.compacted is False
    assert result.messages == messages


def test_history_compaction_preserves_recent_tool_pair() -> None:
    messages = [{"role": "system", "content": "system"}]
    for index in range(10):
        messages.extend(
            [
                {"role": "user", "content": f"request {index} " + "word " * 20},
                {"role": "assistant", "content": f"response {index}"},
            ]
        )
    messages.extend(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"call_id": "call-1", "name": "read_file", "arguments": {}}
                ],
            },
            {"role": "tool", "content": "result", "tool_call_id": "call-1"},
            {"role": "user", "content": "continue"},
        ]
    )
    result = HistoryCompactor(
        max_context_tokens=120,
        completion_reserve=20,
        threshold=0.8,
        recent_messages=4,
    ).compact(messages, count_tokens=count_words)

    assert result.compacted is True
    assert "Compacted events" in result.summary
    roles = [message["role"] for message in result.messages]
    assert roles[-3:] == ["assistant", "tool", "user"]
    assert result.messages[-3]["tool_calls"][0]["call_id"] == "call-1"


def test_force_compaction_works_below_threshold() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "current"},
    ]
    result = HistoryCompactor(
        max_context_tokens=1000,
        completion_reserve=100,
        recent_messages=2,
    ).compact(messages, count_tokens=count_words, force=True)
    assert result.compacted is True


def test_stale_large_tool_output_is_pruned_without_mutating_transcript() -> None:
    large = "x" * 1000
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"call_id": "call-old", "name": "read_file", "arguments": {}}
            ],
        },
        {"role": "tool", "content": large, "tool_call_id": "call-old"},
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "answer"},
    ]
    result = HistoryCompactor(
        max_context_tokens=10_000,
        completion_reserve=100,
        max_tool_output_chars=100,
    ).compact(messages, count_tokens=count_words)
    assert result.pruned_tool_outputs == 1
    assert "call_id=call-old" in result.messages[2]["content"]
    assert messages[2]["content"] == large


def test_context_budget_allocator_normalizes_and_fits_text() -> None:
    allocator = ContextBudgetAllocator(
        max_context_tokens=100,
        completion_reserve=10,
        weights={
            "system": 1,
            "tools": 1,
            "history": 2,
            "repo_map": 1,
            "memory": 1,
        },
    )

    limits = allocator.allocate()
    assert sum(limits.values()) == 90
    assert limits["history"] > limits["system"]

    bounded = allocator.fit_text("word " * 100, limit=12, count_tokens=count_words)
    assert bounded.truncated is True
    assert bounded.tokens <= 12
    assert "context section truncated" in bounded.text

    tiny = allocator.fit_text("word " * 100, limit=1, count_tokens=count_words)
    assert tiny.truncated is True
    assert tiny.tokens <= 1


def test_context_budget_allocator_rejects_invalid_weights() -> None:
    with pytest.raises(ValueError, match="unknown context budget bucket"):
        ContextBudgetAllocator(
            max_context_tokens=100,
            completion_reserve=10,
            weights={"unknown": 1},
        )


def test_image_payload_uses_fixed_token_estimate() -> None:
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "inspect"},
                {
                    "type": "image",
                    "media_type": "image/png",
                    "data": "A" * 1_000_000,
                },
            ],
        },
    ]
    result = HistoryCompactor(
        max_context_tokens=10_000,
        completion_reserve=100,
    ).compact(messages, count_tokens=count_words)

    assert IMAGE_TOKEN_ESTIMATE <= result.estimated_tokens < IMAGE_TOKEN_ESTIMATE + 50


def test_compaction_summary_never_contains_image_base64() -> None:
    secret_payload = "SENSITIVE_BASE64_PAYLOAD"
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "old image"},
                {
                    "type": "image",
                    "media_type": "image/png",
                    "data": secret_payload,
                },
            ],
        },
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current"},
    ]
    result = HistoryCompactor(
        max_context_tokens=10_000,
        completion_reserve=100,
        recent_messages=2,
    ).compact(messages, count_tokens=count_words, force=True)

    assert secret_payload not in result.summary
    assert "[image: image/png]" in result.summary
