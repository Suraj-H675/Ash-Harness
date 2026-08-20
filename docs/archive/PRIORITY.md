# Ash Project — Priority Improvement Plan

> **Purpose:** This file is the single source of truth for all improvement work on the Ash coding harness. An AI agent reading this file should be able to implement each item completely, start to finish, without needing to ask follow-up questions. Every section is self-contained with exact file locations, exact code changes, expected outputs, and test specifications.
>
> **Inspiration sources:** Claude Code CLI, OpenAI Codex, OpenClaw, Hermes (Brev.dev), Aider, Cline, Cody
>
> **File layout:** HIGH → MEDIUM → LOW. Within each tier, items are ordered by dependency (items that unlock other items come first).

---

## Conventions Used in This File

- **File paths** are relative to `/home/suraj/ash/` unless noted otherwise.
- **Exact signatures** are provided for every function/method that must be created or modified.
- **Expected output** describes what the implementation must produce when run correctly.
- **Tests** specify the exact pytest test file and test names that must pass.
- **Tag format:** `[area: tag]` is used to cross-reference related items.
- Items marked ✅ are already implemented and verified. Items marked 📋 are queued for implementation.

---

## HIGH Priority

---

### ✅ H-1: Fix Subagent Concurrency Bug

**Status:** IMPLEMENTED — all tests passing (27 passed)

See prior implementation in `ash/agents/orchestrator.py` `_run_agents` method. Uses `async with semaphore` inside `_run_one` instead of `await semaphore.acquire()` in the outer loop.

---

### ✅ H-2: Make SharedState Public Methods Async-Safe for Async Callers

**Status:** IMPLEMENTED — all tests passing (4 passed)

See `ash/agents/shared_state.py`. Adds `_async_lock = asyncio.Lock()` and async wrappers: `update_status_async`, `send_message_async`, `register_agent_async`, `update_sprint_state_async`, `create_sprint_async`.

---

### ✅ H-3: Wire `auto_commit` Tool into Default Tool Set

**Status:** IMPLEMENTED — tests passing

See `ash/__main__.py` `_build_tools()` function. `AutoCommitTool(safety_guard)` is registered in the returned dict.

---

### ✅ H-4: Add Tool Middleware Chain to AshLoop

**Status:** IMPLEMENTED — all tests passing (5 passed)

See `ash/tools/base.py` (`ToolMiddleware` ABC, `ToolMiddlewareSkip`) and `ash/core/loop.py` (`tool_middlewares` param, `_apply_middlewares_before`, `_apply_middlewares_after`).

---

### ✅ H-5: Enforce Tool Allowlist Inside `SubprocessAgent.run_in_process`

**Status:** IMPLEMENTED — all tests passing (4 passed)

See `ash/agents/subprocess_agent.py` (`enforcement_guard` param, `is_tool_allowed()`) and `ash/agents/orchestrator.py` (`_run_agents` passes `enforcement_guard=lambda tn: tn in spec.tool_allowlist`).

---

### ✅ H-6: Add `PreToolUse` Hook Callback to AshLoop

**Status:** IMPLEMENTED — all tests passing (5 passed)

See `ash/core/loop.py` (`on_tool_approval: ToolApprovalCallback | None` param, conditional approval in `_execute_tool_calls`).

---

### ✅ H-7: Add Missing `__init__.py` Files

**Status:** IMPLEMENTED — all tests passing (3 passed)

See `ash/tools/__init__.py`, `ash/providers/__init__.py`, `ash/context/__init__.py`.

---

### ✅ H-8: Add Integration Tests for Critical Modules

**Status:** IMPLEMENTED — 3/5 test suites passing; 2 blocked by `rich.group` (fixed)

Test files exist for `test_planner.py`, `test_repo_parser.py`, `test_compaction.py`, `test_rate_limiter.py`, `test_terminal_ui.py`. All suite tests pass except those requiring `TerminalUI` import.

---

### ✅ H-9: Architect/Editor Dual-Model Mode for Subagents

**Status:** IMPLEMENTED — tests passing

See `ash/agents/orchestrator.py` (`mode` field in `SubagentSpec`, `use_architect_mode`/`planner`/`project_root` params in `fanout_for_goal`, `make_architect_task`).

---

### ✅ H-10: Subagent Result Consolidation Step

**Status:** IMPLEMENTED — tests passing

See `ash/agents/orchestrator.py` (`consolidated_report` field in `OrchestratorResult`, `consolidate_results` method called in `run_batch`).

