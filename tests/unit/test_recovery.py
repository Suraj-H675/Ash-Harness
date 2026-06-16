# tests/unit/test_recovery.py
import pytest
from ash.core.recovery import CircuitBreaker, CircuitBreakerError


def test_circuit_breaker_includes_suggestions_on_trip():
    cb = CircuitBreaker(max_failures=2)
    cb.record_failure("read_file")
    with pytest.raises(CircuitBreakerError) as exc_info:
        cb.record_failure("read_file")  # 2nd consecutive = trips (max_failures=2)
    assert "read_file" in str(exc_info.value)
    assert exc_info.value.failure_count == 2
    assert len(exc_info.value.suggestions) > 0
    # is_tripped should also be True
    assert cb.is_tripped is True


def test_circuit_breaker_unknown_tool_no_suggestions():
    cb = CircuitBreaker(max_failures=2)
    cb.record_failure("unknown_tool")
    with pytest.raises(CircuitBreakerError) as exc_info:
        cb.record_failure("unknown_tool")
    assert exc_info.value.suggestions == ()
    assert exc_info.value.tool_name == "unknown_tool"


def test_circuit_breaker_resets_on_different_tool():
    cb = CircuitBreaker(max_failures=2)
    cb.record_failure("read_file")
    cb.record_failure("write_file")  # different tool resets counter
    with pytest.raises(CircuitBreakerError) as exc_info:
        cb.record_failure("write_file")  # trips at counter=2 >= max_failures=2
    assert exc_info.value.tool_name == "write_file"
