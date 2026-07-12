import io
import json

import pytest

from ash.runtime import build_runtime
from config import AshConfig
from mcp.server import MCPServerConfig
from providers.base import ProviderABC
from ui.headless import HeadlessUI


class RuntimeProvider(ProviderABC):
    @property
    def model_name(self) -> str:
        return "runtime-model"

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        if False:
            yield


def test_runtime_loads_project_mcp_only_when_trusted(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "project-docs": {
                        "transport": "http",
                        "url": "https://mcp.example.com",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    config = AshConfig(
        model="ollama/runtime-model",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
    )

    trusted = build_runtime(
        config,
        HeadlessUI(output_format="text", stream=io.StringIO()),
        provider=RuntimeProvider(),
        workspace_trusted=True,
    )
    untrusted = build_runtime(
        config,
        HeadlessUI(output_format="text", stream=io.StringIO()),
        provider=RuntimeProvider(),
        workspace_trusted=False,
    )

    assert set(trusted.loop._mcp_configs) == {"project-docs"}
    assert untrusted.loop._mcp_configs == {}
    assert {
        "spawn_agent",
        "delegate_agents",
        "search_tools",
        "web_search",
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_back",
    } <= trusted.loop.tools.keys()


def test_runtime_registers_lsp_only_for_trusted_workspace(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    lsp_config = workspace / ".ash" / "lsp.json"
    lsp_config.parent.mkdir(parents=True)
    lsp_config.write_text(
        json.dumps(
            {
                "servers": {
                    "fake": {
                        "command": ["fake-language-server"],
                        "extensions": {".fake": "fake"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    config = AshConfig(
        model="ollama/runtime-model",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
    )

    trusted = build_runtime(
        config,
        HeadlessUI(output_format="text", stream=io.StringIO()),
        provider=RuntimeProvider(),
        workspace_trusted=True,
    )
    untrusted = build_runtime(
        config,
        HeadlessUI(output_format="text", stream=io.StringIO()),
        provider=RuntimeProvider(),
        workspace_trusted=False,
    )

    assert "lsp" in trusted.loop.tools
    assert any(
        middleware.__class__.__name__ == "LSPDiagnosticsMiddleware"
        for middleware in trusted.loop.tool_middlewares
    )
    assert "lsp" not in untrusted.loop.tools


def test_runtime_merges_explicit_mcp_servers_and_rejects_collisions(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "project-docs": {
                        "transport": "http",
                        "url": "https://project.example.com",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = AshConfig(
        model="ollama/runtime-model",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
    )
    external = MCPServerConfig(
        name="editor-docs",
        command="",
        args=[],
        env={},
        transport="http",
        url="https://editor.example.com",
    )

    runtime = build_runtime(
        config,
        HeadlessUI(output_format="text", stream=io.StringIO()),
        provider=RuntimeProvider(),
        workspace_trusted=True,
        additional_mcp_configs={external.name: external},
    )
    assert set(runtime.loop._mcp_configs) == {"project-docs", "editor-docs"}

    collision = MCPServerConfig(
        name="project-docs",
        command="",
        args=[],
        env={},
        transport="http",
        url="https://editor.example.com",
    )
    with pytest.raises(ValueError, match="duplicate MCP server name"):
        build_runtime(
            config,
            HeadlessUI(output_format="text", stream=io.StringIO()),
            provider=RuntimeProvider(),
            workspace_trusted=True,
            additional_mcp_configs={collision.name: collision},
        )


def test_runtime_registers_only_trusted_configured_remote_agents(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_config = home / ".ash" / "a2a.json"
    project_config = workspace / ".ash" / "a2a.json"
    user_config.parent.mkdir(parents=True)
    project_config.parent.mkdir(parents=True)
    user_config.write_text(
        '{"agents":{"review":{"url":"https://review.example.com"}}}',
        encoding="utf-8",
    )
    project_config.write_text(
        '{"agents":{"project":{"url":"https://project.example.com"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    config = AshConfig(
        model="ollama/runtime-model",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
        repo_map_enabled=False,
    )

    untrusted = build_runtime(
        config,
        HeadlessUI(output_format="text", stream=io.StringIO()),
        provider=RuntimeProvider(),
        workspace_trusted=False,
    )
    assert {
        "list_remote_agents",
        "delegate_remote_agent",
    } <= untrusted.loop.tools.keys()
    assert set(untrusted.loop.tools["list_remote_agents"].agents) == {"review"}

    trusted = build_runtime(
        config,
        HeadlessUI(output_format="text", stream=io.StringIO()),
        provider=RuntimeProvider(),
        workspace_trusted=True,
    )
    assert set(trusted.loop.tools["list_remote_agents"].agents) == {
        "review",
        "project",
    }
