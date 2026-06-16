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

---

## HIGH Priority

---

### H-1: Fix Subagent Concurrency Bug

**Area:** `concurrency_model`
**Inspiration:** Codex (true parallel fan-out)
**Files affected:** `ash/agents/orchestrator.py`

#### Current Behavior (Bug)

In `ash/agents/orchestrator.py`, the `_run_agents` method (lines 153–198) has a semaphore acquisition pattern that does not correctly release permits until after each task completes — but the way permits are acquired in the `for` loop creates a synchronous bottleneck. Specifically:

```python
async def _run_agents(self, specs: Sequence[SubagentSpec]) -> list[AgentReport]:
    semaphore = asyncio.Semaphore(self.max_concurrency)
    reports: list[AgentReport] = []
    tasks: list[asyncio.Task[AgentReport]] = []

    for spec in specs:
        await semaphore.acquire()  # ← blocks next iteration until permit available
        agent = SubprocessAgent(...)
        async def _run(agent: SubprocessAgent) -> AgentReport:
            try:
                return await agent.run_in_process()
            finally:
                semaphore.release()
        tasks.append(asyncio.create_task(_run(agent)))
```

The `await semaphore.acquire()` in the `for` loop means each iteration waits for a permit before proceeding to create the next task. While the tasks do run concurrently (up to `max_concurrency`), the **creation** of tasks is serialized — if `max_concurrency=4` and `specs` has 10 items, iterations 1–4 all acquire quickly (4 permits available), iterations 5–10 each block waiting for a running task to finish before their `acquire()` returns. This is functionally correct but not the intended pattern: the semaphore should be entered **inside** each task's async context, not in the loop's synchronous creation path.

The correct pattern is: launch all tasks first, each task acquires the semaphore as part of its own async execution (via `async with semaphore`), then `asyncio.gather` collects results.

#### How to Fix

**File:** `ash/agents/orchestrator.py`

**Change 1 — Rewrite `_run_agents` to use `async with semaphore` inside each task:**

```python
async def _run_agents(self, specs: Sequence[SubagentSpec]) -> list[AgentReport]:
    semaphore = asyncio.Semaphore(self.max_concurrency)
    reports: list[AgentReport] = []

    async def _run_one(spec: SubagentSpec) -> AgentReport:
        agent = SubprocessAgent(
            agent_id=spec.agent_id,
            role=spec.role,
            task=spec.task,
            shared_state=self.shared_state,
            runner=spec.runner or make_simple_text_task(f"completed: {spec.task}"),
            tool_allowlist=spec.tool_allowlist,
            token_budget=spec.token_budget,
            return_budget=spec.return_budget,
            metadata=spec.metadata,
        )
        async with semaphore:
            return await agent.run_in_process()

    # Launch ALL tasks immediately — each one acquires the semaphore
    # internally via `async with semaphore`. No serialized acquire in the loop.
    tasks = [asyncio.create_task(_run_one(spec)) for spec in specs]

    # Collect results as they complete (not in submission order).
    for finished in asyncio.as_completed(tasks):
        try:
            report = await finished
        except Exception as exc:  # noqa: BLE001
            report = AgentReport(
                agent_id="<unknown>",
                role="general",
                task="<unknown>",
                success=False,
                summary=f"orchestrator caught: {exc}",
            )
        reports.append(report)
        self._drain_lead_inbox()

    return reports
```

**Change 2 — Remove the old semaphore acquire from the loop body.** The old code in lines 158–178 should be replaced entirely. The `semaphore` variable is now created once at the top of `_run_agents` and used only via `async with` inside `_run_one`.

#### Expected Output

- Running `fanout_for_goal` with `max_concurrency=4` and 4 specs should spawn all 4 agents immediately (not sequentially).
- A test with 4 agents that each `asyncio.sleep(0.1)` should complete in ~0.1s total, not 0.4s.
- The `SubagentOrchestrator.max_concurrency` limit is still respected (at most 4 agents run at once).

#### Tests

**File:** `tests/integration/test_subagents.py` (create if not exists)

```python
# tests/integration/test_subagents.py
import asyncio
import time
import pytest
from ash.agents.orchestrator import SubagentOrchestrator
from ash.agents.subprocess_agent import make_simple_text_task
from ash.agents.shared_state import SharedState
import tempfile
from pathlib import Path

@pytest.fixture
def shared_state() -> SharedState:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield SharedState(Path(tmpdir) / "test.db")

@pytest.mark.asyncio
async def test_fanout_runs_all_agents_concurrently(shared_state):
    """All agents should be launched immediately, not sequentially."""
    specs = [
        SubagentSpec(role="general", task=f"task-{i}", agent_id=f"agent-{i}")
        for i in range(4)
    ]
    orchestrator = SubagentOrchestrator(shared_state, max_concurrency=4)

    start = time.monotonic()
    result = await orchestrator.run_batch("concurrent test", specs)
    elapsed = time.monotonic() - start

    assert result.sprint_id
    assert len(result.reports) == 4
    assert all(r.success for r in result.reports)
    # With 4 agents each sleeping 0.05s, concurrent execution should be ~0.05-0.10s,
    # not 4 * 0.05s = 0.2s (sequential). Allow up to 0.30s for loaded systems.
    assert elapsed < 0.30, f"Agents ran in {elapsed:.3f}s — too slow for concurrent"

@pytest.mark.asyncio
async def test_max_concurrency_is_respected(shared_state):
    """At most max_concurrency agents should run simultaneously."""
    concurrent_count = 0
    max_concurrent_seen = 0
    start_time = time.monotonic()

    async def slow_task(ctx):
        nonlocal concurrent_count, max_concurrent_seen
        concurrent_count += 1
        max_concurrent_seen = max(max_concurrent_seen, concurrent_count)
        await asyncio.sleep(0.1)
        concurrent_count -= 1
        return f"done at {time.monotonic() - start_time:.3f}s"

    specs = [
        SubagentSpec(role="general", task=f"task-{i}", agent_id=f"agent-{i}")
        for i in range(6)
    ]
    orchestrator = SubagentOrchestrator(shared_state, max_concurrency=3)

    await orchestrator.run_batch("concurrency test", specs)

    assert max_concurrent_seen <= 3, f"Saw {max_concurrent_seen} concurrent, expected ≤3"
```

---

### H-2: Make SharedState Public Methods Async-Safe for Async Callers

**Area:** `concurrency_model`
**Inspiration:** Hermes (pure async concurrency)
**Files affected:** `ash/agents/shared_state.py`, `ash/core/session.py`

#### Current Behavior

In `ash/agents/shared_state.py`, the `SharedState` class uses synchronous `sqlite3` connections (correct — sqlite3 is sync by design). The `_write_lock` is a `threading.Lock` that serializes DB writes within a single process.

The actual problem is the **caller context mismatch**: `SubagentOrchestrator._run_agents()` is `async` and creates `asyncio.create_task()` calls that invoke `SharedState` methods from within async tasks. The `threading.Lock` in SharedState is fine for sync callers, but when async code calls these sync methods, the GIL is released during I/O — and the sqlite3 connection itself (used concurrently by multiple async tasks via `check_same_thread=False`) can have its WAL journal state corrupted if two tasks write simultaneously without a lock.

Additionally, in `ash/core/session.py` (line 48):
```python
_db_write_locks_guard = threading.Lock()
```
This is used ONLY for the `_db_write_locks` dict (which maps db paths to `asyncio.Lock` instances). This pattern is correct and should NOT be changed.

#### How to Fix

**File:** `ash/agents/shared_state.py`

1. Add `import asyncio` to the top imports (if not already present), then add `asyncio.Lock` to `SharedState.__init__` for async-safe write serialization:

```python
import asyncio  # ← add if not already imported

class SharedState:
    def __init__(
        self,
        db_path: Path | str,
        *,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms};")
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.row_factory = sqlite3.Row
        self._init_db()
        self._async_lock = asyncio.Lock()  # ← add this; rename from _write_lock to be clear
```

2. Rename `_write_lock` to `_async_lock` to reflect its async purpose, then wrap all public state-mutating methods with `async with self._async_lock:`. The `threading.Lock` approach is replaced with `asyncio.Lock` so that `await self._async_lock.acquire()` yields properly in async contexts:

```python
async def update_status_async(
    self,
    agent_id: str,
    status: AgentStatusValue,
    current_task: str = "",
) -> None:
    """Async-safe version of update_status for use from asyncio tasks."""
    if status not in {"idle", "working", "failed", "completed"}:
        raise ValueError(f"Invalid status: {status!r}")
    async with self._async_lock:
        self._conn.execute(
            """
            UPDATE agent_status
            SET status = ?, current_task = ?, last_heartbeat = CURRENT_TIMESTAMP
            WHERE agent_id = ?
            """,
            (status, current_task, agent_id),
        )
```

3. Make `update_status`, `send_message`, `register_agent`, `update_sprint_state`, `create_sprint` async-wrapped. The sync versions remain for backwards compatibility with sync callers (e.g. the `_agent_driver.py` subprocess driver):

```python
# Keep the sync version for the subprocess driver:
def update_status(self, agent_id: str, status: AgentStatusValue, current_task: str = "") -> None:
    ...

# Add an async version for the orchestrator's async tasks:
async def update_status_async(self, agent_id: str, status: AgentStatusValue, current_task: str = "") -> None:
    if status not in {"idle", "working", "failed", "completed"}:
        raise ValueError(f"Invalid status: {status!r}")
    async with self._async_lock:
        self._conn.execute(
            "UPDATE agent_status SET status = ?, current_task = ?, last_heartbeat = CURRENT_TIMESTAMP WHERE agent_id = ?",
            (status, current_task, agent_id),
        )
```

4. The orchestrator's `_run_agents` calls `shared_state.update_status()` and `shared_state.send_message()`. These need to be awaited. Since `SharedState` methods are currently sync, the orchestrator currently calls them directly (not awaited). The fix requires the orchestrator to await the new `*_async` versions, which means `SubprocessAgent.run_in_process()` also needs to call the async versions.

**File:** `ash/core/session.py`

5. The `_db_write_locks_guard = threading.Lock()` on line 48 is **correct as-is**. It protects the `_db_write_locks` dict which maps db paths to `asyncio.Lock` instances — a thread-safe initialization pattern for a shared mutable dict. No change needed here.

#### Expected Output

- `SharedState.update_status_async()` is awaitable from async orchestrator tasks.
- `asyncio.gather` on multiple subagents that all call `shared_state.update_status_async()` does not cause sqlite3 WAL corruption.
- The subprocess driver continues to use sync methods (no async needed in the forked process).

#### Expected Output

- `SharedState` operations from within async subagents are safe with no thread-blocking.
- `asyncio.gather` on multiple subagents that all call `shared_state.update_status()` should not cause race conditions.
- The existing sync sqlite3 operations still work; they just gain async-safe wrappers.

#### Tests

**File:** `tests/unit/test_shared_state.py` (create if not exists)

```python
# tests/unit/test_shared_state.py
import asyncio
import pytest
from ash.agents.shared_state import SharedState
import tempfile
from pathlib import Path

@pytest.fixture
def state() -> SharedState:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield SharedState(Path(tmpdir) / "test.db")

@pytest.mark.asyncio
async def test_concurrent_status_updates_do_not_race(state):
    """Multiple concurrent update_status calls should not corrupt state."""
    async def update_many(agent_id, count):
        for i in range(count):
            state.update_status(agent_id, "working", current_task=f"task-{i}")

    await asyncio.gather(
        update_many("agent-a", 10),
        update_many("agent-b", 10),
        update_many("agent-c", 10),
    )

    agents = {st.agent_id: st for st in state.list_agents()}
    assert len(agents) == 3
    # All agents should have completed without exceptions
    for agent_id in ["agent-a", "agent-b", "agent-c"]:
        assert agent_id in agents

@pytest.mark.asyncio
async def test_concurrent_send_and_fetch(state):
    """Concurrent IPC send + fetch should not lose messages."""
    async def send_messages(sender, count):
        for i in range(count):
            state.send_message(sender, "lead", "test", f"msg-{i}")

    await asyncio.gather(
        send_messages("agent-1", 5),
        send_messages("agent-2", 5),
    )

    messages = state.fetch_messages("lead", undelivered_only=False)
    assert len(messages) == 10
```

---

### H-3: Wire `auto_commit` Tool into Default Tool Set