---

### ✅ H-11: Cross-Session Memory Recall

**Status:** IMPLEMENTED — tests passing

See `ash/core/session.py` (`get_recent_session_summaries`) and `ash/core/loop.py` (`enable_memory_recall` param, `_build_memory_context`).

---

### ✅ H-12: Wire Vector Memory Pipeline into AshLoop

**Status:** IMPLEMENTED — all tests passing (3 new tests in `test_memory_pipeline.py`)

**Area:** `memory`
**Inspiration:** Hermes (semantic memory recall)
**Files affected:** `ash/core/loop.py`, `ash/config.py`, `ash/memory/__init__.py`

#### Why This Matters

`ash/memory/vector.py` contains a fully implemented `VectorSearchPipeline` with ChromaDB, ONNX (local MiniLM), and OpenAI embedding backends, plus FTS5 fallback. `ash/memory/markdown_store.py` has `MarkdownMemoryStore`. `ash/memory/fts5.py` has FTS5 full-text search. None of these are wired into `AshLoop` — the semantic memory pipeline exists but is unreachable from the main loop.

#### How to Fix

**1. Export memory modules in `ash/memory/__init__.py`:**

```python
"""Long-term memory and search indices for Ash."""

from ash.memory.vector import (
    VectorSearchPipeline,
    VectorHit,
    InMemoryVectorIndex,
    ChromaIndex,
    EmbeddingAdapter,
    DeterministicEmbedding,
    ONNXLocalEmbedding,
    OpenAIEmbedding,
    EmbeddingBackendUnavailable,
    VectorBackendUnavailable,
)
from ash.memory.markdown_store import MarkdownMemoryStore
from ash.memory.fts5 import FTS5Index

__all__ = [
    "VectorSearchPipeline",
    "VectorHit",
    "InMemoryVectorIndex",
    "ChromaIndex",
    "EmbeddingAdapter",
    "DeterministicEmbedding",
    "ONNXLocalEmbedding",
    "OpenAIEmbedding",
    "EmbeddingBackendUnavailable",
    "VectorBackendUnavailable",
    "MarkdownMemoryStore",
    "FTS5Index",
]
```

**2. Add vector memory config to `ash/config.py`:**

```python
memory_backend: str = Field(
    "auto",
    description="Memory backend: 'auto' (in-memory), 'chroma' (persistent), or 'fts5' (lexical-only)",
)
chroma_persist_dir: Path = Field(
    Path(".ash/chroma"),
    description="Directory for ChromaDB persistent storage",
)
embedding_provider: str = Field(
    "auto",
    description="Embedding provider: 'auto' (deterministic), 'onnx' (local), 'openai' (remote)",
)
openai_api_key: str = Field("", description="API key for OpenAI embeddings")
onnx_model_path: Path = Field(
    Path(".ash/model.onnx"),
    description="Path to ONNX MiniLM model for local embeddings",
)
```

**3. Wire `VectorSearchPipeline` into `AshLoop` in `ash/core/loop.py`:**

```python
from ash.memory import VectorSearchPipeline, DeterministicEmbedding, InMemoryVectorIndex

def __init__(
    self,
    ...
    enable_semantic_memory: bool = False,
    memory_backend: str = "auto",
    embedding_provider: str = "auto",
    openai_api_key: str = "",
    onnx_model_path: Path | None = None,
    chroma_persist_dir: Path | None = None,
) -> None:
    ...
    self.enable_semantic_memory = enable_semantic_memory
    self._vector_pipeline: VectorSearchPipeline | None = None
    if enable_semantic_memory:
        adapter: EmbeddingAdapter
        if embedding_provider == "onnx":
            from ash.memory import ONNXLocalEmbedding
            adapter = ONNXLocalEmbedding(model_path=onnx_model_path)
        elif embedding_provider == "openai":
            from ash.memory import OpenAIEmbedding
            adapter = OpenAIEmbedding(api_key=openai_api_key)
        else:
            adapter = DeterministicEmbedding()

        vector_index = InMemoryVectorIndex()
        self._vector_pipeline = VectorSearchPipeline(
            adapter=adapter,
            vector_index=vector_index,
        )
```

**4. Add semantic memory index and search to `ash/core/loop.py`:**

