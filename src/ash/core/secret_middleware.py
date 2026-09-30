"""Redact common credentials before tool results reach logs or persistence."""

from __future__ import annotations

from typing import Any, cast

from ash.core.redaction import redact_text, redact_value
from ash.tools.base import BaseTool, ToolMiddleware, ToolResult


class SecretRedactionMiddleware(ToolMiddleware):
    async def before_tool(
        self, tool_name: str, arguments: dict[str, Any], tool: BaseTool
    ) -> None:
        return None

    async def after_tool(
        self, tool_name: str, arguments: dict[str, Any], result: ToolResult
    ) -> None:
        result.output = redact_text(result.output)
        if result.error:
            result.error = redact_text(result.error)
        result.diagnostics = cast(
            list[dict[str, Any]],
            redact_value(result.diagnostics),
        )
        result.citations = cast(
            list[dict[str, Any]],
            redact_value(result.citations),
        )
        result.images = cast(
            list[dict[str, str]],
            redact_value(result.images),
        )
        result.image_blocks = [
            {
                key: value if key == "data" else cast(str, redact_value(value))
                for key, value in block.items()
            }
            for block in result.image_blocks
        ]
