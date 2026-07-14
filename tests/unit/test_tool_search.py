from __future__ import annotations

import io
import json
from typing import Any

import pytest
from pydantic import BaseModel, Field

from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult
from ash.tools.tool_search import SearchToolsTool
from ash.ui.headless import HeadlessUI


class TextArgs(BaseModel):
    text: str = Field(..., description="Database query text.")


class DummyTool(BaseTool):
    args_schema = TextArgs

    def __init__(self, guard: SafetyGuard, name: str, description: str) -> None:
        super().__init__(guard)
        self.name = name
        self.description = description

    async def run(self, **kwargs: Any) -> ToolResult:
        args = TextArgs(**kwargs)
        return ToolResult(success=True, output=f"ran {self.name}: {args.text}")


@pytest.mark.asyncio
async def test_tool_search_ranks_activates_and_resets_exact_schemas(tmp_path) -> None:
    guard = SafetyGuard(tmp_path)
    catalog: dict[str, BaseTool] = {}
    search = SearchToolsTool(guard, lambda: catalog, threshold=2)
    database = DummyTool(guard, "database_query", "Run a SQL database query.")
    unrelated = DummyTool(guard, "send_email", "Send a message.")
    catalog.update(
        {
            search.name: search,
            database.name: database,
            unrelated.name: unrelated,
        }
    )

    assert set(search.visible_tools(catalog)) == {"search_tools"}
    result = await search.run(query="database SQL", limit=1)
    payload = json.loads(result.output)

    assert result.success is True
    assert payload["tools"] == [
        {
            "name": "database_query",
            "description": "Run a SQL database query.",
            "input_schema": database.json_schema(),
        }
    ]
    assert set(search.visible_tools(catalog)) == {
        "search_tools",
        "database_query",
    }
    search.reset_activations()
    assert set(search.visible_tools(catalog)) == {"search_tools"}

    with pytest.raises(ValueError, match="cannot be blank"):
        await search.run(query="   ")


class SearchFlowProvider(ProviderABC):
    model_name = "search-flow"
    _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

    def __init__(self) -> None:
        self.calls = 0
        self.tool_names: list[set[str]] = []

    def count_tokens(self, text: str) -> int:
        return len(text)

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.calls += 1
        names = {item["function"]["name"] for item in (tools or [])}
        self.tool_names.append(names)
        if self.calls == 1:
            assert names == {"search_tools"}
            yield StreamChunk(
                native_tool_calls=[
                    {
                        "id": "search-call",
                        "name": "search_tools",
                        "arguments": {"query": "hidden database", "limit": 1},
                    }
                ],
                is_done=True,
            )
        elif self.calls == 2:
            assert names == {"search_tools", "hidden_database"}
            yield StreamChunk(
                native_tool_calls=[
                    {
                        "id": "hidden-call",
                        "name": "hidden_database",
                        "arguments": {"text": "select one"},
                    }
                ],
                is_done=True,
            )
        else:
            yield StreamChunk(content="tool search complete", is_done=True)


@pytest.mark.asyncio
async def test_loop_exposes_search_match_on_next_iteration_and_budgets_visible_tools(
    tmp_path,
) -> None:
    guard = SafetyGuard(tmp_path)
    catalog: dict[str, BaseTool] = {}
    search = SearchToolsTool(guard, lambda: catalog, threshold=2)
    hidden = DummyTool(guard, "hidden_database", "Hidden database access.")
    other = DummyTool(guard, "other_remote", "Another remote integration.")
    catalog.update({search.name: search, hidden.name: hidden, other.name: other})
    provider = SearchFlowProvider()
    loop = AshLoop(
        SessionStore(tmp_path / "sessions.db"),
        provider,
        guard,
        HeadlessUI(output_format="text", stream=io.StringIO()),
        tmp_path,
        tools=catalog,
        safety_tier="auto_approve",
    )
    await loop.start_session()
    initial_schema_tokens = loop._estimate_tool_schema_tokens()

    response = await loop.run_turn("use the hidden database")

    assert response == "tool search complete"
    assert provider.tool_names == [
        {"search_tools"},
        {"search_tools", "hidden_database"},
        {"search_tools", "hidden_database"},
    ]
    assert loop._estimate_tool_schema_tokens() > initial_schema_tokens
    await loop.start_session()
    assert set(loop._provider_tools()) == {"search_tools"}
    await loop.aclose()