```python
async def index_file_for_memory(self, file_path: Path) -> None:
    """Index a file into the semantic memory pipeline."""
    if self._vector_pipeline is None:
        return
    from ash.context.compaction import Chunk
    chunks = self._chunk_file(file_path)  # implement _chunk_file to split code into chunks
    await self._vector_pipeline.index_chunks(chunks, str(file_path))

async def semantic_search(self, query: str, top_k: int = 5) -> list[VectorHit]:
    """Search semantic memory for relevant context."""
    if self._vector_pipeline is None:
        return []
    hits, source = await self._vector_pipeline.search(query, top_k=top_k)
    return hits

def _chunk_file(self, file_path: Path) -> list[Chunk]:
    """Split a file into memory-indexable chunks."""
    from ash.context.compaction import Chunk
    content = file_path.read_text(errors="replace")
    lines = content.splitlines()
    chunks: list[Chunk] = []
    for i in range(0, len(lines), 50):
        chunk_lines = lines[i : i + 50]
        chunks.append(
            Chunk(
                file_path=str(file_path),
                start=i + 1,
                end=i + len(chunk_lines),
                content="\n".join(chunk_lines),
            )
        )
    return chunks
```

**5. Integrate semantic memory into the turn loop — inject relevant context before provider call:**

In `run_turn`, before building the messages list for the provider, optionally search semantic memory:

```python
# In run_turn, before the provider call:
if self.enable_semantic_memory and self._vector_pipeline is not None:
    memory_hits = await self.semantic_search(user_input, top_k=3)
    if memory_hits:
        memory_context = "\n\n".join(
            f"// From {hit.file_path}:\n{hit.content[:500]}"
            for hit in memory_hits
        )
        system_prompt = f"{system_prompt}\n\n## Relevant Context\n{memory_context}"
```

#### Expected Output

- `enable_semantic_memory=True` in config activates vector memory
- Files can be indexed via `index_file_for_memory()`
- Relevant context is retrieved via `semantic_search()` and injected into the system prompt
- Works offline with `DeterministicEmbedding` or online with `ONNXLocalEmbedding` or `OpenAIEmbedding`

#### Tests

```python
# tests/unit/test_memory_pipeline.py
import pytest
from ash.memory import VectorSearchPipeline, InMemoryVectorIndex, DeterministicEmbedding
from ash.context.compaction import Chunk

@pytest.mark.asyncio
async def test_vector_pipeline_indexes_and_searches():
    adapter = DeterministicEmbedding()
    index = InMemoryVectorIndex()
    pipeline = VectorSearchPipeline(adapter=adapter, vector_index=index)

    chunks = [
        Chunk(file_path="src/auth.py", start=1, end=10, content="def login():\n    pass"),
        Chunk(file_path="src/main.py", start=1, end=10, content="def app():\n    pass"),
    ]
    await pipeline.index_chunks(chunks, "src/auth.py")
    hits, source = await pipeline.search("authentication", top_k=1)
    assert len(hits) >= 1
    assert source == "vector"

def test_deterministic_embedding_produces_stable_vectors():
    adapter = DeterministicEmbedding()
    import asyncio
    vec1 = asyncio.run(adapter.get_embedding("hello world"))
    vec2 = asyncio.run(adapter.get_embedding("hello world"))
    assert vec1 == vec2
```

---

### ✅ H-13: MCP Server Lifecycle Management (Start/Stop Servers)

**Status:** IMPLEMENTED — all tests passing (2 new tests in `test_mcp.py`)

**Area:** `mcp_integration`
**Inspiration:** Claude Code CLI (MCP servers managed as long-running processes)
**Files affected:** `ash/mcp/server.py`, `ash/mcp/client.py`, `ash/core/loop.py`

#### Why This Matters

`load_mcp_servers()` already reads `.mcp.json` and creates `MCPServerConfig` objects. But these configs are never turned into running servers. MCP servers need to be spawned as subprocesses, their stdout/stderr pipes managed, and their lifecycle tied to the AshLoop session.

#### How to Fix

**File:** `ash/mcp/server.py` — add server lifecycle management:

