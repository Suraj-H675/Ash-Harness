"""Base contracts for Ash tools."""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from safety.guard import SafetyGuard

class ToolResult(BaseModel):
    success: bool
    output: str
    error: str | None = None
    token_count: int = 0
    truncated: bool = False


class BaseTool(ABC):
    name: str
    description: str
    args_schema: type[BaseModel]

    def __init__(self, safety_guard: SafetyGuard) -> None:
        self.safety_guard = safety_guard

    @abstractmethod
    async def run(self, **kwargs: Any) -> ToolResult:
        """Execute the tool asynchronously."""

    def validate_args(self, **kwargs: Any) -> BaseModel:
        return self.args_schema(**kwargs)

    async def aclose(self) -> None:
        """Release optional tool resources."""


class ToolMiddleware(ABC):
    """Hook called before and after every tool execution."""

    async def before_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        tool: "BaseTool",
    ) -> None:
        """Called before tool.run(). Raise ToolMiddlewareSkip to skip execution."""
        pass

    async def after_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: "ToolResult",
    ) -> None:
        """Called after tool.run() with the result. Raise to augment result."""
        pass


class ToolMiddlewareSkip(Exception):
    """Raised from before_tool to skip tool execution entirely."""


def count_output_tokens(output: str) -> int:
    """Return a lightweight token estimate until provider tokenizers are wired in."""

    if not output:
        return 0
    return len(output.split())
