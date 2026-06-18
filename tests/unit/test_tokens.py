"""Tests for token counting and rate limiting (Sprint 5)."""

import time

import pytest

from context.tokens import (
    AnthropicTokenCounter,
    OpenAITokenCounter,
    get_token_counter,
)
from providers.rate_limiter import TokenBucketRateLimiter


# --- Token Counter Tests ---


def test_anthropic_counter_empty_string_returns_zero() -> None:
    counter = AnthropicTokenCounter()

    assert counter.count("") == 0


def test_anthropic_counter_returns_positive_int_for_text() -> None:
    counter = AnthropicTokenCounter()

    result = counter.count("hello world")

    assert isinstance(result, int)
    assert result > 0


def test_anthropic_counter_scales_with_input_length() -> None:
    counter = AnthropicTokenCounter()
    short = counter.count("hi")
    long = counter.count("hi " * 1000)

    assert long > short


def test_openai_counter_matches_tiktoken_encoder() -> None:
    counter = OpenAITokenCounter("gpt-4")
    text = "The quick brown fox jumps over the lazy dog."
    expected = len(counter._encoder.encode(text))

    assert counter.count(text) == expected


def test_openai_counter_zero_for_empty_input() -> None:
    counter = OpenAITokenCounter("gpt-4")

    assert counter.count("") == 0


def test_openai_counter_falls_back_for_unknown_model() -> None:
    counter = OpenAITokenCounter("totally-fake-model-xyz")

    assert counter.count("hello world") > 0


def test_get_token_counter_returns_anthropic_for_anthropic_provider() -> None:
    counter = get_token_counter("anthropic", "claude-3-5-sonnet-20241022")

    assert isinstance(counter, AnthropicTokenCounter)


def test_get_token_counter_returns_openai_for_openai_provider() -> None:
    counter = get_token_counter("openai", "gpt-4")

    assert isinstance(counter, OpenAITokenCounter)


def test_get_token_counter_returns_anthropic_heuristic_for_ollama() -> None:
    counter = get_token_counter("ollama", "llama3.1")

    assert isinstance(counter, AnthropicTokenCounter)


def test_get_token_counter_raises_for_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported provider"):
        get_token_counter("fake", "model")


# --- Rate Limiter Tests ---


def test_rate_limiter_initial_state_is_full() -> None:
    limiter = TokenBucketRateLimiter(capacity=10, fill_rate=1.0)

    assert limiter.tokens == 10.0


def test_rate_limiter_consume_succeeds_within_capacity() -> None:
    limiter = TokenBucketRateLimiter(capacity=10, fill_rate=1.0)

    success, wait = limiter.consume(5)

    assert success is True
    assert wait == 0.0
    assert limiter.tokens == 5.0


def test_rate_limiter_consume_fails_when_bucket_is_empty() -> None:
    limiter = TokenBucketRateLimiter(capacity=2, fill_rate=1.0)
    limiter.consume(2)

    success, wait = limiter.consume(1)

    assert success is False
    assert wait > 0.0


def test_rate_limiter_refills_over_time() -> None:
    limiter = TokenBucketRateLimiter(capacity=2, fill_rate=20.0)
    limiter.consume(2)
    time.sleep(0.1)

    success, _ = limiter.consume(2)

    assert success is True


def test_rate_limiter_request_exceeding_capacity_always_fails() -> None:
    limiter = TokenBucketRateLimiter(capacity=2, fill_rate=1.0)

    success, wait = limiter.consume(5)

    assert success is False
    assert wait == float("inf")


def test_rate_limiter_zero_fill_rate_returns_infinite_wait() -> None:
    limiter = TokenBucketRateLimiter(capacity=2, fill_rate=0.0)
    limiter.consume(2)

    success, wait = limiter.consume(1)

    assert success is False
    assert wait == float("inf")


def test_rate_limiter_invalid_capacity_raises() -> None:
    with pytest.raises(ValueError, match="capacity"):
        TokenBucketRateLimiter(capacity=0, fill_rate=1.0)


def test_rate_limiter_negative_fill_rate_raises() -> None:
    with pytest.raises(ValueError, match="fill_rate"):
        TokenBucketRateLimiter(capacity=10, fill_rate=-1.0)


@pytest.mark.asyncio
async def test_rate_limiter_acquire_returns_immediately_when_available() -> None:
    limiter = TokenBucketRateLimiter(capacity=10, fill_rate=1.0)

    start = time.monotonic()
    await limiter.acquire(5)
    elapsed = time.monotonic() - start

    assert elapsed < 0.05


@pytest.mark.asyncio
async def test_rate_limiter_acquire_waits_for_refill() -> None:
    limiter = TokenBucketRateLimiter(capacity=2, fill_rate=20.0)
    await limiter.acquire(2)

    start = time.monotonic()
    await limiter.acquire(2)
    elapsed = time.monotonic() - start

    assert elapsed >= 0.05


@pytest.mark.asyncio
async def test_rate_limiter_acquire_rejects_overcapacity_request() -> None:
    limiter = TokenBucketRateLimiter(capacity=2, fill_rate=1.0)

    with pytest.raises(ValueError, match="exceeds bucket capacity"):
        await limiter.acquire(10)
