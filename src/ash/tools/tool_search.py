"""Deferred provider tool discovery for large runtime inventories."""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from itertools import islice
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
        "git_status",
        "list_dir",
        "read_file",
        "run_command",
        "search_text",
        "write_file",
    }
)
MAX_ACTIVATED_TOOL_SCHEMAS = 64
MAX_TOOL_SEARCH_SCHEMA_TEXT_CHARS = 8_192
MAX_TOOL_SEARCH_SCHEMA_NODES = 512
MAX_TOOL_SEARCH_EXACT_SCHEMA_BYTES = 8 * 1024
MAX_TOOL_SEARCH_DESCRIPTION_CHARS = 2_048
MAX_TOOL_SEARCH_SUMMARY_FIELDS = 32
MAX_TOOL_SEARCH_SUMMARY_FIELD_CHARS = 128
_SEARCH_WORD_PATTERN = re.compile(r"[a-z0-9]+")
_SEARCH_STOP_WORDS = frozenset(
    """
    a an and are as at be by can could did do does for from had has have how i in
    into is it its me my of on or our out please so that the their them then
    show there these they this those to up use we were what when where which who
    will with would you your action actions tool tools
    """.split()
)
_NAME_MATCH_SCORE = 100
_DESCRIPTION_MATCH_SCORE = 30
_SCHEMA_MATCH_SCORE = 10
_EXACT_NAME_MATCH_BOOST = 1_000
_PREFIX_NAME_MATCH_BOOST = 500
_MIN_QUERY_MATCH_TERMS = 2


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
        max_activations: int = MAX_ACTIVATED_TOOL_SCHEMAS,
    ) -> None:
        super().__init__(safety_guard)
        if not 0 <= threshold <= 1000:
            raise ValueError("tool search threshold must be between 0 and 1000")
        if not 1 <= max_activations <= 1000:
            raise ValueError("tool search activation limit must be between 1 and 1000")
        self._catalog = catalog
        self.threshold = threshold
        self.max_activations = max_activations
        self.activated_names: set[str] = set()
        self._activation_order: deque[str] = deque()

    async def run(self, **kwargs: Any) -> ToolResult:
        args = SearchToolsArgs(**kwargs)
        catalog = self._catalog()
        matches = _rank_tools(catalog, args.query, args.limit)
        for name, _, _ in matches:
            if name in self.activated_names:
                try:
                    self._activation_order.remove(name)
                except ValueError:
                    pass
            self.activated_names.add(name)
            self._activation_order.append(name)
        while len(self.activated_names) > self.max_activations:
            oldest = self._activation_order.popleft()
            self.activated_names.discard(oldest)
        payload = {
            "query": redact_text(args.query),
            "tools": [
                _search_result_tool(name, tool)
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
        self._activation_order.clear()

    def set_catalog_provider(
        self, catalog: Callable[[], dict[str, BaseTool]]
    ) -> None:
        """Bind discovery to the owning runtime's live tool inventory."""

        self._catalog = catalog

    def prune_activations(self, available_names: set[str]) -> None:
        self.activated_names.intersection_update(available_names)
        self._activation_order = deque(
            name
            for name in self._activation_order
            if name in self.activated_names
        )


def _rank_tools(
    catalog: dict[str, BaseTool],
    query: str,
    limit: int,
) -> list[tuple[str, BaseTool, int]]:
    normalized = query.strip().casefold()
    terms = tuple(
        dict.fromkeys(
            term
            for term in _SEARCH_WORD_PATTERN.findall(normalized)
            if term not in _SEARCH_STOP_WORDS
        )
    )
    normalized_name_query = re.sub(r"\s+", "_", normalized)
    ranked: list[tuple[str, BaseTool, int]] = []
    for name, tool in catalog.items():
        if name == SearchToolsTool.name:
            continue
        normalized_name = name.casefold()
        name_words = _search_words(name)
        description = str(getattr(tool, "description", ""))[
            :MAX_TOOL_SEARCH_DESCRIPTION_CHARS
        ].casefold()
        description_words = _search_words(description)
        schema_words = _search_words(_bounded_schema_search_text(tool.search_schema()))
        score = 0
        matched_terms = 0
        for term in terms:
            if term in name_words:
                score += _NAME_MATCH_SCORE
                matched_terms += 1
            elif term in description_words:
                score += _DESCRIPTION_MATCH_SCORE
                matched_terms += 1
            elif term in schema_words:
                score += _SCHEMA_MATCH_SCORE
                matched_terms += 1
        exact_name_query = normalized_name == normalized_name_query
        name_prefix_query = bool(normalized_name_query) and normalized_name.startswith(
            normalized_name_query
        )
        if exact_name_query:
            score += _EXACT_NAME_MATCH_BOOST
        elif name_prefix_query:
            score += _PREFIX_NAME_MATCH_BOOST
        if score and (
            len(terms) < _MIN_QUERY_MATCH_TERMS
            or matched_terms >= _MIN_QUERY_MATCH_TERMS
            or name_prefix_query
        ):
            ranked.append((name, tool, score))
    ranked.sort(key=lambda item: (-item[2], item[0]))
    return ranked[:limit]


def _search_words(value: str) -> set[str]:
    return set(_SEARCH_WORD_PATTERN.findall(value.casefold()))


def _bounded_schema_search_text(schema: Any) -> str:
    """Extract bounded searchable schema text without serializing the full tree."""

    pieces: list[str] = []
    remaining = MAX_TOOL_SEARCH_SCHEMA_TEXT_CHARS
    nodes = 0
    stack: list[Any] = [schema]

    def add(value: Any) -> None:
        nonlocal remaining
        if remaining <= 0:
            return
        text = str(value)
        if not text:
            return
        clipped = text[:remaining]
        pieces.append(clipped)
        remaining -= len(clipped) + 1

    while stack and remaining > 0 and nodes < MAX_TOOL_SEARCH_SCHEMA_NODES:
        current = stack.pop()
        nodes += 1
        if isinstance(current, Mapping):
            children: list[Any] = []
            for key, value in current.items():
                if remaining <= 0 or nodes + len(children) >= MAX_TOOL_SEARCH_SCHEMA_NODES:
                    break
                add(key)
                if isinstance(value, (Mapping, list, tuple)):
                    children.append(value)
                elif isinstance(value, (str, int, float, bool)) or value is None:
                    add(value)
            stack.extend(reversed(children))
        elif isinstance(current, (list, tuple)):
            for value in reversed(current[:MAX_TOOL_SEARCH_SCHEMA_NODES]):
                stack.append(value)
        elif isinstance(current, (str, int, float, bool)) or current is None:
            add(current)
    return " ".join(pieces).casefold()


def _search_result_tool(name: str, tool: BaseTool) -> dict[str, Any]:
    description = str(getattr(tool, "description", ""))[
        :MAX_TOOL_SEARCH_DESCRIPTION_CHARS
    ]
    schema = tool.search_schema()
    encoded = json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    result: dict[str, Any] = {"name": name, "description": description}
    if len(encoded.encode("utf-8")) <= MAX_TOOL_SEARCH_EXACT_SCHEMA_BYTES:
        result["input_schema"] = schema
        return result
    result["schema_truncated"] = True
    result["schema_summary"] = _schema_summary(schema)
    return result


def _schema_summary(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, Mapping):
        return {"type": type(schema).__name__}
    summary: dict[str, Any] = {}
    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        summary["type"] = schema_type[:MAX_TOOL_SEARCH_SUMMARY_FIELD_CHARS]
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        summary["properties"] = [
            str(name)[:MAX_TOOL_SEARCH_SUMMARY_FIELD_CHARS]
            for name in islice(properties, MAX_TOOL_SEARCH_SUMMARY_FIELDS)
        ]
    required = schema.get("required")
    if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
        summary["required"] = [
            str(name)[:MAX_TOOL_SEARCH_SUMMARY_FIELD_CHARS]
            for name in islice(iter(required), MAX_TOOL_SEARCH_SUMMARY_FIELDS)
        ]
    return summary
