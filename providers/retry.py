"""Provider failure classification and bounded retry timing."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any


RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429})
NON_RETRYABLE_RATE_LIMIT_MARKERS = (
    "insufficient_quota",
    "exceeded your current quota",
    "billing",
    "credit balance",
)


@dataclass(frozen=True)
class ProviderFailure:
    message: str
    retriable: bool
    status_code: int | None = None
    retry_after: float | None = None


class ProviderHTTPError(RuntimeError):
    """Provider-neutral HTTP failure retaining status and response headers."""

    def __init__(self, message: str, *, status_code: int, headers: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}


class ProviderCircuitOpen(RuntimeError):
    """Raised when a provider circuit is still in its cooldown window."""

    def __init__(self, provider: str, retry_after: float) -> None:
        self.provider = provider
        self.retry_after = max(0.0, retry_after)
        super().__init__(
            f"Provider circuit is open for {provider}; retry in "
            f"{self.retry_after:.1f} seconds"
        )


@dataclass
class _CircuitState:
    failures: int = 0
    opened_at: float | None = None


class ProviderCircuitBreaker:
    """Small per-provider circuit breaker for exhausted transient requests."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
        clock=time.monotonic,
    ) -> None:
        if failure_threshold < 2:
            raise ValueError("failure_threshold must be at least 2")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._states: dict[str, _CircuitState] = {}

    def before_request(self, provider: str) -> None:
        state = self._states.get(provider)
        if state is None or state.opened_at is None:
            return
        elapsed = self._clock() - state.opened_at
        if elapsed < self.cooldown_seconds:
            raise ProviderCircuitOpen(provider, self.cooldown_seconds - elapsed)
        # Half-open: one serial probe. A failed probe reopens immediately.
        state.opened_at = None
        state.failures = self.failure_threshold - 1

    def record_success(self, provider: str) -> None:
        self._states.pop(provider, None)

    def record_failure(self, provider: str) -> bool:
        state = self._states.setdefault(provider, _CircuitState())
        state.failures += 1
        if state.failures < self.failure_threshold:
            return False
        state.opened_at = self._clock()
        return True

    def snapshot(self, provider: str) -> dict[str, float | int | bool]:
        state = self._states.get(provider, _CircuitState())
        remaining = 0.0
        is_open = state.opened_at is not None
        if state.opened_at is not None:
            remaining = max(
                0.0,
                self.cooldown_seconds - (self._clock() - state.opened_at),
            )
        return {
            "failures": state.failures,
            "open": is_open and remaining > 0,
            "retry_after": remaining,
        }


def classify_provider_failure(error: BaseException) -> ProviderFailure:
    """Classify an SDK/http failure without importing provider SDK types."""

    chain = list(_exception_chain(error))
    message = str(error).strip() or type(error).__name__
    lowered = " ".join(str(item).casefold() for item in chain)
    status_code = next(
        (status for item in chain if (status := _status_code(item)) is not None),
        None,
    )
    retry_after = next(
        (delay for item in chain if (delay := _retry_after_seconds(item)) is not None),
        None,
    )
    names = {type(item).__name__.casefold() for item in chain}
    connection_failure = any(
        token in name for name in names for token in ("connection", "timeout")
    )
    retriable = (
        connection_failure
        or status_code in RETRYABLE_STATUS_CODES
        or (status_code is not None and status_code >= 500)
    )
    if status_code in {400, 401, 403, 404, 422}:
        retriable = False
    if status_code == 429 and any(
        marker in lowered for marker in NON_RETRYABLE_RATE_LIMIT_MARKERS
    ):
        retriable = False
    return ProviderFailure(message, retriable, status_code, retry_after)


def retry_delay(
    failure: ProviderFailure,
    retry_number: int,
    *,
    base_delay: float,
    max_delay: float,
    jitter_ratio: float = 0.25,
) -> float:
    """Return Retry-After or capped exponential backoff with bounded jitter."""

    if retry_number < 1:
        raise ValueError("retry_number must be positive")
    if base_delay < 0 or max_delay < 0 or jitter_ratio < 0:
        raise ValueError("retry delay settings must be non-negative")
    if failure.retry_after is not None:
        return min(max_delay, max(0.0, failure.retry_after))
    exponential = min(max_delay, base_delay * (2 ** (retry_number - 1)))
    jitter = random.uniform(0.0, exponential * jitter_ratio)
    return min(max_delay, exponential + jitter)


def _exception_chain(error: BaseException):
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _status_code(error: BaseException) -> int | None:
    value = getattr(error, "status_code", None)
    if value is None:
        value = getattr(getattr(error, "response", None), "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _retry_after_seconds(error: BaseException) -> float | None:
    response = getattr(error, "response", None)
    headers: Any = getattr(response, "headers", None) or getattr(error, "headers", None)
    if headers is None:
        return None
    milliseconds = _header(headers, "retry-after-ms")
    if milliseconds is not None:
        try:
            return max(0.0, float(milliseconds) / 1000.0)
        except (TypeError, ValueError):
            pass
    value = _header(headers, "retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(str(value))
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _header(headers: Any, name: str) -> Any:
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    return getter(name) or getter(name.title())
