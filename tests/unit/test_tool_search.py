from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from ash.cli import _render_context_provenance
from ash.config import AshConfig
from ash.context.history import ContextBudgetAllocator, ContextFragmentKind
from ash.core.loop import AshLoop, _default_system_prompt
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult
from ash.tools.automation import ListAutomationsTool, ManageAutomationTool
from ash.tools.tool_search import ESSENTIAL_TOOL_NAMES, SearchToolsTool
from ash.ui.headless import HeadlessUI


class TextArgs(BaseModel):
    text: str = Field(..., description="Database query text.")


def test_small_context_keeps_discovery_and_verification() -> None:
    # Reproduce the CI macOS provider's 501-token core-system allocation.
    workspace = Path(
        "/private/var/folders/ab/abcdefghijklmnopqrstuvwx/T/pytest-of-runner/"
        "pytest-0/test_default_runtime_discovers2"
    )
    prompt = _default_system_prompt(workspace, native_tools=True, search_tools=True)
    fitted = ContextBudgetAllocator(
        max_context_tokens=4_096, completion_reserve=1_024
    ).fit_text(prompt, limit=501, count_tokens=lambda text: max(1, len(text) // 4))

    assert "next provider iteration" in fitted.text
    assert "Check requested behavior and edge cases" in fitted.text
    assert "never claim unrun tests" in fitted.text
    assert fitted.truncated is False


class DummyTool(BaseTool):
    args_schema = TextArgs

    def __init__(self, guard: SafetyGuard, name: str, description: str) -> None:
        super().__init__(guard)
        self.name = name
        self.description = description

    async def run(self, **kwargs: Any) -> ToolResult:
        args = TextArgs(**kwargs)
        return ToolResult(success=True, output=f"ran {self.name}: {args.text}")


def test_large_catalog_keeps_compact_core_and_searchable_remainder(tmp_path) -> None:
    guard = SafetyGuard(tmp_path)
    catalog: dict[str, BaseTool] = {}
    search = SearchToolsTool(guard, lambda: catalog, threshold=8)
    catalog[search.name] = search
    for name in ESSENTIAL_TOOL_NAMES:
        catalog[name] = DummyTool(guard, name, f"Essential {name} tool.")
    for index in range(40):
        name = f"optional_integration_{index}"
        catalog[name] = DummyTool(guard, name, f"Optional integration {index}.")

    visible = search.visible_tools(catalog)

    assert len(catalog) > search.threshold
    assert set(visible) == {*ESSENTIAL_TOOL_NAMES, search.name}
    assert len(visible) == 10
    assert len(visible) < len(catalog)


@pytest.mark.asyncio
async def test_default_edit_tool_is_exact_replacement_and_patch_is_discoverable(
    tmp_path,
) -> None:
    assert "replace_file_content" in ESSENTIAL_TOOL_NAMES
    assert "apply_patch" not in ESSENTIAL_TOOL_NAMES

    guard = SafetyGuard(tmp_path)
    catalog: dict[str, BaseTool] = {}
    search = SearchToolsTool(guard, lambda: catalog, threshold=8)
    catalog[search.name] = search
    for name in ESSENTIAL_TOOL_NAMES:
        catalog[name] = DummyTool(guard, name, f"Essential {name} tool.")
    catalog["apply_patch"] = DummyTool(
        guard, "apply_patch", "Apply a git-style unified diff to code."
    )

    assert "apply_patch" not in search.visible_tools(catalog)
    result = await search.run(query="apply_patch")
    assert result.success is True
    assert "apply_patch" in search.visible_tools(catalog)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("schedule a reminder", "manage_automation"),
        ("pause a reminder", "manage_automation"),
        ("list scheduled tasks", "list_automations"),
        ("list upcoming reminders", "list_automations"),
    ],
)
@pytest.mark.asyncio
async def test_automation_tools_are_discoverable_by_common_task_intents(
    tmp_path, query: str, expected: str
) -> None:
    guard = SafetyGuard(tmp_path)
    catalog: dict[str, BaseTool] = {}
    search = SearchToolsTool(guard, lambda: catalog, threshold=1)
    catalog.update(
        {
            search.name: search,
            "list_automations": ListAutomationsTool(guard, tmp_path / "automations.db"),
            "manage_automation": ManageAutomationTool(guard, tmp_path / "automations.db"),
        }
    )

    assert set(search.visible_tools(catalog)) == {"search_tools"}
    result = await search.run(query=query, limit=3)
    names = [item["name"] for item in json.loads(result.output)["tools"]]

    assert expected in names
    assert expected in search.visible_tools(catalog)


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