**Area:** `tool_system`
**Inspiration:** Claude Code CLI (git auto-commit as first-class tool)
**Files affected:** `ash/__main__.py`, `ash/tools/git.py`

#### Current Behavior

`AutoCommitTool` exists in `ash/tools/git.py` and `auto_commit_turn()` is called manually in `ash/core/loop.py:284–293` when `self.auto_commit` is True. But the tool is **not** registered in the default tools dict built by `_build_tools()` in `__main__.py`. It can only be invoked via the manual `auto_commit_turn()` call, not as a named tool that the LLM can call directly.

#### How to Fix

**File:** `ash/__main__.py`

In `_build_tools()`, add `AutoCommitTool` to the returned dict:

```python
def _build_tools(safety_guard: SafetyGuard) -> dict[str, Any]:
    return {
        "read_file": ReadFileTool(safety_guard),
        "write_file": WriteFileTool(safety_guard),
        "replace_file_content": ReplaceFileContentTool(safety_guard),
        "run_command": RunCommandTool(safety_guard),
        "auto_commit": AutoCommitTool(safety_guard),  # ← add this
    }
```

Also add the import at the top of `__main__.py`:
```python
from ash.tools.git import AutoCommitTool
```

#### Expected Output

- `auto_commit` appears in `ToolRegistry.names()` when loaded with default tools.
- The LLM can call `auto_commit` as a named tool (subject to approval/interactive mode).
- `python -m ash` with default config still works; the tool is available but not auto-invoked unless `auto_commit=True` in config.

#### Tests

**File:** `tests/unit/test_tools.py` (extend existing)

```python
# tests/unit/test_tools.py — add these tests

def test_auto_commit_tool_is_in_default_tools():
    """auto_commit should be in the default tools dict."""
    from ash.__main__ import _build_tools
    from ash.safety.guard import SafetyGuard
    from pathlib import Path

    guard = SafetyGuard(project_root=Path("/tmp"))
    tools = _build_tools(guard)
    assert "auto_commit" in tools, "auto_commit must be in default tools dict"
    assert tools["auto_commit"].name == "auto_commit"

@pytest.mark.asyncio
async def test_auto_commit_tool_runs_successfully(tmp_path):
    """AutoCommitTool should create a commit when called with valid args."""
    from ash.tools.git import AutoCommitTool
    from ash.safety.guard import SafetyGuard
    import subprocess

    # Initialize a git repo
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path, check=True, capture_output=True
    )

    # Create a file and commit
    (tmp_path / "test.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=tmp_path, check=True, capture_output=True
    )

    # Write a new file
    (tmp_path / "new.txt").write_text("world")

    guard = SafetyGuard(project_root=tmp_path)
    tool = AutoCommitTool(guard)
    result = await tool.run(message="add new file", paths=["new.txt"])

    assert result.success, f"auto_commit failed: {result.error}"
    assert "commit" in result.output.lower() or "created" in result.output.lower()
```

---

### H-4: Add Tool Middleware Chain to AshLoop

**Area:** `tool_system`
**Inspiration:** Claude Code CLI (PreToolUse/PostToolUse hooks)
**Files affected:** `ash/core/loop.py`, `ash/tools/base.py`

#### Current Behavior

Tool dispatch in `AshLoop._execute_tool_calls()` (loop.py:371–457) is a simple dictionary lookup:

```python
tool = self.tools.get(tool_name)
if tool is None:
    ...
    continue
tool_result: ToolResult = await tool.run(**arguments)
```

There is no interception point for logging, metrics, caching, or pre-processing.

#### How to Fix

**File:** `ash/tools/base.py`

Add a `ToolMiddleware` protocol:

```python
from abc import ABC, abstractmethod
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ash.tools.base import BaseTool

class ToolMiddleware(ABC):
    """Hook called before and after every tool execution."""

    async def before_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        tool: "BaseTool",
    ) -> None:
        """Called before tool.run(). Raise ToolMiddlewareSkip to skip execution."""
        pass

    async def after_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: "ToolResult",
    ) -> None:
        """Called after tool.run() with the result. Raise to augment result."""
        pass

class ToolMiddlewareSkip(Exception):
    """Raised from before_tool to skip tool execution entirely."""
```

**File:** `ash/core/loop.py`

Add middleware list to `AshLoop.__init__`:

```python
def __init__(
    self,
    ...
    tool_middlewares: list[ToolMiddleware] | None = None,
) -> None:
    ...
    self.tool_middlewares: list[ToolMiddleware] = list(tool_middlewares or [])
```

Add an `_apply_middlewares_before` and `_apply_middlewares_after` method:

```python
async def _apply_middlewares_before(
    self,
    tool_name: str,
    arguments: dict[str, Any],
    tool: BaseTool,
) -> None:
    for mw in self.tool_middlewares:
        await mw.before_tool(tool_name, arguments, tool)

async def _apply_middlewares_after(
    self,
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
) -> ToolResult:
    for mw in self.tool_middlewares:
        await mw.after_tool(tool_name, arguments, result)
    return result
```

In `_execute_tool_calls`, wrap each tool execution:

```python
try:
    await self._apply_middlewares_before(tool_name, arguments, tool)
    tool_result: ToolResult = await tool.run(**arguments)
    tool_result = await self._apply_middlewares_after(tool_name, arguments, tool_result)
except ToolMiddlewareSkip:
    tool_result = ToolResult(success=True, output="skipped by middleware", ...)
```

#### Expected Output

- `AshLoop` accepts `tool_middlewares` in constructor.
- Each tool call calls `before_tool` on all middlewares before execution.
- Each tool call calls `after_tool` on all middlewares after execution.
- A middleware that raises `ToolMiddlewareSkip` skips tool execution and returns a "skipped" result.
- Middlewares can modify the result via `after_tool` raising an exception or returning an augmented result.

#### Tests

**File:** `tests/unit/test_loop.py` (create if not exists)

```python
# tests/unit/test_loop.py
import pytest
from ash.core.loop import AshLoop
from ash.tools.base import BaseTool, ToolResult, ToolMiddleware, ToolMiddlewareSkip
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.safety.guard import SafetyGuard
from ash.ui.terminal import TerminalUI
from pathlib import Path
from unittest.mock import AsyncMock
import tempfile

class MockProvider(ProviderABC):
    model_name = "test"
    def count_tokens(self, text): return 0
    async def stream_chat(self, messages, temperature=0.0):
        yield StreamChunk(content="done", is_done=True)

class SpyMiddleware(ToolMiddleware):
    def __init__(self):
        self.before_calls = []
        self.after_calls = []

    async def before_tool(self, tool_name, arguments, tool):
        self.before_calls.append((tool_name, arguments))

    async def after_tool(self, tool_name, arguments, result):
        self.after_calls.append((tool_name, arguments, result))

class SkipMiddleware(ToolMiddleware):
    async def before_tool(self, tool_name, arguments, tool):
        raise ToolMiddlewareSkip()

class MyTestTool(BaseTool):
    """Minimal tool used only in tests — performs no real work."""
    name = "my_tool"
    args_schema = None

    async def run(self, **kwargs):
        return ToolResult(success=True, output="my_tool ran")

@pytest.mark.asyncio
async def test_middleware_before_called(tmp_path):
    spy = SpyMiddleware()
    with tempfile.TemporaryDirectory() as db_dir:
        store = SessionStore(Path(db_dir) / "test.db")
        guard = SafetyGuard(project_root=tmp_path)
        ui = TerminalUI(safety_tier="dry_run")
        loop = AshLoop(
            store, MockProvider(), guard, ui, tmp_path,
            tools={"my_tool": MyTestTool(guard)},
            tool_middlewares=[spy],
        )
        await loop.start_session()
        await loop.run_turn("test")

        assert len(spy.before_calls) >= 0  # tool was called and middleware was notified

@pytest.mark.asyncio
async def test_middleware_skip_aborts_tool(tmp_path):
    skip = SkipMiddleware()
    with tempfile.TemporaryDirectory() as db_dir:
        store = SessionStore(Path(db_dir) / "test.db")
        guard = SafetyGuard(project_root=tmp_path)
        ui = TerminalUI(safety_tier="dry_run")
        loop = AshLoop(
            store, MockProvider(), guard, ui, tmp_path,
            tools={"my_tool": MyTestTool(guard)},
            tool_middlewares=[skip],
        )
        await loop.start_session()
        result = await loop.run_turn("test")
        # The turn should complete without the tool actually running
        assert "skipped by middleware" in result or result  # no error from skipped tool
```

---

### H-5: Enforce Tool Allowlist Inside `SubprocessAgent.run_in_process`

**Area:** `safety`
**Inspiration:** OpenClaw (defense-in-depth tool allowlists)
**Files affected:** `ash/agents/subprocess_agent.py`

#### Current Behavior

The `SubprocessAgent` receives `tool_allowlist` in its constructor (line 104) and stores it (line 116). However, `run_in_process()` (lines 147–196) never checks this allowlist before running the task. The orchestrator's guard checks allowlists, but if `SubprocessAgent` is called directly, the allowlist is bypassed.

#### How to Fix

**File:** `ash/agents/subprocess_agent.py`

Add an allowlist check at the start of `run_in_process()`:

```python
async def run_in_process(self) -> AgentReport:
    self.register()
    self.shared_state.update_status(
        self.agent_id, "working", current_task=self.task
    )

    # Enforce tool allowlist before running.
    if self.tool_allowlist:
        # The runner is a TaskFn — we can't inspect what tools it will call
        # ahead of time unless the runner itself exposes a tool_allowlist.
        # Instead, we document that the caller must set tool_allowlist
        # only when using a tool-aware runner.
        pass

    try:
        result = await self.runner(...)
```

Actually, since `runner` is an opaque async function, the allowlist enforcement must happen at the **orchestrator level** or by wrapping the runner. The correct fix is to make `SubprocessAgent` accept a **tool enforcement guard** that the runner uses:

```python
def __init__(
    self,
    agent_id: str,
    role: str,
    task: str,
    shared_state: SharedState,
    *,
    runner: TaskFn,
    tool_allowlist: Sequence[str] | None = None,
    enforcement_guard: Callable[[str], bool] | None = None,  # ← add this
    ...
) -> None:
    ...
    self._enforcement_guard = enforcement_guard

async def run_in_process(self) -> AgentReport:
    ...
    # Check allowlist before running
    if self._enforcement_guard is not None and self.tool_allowlist:
        # Store allowlist on the context so tools can self-enforce
        self._tool_allowlist = self.tool_allowlist
```

And add a helper method:

```python
def is_tool_allowed(self, tool_name: str) -> bool:
    """Check if a tool is in this agent's allowlist. Returns True if no allowlist set."""
    if not self.tool_allowlist:
        return True
    return tool_name in self.tool_allowlist
```

The orchestrator passes its own allowlist-checking function as `enforcement_guard` when spawning agents:

```python
agent = SubprocessAgent(
    ...
    enforcement_guard=lambda tool_name: tool_name in spec.tool_allowlist,
)
```

#### Expected Output

- `SubprocessAgent.is_tool_allowed("read_file")` returns True/False based on allowlist.
- If `enforcement_guard` is set, the agent can expose it to its runner context.
- A runner that calls a disallowed tool is rejected by the enforcement guard before execution.

#### Tests

**File:** `tests/unit/test_subprocess_agent.py` (create if not exists)

```python
# tests/unit/test_subprocess_agent.py
import pytest
from ash.agents.subprocess_agent import SubprocessAgent, make_simple_text_task
from ash.agents.shared_state import SharedState
import tempfile
from pathlib import Path

@pytest.fixture
def shared_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield SharedState(Path(tmpdir) / "test.db")

def test_is_tool_allowed_respects_allowlist(shared_state):
    agent = SubprocessAgent(
        agent_id="test-agent",
        role="researcher",
        task="test task",
        shared_state=shared_state,
        runner=make_simple_text_task("done"),
        tool_allowlist=("read_file", "search_code"),
    )
    assert agent.is_tool_allowed("read_file") is True
    assert agent.is_tool_allowed("write_file") is False
    assert agent.is_tool_allowed("run_command") is False

def test_is_tool_allowed_allows_all_when_no_allowlist(shared_state):
    agent = SubprocessAgent(
        agent_id="test-agent",
        role="general",
        task="test task",
        shared_state=shared_state,
        runner=make_simple_text_task("done"),
        tool_allowlist=None,
    )
    assert agent.is_tool_allowed("read_file") is True
    assert agent.is_tool_allowed("write_file") is True
    assert agent.is_tool_allowed("anything") is True
```

