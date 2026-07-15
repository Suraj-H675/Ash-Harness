"""Deferred provider tool discovery for large runtime inventories."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ash.core.redaction import redact_text
from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult, count_output_tokens


ESSENTIAL_TOOL_NAMES = frozenset(
    {
        "activate_skill",
        "apply_patch",
        "ask_user",
        "delegate_agents",
        "git_status",
        "glob_files",
        "list_dir",
        "read_file",
        "replace_file_content",
        "run_command",
        "search_text",
        "spawn_agent",
        "write_file",
    }
)


class SearchToolsArgs(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    limit: int = Field(8, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("tool search query cannot be blank")
        return normalized


class SearchToolsTool(BaseTool):
    name = "search_tools"
    description = (
        "Search the complete runtime tool catalog and activate exact schemas for "
        "the best matches."
    )
    args_schema = SearchToolsArgs

    def __init__(
        self,
        safety_guard: SafetyGuard,
        catalog: Callable[[], dict[str, BaseTool]],
        *,
        threshold: int = 32,
    ) -> None:
        super().__init__(safety_guard)
        if not 0 <= threshold <= 1000:
            raise ValueError("tool search threshold must be between 0 and 1000")
        self._catalog = catalog
        self.threshold = threshold
        self.activated_names: set[str] = set()

    async def run(self, **kwargs: Any) -> ToolResult:
        args = SearchToolsArgs(**kwargs)
        catalog = self._catalog()
        matches = _rank_tools(catalog, args.query, args.limit)
        self.activated_names.update(item[0] for item in matches)
        payload = {
            "query": redact_text(args.query),
            "tools": [
                {
                    "name": name,
                    "description": getattr(tool, "description", ""),
                    "input_schema": tool.json_schema(),
                }
                for name, tool, _ in matches
            ],
        }
        output = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.emit_event(
            {
                "type": "tool.search.completed",
                "query": redact_text(args.query),
                "matches": [name for name, _, _ in matches],
            }
        )
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
        )

    def visible_tools(self, catalog: dict[str, BaseTool]) -> dict[str, BaseTool]:
        if self.threshold == 0 or len(catalog) <= self.threshold:
            return catalog
        visible_names = ESSENTIAL_TOOL_NAMES | self.activated_names | {self.name}
        return {name: tool for name, tool in catalog.items() if name in visible_names}

    def reset_activations(self) -> None:
        self.activated_names.clear()

    def set_catalog_provider(
        self, catalog: Callable[[], dict[str, BaseTool]]
    ) -> None:
        """Bind discovery to the owning runtime's live tool inventory."""

        self._catalog = catalog

    def prune_activations(self, available_names: set[str]) -> None:
        self.activated_names.intersection_update(available_names)


def _rank_tools(
    catalog: dict[str, BaseTool],
    query: str,
    limit: int,
) -> list[tuple[str, BaseTool, int]]:
    normalized = query.strip().casefold()
    terms = tuple(dict.fromkeys(re.findall(r"[a-z0-9_]+", normalized)))
    ranked: list[tuple[str, BaseTool, int]] = []
    for name, tool in catalog.items():
        if name == SearchToolsTool.name:
            continue
        normalized_name = name.casefold()
        description = str(getattr(tool, "description", "")).casefold()
        schema = json.dumps(
            tool.json_schema(), ensure_ascii=False, sort_keys=True
        ).casefold()
        score = 0
        if normalized_name == normalized:
            score += 1000
        elif normalized_name.startswith(normalized):
            score += 500
        elif normalized in normalized_name:
            score += 300
        for term in terms:
            if term == normalized_name:
                score += 200
            elif term in normalized_name:
                score += 80
            if term in description:
                score += 30
            if term in schema:
                score += 10
        if score:
            ranked.append((name, tool, score))
    ranked.sort(key=lambda item: (-item[2], item[0]))
    return ranked[:limit]
