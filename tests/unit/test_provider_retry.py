from __future__ import annotations

from types import SimpleNamespace

import pytest

from ash.providers.base import ProviderTerminalError
from ash.providers.retry import (
    ProviderCircuitBreaker,
    ProviderCircuitOpen,
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


@pytest.mark.parametrize("reason", ["rate_limit", "timeout"])
def test_retriable_terminal_errors_are_classified_through_wrappers(reason: str) -> None:
    wrapper = RuntimeError("provider chain failed")
    wrapper.__cause__ = ProviderTerminalError(reason)

    assert classify_provider_failure(wrapper).retriable is True


@pytest.mark.parametrize("reason", ["cancelled", "failed", "vendor_unknown"])
def test_nonretriable_terminal_errors_fail_closed(reason: str) -> None:
    assert classify_provider_failure(ProviderTerminalError(reason)).retriable is False


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
    monkeypatch.setattr("ash.providers.retry.random.uniform", lambda _low, high: high)
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


def test_provider_circuit_opens_cools_down_and_resets() -> None:
    now = 100.0
    circuit = ProviderCircuitBreaker(
        failure_threshold=2,
        cooldown_seconds=10,
        clock=lambda: now,
    )
    assert circuit.record_failure("openai/model") is False
    assert circuit.record_failure("openai/model") is True
    assert circuit.snapshot("openai/model") == {
        "failures": 2,
        "open": True,
        "retry_after": 10.0,
    }
    with pytest.raises(ProviderCircuitOpen) as exc:
        circuit.before_request("openai/model")
    assert exc.value.retry_after == 10.0

    now = 111.0
    circuit.before_request("openai/model")
    assert circuit.record_failure("openai/model") is True
    circuit.record_success("openai/model")
    assert circuit.snapshot("openai/model")["failures"] == 0


def test_provider_circuit_tracks_providers_independently() -> None:
    circuit = ProviderCircuitBreaker(failure_threshold=2)
    circuit.record_failure("provider-a")
    circuit.record_failure("provider-a")

    circuit.before_request("provider-b")
    assert circuit.snapshot("provider-b")["open"] is False
