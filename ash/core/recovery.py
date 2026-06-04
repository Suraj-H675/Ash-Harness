"""Recovery primitives for the Ash core loop.

The :class:`CircuitBreaker` prevents infinite agent loops where a tool keeps
failing and the model keeps retrying it. Failures only accumulate when the
*same* tool fails consecutively; a successful run (or a different tool)
resets the counter.
"""

from __future__ import annotations


class CircuitBreakerError(Exception):
    """Raised when a tool has failed too many times in a row."""


class CircuitBreaker:
    """Track consecutive tool failures and trip after ``max_failures``."""

    def __init__(self, max_failures: int = 3) -> None:
        if max_failures < 1:
            raise ValueError("max_failures must be at least 1")
        self.max_failures = max_failures
        self.failure_counter: int = 0
        self.last_failed_tool: str = ""

    def record_failure(self, tool_name: str) -> None:
        """Record a tool failure.

        Only consecutive failures of the *same* tool count toward the trip
        threshold. A different tool name (or a successful call between two
        failures of the same tool) resets the counter to 1 for the new tool.
        """

        if tool_name == self.last_failed_tool:
            self.failure_counter += 1
        else:
            self.last_failed_tool = tool_name
            self.failure_counter = 1

        if self.failure_counter >= self.max_failures:
            raise CircuitBreakerError(
                f"Tool '{tool_name}' failed {self.failure_counter} times "
                "consecutively. Halting for user intervention."
            )

    def record_success(self) -> None:
        """Reset failure metrics after a successful tool execution."""

        self.failure_counter = 0
        self.last_failed_tool = ""

    def reset(self) -> None:
        """Explicitly clear all failure state."""

        self.failure_counter = 0
        self.last_failed_tool = ""

    @property
    def is_tripped(self) -> bool:
        """Whether the breaker is at or past the trip threshold."""

        return self.failure_counter >= self.max_failures
