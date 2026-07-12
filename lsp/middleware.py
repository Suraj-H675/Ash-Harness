"""Non-fatal post-edit language-server diagnostics."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from lsp.manager import LanguageServerManager
from safety.guard import SafetyGuard, SafetyViolation
from tools.base import BaseTool, ToolMiddleware, ToolResult, count_output_tokens
from tools.patch import extract_patch_paths


EDIT_TOOLS = {
    "write_file",
    "replace_file_content",
    "replace_file_edits",
    "whole_edit",
    "apply_patch",
}
MAX_POST_EDIT_FILES = 20
POST_EDIT_TIMEOUT_SECONDS = 3.0


class LSPDiagnosticsMiddleware(ToolMiddleware):
    def __init__(
        self, manager: LanguageServerManager, safety_guard: SafetyGuard
    ) -> None:
        self.manager = manager
        self.safety_guard = safety_guard

    async def before_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        tool: BaseTool,
    ) -> None:
        return None

    async def after_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> None:
        if tool_name not in EDIT_TOOLS or not result.success:
            return
        paths: set[str] = set()
        file_path = arguments.get("file_path")
        if isinstance(file_path, str) and file_path:
            paths.add(file_path)
        elif tool_name == "apply_patch" and isinstance(arguments.get("patch"), str):
            try:
                paths.update(extract_patch_paths(arguments["patch"], self.safety_guard))
            except (OSError, SafetyViolation, ValueError):
                return
        if not paths:
            return
        async def inspect(value: str) -> tuple[str, list[dict[str, Any]]] | None:
            try:
                path = self.manager.resolve_file(value)
                items = await self.manager.diagnostics_for(path)
            except Exception:  # noqa: BLE001 - diagnostics must never invalidate an edit
                return None
            return path.relative_to(self.manager.workspace).as_posix(), items

        tasks = [
            asyncio.create_task(inspect(value))
            for value in sorted(paths)[:MAX_POST_EDIT_FILES]
        ]
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=POST_EDIT_TIMEOUT_SECONDS
            )
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        inspected_results = sorted(
            (
                inspected
                for task in done
                if (inspected := task.result()) is not None
            ),
            key=lambda item: item[0],
        )
        diagnostics = {
            path: items
            for path, items in inspected_results
            if items
        }
        skipped = max(0, len(paths) - MAX_POST_EDIT_FILES)
        if skipped:
            result.output += (
                f"\n[LSP diagnostics skipped for {skipped} additional edited files]"
            )
            result.truncated = True
        if diagnostics:
            rendered = "\nLSP diagnostics after edit:\n" + json.dumps(
                diagnostics, ensure_ascii=True, indent=2
            )
            if len(rendered) > 256 * 1024:
                rendered = rendered[: 256 * 1024] + "\n[LSP diagnostics truncated]"
                result.truncated = True
            result.output += rendered
        if diagnostics or skipped:
            result.token_count = count_output_tokens(result.output)