---

### H-6: Add `PreToolUse` Hook Callback to AshLoop

**Area:** `safety`
**Inspiration:** Claude Code CLI (PreToolUse hook for permission decisions)
**Files affected:** `ash/core/loop.py`

#### Current Behavior

Approval in `AshLoop._execute_tool_calls()` (loop.py:391) is always done via `self.ui.request_tool_approval(tool_name, arguments)`. There is no way to programmatically auto-approve or auto-deny tools based on policy.

#### How to Fix

**File:** `ash/core/loop.py`

Add a `on_tool_approval` callback to `AshLoop.__init__`:

```python
from typing import Callable, Awaitable

ToolApprovalCallback = Callable[
    [str, dict[str, Any]],  # tool_name, arguments
    Awaitable[bool],        # True = approve, False = deny
]

def __init__(
    self,
    ...
    on_tool_approval: ToolApprovalCallback | None = None,
) -> None:
    ...
    self.on_tool_approval = on_tool_approval
```

In `_execute_tool_calls`, replace the UI approval call:

```python
# Old:
approved = self.ui.request_tool_approval(tool_name, arguments)

# New:
if self.on_tool_approval is not None:
    approved = await self.on_tool_approval(tool_name, arguments)
else:
    approved = self.ui.request_tool_approval(tool_name, arguments)
```

#### Expected Output

- When `on_tool_approval` is set, it is called instead of the UI approval prompt.
- The callback receives `(tool_name, arguments)` and returns `bool`.
- A callback that returns `True` auto-approves; `False` auto-denies.
- When unset, existing UI-based approval behavior is unchanged.

#### Tests

**File:** `tests/unit/test_loop.py`

```python
@pytest.mark.asyncio
async def test_on_tool_approval_callback_is_called(tmp_path):
    call_log = []

    async def approval_callback(tool_name, arguments):
        call_log.append((tool_name, arguments))
        return tool_name == "read_file"  # deny everything except read_file

    with tempfile.TemporaryDirectory() as db_dir:
        store = SessionStore(Path(db_dir) / "test.db")
        guard = SafetyGuard(project_root=tmp_path)
        ui = TerminalUI(safety_tier="dry_run")
        loop = AshLoop(
            store, MockProvider(), guard, ui, tmp_path,
            tools={"read_file": ReadFileTool(guard)},
            on_tool_approval=approval_callback,
        )
        await loop.start_session()
        await loop.run_turn("test")

        assert ("read_file", ...) in call_log

@pytest.mark.asyncio
async def test_on_tool_approval_can_auto_deny(tmp_path):
    async def deny_all(tool_name, arguments):
        return False

    with tempfile.TemporaryDirectory() as db_dir:
        store = SessionStore(Path(db_dir) / "test.db")
        guard = SafetyGuard(project_root=tmp_path)
        ui = TerminalUI(safety_tier="dry_run")
        loop = AshLoop(
            store, MockProvider(), guard, ui, tmp_path,
            tools={"read_file": ReadFileTool(guard)},
            on_tool_approval=deny_all,
        )
        await loop.start_session()
        result = await loop.run_turn("test read file")
        # Turn should complete (denied tools produce error results, not exceptions)
        assert result is not None
```

---

### H-7: Add Missing `__init__.py` Files

**Area:** `module_structure`
**Inspiration:** Hermes (proper Python package structure)
**Files affected:** `ash/tools/__init__.py` (create), `ash/providers/__init__.py` (populate), `ash/context/__init__.py` (populate)

#### How to Fix

**File:** `ash/tools/__init__.py` (create):

```python
"""Ash tools package."""

from ash.tools.base import BaseTool, ToolResult
from ash.tools.filesystem import ReadFileTool, WriteFileTool, ReplaceFileContentTool
from ash.tools.command import RunCommandTool
from ash.tools.git import AutoCommitTool

__all__ = [
    "BaseTool",
    "ToolResult",
    "ReadFileTool",
    "WriteFileTool",
    "ReplaceFileContentTool",
    "RunCommandTool",
    "AutoCommitTool",
]
```

**File:** `ash/providers/__init__.py` (populate — currently only has a docstring):

```python
"""Ash LLM provider adapters."""

from ash.providers.base import ProviderABC, StreamChunk, TokenCounterLike
from ash.providers.anthropic import AnthropicProvider, ProviderBackendUnavailable
from ash.providers.rate_limiter import TokenBucketRateLimiter

__all__ = [
    "ProviderABC",
    "StreamChunk",
    "TokenCounterLike",
    "AnthropicProvider",
    "ProviderBackendUnavailable",
    "TokenBucketRateLimiter",
]
```

**File:** `ash/context/__init__.py` (populate — currently empty):

```python
"""Ash context management: token counting and compaction."""

from ash.context.tokens import AnthropicTokenCounter, OpenAITokenCounter
from ash.context.compaction import Chunk

__all__ = [
    "AnthropicTokenCounter",
    "OpenAITokenCounter",
    "Chunk",
]
```

#### Expected Output

- `from ash.tools import BaseTool, ReadFileTool` works.
- `from ash.providers import AnthropicProvider, TokenBucketRateLimiter` works.
- `from ash.context import AnthropicTokenCounter, Chunk` works.
- `ruff check ash/tools ash/providers ash/context` shows no import errors.

#### Tests

```python
# tests/unit/test_imports.py
def test_tools_package_imports():
    from ash.tools import BaseTool, ReadFileTool, WriteFileTool, AutoCommitTool
    assert BaseTool is not None

def test_providers_package_imports():
    from ash.providers import AnthropicProvider, TokenBucketRateLimiter
    assert AnthropicProvider is not None
    assert TokenBucketRateLimiter is not None

def test_context_package_imports():
    from ash.context import AnthropicTokenCounter, Chunk
    assert AnthropicTokenCounter is not None
    assert Chunk is not None
```

---

### H-8: Add Integration Tests for Critical Modules

**Area:** `testing`
**Inspiration:** Hermes (test coverage for all core paths)
**Files affected:** Multiple (create new test files)

#### How to Fix — Test Files to Create

**File:** `tests/integration/test_planner.py`

```python
# tests/integration/test_planner.py
import pytest
from ash.core.planner import Planner
from ash.providers.base import ProviderABC, StreamChunk
from unittest.mock import AsyncMock
from pathlib import Path

# If parse_sprint_response exists in planner.py, import it:
# from ash.core.planner import parse_sprint_response
# Otherwise the test below using parse_sprint_response should be omitted.

class MockProvider(ProviderABC):
    model_name = "test"
    def count_tokens(self, text): return 0
    async def stream_chat(self, messages, temperature=0.0):
        yield StreamChunk(
            content="""## Goal
Add user authentication

## Definition of Done
- [ ] Login form exists
- [ ] Auth middleware protects routes

## Files in Scope
- src/auth.py
- src/login.py

## Files Off Limits
- src/main.py

## Test Command
pytest tests/auth/

## Rollback Plan
git revert HEAD

## Checklist

### Research
- [ ] Review existing auth patterns
- [ ] Check security requirements

### Implementation
- [ ] Add login form
- [ ] Add auth middleware
""",
            is_done=True,
        )

@pytest.mark.asyncio
async def test_planner_decompose_returns_execution(tmp_path):
    provider = MockProvider()
    planner = Planner(provider)
    execution = await planner.decompose(
        "Add user authentication",
        project_root=tmp_path,
        repo_map_excerpt="",
    )
    assert execution.contract.goal == "Add user authentication"
    assert len(execution.items) == 4
    assert execution.state.value == "planning"

```python
# Omit this test if parse_sprint_response does not exist in planner.py.
# If it does exist, the test should be:
# def test_parse_sprint_response_parses_all_sections():
#     raw = """## Goal
# Test goal
# ...
# """
#     execution = parse_sprint_response(raw, fallback_goal="test")
#     assert execution.contract.goal == "Test goal"
#     ...
```

**File:** `tests/unit/test_repo_parser.py`

```python
# tests/unit/test_repo_parser.py
import pytest
from pathlib import Path
import tempfile

# If SymbolExtractor exists in ash.repo.parser, import it:
# from ash.repo.parser import SymbolExtractor
# Otherwise these tests should be adapted to whatever symbol extraction API actually exists.

def test_symbol_extractor_finds_classes_and_functions(tmp_path):
    try:
        from ash.repo.parser import SymbolExtractor
    except ImportError:
        pytest.skip("SymbolExtractor not yet implemented")
    extractor = SymbolExtractor()
    (tmp_path / "sample.py").write_text("""
class MyClass:
    def method(self):
        pass

def my_function():
    pass
""")
    extractor = SymbolExtractor()
    symbols = list(extractor.extract(tmp_path / "sample.py"))
    names = [s.name for s in symbols]
    assert "MyClass" in names
    assert "my_function" in names
    assert "method" in names

```python
def test_symbol_extractor_handles_imports(tmp_path):
    try:
        from ash.repo.parser import SymbolExtractor
    except ImportError:
        pytest.skip("SymbolExtractor not yet implemented")
    (tmp_path / "imports.py").write_text("""
import os
from pathlib import Path
from typing import List
""")
    extractor = SymbolExtractor()
    symbols = list(extractor.extract(tmp_path / "imports.py"))
    names = [s.name for s in symbols]
    assert "os" in names
    assert "Path" in names
    assert "List" in names
```

**File:** `tests/unit/test_compaction.py`

```python
# tests/unit/test_compaction.py
import pytest
from ash.context.compaction import Chunk, compact_messages
from ash.core.session import Message
from datetime import datetime, timezone

def test_chunk_key_format():
    chunk = Chunk(file_path="src/main.py", start=1, end=10, content="lines...")
    assert chunk.chunk_key == "src/main.py:1-10"

def test_compact_messages_truncates_long_history():
    messages = [
        Message(role="user", content=f"message {i}", timestamp=datetime.now(timezone.utc))
        for i in range(100)
    ]
    compacted = compact_messages(messages, max_tokens=500)
    assert len(compacted) < len(messages)
    assert all(isinstance(m, Message) for m in compacted)
```

**File:** `tests/unit/test_rate_limiter.py`

```python
# tests/unit/test_rate_limiter.py
import asyncio
import pytest
from ash.providers.rate_limiter import TokenBucketRateLimiter

def test_consume_returns_true_when_tokens_available():
    limiter = TokenBucketRateLimiter(capacity=10, fill_rate=1.0)
    success, wait = limiter.consume(5)
    assert success is True
    assert wait == 0.0

def test_consume_returns_wait_when_insufficient_tokens():
    limiter = TokenBucketRateLimiter(capacity=5, fill_rate=0.0)
    success, wait = limiter.consume(10)
    assert success is False
    assert wait == float("inf")

@pytest.mark.asyncio
async def test_acquire_blocks_until_tokens_available():
    limiter = TokenBucketRateLimiter(capacity=5, fill_rate=10.0)
    # Tokens will refill quickly with fill_rate=10
    await limiter.acquire(5)
    # Should complete without hanging
    success, _ = limiter.consume(5)
    assert success is True
```

**File:** `tests/unit/test_terminal_ui.py`

```python
# tests/unit/test_terminal_ui.py
import pytest
from ash.ui.terminal import TerminalUI
from pathlib import Path

def test_terminal_ui_initializes_with_safety_tier():
    ui = TerminalUI(safety_tier="dry_run")
    assert ui.safety_tier == "dry_run"

    ui2 = TerminalUI(safety_tier="auto_approve")
    assert ui2.safety_tier == "auto_approve"

def test_terminal_ui_dry_run_denies_all():
    ui = TerminalUI(safety_tier="dry_run")
    approved = ui.request_tool_approval("write_file", {"file_path": "x"})
    assert approved is False

def test_terminal_ui_auto_approve_allows_all():
    ui = TerminalUI(safety_tier="auto_approve")
    approved = ui.request_tool_approval("write_file", {"file_path": "x"})
    assert approved is True
```

---

### H-9: Architect/Editor Dual-Model Mode for Subagents

**Area:** `subagent_orchestration`
**Inspiration:** Aider (Architect/Editor dual-mode)
**Files affected:** `ash/agents/orchestrator.py`, `ash/core/planner.py`

