# tests/unit/test_rate_limiter.py
import asyncio
import pytest
from providers.rate_limiter import TokenBucketRateLimiter

def test_consume_returns_true_when_tokens_available():
    limiter = TokenBucketRateLimiter(capacity=10, fill_rate=1.0)
    success, wait = limiter.consume(5)
    assert success is True
    assert wait == 0.0

def test_consume_returns_wait_when_insufficient_tokens():
    limiter = TokenBucketRateLimiter(capacity=5, fill_rate=0.0)
    success, wait = limiter.consume(10)
    assert success is False
    assert wait == float("inf")

@pytest.mark.asyncio
async def test_acquire_blocks_until_tokens_available():
    limiter = TokenBucketRateLimiter(capacity=5, fill_rate=10.0)
    # Tokens will refill quickly with fill_rate=10
    await limiter.acquire(5)
    # acquire(5) consumed the tokens, so bucket is now empty.
    # Wait for refill and try again
    await asyncio.sleep(0.6)  # fill_rate=10 -> 6 tokens in 0.6s
    success, _ = limiter.consume(5)
    assert success is True