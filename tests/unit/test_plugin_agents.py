from __future__ import annotations

from pathlib import Path
import json

import pytest

from ash.agents.shared_state import SharedState
from ash.cli import _build_tools
from ash.plugins.agents import (
    AgentCatalog,
    AgentDefinition,
    AgentSource,
    parse_agent_definition,
)
from ash.providers.base import ProviderABC, StreamChunk
from ash.providers.capabilities import ProviderCapabilities
from ash.safety.guard import SafetyGuard
from ash.tools.agent import SpawnAgentTool


def test_agent_catalog_namespaces_declared_plugin_agents(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    declared = plugin / "custom" / "reviewer.md"
    hidden = plugin / "private" / "hidden.md"
    declared.parent.mkdir(parents=True)
    hidden.parent.mkdir(parents=True)
    declared.write_text(
        "---\ndescription: Review changes\nbase-role: reviewer\n"
        "tools: read_file, search_text\n---\nReview correctness and tests.\n",
        encoding="utf-8",
    )
    hidden.write_text("Do not load.", encoding="utf-8")
    catalog = AgentCatalog((AgentSource(paths=(declared,), namespace="example"),))

    definitions = catalog.discover()

    assert [definition.name for definition in definitions] == ["example:reviewer"]
    assert definitions[0].base_role == "reviewer"
    assert definitions[0].allowed_tools == ("read_file", "search_text")


def test_agent_catalog_isolates_invalid_definitions(tmp_path: Path) -> None:
    root = tmp_path / "agents"
    valid = root / "valid.md"
    invalid = root / "invalid.md"
    root.mkdir()
    valid.write_text("Inspect the task.", encoding="utf-8")
    invalid.write_text("---\nbase-role: admin\n---\nEscalate.", encoding="utf-8")
    catalog = AgentCatalog((root,))

    definitions = catalog.discover()

    assert [definition.name for definition in definitions] == ["valid"]
    assert "base-role" in catalog.errors[str(invalid)]


def test_agent_catalog_rejects_direct_linked_definition(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_text("Inspect the task.", encoding="utf-8")
    linked = tmp_path / "linked.md"
    try:
        linked.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ValueError, match="cannot be a link"):
        parse_agent_definition(linked)


def test_agent_catalog_bounds_recursive_discovery(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "agents"
    root.mkdir()
    for name in ("a.md", "b.md", "c.md"):
        (root / name).write_text(f"Instructions for {name}", encoding="utf-8")
    monkeypatch.setattr("ash.plugins.agents.MAX_AGENT_DISCOVERY_ENTRIES", 2)

    definitions = AgentCatalog((root,)).discover()

    assert len(definitions) <= 2


class CustomAgentProvider(ProviderABC):
    model_name = "fake"
    _ash_declared_capabilities = ProviderCapabilities(native_tools=True)

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        assert any(
            "Review only security-sensitive changes." in message["content"]
            for message in messages
            if isinstance(message.get("content"), str)
        )
        assert tools is not None
        assert {tool["function"]["name"] for tool in tools} == {"read_file"}
        yield StreamChunk(content="custom review complete", is_done=True)

    def count_tokens(self, text: str) -> int:
        return len(text.split())


@pytest.mark.asyncio
async def test_spawn_agent_runs_custom_definition_with_restricted_tools(
    tmp_path: Path,
) -> None:
    definition = AgentDefinition(
        name="example:security",
        description="Security reviewer",
        instructions="Review only security-sensitive changes.",
        path=tmp_path / "security.md",
        base_role="reviewer",
        allowed_tools=("read_file",),
    )
    state = SharedState(tmp_path / "state" / "agents.db")
    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        state,
        CustomAgentProvider,
        custom_agents={definition.name: definition},
    )

    result = await tool.run(role=definition.name, task="inspect changes")

    assert result.success is True
    assert result.output == "custom review complete"
    report = state.fetch_messages("lead", undelivered_only=False)[-1]
    assert report.content["role"] == definition.name
    await tool.aclose()


@pytest.mark.asyncio
async def test_custom_agent_cannot_elevate_beyond_base_role(tmp_path: Path) -> None:
    definition = AgentDefinition(
        name="unsafe",
        description="Invalid elevation",
        instructions="Run commands.",
        path=tmp_path / "unsafe.md",
        base_role="reviewer",
        allowed_tools=("run_command",),
    )
    state = SharedState(tmp_path / "state" / "agents.db")
    tool = SpawnAgentTool(
        SafetyGuard(tmp_path),
        state,
        CustomAgentProvider,
        custom_agents={definition.name: definition},
    )

    result = await tool.run(role=definition.name, task="run a command")

    assert result.success is False
    assert "requests unavailable tools: run_command" in (result.error or "")
    await tool.aclose()


@pytest.mark.asyncio
async def test_default_tools_discover_plugin_agent(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    plugin = home / ".ash" / "plugins" / "example"
    agent = plugin / "agents" / "security.md"
    agent.parent.mkdir(parents=True)
    agent.write_text(
        "---\nbase-role: reviewer\ntools: read_file\n---\n"
        "Review only security-sensitive changes.\n",
        encoding="utf-8",
    )
    (plugin / "plugin.json").write_text(
        json.dumps({"name": "example"}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    tools = _build_tools(
        SafetyGuard(tmp_path),
        tmp_path,
        provider_factory=CustomAgentProvider,
        agent_db_path=tmp_path / "state" / "agents.db",
    )

    result = await tools["spawn_agent"].run(
        role="example:security", task="inspect changes"
    )

    assert result.success is True
    await tools["spawn_agent"].aclose()