```python
import asyncio
import subprocess
from dataclasses import dataclass, field
from typing import Any

@dataclass
class MCPServerInstance:
    """A running MCP server subprocess."""
    name: str
    config: MCPServerConfig
    process: subprocess.Popen[bytes]
    transport: str = "stdio"

class MCPServerManager:
    """Manages the lifecycle of MCP server subprocesses."""

    def __init__(self) -> None:
        self._servers: dict[str, MCPServerInstance] = {}

    def start_server(self, config: MCPServerConfig) -> MCPServerInstance:
        """Start an MCP server as a subprocess."""
        env = {**os.environ, **config.env}
        transport = getattr(config, "transport", "stdio")

        if transport == "stdio":
            proc = subprocess.Popen(
                [config.command] + config.args,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        elif transport in ("sse", "http", "websocket"):
            # For network transports, the server is already running;
            # just store the config for the client to connect to.
            proc = None  # type: ignore[assignment]
        else:
            raise ValueError(f"Unknown MCP transport: {transport}")

        instance = MCPServerInstance(
            name=config.name,
            config=config,
            process=proc,  # type: ignore[arg-type]
            transport=transport,
        )
        self._servers[config.name] = instance
        return instance

    def stop_server(self, name: str) -> None:
        """Stop a running MCP server."""
        instance = self._servers.get(name)
        if instance is None:
            return
        if instance.process is not None and instance.process.poll() is None:
            instance.process.terminate()
            try:
                instance.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                instance.process.kill()
        del self._servers[name]

    def stop_all(self) -> None:
        """Stop all running MCP servers."""
        for name in list(self._servers.keys()):
            self.stop_server(name)

    def get_server(self, name: str) -> MCPServerInstance | None:
        return self._servers.get(name)

    def list_servers(self) -> list[MCPServerInstance]:
        return list(self._servers.values())
```

**2. Wire into `AshLoop.__init__` and `__del__` / context manager:**

In `ash/core/loop.py`:

```python
from ash.mcp.server import MCPServerManager, load_mcp_servers

def __init__(
    self,
    ...
    mcp_config_path: Path | None = None,  # path to .mcp.json
) -> None:
    ...
    self._mcp_manager: MCPServerManager | None = None
    if mcp_config_path is not None and mcp_config_path.exists():
        self._mcp_manager = MCPServerManager()
        servers = load_mcp_servers(mcp_config_path)
        for name, config in servers.items():
            try:
                self._mcp_manager.start_server(config)
            except Exception as exc:
                logger.warning(f"Failed to start MCP server {name}: {exc}")

def __del__(self) -> None:
    if self._mcp_manager is not None:
        self._mcp_manager.stop_all()

async def __aenter__(self) -> "AshLoop":
    return self

async def __aexit__(self, *args: Any) -> None:
    if self._mcp_manager is not None:
        self._mcp_manager.stop_all()
```

#### Expected Output

- When `mcp_config_path` is set in config/constructor, MCP servers are started as subprocesses at loop init
- All servers are stopped when the loop exits (via `__aexit__` or `__del__`)
- `MCPServerManager` provides start/stop/list operations

#### Tests

```python
# tests/unit/test_mcp.py — add these tests alongside existing M-1 tests
import pytest
import tempfile
from pathlib import Path
from ash.mcp.server import MCPServerManager, MCPServerConfig

def test_manager_starts_and_stops_server(tmp_path):
    manager = MCPServerManager()
    config = MCPServerConfig(
        name="test-server",
        command="sleep",
        args=["10"],
        env={},
    )
    instance = manager.start_server(config)
    assert instance.name == "test-server"
    assert instance.process.poll() is None  # still running

    manager.stop_server("test-server")
    assert manager.get_server("test-server") is None

def test_manager_stop_all(tmp_path):
    manager = MCPServerManager()
    for i in range(3):
        manager.start_server(MCPServerConfig(
            name=f"server-{i}",
            command="sleep",
            args=["100"],
            env={},
        ))
    manager.stop_all()
    assert len(manager.list_servers()) == 0
```

---

## MEDIUM Priority

---

### ✅ M-1: Implement `.mcp.json` MCP Server Configuration

**Status:** IMPLEMENTED — tests passing (2 passed)

See `ash/mcp/server.py` (`MCPServerConfig`, `load_mcp_servers`, `expand_env_vars`).

---

### ✅ M-2: Hook System with Matcher-Based Registration

**Status:** IMPLEMENTED — tests passing (4 passed)

See `ash/hooks/registry.py` (`Hook`, `PreToolUseHook`, `PostToolUseHook`, `SessionStartHook`, `HookRegistry`, `get_injected_prompt`).

---

### ✅ M-3: Add `SessionStart` Hook for System Prompt Injection

**Status:** IMPLEMENTED — tests passing (1 passed)

See `ash/core/loop.py` (`hooks: HookRegistry` param, `hooks.fire_session_start()` in `start_session`).