@pytest.mark.asyncio
async def test_tool_search_activations_use_bounded_lru_window(tmp_path) -> None:
    guard = SafetyGuard(tmp_path)
    catalog: dict[str, BaseTool] = {}
    search = SearchToolsTool(
        guard,
        lambda: catalog,
        threshold=1,
        max_activations=2,
    )
    first = DummyTool(guard, "first_database", "First database integration")
    second = DummyTool(guard, "second_database", "Second database integration")
    third = DummyTool(guard, "third_database", "Third database integration")
    catalog.update(
        {
            search.name: search,
            first.name: first,
            second.name: second,
            third.name: third,
        }
    )

    await search.run(query="first database", limit=1)
    await search.run(query="second database", limit=1)
    await search.run(query="first database", limit=1)
    await search.run(query="third database", limit=1)

    assert search.activated_names == {first.name, third.name}
    assert set(search.visible_tools(catalog)) == {
        search.name,
        first.name,
        third.name,
    }


@pytest.mark.asyncio
async def test_tool_search_uses_bounded_search_schema_and_summarizes_large_match(
    tmp_path,
) -> None:
    guard = SafetyGuard(tmp_path)

    class LargeSchemaTool(DummyTool):
        def __init__(self) -> None:
            super().__init__(guard, "large_remote", "Large remote integration")
            self._schema = {
                "type": "object",
                "properties": {
                    "needle_field": {
                        "type": "string",
                        "description": "needle searchable metadata",
                    },
                    "payload": {
                        "type": "string",
                        "description": "x" * 20_000,
                    },
                },
                "required": ["needle_field"],
            }

        def search_schema(self) -> dict[str, Any]:
            return self._schema

        def json_schema(self) -> dict[str, Any]:
            raise AssertionError("search must not copy the provider-facing schema")

    tool = LargeSchemaTool()
    catalog: dict[str, BaseTool] = {}
    search = SearchToolsTool(guard, lambda: catalog, threshold=1)
    catalog.update({search.name: search, tool.name: tool})

    result = await search.run(query="needle", limit=1)
    payload = json.loads(result.output)

    assert result.success is True
    assert payload["tools"] == [
        {
            "name": tool.name,
            "description": tool.description,
            "schema_truncated": True,
            "schema_summary": {
                "type": "object",
                "properties": ["needle_field", "payload"],
                "required": ["needle_field"],
            },
        }
    ]
    assert tool.name in search.activated_names
    assert tool.name in search.visible_tools(catalog)
    assert len(result.output.encode("utf-8")) < 16_000


def test_tool_search_rejects_invalid_activation_limit(tmp_path) -> None:
    guard = SafetyGuard(tmp_path)

    with pytest.raises(ValueError, match="activation limit"):
        SearchToolsTool(guard, lambda: {}, max_activations=0)


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


class ScriptedDiscoveryProvider(ProviderABC):
    model_name = "intent-discovery"

    def __init__(
        self,
        query: str,
        expected_tool: str | None,
        context_window: int,
    ) -> None:
        self.query = query
        self.expected_tool = expected_tool
        self._ash_declared_capabilities = ProviderCapabilities(
            native_tools=True,
            context_window=context_window,
            max_input_tokens=context_window - 1_024,
            max_output_tokens=1_024,
        )
        self.calls = 0
        self.tool_names: list[set[str]] = []
        self.tool_schemas: list[dict[str, dict[str, Any]]] = []
        self.initial_system_prompt = ""

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        self.calls += 1
        names = {item["function"]["name"] for item in (tools or [])}
        self.tool_names.append(names)
        self.tool_schemas.append(
            {
                item["function"]["name"]: item["function"]["parameters"]
                for item in (tools or [])
            }
        )
        if self.calls == 1:
            self.initial_system_prompt = messages[0]["content"]
            yield StreamChunk(
                native_tool_calls=[
                    {
                        "id": "discover-intent",
                        "name": "search_tools",
                        "arguments": {"query": self.query},
                    }
                ],
                is_done=True,
            )
        else:
            if self.expected_tool is not None:
                assert self.expected_tool in names
            yield StreamChunk(content="discovery complete", is_done=True)


