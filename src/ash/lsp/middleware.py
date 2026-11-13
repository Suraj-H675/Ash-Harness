"""Non-fatal post-edit language-server diagnostics."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ash.core.redaction import redact_text
from ash.lsp.manager import LanguageServerManager
from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.tools.base import BaseTool, ToolMiddleware, ToolResult, count_output_tokens
from ash.tools.patch import extract_patch_paths


EDIT_TOOLS = {
    "write_file",
    "replace_file_content",
    "replace_file_edits",
    "whole_edit",
    "apply_patch",
}
MAX_POST_EDIT_FILES = 20
POST_EDIT_TIMEOUT_SECONDS = 3.0
MAX_POST_EDIT_DIAGNOSTIC_OUTPUT_BYTES = 16 * 1024
MAX_POST_EDIT_DIAGNOSTICS = 24
MAX_POST_EDIT_DIAGNOSTICS_PER_FILE = 6
MAX_POST_EDIT_MESSAGE_CHARS = 240


def _bounded_diagnostic_preview(
    diagnostics: dict[str, list[dict[str, Any]]],
) -> tuple[str, int]:
    """Prioritize errors in a small model-facing preview, not a full LSP dump.

    The separate explicit ``lsp`` tool retains full bounded diagnostic queries.
    Workspace diagnostics are untrusted and can be very large or contain
    secrets; never echo original message bodies or cut serialized JSON midway.
    """

    rows = [
        (item.get("severity") if isinstance(item.get("severity"), int) else 5,
         path, index, item)
        for path, items in diagnostics.items()
        for index, item in enumerate(items)
        if isinstance(item, dict)
    ]
    rows.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    selected: dict[str, list[dict[str, Any]]] = {}
    shown = 0
    heading = "\nLSP diagnostics after edit:\n"
    # Reserve space for the omitted-diagnostic notice, regardless of the
    # number of candidate messages or the length of a workspace-relative path.
    json_limit = MAX_POST_EDIT_DIAGNOSTIC_OUTPUT_BYTES - len(heading) - 256
    for _, path, _, item in rows:
        if shown >= MAX_POST_EDIT_DIAGNOSTICS:
            break
        chosen = selected.setdefault(path, [])
        if len(chosen) >= MAX_POST_EDIT_DIAGNOSTICS_PER_FILE:
            continue
        preview = {
            "range": item.get("range"),
            "severity": item.get("severity"),
            "message": redact_text(str(item.get("message", "")))
            [:MAX_POST_EDIT_MESSAGE_CHARS],
            "source": redact_text(str(item.get("source", "")))[:128],
        }
        if "code" in item:
            preview["code"] = redact_text(str(item["code"]))[:80]
        chosen.append(preview)
        rendered = json.dumps(selected, ensure_ascii=True, indent=2)
        if len(rendered.encode("utf-8")) > json_limit:
            chosen.pop()
            if not chosen:
                selected.pop(path)
            continue
        shown += 1

    rendered = json.dumps(selected, ensure_ascii=True, indent=2)
    omitted = max(0, sum(map(len, diagnostics.values())) - shown)
    output = heading + rendered
    if omitted:
        output += (
            f"\n[LSP diagnostics omitted: {omitted}; use the lsp diagnostics "
            "tool for full results]"
        )
    return output, omitted


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
            done, pending = await asyncio.wait(tasks, timeout=POST_EDIT_TIMEOUT_SECONDS)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        inspected_results = sorted(
            (inspected for task in done if (inspected := task.result()) is not None),
            key=lambda item: item[0],
        )
        diagnostics = {path: items for path, items in inspected_results if items}
        skipped = max(0, len(paths) - MAX_POST_EDIT_FILES)
        if skipped:
            result.output += (
                f"\n[LSP diagnostics skipped for {skipped} additional edited files]"
            )
            result.truncated = True
        if diagnostics:
            rendered, omitted = _bounded_diagnostic_preview(diagnostics)
            if omitted:
                result.truncated = True
            result.output += rendered
        if diagnostics or skipped:
            result.token_count = count_output_tokens(result.output)