---

### ✅ M-4: Add `PostToolUse` Hook for Logging/Metrics

**Status:** IMPLEMENTED — tests passing (1 passed)

See `ash/core/loop.py` (`hooks.fire_post_tool()` after tool execution).

---

### ✅ M-5: Plugin Manifest Schema (`plugin.json`)

**Status:** IMPLEMENTED — tests passing (2 passed)

See `ash/plugins/manifest.py` (`PluginManifest` with `from_dict`, `load`, all fields).

---

### ✅ M-6: In-Memory Context Object for Turn-Level State Sharing

**Status:** IMPLEMENTED — tests passing (3 passed)

See `ash/context/turn.py` (`TurnContext`) and `ash/core/loop.py` (`turn_context` param).

---

### ✅ M-7: Per-Agent Sandbox Modes

**Status:** IMPLEMENTED — tests passing (2 passed)

See `ash/agents/subprocess_agent.py` (`sandbox_tier` param) and `ash/agents/orchestrator.py` (`ROLE_SANDBOX_TIER` dict).

---

### ✅ M-8: Agent-to-Agent IPC Messages

**Status:** IMPLEMENTED — tests passing (2 passed)

See `ash/agents/shared_state.py` (`send_to_agent`, `broadcast`).

---

### ⚠️ Historical M-9: Retry with Exponential Backoff for Transient Tool Failures

**Superseded:** This historical experiment was removed. Ash now executes every
dispatched tool call exactly once by default; retrying a tool after a lost
result can duplicate an external side effect. Provider pre-output retries are
separate and remain bounded.

See `ash/core/loop.py` (`_execute_tool_once`).

---

### ✅ M-10: Recovery Suggestions When Circuit Breaker Trips

**Status:** IMPLEMENTED — tests passing (3 passed)

See `ash/core/recovery.py` (`CircuitBreakerError`, `SUGGESTIONS`, `suggest_alternatives`).

---

### ✅ M-11: Implement OpenAI Provider

**Status:** IMPLEMENTED — code complete (live test needs API key)

See `ash/providers/openai.py` and `ash/__main__.py` `provider="openai"` wiring.

---

### ✅ M-12: Implement Ollama Provider for Local Models

**Status:** IMPLEMENTED — tests passing (1 passed)

See `ash/providers/ollama.py` and `ash/__main__.py` `provider="ollama"` wiring.

---

### ✅ M-13: Context Filters for RepoMap

**Status:** IMPLEMENTED — tests passing (1 passed)

See `ash/repo/repomap.py` (`exclude_patterns`, `_is_excluded`) and `ash/config.py`.

---

### ✅ M-14: Memory Nudge Intervals

**Status:** IMPLEMENTED — code complete, nudge placed inside turn loop

See `ash/core/loop.py` (`memory_nudge_interval`, `_turns_since_nudge`, `_build_memory_nudge`).

---

### ✅ M-15: Skill Nudge Intervals

**Status:** IMPLEMENTED — code complete

See `ash/core/loop.py` (`tools_registry`, `skill_nudge_interval`, `_iterations_since_skill_use`, `_build_skill_nudge`).

---

### ✅ M-16: Whole-Edit Format for Code Modifications

**Status:** IMPLEMENTED — tests passing (1 passed)

See `ash/tools/filesystem.py` (`WholeEditTool`, `WholeEditArgs`).

---

### ✅ M-17: Implement `check_dependencies()` in PluginManifest

**Status:** IMPLEMENTED — all tests passing (4 tests in `test_plugin_manifest.py`)

**Area:** `plugin_system`
**Inspiration:** Claude Code CLI
**Files affected:** `ash/plugins/manifest.py`

#### Why This Matters

`PluginManifest.check_dependencies()` currently only has a docstring — no actual validation. Plugins declaring dependencies can't be validated.

#### How to Fix

**File:** `ash/plugins/manifest.py`

Replace the empty stub with actual validation:

```python
def check_dependencies(self) -> list[str]:
    """Validate all declared dependencies are installed.

    Returns a list of error messages for unmet dependencies.
    Returns an empty list if all dependencies are satisfied.
    """
    errors: list[str] = []
    for dep in self.dependencies:
        name = dep.get("name", "")
        version_spec = dep.get("version", "")
        if not name:
            continue
        # Try to import the plugin package
        try:
            import importlib
            spec = importlib.util.find_spec(name)
            if spec is None:
                errors.append(f"Missing dependency: {name} ({version_spec})")
                continue
            # If version spec is given (e.g. ">=1.0.0"), check it
            if version_spec:
                try:
                    import packaging.version
                    installed = importlib.metadata.version(name)
                    if not packaging.version.satisfied(
                        packaging.version.InvalidSpecifier(),
                        version_spec,
                    ):
                        errors.append(
                            f"{name} {installed} does not satisfy {version_spec}"
                        )
                except Exception:
                    # If version checking fails, skip
                    pass
        except Exception:
            errors.append(f"Missing dependency: {name} ({version_spec})")
    return errors
```

Also add the needed imports at the top:

```python
import importlib
import importlib.metadata
import importlib.util
from packaging.version import InvalidSpecifier, Version, parse
from packaging.specifiers import SpecifierSet
```

#### Expected Output

- `manifest.check_dependencies()` returns `[]` when all dependencies are installed
- Returns a list of error strings for each missing or version-mismatched dependency

#### Tests

```python
# tests/unit/test_plugin_manifest.py — add

def test_check_dependencies_returns_empty_for_installed_deps(tmp_path):
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(json.dumps({
        "name": "my-plugin",
        "version": "1.0.0",
        "dependencies": [{"name": "pytest"}],
    }))
    manifest = PluginManifest.load(manifest_file)
    errors = manifest.check_dependencies()
    assert errors == []  # pytest is installed

def test_check_dependencies_returns_error_for_missing_dep(tmp_path):
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(json.dumps({
        "name": "my-plugin",
        "version": "1.0.0",
        "dependencies": [{"name": "nonexistent-package-xyz"}],
    }))
    manifest = PluginManifest.load(manifest_file)
    errors = manifest.check_dependencies()
    assert len(errors) == 1
    assert "nonexistent-package-xyz" in errors[0]
```

---

## LOW Priority

---

### ✅ L-1: ClawHub-Style Remote Skill Registry

**Status:** IMPLEMENTED — code complete

See `ash/plugins/registry.py` (`RemoteSkill`, `search_registry`).

---

### ✅ L-2: Environment Variable Expansion in Plugin Configs

**Status:** IMPLEMENTED — code complete

See `ash/mcp/server.py` (`expand_env_vars`, `MCPServerConfig.__post_init__`).

---

### ✅ L-3: Plugin Dependency Management

**Status:** PARTIALLY IMPLEMENTED — `dependencies` field and `check_dependencies()` stub exist; `check_dependencies()` needs implementation (see M-17 above).

---

### ✅ L-4: MCP CLI Management Commands

**Status:** IMPLEMENTED — code complete

See `ash/__main__.py` (`subparsers`, `mcp` subparser, `args.command == "mcp"` handling).

---

### ✅ L-5: SSE and WebSocket MCP Transport Support

**Status:** IMPLEMENTED — code complete

See `ash/mcp/client.py` (`connect_sse`, `connect_websocket`, `connect_http`, `connect_stdio`).

---

### ✅ L-6: RepoMap Dependency Graph Visualization

**Status:** IMPLEMENTED — code complete

See `ash/repo/repomap.py` (`to_dot_graph`).

---

### ✅ L-7: Context Window Usage Indicator

**Status:** IMPLEMENTED — tests passing (3 passed)

See `ash/ui/terminal.py` (`show_token_meter`, `_render_token_meter`, `Columns`-based rendering).

---

### ✅ L-8: JSON-Configurable Dynamic Agent Spawning

**Status:** IMPLEMENTED — code complete

See `ash/agents/orchestrator.py` (`SubagentSpec.from_dict` with `mode` and `sandbox_tier`, `run_batch_from_config`).

---

### ✅ L-9: Agent Workspace Isolation

**Status:** IMPLEMENTED — code complete

See `ash/agents/orchestrator.py` (`workspace_root` in `SubagentSpec`) and `ash/agents/subprocess_agent.py` (`ASH_WORKSPACE_ROOT` env var).

---

### ✅ L-10: Agent-Created Self-Improving Skills

**Status:** IMPLEMENTED — code complete

See `ash/tools/skill_writer.py` (`SkillWriter`, `on_agent_success`) and `ash/tools/skills.py` (`write_python_skill`).

---

### ✅ L-11: Memory as Markdown Files

**Status:** IMPLEMENTED — code complete

See `ash/memory/markdown_store.py` (`MarkdownMemoryStore`).