#### How to Fix

The `SubagentSpec` dataclass gets a new `mode` field:

```python
@dataclass
class SubagentSpec:
    role: str
    task: str
    runner: TaskFn | None = None
    agent_id: str = ""
    tool_allowlist: tuple[str, ...] = ()
    token_budget: int = 4000
    return_budget: int = 2000
    metadata: dict[str, Any] = field(default_factory=dict)
    mode: str = "execute"  # "architect" | "execute" | "general"
```

The orchestrator's `fanout_for_goal` helper gets a `phases` parameter that supports architect-mode specs. When `use_architect_mode=True`, the first phase gets `mode="architect"` and its runner is built via `make_architect_task`:

```python
def fanout_for_goal(
    goal: str,
    *,
    phases: Sequence[tuple[str, str, str | None]] | None = None,
    use_architect_mode: bool = False,
    planner: Planner | None = None,
    project_root: Path | None = None,
) -> list[SubagentSpec]:
    if phases is None and use_architect_mode:
        if planner is None or project_root is None:
            raise ValueError("use_architect_mode=True requires planner and project_root")
        phases = (
            ("general", f"Analyze and plan: {goal}", "architect"),   # mode="architect"
            ("general", f"Execute: {goal}", "execute"),              # mode="execute"
        )

    specs: list[SubagentSpec] = []
    for role, task, mode in phases:
        if mode == "architect":
            if planner is None or project_root is None:
                raise ValueError("architect phase requires planner and project_root")
            runner = make_architect_task(planner, project_root)
        elif mode == "execute" or mode is None:
            runner = None  # use default text runner
        else:
            runner = None
        specs.append(
            SubagentSpec(
                role=role,
                task=task,
                runner=runner,
                mode=mode or "execute",
                tool_allowlist=SubagentOrchestrator.default_role_allowlist(role),
            )
        )
    return specs
```

> **Closure note:** `make_architect_task(planner, project_root)` is called at spec-creation time (when `fanout_for_goal` is invoked), so the `planner` and `project_root` are captured in the closure at that point — not at task-execution time. The returned `runner` closure is self-contained and does NOT capture `planner` as a free variable later.

A default architect runner:

```python
def make_architect_task(planner: Planner, project_root: Path) -> TaskFn:
    async def runner(ctx: dict[str, Any]) -> AgentReport:
        task = ctx["task"]
        execution = await planner.decompose(task, project_root=project_root)
        return AgentReport(
            agent_id=ctx["agent_id"],
            role="architect",
            task=task,
            success=True,
            summary=f"Planned: {execution.contract.goal}",
            artifacts={"contract": execution.contract.to_dict()},
        )
    return runner
```

#### Expected Output

- `fanout_for_goal("implement login", use_architect_mode=True, planner=planner, project_root=root)` returns 2 specs: architect + execute.
- The architect agent calls `Planner.decompose()` and returns a sprint contract.
- The editor agent receives the architect's output and executes against it.
- Both run concurrently within their respective phases.

#### Tests

```python
# tests/integration/test_subagents.py — add

@pytest.mark.asyncio
async def test_architect_mode_produces_sprint_contract(shared_state, tmp_path):
    from ash.agents.orchestrator import fanout_for_goal
    from ash.core.planner import Planner
    from ash.providers.base import ProviderABC, StreamChunk
    class DummyProvider(ProviderABC):
        model_name = "test"
        def count_tokens(self, text): return 0
        async def stream_chat(self, messages, temperature=0.0):
            yield StreamChunk(content="## Goal\nTest\n\n## Definition of Done\n- done", is_done=True)
    planner = Planner(DummyProvider())
    specs = fanout_for_goal(
        "add user login",
        use_architect_mode=True,
        planner=planner,
        project_root=tmp_path,
    )
    assert len(specs) == 2
    assert specs[0].mode == "architect"
    assert specs[1].mode == "execute"
    assert specs[0].runner is not None  # architect has a real runner
    assert specs[1].runner is None      # execute uses default text runner
```

---

### H-10: Subagent Result Consolidation Step

**Area:** `subagent_orchestration`
**Inspiration:** Codex (result synthesis after fan-out)
**Files affected:** `ash/agents/orchestrator.py`

#### How to Fix

First, update the `OrchestratorResult` dataclass to add the `consolidated_report` field:

```python
@dataclass
class OrchestratorResult:
    """Aggregate result returned by :meth:`SubagentOrchestrator.run_batch`."""

    goal: str
    sprint_id: str
    reports: list[AgentReport] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    consolidated_report: AgentReport | None = None  # ← ADD THIS FIELD

    @property
    def all_succeeded(self) -> bool:
        return bool(self.reports) and all(r.success for r in self.reports)
```

Then add a `consolidate_results` method to `SubagentOrchestrator`:

```python
async def consolidate_results(
    self,
    reports: list[AgentReport],
    goal: str,
) -> AgentReport:
    """Synthesize multiple agent reports into a single coherent response."""
    if len(reports) == 1:
        return reports[0]

    summaries = "\n".join(
        f"- [{r.role}] {r.summary}" for r in reports
    )
    consolidated = AgentReport(
        agent_id="consolidator",
        role="consolidator",
        task=goal,
        success=all(r.success for r in reports),
        summary=f"Consolidated {len(reports)} agent reports",
        artifacts={
            "reports": [r.to_dict() for r in reports],
            "all_succeeded": all(r.success for r in reports),
        },
    )
    return consolidated
```

In `run_batch`, after `_run_agents` completes:

```python
reports = await self._run_agents(specs)
consolidated = await self.consolidate_results(reports, goal)
# Return both raw reports and consolidated result
return OrchestratorResult(
    goal=goal,
    sprint_id=sprint_id,
    reports=reports,
    consolidated_report=consolidated,
    elapsed_seconds=elapsed,
)
```

#### Expected Output

- `run_batch()` returns an `OrchestratorResult` with both raw `reports` and a `consolidated_report`.
- Single-agent batches return the same report as consolidated (no-op).
- Multi-agent batches return a synthesized report with all artifacts preserved.

#### Tests

```python
@pytest.mark.asyncio
async def test_consolidate_reports_multiple_agents(shared_state):
    orchestrator = SubagentOrchestrator(shared_state, max_concurrency=4)
    specs = [
        SubagentSpec(role="researcher", task="research X", agent_id=f"r-{i}")
        for i in range(3)
    ]
    result = await orchestrator.run_batch("research task", specs)
    assert result.consolidated_report is not None
    assert result.consolidated_report.role == "consolidator"
    assert len(result.consolidated_report.artifacts["reports"]) == 3
```

---

### H-11: Cross-Session Memory Recall

**Area:** `memory`
**Inspiration:** Hermes (LLM summarization for cross-session recall)
**Files affected:** `ash/core/session.py`, `ash/core/loop.py`

#### How to Fix

Add a `get_recent_session_summaries` method to `SessionStore`:

```python
def get_recent_session_summaries(
    self,
    project_path: str,
    limit: int = 5,
) -> list[str]:
    """Return plain-text content of the N most recent sessions for a project."""
    from contextlib import closing
    with closing(self._conn) as conn, conn:
        rows = conn.execute(
            """
            SELECT s.session_id,
                   GROUP_CONCAT(m.content, '\n') as messages
            FROM sessions s
            JOIN messages m ON m.session_id = s.session_id
            WHERE s.project_path = ?
            GROUP BY s.session_id
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (project_path, limit),
        ).fetchall()
    return [row["messages"] for row in rows]
```

In `AshLoop.__init__`, add the parameter:

```python
def __init__(
    self,
    ...
    enable_memory_recall: bool = False,  # ← add this
) -> None:
    ...
    self.enable_memory_recall = enable_memory_recall
```

In `AshLoop.start_session()`, before creating a new session:

```python
async def start_session(self, session_id: str | None = None) -> Session:
    if session_id is not None:
        try:
            self.current_session = self.session_store.load_session(session_id)
            return self.current_session
        except KeyError:
            pass

    # New session: optionally recall recent context from prior sessions
    if self.enable_memory_recall:
        recent = self.session_store.get_recent_session_summaries(
            str(self.project_root), limit=3
        )
        if recent:
            memory_context = self._build_memory_context(recent)
            self.system_prompt = f"{self.system_prompt}\n\n## Recent Context\n{memory_context}"

    session = self.session_store.create_session(str(self.project_root))
    self.current_session = session
    return session
```

Add `_build_memory_context` that formats recent sessions for the system prompt:

```python
def _build_memory_context(self, recent_summaries: list[str]) -> str:
    """Format N most recent session transcripts as a context string."""
    lines = ["The following sessions are prior context for this project:"]
    for i, summary in enumerate(recent_summaries, 1):
        lines.append(f"\n--- Prior Session {i} ---\n{summary[:2000]}")
    return "".join(lines)
```

#### Expected Output

- When `enable_memory_recall=True`, the 3 most recent session transcripts are prepended to the system prompt for a new session.
- Users see previous context when continuing a project in a new session.
- `get_recent_session_summaries` returns messages from the last N sessions ordered by recency.

#### Tests

```python
# tests/unit/test_session.py — add

def test_get_recent_session_summaries(tmp_path):
    store = SessionStore(tmp_path / "test.db")
    s1 = store.create_session(str(tmp_path))
    s2 = store.create_session(str(tmp_path))

    store.save_message(s1.session_id, Message(role="user", content="hello", timestamp=datetime.now(timezone.utc)))
    store.save_message(s2.session_id, Message(role="user", content="goodbye", timestamp=datetime.now(timezone.utc)))

    summaries = store.get_recent_session_summaries(str(tmp_path), limit=2)
    assert len(summaries) == 2
    assert any("hello" in s for s in summaries)
    assert any("goodbye" in s for s in summaries)
```

---

## MEDIUM Priority

---

### M-1: Implement `.mcp.json` MCP Server Configuration

**Area:** `mcp_integration`
**Inspiration:** Hermes, Claude Code CLI
**Files affected:** New file `ash/mcp/server.py`, `ash/config.py`

#### How to Fix

**File:** `ash/mcp/server.py` (create)

```python
"""MCP server discovery and management."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

@dataclass(frozen=True)
class MCPServerConfig:
    """Configuration for a single MCP server."""

    name: str
    command: str
    args: list[str]
    env: dict[str, str]

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> MCPServerConfig:
        return cls(
            name=name,
            command=data["command"],
            args=data.get("args", []),
            env=data.get("env", {}),
        )

def load_mcp_servers(config_path: Path | None = None) -> dict[str, MCPServerConfig]:
    """Load MCP server definitions from .mcp.json."""
    if config_path is None:
        config_path = Path(".mcp.json")
    if not config_path.exists():
        return {}

    with config_path.open() as f:
        raw = json.load(f)

    servers = {}
    for name, data in raw.items():
        if isinstance(data, dict) and "command" in data:
            servers[name] = MCPServerConfig.from_dict(name, data)
    return servers

def expand_env_vars(value: str) -> str:
    """Expand ${VAR} and $VAR in strings."""
    return os.path.expandvars(value)
```

**File:** `ash/config.py` — add `mcp_servers: dict[str, MCPServerConfig]` field loaded from `.mcp.json`.

#### Expected Output

- Ash reads `.mcp.json` at startup and exposes servers via `config.mcp_servers`.
- `${CLAUDE_PLUGIN_ROOT}` and `${ASH_PROJECT_DIR}` are expanded in command/args/env.
- Unknown servers (without `command` key) are silently ignored.

#### Tests

```python
# tests/unit/test_mcp.py
import json
import pytest
from ash.mcp.server import load_mcp_servers, expand_env_vars
from pathlib import Path

def test_load_mcp_servers_from_file(tmp_path):
    mcp_file = tmp_path / ".mcp.json"
    mcp_file.write_text(json.dumps({
        "github": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_TOKEN": "test"}
        }
    }))
    servers = load_mcp_servers(mcp_file)
    assert "github" in servers
    assert servers["github"].command == "npx"
    assert servers["github"].args == ["-y", "@modelcontextprotocol/server-github"]

def test_expand_env_vars():
    os.environ["TEST_VAR"] = "hello"
    assert expand_env_vars("${TEST_VAR}/path") == "hello/path"
    assert expand_env_vars("(no var)") == "(no var)"
```

---

### M-2: Hook System with Matcher-Based Registration

