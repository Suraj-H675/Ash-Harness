"""Provider-facing semantic code navigation through managed LSP servers."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ash.lsp.client import LSPError
from ash.lsp.manager import LanguageServerManager
from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult, count_output_tokens


LSPOperation = Literal[
    "status",
    "diagnostics",
    "hover",
    "definition",
    "references",
    "implementation",
    "documentSymbol",
    "workspaceSymbol",
    "prepareRename",
    "rename",
    "codeAction",
    "prepareCallHierarchy",
    "incomingCalls",
    "outgoingCalls",
]


class LSPQueryArgs(BaseModel):
    operation: LSPOperation
    file_path: str = Field(default="", max_length=4096)
    line: int = Field(default=1, ge=1, le=10_000_000)
    character: int = Field(default=1, ge=1, le=10_000_000)
    query: str = Field(default="", max_length=512)
    new_name: str = Field(default="", max_length=512)
    end_line: int | None = Field(default=None, ge=1, le=10_000_000)
    end_character: int | None = Field(default=None, ge=1, le=10_000_000)
    code_action_kind: str = Field(default="", max_length=256)

    @model_validator(mode="after")
    def validate_operation_fields(self) -> "LSPQueryArgs":
        if self.operation not in {"status", "workspaceSymbol"} and not self.file_path:
            raise ValueError("file_path is required for this LSP operation")
        if self.operation == "rename" and not self.new_name.strip():
            raise ValueError("new_name is required for rename")
        if self.operation != "rename" and self.new_name:
            raise ValueError("new_name is only valid for rename")
        if (self.end_line is None) != (self.end_character is None):
            raise ValueError("end_line and end_character must be provided together")
        if self.operation != "codeAction" and (
            self.end_line is not None
            or self.end_character is not None
            or self.code_action_kind
        ):
            raise ValueError("code-action range and kind are only valid for codeAction")
        return self


class LSPTool(BaseTool):
    name = "lsp"
    description = (
        "Query installed language servers for diagnostics, hover, definitions, "
        "references, implementations, symbols, advisory rename/code actions, and "
        "call hierarchy. LSP edits and commands are never executed directly. "
        "Coordinates are 1-based as shown in editors."
    )
    args_schema = LSPQueryArgs

    def __init__(
        self, safety_guard: SafetyGuard, manager: LanguageServerManager
    ) -> None:
        super().__init__(safety_guard)
        self.manager = manager

    async def run(self, **kwargs: Any) -> ToolResult:
        args = self.validate_args(**kwargs)
        assert isinstance(args, LSPQueryArgs)
        try:
            result = await self.manager.query(
                args.operation,
                file_path=args.file_path,
                line=args.line,
                character=args.character,
                query=args.query,
                new_name=args.new_name,
                end_line=args.end_line,
                end_character=args.end_character,
                code_action_kind=args.code_action_kind,
            )
        except (LSPError, OSError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
        output = json.dumps(result, ensure_ascii=True, indent=2)
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
        )

    async def aclose(self) -> None:
        await self.manager.aclose()