---

### ✅ L-12: Continuous Autonomous Agent Mode

**Status:** IMPLEMENTED — code complete

See `ash/core/loop.py` (`continuous_mode`, `max_continuous_turns`, continuous mode follow-up).

---

### ✅ L-13: Streamable HTTP Extension for Remote Control

**Status:** IMPLEMENTED — code complete

See `ash/server/http.py` (FastAPI app with `/turn` endpoint, `app.state.ash_loop` wired).

---

### ✅ L-14: JSON-RPC Client/Server Protocol

**Status:** IMPLEMENTED — code complete

See `ash/server/jsonrpc.py` (`JSONRPCServer`, `handle_request`).

---

### L-15: Managed LSP Integration

**Status:** IMPLEMENTED - supersedes the original diagnostic-emitter sketch

See `lsp/config.py`, `lsp/client.py`, `lsp/manager.py`, `lsp/middleware.py`,
`tools/lsp.py`, and `cli/lsp.py`.

---

### ✅ L-16: Autonomous `--auto-approve` Mode

**Status:** IMPLEMENTED — code complete

See `ash/config.py` (`safety_tier`) and `ash/core/loop.py` (conditional approval based on `safety_tier`).

---

### ✅ L-17: Agent Teams and Spawn Commands

**Status:** IMPLEMENTED — code complete (fixed args_schema)

See `ash/tools/agent.py` (`SpawnAgentTool`, `SpawnAgentArgs`, properly typed `args_schema`).

---

## Pre-Existing Bugs (Not From PRIORITY.md)

These are bugs discovered during verification that are NOT part of any priority item — they exist in pre-existing code:

| Bug | File | Issue | Severity |
|-----|------|-------|----------|
| `RunCommandTool` cwd | `ash/tools/command.py` | `test_run_command_defaults_to_project_root_cwd` fails — cwd not set correctly | Medium |
| `rich.group` removed | `ash/ui/terminal.py` | Was importing non-existent `rich.group.Group` — fixed to `Columns` | Fixed |
| `SpawnAgentTool` args_schema | `ash/tools/agent.py` | Invalid tuple syntax `(str, ...)` — fixed to use class directly | Fixed |

---

## Implementation Order

```
Phase 1 (prerequisites):
  H-7  → Add __init__.py files (required for all later imports to work)
  H-8  → Add missing test files (build confidence as you go)

Phase 2 (core architecture fixes):
  H-1  → Fix concurrency bug (unblocks parallel subagent promise)
  H-2  → Replace threading.Lock with asyncio.Lock (prerequisite for H-1 stability)

Phase 3 (tool system):
  H-3  → Wire auto_commit into default tools
  H-4  → Add tool middleware chain
  H-6  → Add PreToolUse hook callback
  M-2  → Hook system with matcher registration
  M-3  → SessionStart hook
  M-4  → PostToolUse hook
  M-16 → Whole-edit format

Phase 4 (safety):
  H-5  → Enforce tool allowlist inside SubprocessAgent
  H-6  → PreToolUse hook (already listed above)

Phase 5 (subagent orchestration):
  H-9  → Architect/Editor dual-mode
  H-10 → Result consolidation
  M-7  → Per-agent sandbox modes
  M-8  → Agent-to-agent IPC
  L-8  → JSON-configurable agent spawning
  L-9  → Agent workspace isolation
  L-17 → Agent teams and spawn commands

Phase 6 (providers):
  M-11 → OpenAI provider
  M-12 → Ollama provider

Phase 7 (MCP integration):
  M-1  → .mcp.json support
  L-4  → MCP CLI management commands
  L-5  → SSE/WebSocket transport
  H-13 → MCP server lifecycle management  ← NEW

Phase 8 (memory & context):
  H-11 → Cross-session memory recall
  M-13 → Context filters for RepoMap
  M-14 → Memory nudge intervals
  M-15 → Skill nudge intervals
  L-10 → Agent-created self-improving skills
  L-11 → Memory as Markdown files
  H-12 → Wire vector memory pipeline into AshLoop  ← NEW

Phase 9 (error recovery):
  M-9  → Retry with exponential backoff
  M-10 → Recovery suggestions on circuit breaker trip
  M-17 → Implement check_dependencies()  ← NEW
  L-16 → Autonomous --auto-approve mode

Phase 10 (UI/UX):
  M-6  → In-memory TurnContext
  L-7  → Context window usage indicator
  L-6  → RepoMap dependency graph visualization

Phase 11 (server/multi-agent):
  L-13 → Streamable HTTP extension
  L-14 → JSON-RPC protocol
  L-15 → LSP diagnostics
  L-12 → Continuous autonomous mode

Phase 12 (ecosystem):
  M-5  → Plugin manifest schema
  L-1  → Remote skill registry
  L-2  → Environment variable expansion
  L-3  → Plugin dependency management
```

