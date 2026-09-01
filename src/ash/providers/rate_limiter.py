"""Token bucket rate limiter for provider request throttling."""

from __future__ import annotations

import asyncio
import math
import time
from typing import Tuple


class TokenBucketRateLimiter:
    """
    Throttle token consumption at a fixed fill rate using a leaky bucket model.

    The bucket starts full. Each ``consume`` call subtracts tokens (or returns
    the wait time needed to accumulate them) and tokens refill at ``fill_rate``
    tokens per second, capped at ``capacity``.
    """

    def __init__(self, capacity: int, fill_rate: float) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be positive")
        if (
            isinstance(fill_rate, bool)
            or not isinstance(fill_rate, int | float)
            or not math.isfinite(fill_rate)
            or fill_rate < 0
        ):
            raise ValueError("fill_rate must be non-negative")
        self.capacity = capacity
        self.fill_rate = fill_rate
        self.tokens: float = float(capacity)
        self.last_update: float = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_update
        self.last_update = now
        self.tokens = min(float(self.capacity), self.tokens + elapsed * self.fill_rate)

    def consume(self, tokens_needed: int) -> Tuple[bool, float]:
        """
        Attempt to consume tokens from the bucket.

        Returns ``(True, 0.0)`` on success, or ``(False, wait_seconds)`` when
        the request cannot yet be satisfied. A request larger than capacity
        is unsatisfiable and returns an infinite wait time.
        """

        self._validate_tokens_needed(tokens_needed)
        if tokens_needed > self.capacity:
            return False, float("inf")

        self._refill()

        if self.tokens >= tokens_needed:
            self.tokens -= tokens_needed
            return True, 0.0

        if self.fill_rate == 0.0:
            return False, float("inf")

        deficit = tokens_needed - self.tokens
        wait_time = deficit / self.fill_rate
        return False, wait_time

    async def acquire(self, tokens_needed: int) -> None:
        """
        Block until ``tokens_needed`` tokens are available, then consume them.

        Raises ``ValueError`` if the request exceeds the bucket capacity —
        such a request can never be satisfied.
        """

        self._validate_tokens_needed(tokens_needed)
        if tokens_needed > self.capacity:
            raise ValueError(
                f"Requested tokens ({tokens_needed}) exceeds bucket capacity ({self.capacity})."
            )

        while True:
            async with self._lock:
                success, wait = self.consume(tokens_needed)
                if success:
                    return

            await asyncio.sleep(max(wait, 0.0))

    def _validate_tokens_needed(self, tokens_needed: int) -> None:
        if (
            isinstance(tokens_needed, bool)
            or not isinstance(tokens_needed, int)
            or tokens_needed < 0
        ):
            raise ValueError("tokens_needed must be a non-negative integer")
