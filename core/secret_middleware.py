"""Redact common credentials before tool results reach logs or persistence."""

from __future__ import annotations

from typing import Any

from core.redaction import redact_text
from tools.base import BaseTool, ToolMiddleware, ToolResult


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
