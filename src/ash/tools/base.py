"""Base contracts for Ash tools."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from ash.core.redaction import redact_value
from ash.safety.guard import SafetyGuard


class ToolExecutionOutcome(StrEnum):
    """How confidently Ash knows a dispatched tool invocation completed."""

    COMPLETED = "completed"
    UNKNOWN = "unknown"


MAX_TOOL_RESULT_STRUCTURED_ITEMS = 256
MAX_TOOL_RESULT_SUMMARY_FIELDS = 64
MAX_TOOL_RESULT_STRUCTURED_BYTES = 2 * 1024 * 1024
MAX_TOOL_RESULT_IMAGE_BLOCKS = 16
MAX_TOOL_RESULT_IMAGE_DATA_BYTES = 16 * 1024 * 1024
MAX_TOOL_RESULT_TEXT_BYTES = 16 * 1024 * 1024


class ToolResult(BaseModel):
    success: bool
    output: str
    error: str | None = None
    token_count: int = 0
    truncated: bool = False
    outcome: ToolExecutionOutcome = ToolExecutionOutcome.COMPLETED
    diagnostics: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=MAX_TOOL_RESULT_STRUCTURED_ITEMS,
    )
    diagnostic_summary: dict[str, int] = Field(
        default_factory=dict,
        max_length=MAX_TOOL_RESULT_SUMMARY_FIELDS,
    )
    citations: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=MAX_TOOL_RESULT_STRUCTURED_ITEMS,
    )
    images: list[dict[str, str]] = Field(
        default_factory=list,
        max_length=MAX_TOOL_RESULT_STRUCTURED_ITEMS,
    )
    image_blocks: list[dict[str, str]] = Field(
        default_factory=list,
        max_length=MAX_TOOL_RESULT_IMAGE_BLOCKS,
    )

    @model_validator(mode="after")
    def validate_text_bytes(self) -> "ToolResult":
        total = 0
        for label, value in (("output", self.output), ("error", self.error or "")):
            try:
                total += len(value.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ValueError(f"tool-result {label} must be valid UTF-8") from exc
            if total > MAX_TOOL_RESULT_TEXT_BYTES:
                raise ValueError(
                    "tool-result text exceeds "
                    f"{MAX_TOOL_RESULT_TEXT_BYTES} UTF-8 bytes"
                )
        return self

    @field_validator("image_blocks")
    @classmethod
    def validate_image_block_bytes(
        cls,
        value: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        total = 0
        for block in value:
            data = block.get("data", "")
            try:
                total += len(data.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ValueError("tool-result image data must be valid UTF-8") from exc
            if total > MAX_TOOL_RESULT_IMAGE_DATA_BYTES:
                raise ValueError(
                    "tool-result image data exceeds "
                    f"{MAX_TOOL_RESULT_IMAGE_DATA_BYTES} UTF-8 bytes"
                )
        return value

    @model_validator(mode="after")
    def validate_structured_metadata_bytes(self) -> "ToolResult":
        structured = {
            "diagnostics": self.diagnostics,
            "diagnostic_summary": self.diagnostic_summary,
            "citations": self.citations,
            "images": self.images,
            "image_blocks": [
                {key: value for key, value in block.items() if key != "data"}
                for block in self.image_blocks
            ],
        }
        try:
            size = len(
                json.dumps(
                    structured,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("tool-result structured metadata must be serializable") from exc
        if size > MAX_TOOL_RESULT_STRUCTURED_BYTES:
            raise ValueError(
                "tool-result structured metadata exceeds "
                f"{MAX_TOOL_RESULT_STRUCTURED_BYTES} UTF-8 bytes"
            )
        return self


class ToolReplayPolicy(StrEnum):
    """Automatic host replay permitted after a tool invocation starts."""

    NEVER = "never"


@dataclass(frozen=True)
class ToolExecutionContract:
    """Fail-closed execution semantics shared by built-in and extension tools."""

    replay_policy: ToolReplayPolicy = ToolReplayPolicy.NEVER
    parallel_safe: bool = False


class BaseTool(ABC):
    name: str
    description: str
    args_schema: type[BaseModel] | None
    sensitive_argument_fields: frozenset[str] = frozenset()
    execution_contract = ToolExecutionContract()

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

    def search_schema(self) -> dict[str, Any]:
        """Return schema metadata for discovery without changing provider semantics."""

        return self.json_schema()

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

    def event_context_data(self) -> dict[str, Any]:
        """Return a copy of the current per-invocation event context."""

        return dict(self._event_context.get() or {})


def redact_tool_arguments(
    tool: BaseTool | None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Redact generic secrets plus declaratively sensitive tool fields."""

    redacted = redact_value(arguments)
    if not isinstance(redacted, dict):
        return {}
    sensitive = sensitive_tool_argument_fields(tool)
    for field in sensitive:
        if field in redacted:
            redacted[field] = "[REDACTED]"
    return redacted


def sensitive_tool_argument_fields(tool: BaseTool | None) -> frozenset[str]:
    """Return validated class-declared sensitive fields without invoking tool code."""

    if tool is None:
        return frozenset()
    declared: object = getattr(type(tool), "sensitive_argument_fields", frozenset())
    if not isinstance(declared, frozenset):
        return frozenset()
    validated = frozenset(
        field for field in declared if isinstance(field, str) and field
    )
    if len(validated) != len(declared):
        return frozenset()
    return validated


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