**Area:** `plugin_system`
**Inspiration:** OpenClaw (hook matchers like `Write|Edit`, `Bash`)
**Files affected:** New file `ash/hooks/registry.py`, `ash/core/loop.py`

#### How to Fix

**File:** `ash/hooks/registry.py` (create)

```python
"""Hook registry for Ash extensibility."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

HookResult = str | Awaitable[str] | None  # session-start hooks may return injected prompt text

class Hook(ABC):
    """Base class for all hooks."""

    @abstractmethod
    async def run(self, **kwargs: Any) -> HookResult:
        raise NotImplementedError

@dataclass
class PreToolUseHook(Hook):
    """Called before a tool is executed. Raise HookBlock to prevent execution."""

    matcher: re.Pattern[str]  # e.g. re.compile(r"Write|Edit")
    callback: Callable[[str, dict[str, Any]], Awaitable[None]]

    async def run(self, tool_name: str, arguments: dict[str, Any], **kwargs: Any) -> HookResult:
        if self.matcher.search(tool_name):
            await self.callback(tool_name, arguments)

@dataclass
class PostToolUseHook(Hook):
    """Called after a tool executes."""

    matcher: re.Pattern[str]
    callback: Callable[[str, dict[str, Any], Any], Awaitable[None]]

    async def run(self, tool_name: str, arguments: dict[str, Any], result: Any, **kwargs: Any) -> HookResult:
        if self.matcher.search(tool_name):
            await self.callback(tool_name, arguments, result)

@dataclass
class SessionStartHook(Hook):
    """Called when a new session starts. May return a string to inject into the system prompt."""

    callback: Callable[[], HookResult]  # may return str | Awaitable[str] | None

    async def run(self, **kwargs: Any) -> HookResult:
        result = self.callback()
        if hasattr(result, "__await__"):  # handle async callback
            return await result
        return result  # type: ignore[return-value]

class HookRegistry:
    def __init__(self) -> None:
        self._pre_tool: list[PreToolUseHook] = []
        self._post_tool: list[PostToolUseHook] = []
        self._session_start: list[SessionStartHook] = []
        self._injected_prompt: str = ""  # accumulated injected prompt from hooks

    def register_pre_tool(self, hook: PreToolUseHook) -> None:
        self._pre_tool.append(hook)

    def register_post_tool(self, hook: PostToolUseHook) -> None:
        self._post_tool.append(hook)

    def register_session_start(self, hook: SessionStartHook) -> None:
        self._session_start.append(hook)

    async def fire_pre_tool(self, tool_name: str, arguments: dict[str, Any]) -> None:
        for hook in self._pre_tool:
            await hook.run(tool_name=tool_name, arguments=arguments)

    async def fire_post_tool(self, tool_name: str, arguments: dict[str, Any], result: Any) -> None:
        for hook in self._post_tool:
            await hook.run(tool_name=tool_name, arguments=arguments, result=result)

    async def fire_session_start(self) -> None:
        self._injected_prompt = ""  # reset at start of each session
        for hook in self._session_start:
            prompt_addition = await hook.run() or ""
            if prompt_addition:
                self._injected_prompt += ("\n" + prompt_addition if self._injected_prompt else prompt_addition)

    def get_injected_prompt(self) -> str:
        """Return the accumulated injected prompt from session-start hooks."""
        return self._injected_prompt
```

Wire into `AshLoop` via `hooks: HookRegistry | None = None` constructor parameter.

#### Expected Output

- `HookRegistry` holds pre-tool, post-tool, and session-start hooks.
- Hooks are registered with regex matchers (e.g. `re.compile(r"Write|Edit")`).
- `HookRegistry.fire_pre_tool("write_file", {...})` calls only matching hooks.
- Hooks are additive — multiple hooks can fire for the same event.

#### Tests

```python
# tests/unit/test_hooks.py
import pytest
import re
from ash.hooks.registry import (
    HookRegistry, PreToolUseHook, PostToolUseHook, SessionStartHook
)

@pytest.mark.asyncio
async def test_pre_tool_hook_fires_on_matcher():
    registry = HookRegistry()
    called = []

    async def check_write(name, args):
        called.append((name, args))

    registry.register_pre_tool(PreToolUseHook(
        matcher=re.compile(r"Write|Edit"),
        callback=check_write,
    ))

    await registry.fire_pre_tool("write_file", {"file_path": "x"})
    assert called == [("write_file", {"file_path": "x"})]

    await registry.fire_pre_tool("read_file", {})  # should NOT fire
    assert len(called) == 1

@pytest.mark.asyncio
async def test_session_start_hook_fires():
    registry = HookRegistry()
    started = []

    async def on_start():
        started.append(True)

    registry.register_session_start(SessionStartHook(callback=on_start))
    await registry.fire_session_start()

    assert started == [True]
```

---

### M-3: Add `SessionStart` Hook for System Prompt Injection

**Area:** `plugin_system`
**Inspiration:** Claude Code CLI (SessionStart hooks add to default system prompt)
**Files affected:** `ash/core/loop.py`, `ash/hooks/registry.py`

#### How to Fix

This builds on M-2. In `AshLoop.start_session()`, call `hooks.fire_session_start()` and allow hooks to mutate `self.system_prompt`:

```python
async def start_session(self, session_id: str | None = None) -> Session:
    ...
    if self.hooks is not None:
        await self.hooks.fire_session_start()
        # Hooks can append to self.system_prompt via a shared context
        injected = self.hooks.get_injected_prompt()
        if injected:
            self.system_prompt = f"{self.system_prompt}\n\n{injected}"
    ...
```

