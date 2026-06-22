"""Typed user clarification tool."""

from __future__ import annotations

import sys
from typing import Any, Callable

from pydantic import BaseModel, Field

from safety.guard import SafetyGuard
from tools.base import BaseTool, ToolResult, count_output_tokens


class AskUserArgs(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    options: list[str] = Field(default_factory=list, max_length=10)


class AskUserTool(BaseTool):
    name = "ask_user"
    description = "Ask the user one blocking clarification question."
    args_schema = AskUserArgs

    def __init__(
        self,
        safety_guard: SafetyGuard,
        callback: Callable[[str, list[str]], str] | None = None,
    ) -> None:
        super().__init__(safety_guard)
        self.callback = callback or self._terminal_callback

    async def run(self, **kwargs: Any) -> ToolResult:
        args = AskUserArgs(**kwargs)
        answer = self.callback(args.question, args.options).strip()
        if not answer:
            return ToolResult(success=False, output="", error="User provided no answer")
        return ToolResult(
            success=True,
            output=answer,
            token_count=count_output_tokens(answer),
        )

    @staticmethod
    def _terminal_callback(question: str, options: list[str]) -> str:
        print(f"\n{question}", file=sys.stderr)
        if options:
            print("Options: " + " | ".join(options), file=sys.stderr)
        print("> ", end="", file=sys.stderr, flush=True)
        return sys.stdin.readline()