---

## Quick Reference: File → Priority Item Map

| File | Priority Items |
|------|---------------|
| `ash/agents/orchestrator.py` | H-1, H-9, H-10, M-7, M-8 |
| `ash/agents/subprocess_agent.py` | H-1, H-5, M-7 |
| `ash/agents/shared_state.py` | H-2, M-8 |
| `ash/core/loop.py` | H-4, H-6, H-11, H-12🆕, M-2, M-3, M-4, M-6, M-9, M-10, M-14, M-15 |
| `ash/core/session.py` | H-2, H-11 |
| `ash/core/recovery.py` | M-10 |
| `ash/core/planner.py` | H-9 |
| `ash/tools/base.py` | H-4 |
| `ash/tools/registry.py` | M-15 |
| `ash/tools/git.py` | H-3 |
| `ash/tools/filesystem.py` | M-16 |
| `ash/providers/base.py` | M-11, M-12 |
| `ash/providers/anthropic.py` | — |
| `ash/providers/rate_limiter.py` | H-8 (test only) |
| `ash/safety/guard.py` | H-5 |
| `ash/repo/repomap.py` | M-13 |
| `ash/repo/parser.py` | H-8 (test only) |
| `ash/context/compaction.py` | H-8 (test only) |
| `ash/context/tokens.py` | — |
| `ash/context/turn.py` | M-6 (new file) |
| `ash/ui/terminal.py` | H-8 (test only) |
| `ash/__main__.py` | H-3, M-11, M-12 |
| `ash/mcp/server.py` | M-1, L-2, H-13🆕 |
| `ash/mcp/client.py` | L-5 |
| `ash/hooks/registry.py` | M-2, M-3, M-4 (new file) |
| `ash/plugins/manifest.py` | M-5, M-17🆕 |
| `ash/plugins/registry.py` | L-1 |
| `ash/server/http.py` | L-13 |
| `ash/server/jsonrpc.py` | L-14 |
| `lsp/config.py`, `lsp/client.py`, `lsp/manager.py`, `lsp/middleware.py` | L-15 |
| `tools/lsp.py`, `cli/lsp.py` | L-15 |
| `ash/memory/__init__.py` | H-12🆕 |
| `ash/memory/vector.py` | H-12🆕 |
| `ash/memory/markdown_store.py` | L-11 |
| `ash/memory/fts5.py` | — |
| `ash/providers/openai.py` | M-11 (new file) |
| `ash/providers/ollama.py` | M-12 (new file) |
| `ash/tools/agent.py` | L-17 |
| `ash/tools/skill_writer.py` | L-10 |
| `tests/unit/test_subprocess_agent.py` | H-5, H-8, M-7, M-8 |
| `tests/integration/test_subagents.py` | H-1, H-9, H-10 |
| `tests/unit/test_loop.py` | H-4, H-6, M-9 |
| `tests/unit/test_shared_state.py` | H-2, M-8 |
| `tests/unit/test_session.py` | H-11 |
| `tests/unit/test_recovery.py` | M-10 |
| `tests/integration/test_planner.py` | H-8 |
| `tests/unit/test_repo_parser.py` | H-8 |
| `tests/unit/test_compaction.py` | H-8 |
| `tests/unit/test_rate_limiter.py` | H-8 |
| `tests/unit/test_terminal_ui.py` | H-8 |
| `tests/unit/test_tools.py` | H-3, M-16 |
| `tests/unit/test_hooks.py` | M-2, M-3, M-4 |
| `tests/unit/test_mcp.py` | M-1, H-13🆕 |
| `tests/unit/test_plugin_manifest.py` | M-5, M-17🆕 |
| `tests/unit/test_turn_context.py` | M-6 |
| `tests/unit/test_providers.py` | M-11, M-12 |
| `tests/unit/test_repomap.py` | M-13 |
| `tests/unit/test_imports.py` | H-7 |
| `tests/unit/test_memory_pipeline.py` | H-12🆕 |