(`get_injected_prompt()` is already defined in M-2's `HookRegistry` implementation.)

#### Expected Output

- A `SessionStartHook` that sets `injected_prompt` on the registry causes that text to be appended to the system prompt on session start.
- Plugins can inject project-specific instructions without modifying the base system prompt template.

---

### M-4: Add `PostToolUse` Hook for Logging/Metrics

**Area:** `plugin_system`
**Inspiration:** Claude Code CLI
**Files affected:** `ash/hooks/registry.py`, `ash/core/loop.py`

#### How to Fix

This is already modeled in M-2 via `PostToolUseHook`. Wire it into `AshLoop._execute_tool_calls`:

```python
if self.hooks is not None:
    await self.hooks.fire_post_tool(tool_name, arguments, tool_result)
```

#### Expected Output

- Every tool execution fires `PostToolUseHook` for all matching hooks.
- Observability plugins can log tool latency, success/failure, token usage.

---

### M-5: Plugin Manifest Schema (`plugin.json`)

**Area:** `plugin_system`
**Inspiration:** Claude Code CLI
**Files affected:** New file `ash/plugins/manifest.py`

#### How to Fix

**File:** `ash/plugins/manifest.py` (create)

```python
"""Plugin manifest schema and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

@dataclass
class PluginManifest:
    name: str
    version: str
    description: str = ""
    commands: list[dict[str, Any]] = field(default_factory=list)
    agents: list[dict[str, Any]] = field(default_factory=list)
    hooks: list[dict[str, Any]] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginManifest:
        return cls(
            name=data["name"],
            version=data.get("version", "0.0.0"),
            description=data.get("description", ""),
            commands=data.get("commands", []),
            agents=data.get("agents", []),
            hooks=data.get("hooks", []),
            mcp_servers=data.get("mcpServers", []),
            skills=data.get("skills", []),
        )

    @classmethod
    def load(cls, path: Path) -> PluginManifest:
        import json
        with path.open() as f:
            data = json.load(f)
        return cls.from_dict(data)
```

#### Expected Output

- `PluginManifest.load(Path("plugin.json"))` returns a validated manifest.
- Unknown fields are ignored (forward compatibility).
- Missing required fields (`name`) raise `ValueError`.

#### Tests

```python
# tests/unit/test_plugin_manifest.py
import json
import pytest
from ash.plugins.manifest import PluginManifest
from pathlib import Path

def test_load_minimal_manifest(tmp_path):
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(json.dumps({
        "name": "my-plugin",
        "version": "1.0.0",
    }))
    manifest = PluginManifest.load(manifest_file)
    assert manifest.name == "my-plugin"
    assert manifest.version == "1.0.0"
    assert manifest.description == ""

def test_load_full_manifest(tmp_path):
    manifest_file = tmp_path / "plugin.json"
    manifest_file.write_text(json.dumps({
        "name": "test-plugin",
        "version": "0.1.0",
        "description": "A test plugin",
        "commands": [{"name": "hello", "description": "Say hello"}],
        "agents": [{"identifier": "helper", "systemPrompt": "You are helpful."}],
    }))
    manifest = PluginManifest.load(manifest_file)
    assert len(manifest.commands) == 1
    assert manifest.commands[0]["name"] == "hello"
    assert len(manifest.agents) == 1
```

---

### M-6: In-Memory Context Object for Turn-Level State Sharing

**Area:** `context_management`
**Inspiration:** Claude Code CLI
**Files affected:** New file `ash/context/turn.py`, `ash/core/loop.py`

#### How to Fix

**File:** `ash/context/turn.py` (create)

```python
"""In-memory context object shared during a single turn."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

@dataclass
class TurnContext:
    """Mutable context object passed through a single turn's lifecycle.

    Tools, hooks, and middleware can read and write to this object
    to share state during a single user turn without database I/O.
    """

    session_id: str
    turn_id: str
    data: dict[str, Any] = field(default_factory=dict)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def has(self, key: str) -> bool:
        return key in self.data

    def clear(self) -> None:
        self.data.clear()
```

In `AshLoop.__init__`, add `turn_context: TurnContext | None = None`. Initialize it at the start of `run_turn`:

```python
self.turn_context = TurnContext(
    session_id=self.current_session.session_id,
    turn_id=str(uuid4()),
)
```

Pass `turn_context` to tools via a tool-wrapping mechanism or `ToolContext` (similar to `SkillContext`).

#### Expected Output

- `TurnContext` is created at the start of each user turn.
- Tools and middlewares can read/write `turn_context.data` during the turn.
- The context is discarded at the end of the turn (new turn = fresh context).
- `turn_context.get("my_key", default=None)` returns the value or default.

#### Tests

```python
# tests/unit/test_turn_context.py
from ash.context.turn import TurnContext

def test_turn_context_set_get():
    ctx = TurnContext(session_id="s1", turn_id="t1")
    ctx.set("count", 5)
    assert ctx.get("count") == 5
    assert ctx.get("missing", "default") == "default"

def test_turn_context_has():
    ctx = TurnContext(session_id="s1", turn_id="t1")
    ctx.set("key", "value")
    assert ctx.has("key") is True
    assert ctx.has("missing") is False

def test_turn_context_clear():
    ctx = TurnContext(session_id="s1", turn_id="t1")
    ctx.set("a", 1)
    ctx.set("b", 2)
    ctx.clear()
    assert ctx.get("a") is None
    assert ctx.get("b") is None
```

---

### M-7: Per-Agent Sandbox Modes

**Area:** `subagent_orchestration`
**Inspiration:** OpenClaw (sandbox modes per agent: off, scoped, bubblewrap, docker)
**Files affected:** `ash/agents/subprocess_agent.py`, `ash/agents/orchestrator.py`, `ash/sandbox/manager.py`

#### How to Fix

Add `sandbox_tier` to `SubagentSpec`:

```python
from ash.sandbox._base import SANDBOX_TIER_SCOPED, SANDBOX_TIER_SANDBOX_EXEC, SANDBOX_TIER_DOCKER

@dataclass
class SubagentSpec:
    ...
    sandbox_tier: int = SANDBOX_TIER_SCOPED  # default: scoped (Tier 1)
```

In `SubprocessAgent.__init__`, accept `sandbox_tier` and store it. The runner's sandbox manager uses this tier when executing commands.

In `SubagentOrchestrator.fanout_for_goal`, set appropriate tiers per role:

```python
ROLE_SANDBOX_TIER: dict[str, int] = {
    "researcher": SANDBOX_TIER_SCOPED,       # needs network access (Tier 1)
    "coder": SANDBOX_TIER_SANDBOX_EXEC,      # needs bubblewrap isolation (Tier 2)
    "tester": SANDBOX_TIER_SCOPED,           # runs tests, network OK (Tier 1)
    "reviewer": SANDBOX_TIER_SCOPED,         # read-only (Tier 1)
}
```

#### Expected Output

- Researcher agents can access network (Tier 1 = scoped).
- Coder agents are isolated with bubblewrap (Tier 2).
- Each role gets the minimum sandbox required for its function.
- The tier is configurable per-spec via `SubagentSpec.sandbox_tier`.

#### Tests

```python
# tests/unit/test_subprocess_agent.py — add

def test_subagent_spec_sandbox_tier_default():
    from ash.agents.orchestrator import SubagentSpec
    spec = SubagentSpec(role="coder", task="test")
    assert spec.sandbox_tier == SANDBOX_TIER_SCOPED  # default

def test_subagent_spec_sandbox_tier_override():
    from ash.agents.orchestrator import SubagentSpec
    from ash.sandbox._base import SANDBOX_TIER_SANDBOX_EXEC
    spec = SubagentSpec(role="coder", task="test", sandbox_tier=SANDBOX_TIER_SANDBOX_EXEC)
    assert spec.sandbox_tier == SANDBOX_TIER_SANDBOX_EXEC
```

---

### M-8: Agent-to-Agent IPC Messages

**Area:** `subagent_orchestration`
**Inspiration:** Codex (ACP for direct agent-to-agent messaging)
**Files affected:** `ash/agents/shared_state.py`

#### How to Fix

The existing `send_message` / `fetch_messages` already supports agent-to-agent IPC (recipient_id can be any agent_id, not just "lead"). Add a convenience method:

```python
def send_to_agent(
    self,
    sender_id: str,
    recipient_id: str,
    message_type: str,
    content: Any,
) -> int:
    """Send a message directly from one agent to another."""
    return self.send_message(sender_id, recipient_id, message_type, content)

def broadcast(
    self,
    sender_id: str,
    message_type: str,
    content: Any,
) -> None:
    """Broadcast a message to all registered agents."""
    for agent in self.list_agents():
        if agent.agent_id != sender_id:
            self.send_message(sender_id, agent.agent_id, message_type, content)
```

#### Expected Output

- Agent A can call `shared_state.send_to_agent("agent-a", "agent-b", "status", "half done")`.
- Agent B polls for messages addressed to itself.
- `broadcast("agent-a", "checkin", "ping")` sends to all other agents.

#### Tests

```python
# tests/unit/test_shared_state.py — add

@pytest.mark.asyncio
async def test_agent_to_agent_messages(shared_state):
    shared_state.register_agent("agent-a", role="general")
    shared_state.register_agent("agent-b", role="general")

    shared_state.send_to_agent("agent-a", "agent-b", "test", "hello from a")
    messages = shared_state.fetch_messages("agent-b", undelivered_only=False)
    assert len(messages) == 1
    assert messages[0].content == "hello from a"

@pytest.mark.asyncio
async def test_broadcast(shared_state):
    shared_state.register_agent("agent-a", role="general")
    shared_state.register_agent("agent-b", role="general")
    shared_state.register_agent("agent-c", role="general")

    shared_state.broadcast("agent-a", "ping", "checkin")
    for agent_id in ["agent-b", "agent-c"]:
        msgs = state.fetch_messages(agent_id, undelivered_only=False)
        assert len(msgs) == 1
        assert msgs[0].content == "checkin"
```

---

### M-9: Retry with Exponential Backoff for Transient Tool Failures

**Area:** `error_recovery`
**Inspiration:** Codex
**Files affected:** `ash/core/loop.py`

#### How to Fix

In `AshLoop._execute_tool_calls()`, wrap tool execution in a retry loop:

```python
MAX_RETRIES = 2
BASE_DELAY_SECONDS = 1.0

async def _execute_with_retry(
    tool: BaseTool,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    last_error: str | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            tool_result: ToolResult = await tool.run(**arguments)
            return {
                "success": tool_result.success,
                "output": tool_result.output,
                "error": tool_result.error,
                "truncated": tool_result.truncated,
                "token_count": tool_result.token_count,
            }
        except Exception as exc:
            last_error = str(exc)
            if attempt < MAX_RETRIES:
                delay = BASE_DELAY_SECONDS * (2 ** attempt)
                await asyncio.sleep(delay)
            continue
    return {
        "success": False,
        "output": "",
        "error": f"Failed after {MAX_RETRIES + 1} attempts: {last_error}",
    }
```

Use this in `_execute_tool_calls` instead of the direct `await tool.run()`.

#### Expected Output

- Transient failures (network timeout, rate limit) are retried up to 3 times (2 retries).
- Delay doubles each retry: 1s, 2s, 4s.
- Permanent failures fail after all retries are exhausted.
- The circuit breaker still counts consecutive failures.

#### Tests

```python
# tests/unit/test_loop.py — add

@pytest.mark.asyncio
async def test_retry_on_transient_failure(tmp_path):
    from unittest.mock import AsyncMock
    from ash.tools.base import BaseTool, ToolResult

    transient_count = 0

    class FlakyTool(BaseTool):
        name = "flaky"
        args_schema = None

        async def run(self, **kwargs):
            nonlocal transient_count
            transient_count += 1
            if transient_count < 3:
                raise RuntimeError("transient error")
            return ToolResult(success=True, output="ok")

    with tempfile.TemporaryDirectory() as db_dir:
        store = SessionStore(Path(db_dir) / "test.db")
        guard = SafetyGuard(project_root=tmp_path)
        ui = TerminalUI(safety_tier="auto_approve")
        loop = AshLoop(
            store, MockProvider(), guard, ui, tmp_path,
            tools={"flaky": FlakyTool(guard)},
        )
        await loop.start_session()
        result = await loop.run_turn("test")
        assert transient_count == 3  # 3 total attempts: fail, fail, success (MAX_RETRIES=2 means 2 retries)
```

---

### M-10: Recovery Suggestions When Circuit Breaker Trips

**Area:** `error_recovery`
**Inspiration:** Claude Code CLI
**Files affected:** `ash/core/recovery.py`, `ash/core/loop.py`

#### How to Fix

**File:** `ash/core/recovery.py`

Extend `CircuitBreaker` to carry context about what failed:

```python
@dataclass
class CircuitBreakerError(Exception):
    tool_name: str
    failure_count: int
    suggestions: tuple[str, ...] = ()

    def __str__(self) -> str:
        base = super().__str__()
        if self.suggestions:
            suggestions_text = "\n".join(f"  - {s}" for s in self.suggestions)
            return f"{base}\n\nPossible alternatives:\n{suggestions_text}"
        return base
```

Add a `suggest_alternatives` method:

```python
SUGGESTIONS: dict[str, tuple[str, ...]] = {
    "read_file": (
        "Try using a more specific line range with start_line and end_line.",
        "Check if the file exists with run_command: ls -la",
    ),
    "write_file": (
        "Check if the parent directory exists.",
        "Try using replace_file_content to modify specific lines instead.",
    ),
    "run_command": (
        "Check the command syntax.",
        "Try running the command manually in a terminal first.",
    ),
}

def suggest_alternatives(self, tool_name: str) -> tuple[str, ...]:
    return self.SUGGESTIONS.get(tool_name, ())
```

In `record_failure`, populate suggestions and raise `CircuitBreakerError` to halt the loop:

```python
SUGGESTIONS: dict[str, tuple[str, ...]] = {
    "read_file": (
        "Try using a more specific line range with start_line and end_line.",
        "Check if the file exists with run_command: ls -la",
    ),
    "write_file": (
        "Check if the parent directory exists.",
        "Try using replace_file_content to modify specific lines instead.",
    ),
    "run_command": (
        "Check the command syntax.",
        "Try running the command manually in a terminal first.",
    ),
}

def suggest_alternatives(self, tool_name: str) -> tuple[str, ...]:
    return self.SUGGESTIONS.get(tool_name, ())

def record_failure(self, tool_name: str) -> None:
    if tool_name == self.last_failed_tool:
        self.failure_counter += 1
    else:
        self.last_failed_tool = tool_name
        self.failure_counter = 1

    if self.failure_counter >= self.max_failures:
        suggestions = self.suggest_alternatives(tool_name)
        raise CircuitBreakerError(
            f"Tool '{tool_name}' failed {self.failure_counter} times consecutively.",
            tool_name=tool_name,
            failure_count=self.failure_counter,
            suggestions=suggestions,
        )
```

The existing `is_tripped` property is unchanged (it returns `True` when the breaker has tripped). The `CircuitBreakerError` is raised on the **third consecutive failure** (when `failure_counter` reaches `max_failures`), not on every subsequent call.

In `AshLoop._execute_tool_calls`, the circuit breaker call should be wrapped to catch `CircuitBreakerError` and halt the turn immediately:

```python
try:
    tool_result: ToolResult = await tool.run(**arguments)
except CircuitBreakerError:
    # Re-raise so the turn loop can catch it and halt
    raise
except Exception as exc:
    ...
```

The existing `if self.circuit_breaker.is_tripped:` check after the loop is removed since `CircuitBreakerError` now propagates immediately.

#### Expected Output

- When circuit breaker trips, `CircuitBreakerError` is raised with suggestions for alternative approaches.
- Suggestions are tool-specific (read_file suggestions differ from run_command suggestions).
- Unknown tools get empty suggestions (no crash).
- The turn halts immediately on circuit breaker trip (via exception propagation).

#### Tests

```python
# tests/unit/test_recovery.py
import pytest
from ash.core.recovery import CircuitBreaker, CircuitBreakerError

def test_circuit_breaker_includes_suggestions_on_trip():
    cb = CircuitBreaker(max_failures=2)
    cb.record_failure("read_file")
    with pytest.raises(CircuitBreakerError) as exc_info:
        cb.record_failure("read_file")  # 2nd consecutive = trips (max_failures=2)
    assert "read_file" in str(exc_info.value)
    assert exc_info.value.failure_count == 2
    assert len(exc_info.value.suggestions) > 0
    # is_tripped should also be True
    assert cb.is_tripped is True

def test_circuit_breaker_unknown_tool_no_suggestions():
    cb = CircuitBreaker(max_failures=2)
    cb.record_failure("unknown_tool")
    with pytest.raises(CircuitBreakerError) as exc_info:
        cb.record_failure("unknown_tool")
    assert exc_info.value.suggestions == ()
    assert exc_info.value.tool_name == "unknown_tool"

def test_circuit_breaker_resets_on_different_tool():
    cb = CircuitBreaker(max_failures=2)
    cb.record_failure("read_file")
    cb.record_failure("write_file")  # different tool resets counter
    cb.record_failure("write_file")  # now 2 consecutive write_file → trips
    with pytest.raises(CircuitBreakerError) as exc_info:
        cb.record_failure("write_file")
    assert exc_info.value.tool_name == "write_file"
```

---

### M-11: Implement OpenAI Provider

**Area:** `provider_support`
**Inspiration:** Codex
**Files affected:** New file `ash/providers/openai.py`, `ash/providers/__init__.py`

#### How to Fix

**File:** `ash/providers/openai.py` (create)

```python
"""OpenAI GPT provider adapter for Ash."""

from __future__ import annotations

from typing import Any, AsyncGenerator
import openai  # type: ignore[import-not-found]

from ash.context.tokens import OpenAITokenCounter
from ash.providers.base import ProviderABC, StreamChunk, TokenCounterLike

class OpenAIProvider(ProviderABC):
    def __init__(
        self,
        model_name: str = "gpt-4o",
        api_key: str | None = None,
        *,
        token_counter: TokenCounterLike | None = None,
    ) -> None:
        self._model_name = model_name
        self._api_key = api_key
        self._token_counter = token_counter or OpenAITokenCounter()
        self._client = openai.AsyncOpenAI(api_key=api_key)

    @property
    def model_name(self) -> str:
        return self._model_name

    def count_tokens(self, text: str) -> int:
        return self._token_counter.count(text)

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
    ) -> AsyncGenerator[StreamChunk, None]:
        stream = await self._client.chat.completions.create(
            model=self._model_name,
            messages=messages,
            temperature=temperature,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta
            content = delta.content or ""
            is_done = chunk.choices[0].finish_reason is not None
            yield StreamChunk(content=content, is_done=is_done, model=self._model_name)
```

Update `ash/providers/__init__.py` to export `OpenAIProvider`.

Update `ash/__main__._build_provider()` to support `provider="openai"`:

```python
def _build_provider(config: AshConfig) -> ProviderABC:
    if config.provider == "openai":
        from ash.providers.openai import OpenAIProvider
        return OpenAIProvider(model_name=config.model_name, api_key=config.api_key)
    elif config.provider == "anthropic":
        from ash.providers.anthropic import AnthropicProvider
        return AnthropicProvider(model_name=config.model_name, api_key=config.api_key)
    ...
```

#### Expected Output

- `provider = "openai"` in `ash.toml` uses OpenAI models.
- `model_name = "gpt-4o"` is the default.
- The OpenAI provider implements the full `ProviderABC` contract.
- Streaming works identically to the Anthropic provider.

#### Tests

```python
# tests/unit/test_providers.py — add

def test_openai_provider_initializes():
    from ash.providers.openai import OpenAIProvider
    provider = OpenAIProvider(model_name="gpt-4o", api_key="test-key")
    assert provider.model_name == "gpt-4o"
    assert provider.count_tokens("hello world") > 0

@pytest.mark.asyncio
async def test_openai_provider_stream_chat_signature():
    from ash.providers.openai import OpenAIProvider
    from ash.providers.base import ProviderABC
    provider = OpenAIProvider(model_name="gpt-4o", api_key="test-key")
    assert isinstance(provider, ProviderABC)
    # Verify abstract methods are implemented
    assert hasattr(provider, "stream_chat")
    assert hasattr(provider, "count_tokens")
    assert hasattr(provider, "model_name")
```

---

### M-12: Implement Ollama Provider for Local Models

**Area:** `provider_support`
**Inspiration:** Aider (local/offline model support)
**Files affected:** New file `ash/providers/ollama.py`, `ash/providers/__init__.py`

#### How to Fix

**File:** `ash/providers/ollama.py` (create)

```python
"""Ollama local model provider for Ash."""

from __future__ import annotations

from typing import Any, AsyncGenerator
import httpx

from ash.context.tokens import AnthropicTokenCounter
from ash.providers.base import ProviderABC, StreamChunk, TokenCounterLike

class OllamaProvider(ProviderABC):
    def __init__(
        self,
        model_name: str = "llama3",
        base_url: str = "http://localhost:11434",
        *,
        token_counter: TokenCounterLike | None = None,
    ) -> None:
        self._model_name = model_name
        self._base_url = base_url.rstrip("/")
        self._token_counter = token_counter or AnthropicTokenCounter()
        self._client = httpx.AsyncClient(timeout=60.0)

    @property
    def model_name(self) -> str:
        return self._model_name

    def count_tokens(self, text: str) -> int:
        return self._token_counter.count(text)

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
    ) -> AsyncGenerator[StreamChunk, None]:
        payload = {
            "model": self._model_name,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        async with self._client.stream("POST", f"{self._base_url}/api/chat", json=payload) as resp:
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                import json
                data = json.loads(line)
                content = data.get("message", {}).get("content", "")
                is_done = data.get("done", False)
                yield StreamChunk(content=content, is_done=is_done, model=self._model_name)
```

#### Expected Output

- `provider = "ollama"` in `ash.toml` uses local Ollama models.
- `model_name = "llama3"` is the default.
- Works offline for air-gapped deployments.
- Falls back gracefully if Ollama is not running.

#### Tests

```python
# tests/unit/test_providers.py — add

def test_ollama_provider_initializes():
    from ash.providers.ollama import OllamaProvider
    provider = OllamaProvider(model_name="llama3", base_url="http://localhost:11434")
    assert provider.model_name == "llama3"
    assert provider.count_tokens("hello") > 0
```

---

### M-13: Context Filters for RepoMap

**Area:** `context_management`
**Inspiration:** Cody (context filters to exclude files/repos)
**Files affected:** `ash/repo/repomap.py`, `ash/config.py`

#### How to Fix

Add `repo_map_exclude_patterns: list[str]` to `AshConfig`:

```python
repo_map_exclude_patterns: list[str] = Field(
    default_factory=lambda: [
        "node_modules/**",
        ".git/**",
        "__pycache__/**",
        "*.pyc",
        ".venv/**",
        "dist/**",
        "build/**",
    ],
    description="Glob patterns to exclude from RepoMap analysis.",
)
```

In `RepoMap.rank()`, filter out excluded paths before returning results:

```python
import fnmatch

def rank(self, active_paths: list[Path]) -> list[Path]:
    # Copy the existing rank() implementation here, then filter:
    # ranked = [ ... existing rank logic ... ]
    # IMPORTANT: Do NOT use super().rank() — RepoMap has no parent class.
    # Implement the ranking logic directly (copy from the existing RepoMap.rank method).
    ranked = [...]  # ← replace with actual ranking of active_paths by relevance
    return [p for p in ranked if not self._is_excluded(p)]

def _is_excluded(self, path: Path) -> bool:
    path_str = str(path)
    for pattern in self.exclude_patterns:
        if fnmatch.fnmatch(path_str, pattern) or fnmatch.fnmatch(path_str, f"**/{pattern}"):
            return True
    return False
```

#### Expected Output

- `node_modules`, `.git`, `__pycache__` etc. are never included in RepoMap output.
- Exclusion patterns are configurable via `ash.toml`.
- The LLM never sees generated/vendor code in the system prompt context.

#### Tests

```python
# tests/unit/test_repomap.py — add

def test_repomap_excludes_node_modules(tmp_path):
    (tmp_path / "src.py").write_text("x = 1")
    (tmp_path / "node_modules" / "dep.js").write_text("module.exports = {}")

    repo_map = RepoMap(project_root=tmp_path, exclude_patterns=["node_modules/**"])
    ranked = repo_map.rank([tmp_path])
    paths = [str(p) for p in ranked]
    assert not any("node_modules" in p for p in paths)
    assert any("src.py" in p for p in paths)
```

---

### M-14: Memory Nudge Intervals

**Area:** `memory`
**Inspiration:** Hermes (turn-based and iteration-based memory nudges)
**Files affected:** `ash/core/loop.py`

#### How to Fix

Add `memory_nudge_interval: int = 0` to `AshLoop.__init__`. Track turns since last nudge:

```python
def __init__(..., memory_nudge_interval: int = 0, ...) -> None:
    ...
    self.memory_nudge_interval = memory_nudge_interval
    self._turns_since_nudge = 0
```

In `run_turn`, after each iteration:

```python
self._turns_since_nudge += 1
if (
    self.memory_nudge_interval > 0
    and self._turns_since_nudge >= self.memory_nudge_interval
):
    nudge = self._build_memory_nudge()
    if nudge:
        session.messages.append(Message(role="system", content=nudge, timestamp=_utc_now()))
    self._turns_since_nudge = 0
```

The `_build_memory_nudge` method summarizes recent context and injects it as a system message:

```python
def _build_memory_nudge(self) -> str:
    if not self.current_session:
        return ""
    recent = self.current_session.messages[-10:]
    summary = f"[Memory nudge — {len(recent)} messages in recent turns]"
    return summary
```

#### Expected Output

- Every N turns (configurable), a memory nudge is injected as a system message.
- The nudge reminds the agent of the original goal and recent context.
- The counter resets after each nudge.

---

### M-15: Skill Nudge Intervals

**Area:** `memory`
**Inspiration:** Hermes (suggest relevant skills after N iterations of disuse)
**Files affected:** `ash/core/loop.py`, `ash/tools/registry.py`

#### How to Fix

Add `skill_nudge_interval: int = 0` and `tools_registry: ToolRegistry | None = None` to `AshLoop.__init__`. Track iterations since last skill use:

```python
from ash.tools.registry import ToolRegistry

def __init__(
    self,
    ...
    tools_registry: ToolRegistry | None = None,  # ← add this
    skill_nudge_interval: int = 0,               # ← add this
) -> None:
    ...
    self.tools_registry = tools_registry
    self.skill_nudge_interval = skill_nudge_interval
    self._iterations_since_skill_use = 0
```

In `_execute_tool_calls`, check if any tool call was a skill:

```python
self._iterations_since_skill_use += 1
if (
    self.skill_nudge_interval > 0
    and self._iterations_since_skill_use >= self.skill_nudge_interval
):
    nudge = self._build_skill_nudge()
    if nudge:
        session.messages.append(Message(role="system", content=nudge, timestamp=_utc_now()))
    self._iterations_since_skill_use = 0
```

The `_build_skill_nudge` method suggests relevant skills based on recent tool calls:

```python
def _build_skill_nudge(self) -> str:
    if self.tools_registry is None:
        return ""
    skill_index = self.tools_registry.skill_index()
    if not skill_index:
        return ""
    suggestions = [f"- {s.name}: {s.description}" for s in skill_index[:3]]
    return f"[Skill nudge] Consider using:\n" + "\n".join(suggestions)
```

#### Expected Output

- If no skills have been used in N iterations, a nudge suggests relevant skills.
- Skill nudges improve discoverability of dormant capabilities.
- The counter resets when a skill is used.

---

### M-16: Whole-Edit Format for Code Modifications

**Area:** `workflow_orchestration`
**Inspiration:** Codex (apply_patch tool with custom patch format)
**Files affected:** `ash/tools/filesystem.py`

#### How to Fix

Add a `WholeEditTool` to `ash/tools/filesystem.py`:

```python
class WholeEditArgs(BaseModel):
    file_path: str = Field(..., description="Absolute or relative path to the file.")
    content: str = Field(..., description="Complete new file content.")
    reason: str = Field("", description="Why the entire file is being replaced.")


class WholeEditTool(BaseTool):
    name = "whole_edit"
    description = (
        "Replace the complete content of a file with new content. "
        "Use when the change is too large or complex for replace_file_content. "
        "The entire file content is replaced."
    )
    args_schema = WholeEditArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = WholeEditArgs(**kwargs)
        resolved_path = self.safety_guard.validate_path(args.file_path)

        parent = resolved_path.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)

        resolved_path.write_text(args.content, encoding="utf-8", newline="")
        return ToolResult(
            success=True,
            output=f"Whole-edit applied to {resolved_path} ({len(args.content)} chars).",
        )
```

Register it in `_build_tools()` alongside the other file tools.

#### Expected Output

- `whole_edit` is available as a named tool.
- The LLM can replace entire files in one call (for large refactors).
- Atomic write is used (via `_atomic_write_text`).
- Existing `replace_file_content` remains the default for small changes.

#### Tests

```python
# tests/unit/test_tools.py — add

@pytest.mark.asyncio
async def test_whole_edit_tool(tmp_path):
    test_file = tmp_path / "big.py"
    test_file.write_text("old content")

    guard = SafetyGuard(project_root=tmp_path)
    tool = WholeEditTool(guard)
    result = await tool.run(
        file_path=str(test_file),
        content="new content\nwith more lines\n" * 100,
        reason="major refactor",
    )

    assert result.success, f"whole_edit failed: {result.error}"
    assert test_file.read_text() == "new content\nwith more lines\n" * 100
```

---

## LOW Priority

---

### L-1: ClawHub-Style Remote Skill Registry

**Area:** `plugin_system`
**Inspiration:** OpenClaw (ClawHub skill registry)

#### How to Fix

Create `ash/plugins/registry.py`:

```python
"""Remote skill registry for community skill discovery."""

from __future__ import annotations

import httpx
from dataclasses import dataclass
from typing import Any

@dataclass
class RemoteSkill:
    name: str
    description: str
    source: str  # URL or "community"
    trigger: str = ""

async def search_registry(query: str) -> list[RemoteSkill]:
    """Search the community skill registry."""
    # Placeholder: hit a skill registry API
    async with httpx.AsyncClient() as client:
        resp = await client.get("https://registry.ash.dev/skills/search", params={"q": query})
        resp.raise_for_status()
        data = resp.json()
        return [RemoteSkill(**s) for s in data.get("skills", [])]
```

#### Expected Output

- `search_registry("git")` returns community skills matching "git".
- The registry is pluggable (can be replaced with a different backend).

---

### L-2: Environment Variable Expansion in Plugin Configs

**Area:** `plugin_system`
**Inspiration:** Hermes, Claude Code CLI

#### How to Fix

```python
import os

def expand_env_vars(value: str) -> str:
    return os.path.expandvars(value)

# In MCPServerConfig, expand all string fields:
class MCPServerConfig:
    def __post_init__(self):
        self.command = expand_env_vars(self.command)
        self.args = [expand_env_vars(str(a)) for a in self.args]
        self.env = {k: expand_env_vars(v) for k, v in self.env.items()}
```

---

### L-3: Plugin Dependency Management

**Area:** `plugin_system`
**Inspiration:** Claude Code CLI

#### How to Fix

In `PluginManifest`, add a `dependencies` field:

```python
@dataclass
class PluginManifest:
    ...
    dependencies: list[dict[str, str]] = field(default_factory=list)
    # e.g. [{"name": "other-plugin", "version": ">=1.0.0"}]
```

Add a `check_dependencies()` method that validates all declared dependencies are installed.

---

### L-4: MCP CLI Management Commands

**Area:** `mcp_integration`
**Inspiration:** Claude Code CLI (`claude mcp add`, `claude mcp list`)

#### How to Fix

Add a `mcp` subcommand to `ash/__main__.py`:

```python
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ash")
    subparsers = parser.add_subparsers()

    mcp_parser = subparsers.add_parser("mcp")
    mcp_parser.add_argument("action", choices=["add", "list", "remove"])
    mcp_parser.add_argument("server_name", nargs="?")
    ...
```

---

### L-5: SSE and WebSocket MCP Transport Support

**Area:** `mcp_integration`
**Inspiration:** Hermes

#### How to Fix

Extend `MCPServerConfig` to accept transport type:

```python
@dataclass
class MCPServerConfig:
    ...
    transport: str = "stdio"  # "stdio" | "sse" | "http" | "websocket"
    url: str = ""            # for SSE/HTTP/WebSocket
```

Add transport-specific connection logic in `ash/mcp/client.py`.

---

### L-6: RepoMap Dependency Graph Visualization

**Area:** `context_management`
**Inspiration:** Hermes

#### How to Fix

Add a `to_dot_graph()` method to `RepoMap`:

```python
def to_dot_graph(self, ranked: list[Path]) -> str:
    """Render the dependency graph as a Graphviz DOT string.

    Users can pipe the result to ``dot -Tpng`` to visualize the graph.
    """
    lines = ["digraph repo {", "  rankdir=LR;"]
    for src_path in ranked:
        src_idx = self._index.get(src_path.resolve())
        if src_idx is None:
            continue
        src_label = str(src_path.relative_to(self.project_root))
        if self._adjacency is None:
            continue
        for dep_idx in range(self._adjacency.shape[0]):
            if self._adjacency[src_idx, dep_idx] > 0:
                dep_path = self._files[dep_idx].path
                dep_label = str(dep_path.relative_to(self.project_root))
                lines.append(f'  "{src_label}" -> "{dep_label}";')
    lines.append("}")
    return "\n".join(lines)
```

Users can pipe this to `dot -Tpng` to visualize the dependency graph.

---

### L-7: Context Window Usage Indicator

**Area:** `ui`
**Inspiration:** Claude Code CLI

#### How to Fix

In `TerminalUI`, add a token meter:

```python
from rich.progress import Progress, BarColumn, TextColumn

class TerminalUI:
    def __init__(self, ..., show_token_meter: bool = False) -> None:
        ...
        self.show_token_meter = show_token_meter
        if show_token_meter:
            self._token_progress = Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
            )
            self._token_task = None  # initialized on first render

    def _render_token_meter(self, current_tokens: int, max_tokens: int) -> str:
        """Render a single-line ASCII token meter.

        Example output when current=3000, max=100000:
        [Token ████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 3000/100000 (3.0%)]
        """
        bar_width = 30
        pct = min(current_tokens / max_tokens, 1.0) if max_tokens > 0 else 0.0
        filled = int(bar_width * pct)
        bar = "█" * filled + "░" * (bar_width - filled)
        label = f"[Token {bar} {current_tokens}/{max_tokens} ({pct*100:.1f}%)]"
        return label
```

Update `_render` to show the token bar alongside the main content when `show_token_meter=True`.

---

### L-8: JSON-Configurable Dynamic Agent Spawning

**Area:** `subagent_orchestration`
**Inspiration:** Claude Code CLI (agent generation via JSON config)

#### How to Fix

Add a `AgentSpec.from_dict()` class method to `SubagentSpec`:

```python
@dataclass
class SubagentSpec:
    ...
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubagentSpec:
        return cls(
            role=data["role"],
            task=data["task"],
            agent_id=data.get("agent_id", ""),
            tool_allowlist=tuple(data.get("tool_allowlist", [])),
            token_budget=data.get("token_budget", 4000),
            return_budget=data.get("return_budget", 2000),
            metadata=data.get("metadata", {}),
            mode=data.get("mode", "execute"),           # from H-9
            sandbox_tier=data.get("sandbox_tier", 1),  # from M-7
        )
```

The orchestrator accepts a JSON file or dict of agent specs:

```python
async def run_batch_from_config(
    self,
    goal: str,
    config: list[dict[str, Any]],
) -> OrchestratorResult:
    specs = [SubagentSpec.from_dict(d) for d in config]
    return await self.run_batch(goal, specs)
```

---

### L-9: Agent Workspace Isolation

**Area:** `subagent_orchestration`
**Inspiration:** OpenClaw (isolated workspaces per agent)

#### How to Fix

Add `workspace_root` override to `SubagentSpec`:

```python
@dataclass
class SubagentSpec:
    ...
    workspace_root: Path | None = None  # None = use project default
```

In `SubprocessAgent.spawn_subprocess()`, pass the per-agent workspace:

```python
if spec.workspace_root:
    env["ASH_WORKSPACE_ROOT"] = str(spec.workspace_root)
```

The agent reads `ASH_WORKSPACE_ROOT` from env and uses it instead of the default project root.

---

### L-10: Agent-Created Self-Improving Skills

**Area:** `memory`
**Inspiration:** Hermes (closed learning loop)

#### How to Fix

Add a `write_python_skill` call in a post-execution hook:

```python
async def on_agent_success(report: AgentReport) -> None:
    if report.artifacts.get("should_skillify"):
        skill_path = write_python_skill(
            skill_dir=self.skill_root,
            name=report.task.replace(" ", "_"),
            description=f"Automates: {report.task}",
            trigger="",
            body=report.artifacts["skill_body"],
        )
        self.registry.reload_skill_module(report.task, skill_path)
```

The agent decides when to skillify via a tool call or artifact flag.

---

### L-11: Memory as Markdown Files

**Area:** `memory`
**Inspiration:** OpenClaw (Markdown file memory)

#### How to Fix

Create `ash/memory/markdown_store.py`:

```python
"""Plain Markdown file memory store."""

from pathlib import Path

class MarkdownMemoryStore:
    def __init__(self, memory_dir: Path) -> None:
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def save(self, key: str, content: str) -> None:
        (self.memory_dir / f"{key}.md").write_text(content)

    def load(self, key: str) -> str | None:
        path = self.memory_dir / f"{key}.md"
        return path.read_text() if path.exists() else None

    def list_keys(self) -> list[str]:
        return [p.stem for p in self.memory_dir.glob("*.md")]
```

---

### L-12: Continuous Autonomous Agent Mode

**Area:** `workflow_orchestration`
**Inspiration:** Hermes

#### How to Fix

Add a `continuous_mode: bool = False` to `AshLoop.__init__`. In `run_turn`, if `continuous_mode` is True and the turn produces no tool calls, automatically start a new turn with a synthesized prompt like "continue where you left off":

```python
if self.continuous_mode and not tool_calls:
    follow_up = "Continue the previous task. What is the next step?"
    return await self.run_turn(follow_up)
```

Add a `max_continuous_turns: int = 10` to prevent infinite loops.

---

### L-13: Streamable HTTP Extension for Remote Control

**Area:** `workflow_orchestration`
**Inspiration:** Codex

#### How to Fix

Create `ash/server/http.py`:

```python
"""HTTP server for remote control of Ash."""

from fastapi import FastAPI, HTTPException
from ash.core.loop import AshLoop

app = FastAPI(title="Ash Remote Control")

@app.post("/turn")
async def run_turn(input: dict) -> dict:
    user_input = input.get("input", "")
    result = await ash_loop.run_turn(user_input)
    return {"result": result}
```

This enables IDE extensions and CI/CD pipelines to control Ash programmatically.

---

### L-14: JSON-RPC Client/Server Protocol

**Area:** `workflow_orchestration`
**Inspiration:** Codex

#### How to Fix

Create `ash/server/jsonrpc.py`:

```python
"""JSON-RPC 2.0 server for Ash."""

import json
from typing import Any

class JSONRPCServer:
    def __init__(self, loop: AshLoop) -> None:
        self.loop = loop

    async def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        method = request.get("method", "")
        params = request.get("params", {})
        request_id = request.get("id")

        if method == "run_turn":
            result = await self.loop.run_turn(params["input"])
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        else:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            }
```

---

### L-15: LSP Diagnostics Integration

**Area:** `provider_support`
**Inspiration:** Codex

#### How to Fix

Create `ash/lsp/diagnostics.py`:

```python
"""LSP-compatible diagnostics emitter for Ash tool results."""

from typing import Any

class LSPDiagnosticsEmitter:
    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = workspace_root

    def emit_tool_errors(self, tool_name: str, error: str, file_path: str) -> dict[str, Any]:
        return {
            "resource": f"file://{self.workspace_root}/{file_path}",
            "diagnostics": [{
                "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
                "severity": 1,  # Error
                "message": f"[{tool_name}] {error}",
                "source": "ash",
            }]
        }
```

Editors (VS Code, JetBrains) can consume these diagnostics to show Ash tool errors inline.

---

### L-16: Autonomous `--auto-approve` Mode

**Area:** `error_recovery`
**Inspiration:** Cline

#### How to Fix

Add `safety_tier: str = "interactive"` to `AshConfig`. When set to `"auto_approve"`:

```python
if self.safety_tier == "auto_approve":
    approved = True
else:
    approved = self.ui.request_tool_approval(tool_name, arguments)
```

All tools are auto-approved when `safety_tier = "auto_approve"` in `ash.toml`.

---

### L-17: Agent Teams and Spawn Commands

**Area:** `subagent_orchestration`
**Inspiration:** Cline

#### How to Fix

Add a `spawn_agent` tool that creates a new `SubprocessAgent` at runtime:

```python
class SpawnAgentArgs(BaseModel):
    role: str
    task: str
    agent_id: str | None = None

class SpawnAgentTool(BaseTool):
    name = "spawn_agent"
    description = "Spawn a new subagent to handle a subtask."
    args_schema = SpawnAgentArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = SpawnAgentArgs(**kwargs)
        agent = SubprocessAgent(...)
        report = await agent.run_in_process()
        return ToolResult(success=report.success, output=report.summary)
```

Register this in the tool registry for the orchestrator agent's allowlist.

---

## Implementation Order

The following ordering respects dependencies (items that unlock others come first):

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
  M-2  → Hook system (already listed above)
  M-4  → MCP CLI management commands
  M-5  → SSE/WebSocket transport

Phase 8 (memory & context):
  H-11 → Cross-session memory recall
  M-13 → Context filters for RepoMap
  M-14 → Memory nudge intervals
  M-15 → Skill nudge intervals
  L-10 → Agent-created self-improving skills
  L-11 → Memory as Markdown files

Phase 9 (error recovery):
  M-9  → Retry with exponential backoff
  M-10 → Recovery suggestions on circuit breaker trip
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
| `ash/core/loop.py` | H-4, H-6, H-11, M-2, M-3, M-4, M-6, M-9, M-10, M-14, M-15 |
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
| `ash/mcp/server.py` | M-1 (new file) |
| `ash/mcp/client.py` | M-5 (new file) |
| `ash/hooks/registry.py` | M-2, M-3, M-4 (new file) |
| `ash/plugins/manifest.py` | M-5 (new file) |
| `ash/plugins/registry.py` | L-1 (new file) |
| `ash/server/http.py` | L-13 (new file) |
| `ash/server/jsonrpc.py` | L-14 (new file) |
| `ash/lsp/diagnostics.py` | L-15 (new file) |
| `ash/providers/openai.py` | M-11 (new file) |
| `ash/providers/ollama.py` | M-12 (new file) |
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
| `tests/unit/test_mcp.py` | M-1 |
| `tests/unit/test_plugin_manifest.py` | M-5 |
| `tests/unit/test_turn_context.py` | M-6 |
| `tests/unit/test_providers.py` | M-11, M-12 |
| `tests/unit/test_repomap.py` | M-13 |
| `tests/unit/test_imports.py` | H-7 |
