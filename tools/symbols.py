"""Tree-sitter-backed structural code-navigation tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from repo.repomap import RepoMap
from safety.guard import SafetyGuard
from tools.base import BaseTool, ToolResult, count_output_tokens


class SymbolQueryArgs(BaseModel):
    query: str = Field(..., min_length=1, max_length=256)
    case_sensitive: bool = True
    path_glob: str | None = Field(
        None,
        max_length=512,
        description="Optional workspace-relative glob such as 'src/**/*.ts'.",
    )
    max_results: int = Field(100, ge=1, le=500)


class FindSymbolTool(BaseTool):
    name = "find_symbol"
    description = (
        "Find exact class, type, function, and method definitions using the "
        "workspace Tree-sitter index."
    )
    args_schema = SymbolQueryArgs

    def __init__(self, safety_guard: SafetyGuard, repo_map: RepoMap) -> None:
        super().__init__(safety_guard)
        self.repo_map = repo_map

    async def run(self, **kwargs: Any) -> ToolResult:
        args = SymbolQueryArgs(**kwargs)
        matches = self.repo_map.find_definitions(
            args.query,
            case_sensitive=args.case_sensitive,
            path_glob=args.path_glob,
            limit=args.max_results + 1,
        )
        truncated = len(matches) > args.max_results
        matches = matches[: args.max_results]
        if not matches:
            output = f"No definitions found for {args.query!r}."
        else:
            lines: list[str] = []
            for item in matches:
                path = self._relative_path(item.file_path)
                parent = f" in {item.parent}" if item.parent else ""
                lines.append(
                    f"{path}:{item.start_line}: {item.kind} {item.name}{parent} "
                    f"[{item.language}]"
                )
            output = "\n".join(lines)
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
            truncated=truncated,
        )

    def _relative_path(self, path: str) -> str:
        try:
            return (
                Path(path).resolve().relative_to(self.repo_map.project_root).as_posix()
            )
        except ValueError:
            return path


class FindReferencesTool(BaseTool):
    name = "find_references"
    description = (
        "Find structural identifier uses while excluding declarations, comments, "
        "and string literals."
    )
    args_schema = SymbolQueryArgs

    def __init__(self, safety_guard: SafetyGuard, repo_map: RepoMap) -> None:
        super().__init__(safety_guard)
        self.repo_map = repo_map

    async def run(self, **kwargs: Any) -> ToolResult:
        args = SymbolQueryArgs(**kwargs)
        matches = self.repo_map.find_references(
            args.query,
            case_sensitive=args.case_sensitive,
            path_glob=args.path_glob,
            limit=args.max_results + 1,
        )
        truncated = len(matches) > args.max_results
        matches = matches[: args.max_results]
        if not matches:
            output = f"No references found for {args.query!r}."
        else:
            lines = [
                (
                    f"{self._relative_path(item.file_path)}:"
                    f"{item.start_line}:{item.start_column}: {item.name} "
                    f"[{item.language}]"
                )
                for item in matches
            ]
            output = "\n".join(lines)
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
            truncated=truncated,
        )

    def _relative_path(self, path: str) -> str:
        try:
            return (
                Path(path).resolve().relative_to(self.repo_map.project_root).as_posix()
            )
        except ValueError:
            return path
