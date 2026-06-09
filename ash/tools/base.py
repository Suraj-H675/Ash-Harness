"""Base contracts for Ash tools."""

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel

from ash.safety.guard import SafetyGuard


class ToolResult(BaseModel):
    success: bool
    output: str
    error: str | None = None
    token_count: int = 0
    truncated: bool = False


class BaseTool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    args_schema: ClassVar[type[BaseModel]]

    def __init__(self, safety_guard: SafetyGuard) -> None:
        self.safety_guard = safety_guard

    @abstractmethod
    async def run(self, **kwargs: Any) -> ToolResult:
        """Execute the tool asynchronously."""

    def validate_args(self, **kwargs: Any) -> BaseModel:
        return self.args_schema(**kwargs)


def count_output_tokens(output: str) -> int:
    """Return a lightweight token estimate until provider tokenizers are wired in."""

    if not output:
        return 0
    return len(output.split())
