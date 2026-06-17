"""Recovery primitives for the Ash core loop.

The :class:`CircuitBreaker` prevents infinite agent loops where a tool keeps
failing and the model keeps retrying it. Failures only accumulate when the
*same* tool fails consecutively; a successful run (or a different tool)
resets the counter.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CircuitBreakerError(Exception):
    tool_name: str
    failure_count: int
    suggestions: tuple[str, ...] = ()

    def __str__(self) -> str:
        if self.suggestions:
            suggestions_text = "\n".join(f"  - {s}" for s in self.suggestions)
            return (
                f"Tool '{self.tool_name}' failed {self.failure_count} times consecutively."
                f"\n\nPossible alternatives:\n{suggestions_text}"
            )
        return (
            f"Tool '{self.tool_name}' failed {self.failure_count} times consecutively."
        )


class CircuitBreaker:
    """Track consecutive tool failures and trip after ``max_failures``."""

    SUGGESTIONS: dict[str, tuple[str, ...]] = {
        "read_file": (
            "Try using a more specific line range with start_line and end_line.",
            "Check if the file exists with run_command: ls -la",
        ),
        "write_file": (
            "Check if the parent directory exists.",
            "Try using replace_file_content to modify specific lines instead.",
        ),
        "run_command": (
            "Check the command syntax.",
            "Try running the command manually in a terminal first.",
        ),
    }

    def __init__(self, max_failures: int = 3) -> None:
        if max_failures < 1:
            raise ValueError("max_failures must be at least 1")
        self.max_failures = max_failures
        self.failure_counter: int = 0
        self.last_failed_tool: str = ""

    def suggest_alternatives(self, tool_name: str) -> tuple[str, ...]:
        return self.SUGGESTIONS.get(tool_name, ())

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
            suggestions = self.suggest_alternatives(tool_name)
            raise CircuitBreakerError(
                tool_name=tool_name,
                failure_count=self.failure_counter,
                suggestions=suggestions,
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
