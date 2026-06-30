from __future__ import annotations

from types import SimpleNamespace

import pytest

from providers.retry import (
    ProviderFailure,
    classify_provider_failure,
    retry_delay,
)


class APIError(RuntimeError):
    def __init__(self, message: str, status: int, headers=None) -> None:
        super().__init__(message)
        self.status_code = status
        self.response = SimpleNamespace(status_code=status, headers=headers or {})


@pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 503])
def test_retryable_http_statuses(status: int) -> None:
    assert classify_provider_failure(APIError("temporary", status)).retriable is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_http_statuses(status: int) -> None:
    assert classify_provider_failure(APIError("permanent", status)).retriable is False


def test_quota_exhaustion_is_not_retried() -> None:
    failure = classify_provider_failure(
        APIError("You exceeded your current quota; check billing", 429)
    )
    assert failure.retriable is False


def test_wrapped_connection_and_status_errors_are_classified() -> None:
    class APIConnectionError(RuntimeError):
        pass

    cause = APIConnectionError("connection reset")
    wrapper = RuntimeError("OpenAI API error")
    wrapper.__cause__ = cause
    assert classify_provider_failure(wrapper).retriable is True

    status_cause = APIError("overloaded", 503, {"retry-after-ms": "1250"})
    wrapper.__cause__ = status_cause
    failure = classify_provider_failure(wrapper)
    assert failure.status_code == 503
    assert failure.retry_after == 1.25


def test_retry_delay_prefers_header_and_caps_backoff(monkeypatch) -> None:
    assert (
        retry_delay(
            ProviderFailure("rate", True, 429, 12.0),
            1,
            base_delay=0.5,
            max_delay=5.0,
        )
        == 5.0
    )
    monkeypatch.setattr("providers.retry.random.uniform", lambda _low, high: high)
    assert (
        retry_delay(
            ProviderFailure("server", True, 503),
            3,
            base_delay=1.0,
            max_delay=10.0,
            jitter_ratio=0.25,
        )
        == 5.0
    )
