"""Base contracts for Ash tools."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from pydantic import BaseModel

from ash.safety.guard import SafetyGuard


class ToolResult(BaseModel):
    success: bool
    output: str
    error: str | None = None
    token_count: int = 0
    truncated: bool = False


class BaseTool(ABC):
    name: str
    description: str
    args_schema: type[BaseModel] | None

    def __init__(self, safety_guard: SafetyGuard) -> None:
        self.safety_guard = safety_guard
        self._event_sink: Callable[[dict[str, Any]], None] | None = None
        self._event_context: ContextVar[dict[str, Any] | None] = ContextVar(
            f"tool_event_context_{id(self)}", default=None
        )

    @abstractmethod
    async def run(self, **kwargs: Any) -> ToolResult:
        """Execute the tool asynchronously."""

    def validate_args(self, **kwargs: Any) -> BaseModel:
        if self.args_schema is None:
            raise ValueError(f"tool {self.name!r} does not declare an argument model")
        return self.args_schema(**kwargs)

    def json_schema(self) -> dict[str, Any]:
        """Return the exact provider-facing input schema for this tool."""

        args_schema = getattr(self, "args_schema", None)
        if args_schema is None:
            return {}
        if hasattr(args_schema, "model_json_schema"):
            return args_schema.model_json_schema()
        if hasattr(args_schema, "schema"):
            return args_schema.schema()
        return {}

    async def aclose(self) -> None:
        """Release optional tool resources."""

    async def start(self) -> None:
        """Start optional background services after runtime assembly."""

    def set_event_sink(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        """Attach the owning runtime's typed event sink."""

        self._event_sink = sink

    @contextmanager
    def event_context(self, context: dict[str, Any]) -> Iterator[None]:
        """Bind per-invocation metadata without leaking across async tasks."""

        token = self._event_context.set(dict(context))
        try:
            yield
        finally:
            self._event_context.reset(token)

    def emit_event(self, payload: dict[str, Any]) -> None:
        if self._event_sink is None:
            return
        context = self._event_context.get() or {}
        self._event_sink({**context, **payload})


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