@pytest.mark.asyncio
async def test_context_provenance_reports_frozen_visible_tool_selection(
    tmp_path,
) -> None:
    guard = SafetyGuard(tmp_path)
    catalog: dict[str, BaseTool] = {}
    search = SearchToolsTool(guard, lambda: catalog, threshold=1)
    catalog.update(
        {
            search.name: search,
            "hidden_database": DummyTool(
                guard, "hidden_database", "A database integration."
            ),
            "remote_storage": DummyTool(
                guard, "remote_storage", "A remote storage integration."
            ),
        }
    )
    loop = AshLoop(
        SessionStore(tmp_path / "context-tool-provenance.db"),
        SearchFlowProvider(),
        guard,
        HeadlessUI(output_format="text", stream=io.StringIO()),
        tmp_path,
        tools=catalog,
        config=AshConfig(
            model="local/search-flow",
            workspace_root=tmp_path,
            db_directory=tmp_path / "db",
            max_context_tokens=16_384,
            max_completion_tokens=1_024,
            memory_backend="off",
        ),
    )
    session = await loop.start_session()

    loop._build_messages(session)

    assert loop._last_context_budget is not None
    tool_fragment = next(
        fragment
        for fragment in loop._last_context_budget.fragments
        if fragment.kind is ContextFragmentKind.TOOL_SCHEMA
    )
    metadata = dict(tool_fragment.metadata)
    provenance = _render_context_provenance(loop._last_context_budget)

    assert metadata["tool_count"] == "1"
    assert metadata["registry_tool_count"] == "3"
    assert metadata["deferred_tool_count"] == "2"
    assert metadata["provider_visible_tool_names"] == "search_tools"
    assert "provider_visible_tool_names=search_tools" in provenance
    assert "deferred_tool_count=2" in provenance
    await loop.aclose()


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


@pytest.mark.asyncio
async def test_loop_search_catalog_tracks_runtime_additions_and_removals(
    tmp_path,
) -> None:
    guard = SafetyGuard(tmp_path)
    initial_catalog: dict[str, BaseTool] = {}
    search = SearchToolsTool(guard, lambda: initial_catalog, threshold=1)
    initial_catalog[search.name] = search
    loop = AshLoop(
        SessionStore(tmp_path / "live-search.db"),
        SearchFlowProvider(),
        guard,
        HeadlessUI(output_format="text", stream=io.StringIO()),
        tmp_path,
        tools=initial_catalog,
    )
    late = DummyTool(guard, "late_mcp_tool", "Query a live remote catalog.")
    loop.tools[late.name] = late

    result = await search.run(query="live remote", limit=1)
    assert json.loads(result.output)["tools"][0]["name"] == late.name
    assert late.name in search.activated_names

    loop.tools.pop(late.name)
    search.prune_activations(set(loop.tools))
    result = await search.run(query="live remote", limit=1)
    assert json.loads(result.output)["tools"] == []
    assert late.name not in search.activated_names
    await loop.aclose()


@pytest.mark.asyncio
async def test_iteration_tool_snapshot_survives_live_catalog_removal(tmp_path) -> None:
    guard = SafetyGuard(tmp_path)
    tool = DummyTool(guard, "dynamic_tool", "A changing remote tool.")
    loop = AshLoop(
        SessionStore(tmp_path / "snapshot.db"),
        SearchFlowProvider(),
        guard,
        HeadlessUI(output_format="text", stream=io.StringIO()),
        tmp_path,
        tools={tool.name: tool},
        safety_tier="auto_approve",
    )
    session = await loop.start_session()
    snapshot = dict(loop.tools)
    loop.tools.pop(tool.name)

    results = await loop._execute_tool_calls(
        [
            {
                "call_id": "dynamic-1",
                "name": tool.name,
                "arguments": {"text": "still valid"},
            }
        ],
        session,
        tools_snapshot=snapshot,
    )

    assert results[0]["success"] is True
    assert results[0]["output"] == "ran dynamic_tool: still valid"
    await loop.aclose()


@pytest.mark.parametrize(
    ("intent", "query", "expected_tool", "context_window"),
    [
        (
            "Hand this focused coding subtask to another agent to work in parallel.",
            "hand this focused coding subtask to another agent to work in parallel",
            "delegate_agents",
            8_192,
        ),
        (
            "Apply several exact replacements to one file atomically.",
            "apply several exact replacements to one file atomically",
            "replace_file_edits",
            8_192,
        ),
        (
            "Fill out a browser form.",
            "fill out a browser form",
            "browser_type",
            4_096,
        ),
        (
            "Review my staged and unstaged code changes.",
            "review my staged and unstaged code changes",
            "git_diff",
            4_096,
        ),
        (
            "Run pytest and leave it running so I can check progress.",
            "run pytest and leave it running so I can check progress",
            "background_process",
            4_096,
        ),
        (
            "Start a server and keep it running while I edit code.",
            "start server",
            "background_process",
            4_096,
        ),
        (
            "Show the current Git patch.",
            "get git patch",
            "git_diff",
            4_096,
        ),
        (
            "Show me a plan before making changes.",
            "show me a plan before making changes",
            None,
            4_096,
        ),
        ("Undo my last change.", "undo my last change", None, 4_096),
        (
            "Find a connected MCP action to create an issue.",
            "find a connected MCP action to create an issue",
            None,
            4_096,
        ),
    ],
)
@pytest.mark.asyncio
async def test_default_runtime_discovers_tools_for_natural_language_intents(
    tmp_path,
    monkeypatch,
    intent: str,
    query: str,
    expected_tool: str | None,
    context_window: int,
) -> None:
    from ash.runtime import build_tools

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    config = AshConfig(
        model="local/intent-discovery",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        max_context_tokens=16_384,
        max_completion_tokens=1_024,
        memory_backend="off",
        tool_search_threshold=32,
    )
    guard = SafetyGuard(tmp_path)

    def unexpected_provider_factory(*_args, **_kwargs):
        raise AssertionError("discovery evaluation does not call provider tools")

    catalog = build_tools(
        guard,
        tmp_path,
        runtime_config=config,
        provider_factory=unexpected_provider_factory,
        agent_db_path=tmp_path / "agents.db",
    )
    provider = ScriptedDiscoveryProvider(query, expected_tool, context_window)
    loop = AshLoop(
        SessionStore(tmp_path / "intent-discovery.db"),
        provider,
        guard,
        HeadlessUI(output_format="text", stream=io.StringIO()),
        tmp_path,
        tools=catalog,
        config=config,
    )

    assert len(catalog) > config.tool_search_threshold
    initial_payload = loop._tool_schema_payload()
    initial_names = {item["function"]["name"] for item in initial_payload}
    assert initial_names == {*ESSENTIAL_TOOL_NAMES, "search_tools"}

    await loop.start_session()
    response = await loop.run_turn(intent)

    assert response == "discovery complete"
    assert provider.tool_names[0] == initial_names
    assert (
        provider.tool_schemas[0]["search_tools"]
        == catalog["search_tools"].json_schema()
    )
    if expected_tool is None:
        assert provider.tool_names[1] == initial_names
        assert not catalog["search_tools"].activated_names
    else:
        assert expected_tool in provider.tool_names[1]
        assert expected_tool in catalog["search_tools"].activated_names
        assert (
            provider.tool_schemas[1][expected_tool]
            == catalog[expected_tool].json_schema()
        )
    assert "search_tools" in provider.initial_system_prompt
    assert "next provider iteration" in provider.initial_system_prompt.casefold()
    assert "concrete tool actions" in provider.initial_system_prompt.casefold()
    assert "Check requested behavior and edge cases" in provider.initial_system_prompt
    await loop.aclose()
