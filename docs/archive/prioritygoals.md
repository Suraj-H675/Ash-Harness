# Priority Goals — One Command Per Item

> **Purpose:** This file tells an AI exactly what to implement, by section name.
> The AI reads `/home/suraj/ash/PRIORITY.md`, finds the section, and implements the EXACT code shown there.
>
> **How to use:** Give the AI one command at a time. Each command tells the AI:
> 1. Read `/home/suraj/ash/PRIORITY.md`, find section [X]
> 2. Implement the EXACT code shown in that section's "How to Fix" block
> 3. Add the EXACT test code shown in that section's "Tests" block
> 4. Run the tests
> 5. Say "do NOT implement any other item from PRIORITY.md"

---

## HIGH Priority

---

### H-1: Fix Subagent Concurrency Bug

```
/goal h1

You are implementing H-1 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section H-1 in /home/suraj/ash/PRIORITY.md
2. In ash/agents/orchestrator.py, find the _run_agents method (around line 153)
3. Replace the entire _run_agents method with the "How to Fix" code shown in H-1
4. The key change: remove the await semaphore.acquire() from the for loop, and instead use "async with semaphore:" inside _run_one
5. Run: ruff check ash/agents/orchestrator.py && ruff format ash/agents/orchestrator.py
6. Create tests/integration/test_subagents.py with the EXACT test code from H-1's "Tests" section
   IMPORTANT: In the test file, the pytest fixture MUST be defined as:
   @pytest.fixture
   def shared_state() -> SharedState:   # note: NO "self" parameter
       ...
   NOT as "def shared_state(self)" which is invalid pytest fixture syntax
7. Run: pytest tests/integration/test_subagents.py -v

DO NOT implement any other item from PRIORITY.md.
```

---

### H-2: Make SharedState Public Methods Async-Safe

```
/goal h2

You are implementing H-2 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section H-2 in /home/suraj/ash/PRIORITY.md
2. In ash/agents/shared_state.py:
   a. Add "import asyncio" to the imports
   b. Add "self._async_lock = asyncio.Lock()" in __init__ (after self._init_db())
   c. Add the async wrapper methods shown in H-2's "How to Fix": update_status_async, send_message_async, register_agent_async, update_sprint_state_async, create_sprint_async
   d. Keep all existing sync methods unchanged (needed by the subprocess driver)
3. Run: ruff check ash/agents/shared_state.py && ruff format ash/agents/shared_state.py
4. Create tests/unit/test_shared_state.py with the EXACT test code from H-2's "Tests" section
   IMPORTANT: In test fixtures, define as:
   @pytest.fixture
   def shared_state() -> SharedState:   # NO "self" parameter
       ...
   The async tests (test_concurrent_status_updates_do_not_race, test_concurrent_send_and_fetch) use async def with await — those call the async wrappers from H-2. The sync tests use sync calls without await.
5. Run: pytest tests/unit/test_shared_state.py -v

DO NOT implement any other item from PRIORITY.md.
```

---

### H-3: Wire auto_commit Tool into Default Tool Set

```
/goal h3

You are implementing H-3 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section H-3 in /home/suraj/ash/PRIORITY.md
2. In ash/__main__.py:
   a. Add "from ash.tools.git import AutoCommitTool" to imports
   b. In _build_tools(), add "auto_commit": AutoCommitTool(safety_guard), to the dict
3. Run: ruff check ash/__main__.py && ruff format ash/__main__.py
4. In tests/unit/test_tools.py, add the EXACT tests shown in H-3's "Tests" section
5. Run: pytest tests/unit/test_tools.py::test_auto_commit_tool_is_in_default_tools tests/unit/test_tools.py::test_auto_commit_tool_runs_successfully -v

DO NOT implement any other item from PRIORITY.md.
```

---

### H-4: Add Tool Middleware Chain to AshLoop

```
/goal h4

You are implementing H-4 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section H-4 in /home/suraj/ash/PRIORITY.md
2. In ash/tools/base.py: add the ToolMiddleware ABC and ToolMiddlewareSkip exception shown in H-4's "How to Fix"
3. In ash/core/loop.py:
   a. Add tool_middlewares parameter to AshLoop.__init__
   b. Add _apply_middlewares_before and _apply_middlewares_after methods
   c. In _execute_tool_calls, wrap each tool execution with the middleware calls shown in H-4
4. Run: ruff check ash/tools/base.py ash/core/loop.py && ruff format ash/tools/base.py ash/core/loop.py
5. Create tests/unit/test_loop.py with the EXACT test code from H-4's "Tests" section
6. Run: pytest tests/unit/test_loop.py -v

DO NOT implement any other item from PRIORITY.md.
```

---

### H-5: Enforce Tool Allowlist Inside SubprocessAgent.run_in_process

```
/goal h5

You are implementing H-5 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section H-5 in /home/suraj/ash/PRIORITY.md
2. In ash/agents/subprocess_agent.py:
   a. Add "enforcement_guard: Callable[[str], bool] | None = None" parameter to __init__
   b. Store it as self._enforcement_guard
   c. Add the is_tool_allowed() helper method shown in H-5
3. ALSO update ash/agents/orchestrator.py _run_agents() — in the SubprocessAgent construction inside _run_agents, add:
   enforcement_guard=lambda tool_name: tool_name in (spec.tool_allowlist or set()),
   This is the critical fix — without it, the allowlist is never actually enforced!
4. Run: ruff check ash/agents/subprocess_agent.py ash/agents/orchestrator.py && ruff format ash/agents/subprocess_agent.py ash/agents/orchestrator.py
5. Create tests/unit/test_subprocess_agent.py with the EXACT tests from H-5's "Tests" section
6. Run: pytest tests/unit/test_subprocess_agent.py -v

DO NOT implement any other item from PRIORITY.md.
```

---

### H-6: Add PreToolUse Hook Callback to AshLoop

```
/goal h6

You are implementing H-6 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section H-6 in /home/suraj/ash/PRIORITY.md
2. In ash/core/loop.py:
   a. Add the ToolApprovalCallback type alias and on_tool_approval parameter to AshLoop.__init__
   b. In _execute_tool_calls, replace the UI approval call with the conditional shown in H-6
3. Run: ruff check ash/core/loop.py && ruff format ash/core/loop.py
4. In tests/unit/test_loop.py, add the EXACT tests from H-6's "Tests" section
5. Run: pytest tests/unit/test_loop.py::test_on_tool_approval_callback_is_called tests/unit/test_loop.py::test_on_tool_approval_can_auto_deny -v

DO NOT implement any other item from PRIORITY.md.
```

---

### H-7: Add Missing __init__.py Files

```
/goal h7

You are implementing H-7 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section H-7 in /home/suraj/ash/PRIORITY.md
2. Create ash/tools/__init__.py with the EXACT code from H-7
3. Update ash/providers/__init__.py (it has a docstring but no exports) with the EXACT code from H-7
4. Update ash/context/__init__.py (it is empty) with the EXACT code from H-7
5. Run: ruff check ash/tools ash/providers ash/context
6. Create tests/unit/test_imports.py with the EXACT tests from H-7's "Tests" section
7. Run: pytest tests/unit/test_imports.py -v

DO NOT implement any other item from PRIORITY.md.
```

---

### H-8: Add Integration Tests for Critical Modules

```
/goal h8

You are implementing H-8 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section H-8 in /home/suraj/ash/PRIORITY.md
2. Create ALL these test files with the EXACT test code from H-8:
   a. tests/integration/test_planner.py
   b. tests/unit/test_repo_parser.py
   c. tests/unit/test_compaction.py
   d. tests/unit/test_rate_limiter.py
   e. tests/unit/test_terminal_ui.py
   NOTE: Before writing test_rate_limiter.py, check ash/providers/rate_limiter.py to get the actual TokenBucketRateLimiter constructor parameter names. Use the real names, not assumed ones.
   NOTE: Before writing test_planner.py, check ash/core/planner.py to verify parse_sprint_response exists and has the signature used in the test.
   NOTE: Before writing test_repo_parser.py, verify SymbolExtractor exists in ash/repo/parser.py.
3. Run: pytest tests/integration/test_planner.py tests/unit/test_repo_parser.py tests/unit/test_compaction.py tests/unit/test_rate_limiter.py tests/unit/test_terminal_ui.py -v

DO NOT implement any other item from PRIORITY.md.
```

---

### H-9: Architect/Editor Dual-Model Mode for Subagents

```
/goal h9

You are implementing H-9 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section H-9 in /home/suraj/ash/PRIORITY.md
2. In ash/agents/orchestrator.py:
   a. Add "mode: str = 'execute'" field to SubagentSpec dataclass
   b. Replace fanout_for_goal with the version shown in H-9 that has phases= and use_architect_mode= parameters
   c. Add the make_architect_task function shown in H-9
3. Run: ruff check ash/agents/orchestrator.py && ruff format ash/agents/orchestrator.py
4. In tests/integration/test_subagents.py, add the EXACT test from H-9's "Tests" section
5. Run: pytest tests/integration/test_subagents.py::test_architect_mode_produces_sprint_contract -v

DO NOT implement any other item from PRIORITY.md.
```

---

### H-10: Subagent Result Consolidation Step

```
/goal h10

You are implementing H-10 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section H-10 in /home/suraj/ash/PRIORITY.md
2. In ash/agents/orchestrator.py:
   a. Add "consolidated_report: AgentReport | None = None" field to OrchestratorResult dataclass
   b. Add the consolidate_results method to SubagentOrchestrator shown in H-10
   c. In run_batch, call consolidate_results after _run_agents and include consolidated_report in the return
3. Run: ruff check ash/agents/orchestrator.py && ruff format ash/agents/orchestrator.py
4. In tests/integration/test_subagents.py, add the EXACT test from H-10's "Tests" section
5. Run: pytest tests/integration/test_subagents.py::test_consolidate_reports_multiple_agents -v

DO NOT implement any other item from PRIORITY.md.
```

---

### H-11: Cross-Session Memory Recall

```
/goal h11

You are implementing H-11 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section H-11 in /home/suraj/ash/PRIORITY.md
2. In ash/core/session.py — add get_recent_session_summaries method to SessionStore:
   CRITICAL FIX: Use "with closing(self._conn) as conn, conn:" NOT "get_db_connection(self)"
   SessionStore has self._conn (the sqlite connection directly), NOT a get_db_connection() method.
   The correct pattern is:
   def get_recent_session_summaries(self, project_path: str, limit: int = 5) -> list[str]:
       with closing(self._conn) as conn, conn:
           rows = conn.execute(
               """SELECT s.session_id, GROUP_CONCAT(m.content, '\n') as messages
                  FROM sessions s
                  JOIN messages m ON m.session_id = s.session_id
                  WHERE s.project_path = ?
                  GROUP BY s.session_id
                  ORDER BY s.created_at DESC
                  LIMIT ?""",
               (project_path, limit),
           ).fetchall()
       return [row["messages"] for row in rows]
3. In ash/core/loop.py:
   a. Add enable_memory_recall parameter to AshLoop.__init__
   b. Add _build_memory_context method
   c. In start_session(), add the memory recall block BEFORE create_session (shown in H-11)
4. Run: ruff check ash/core/session.py ash/core/loop.py && ruff format ash/core/session.py ash/core/loop.py
5. In tests/unit/test_session.py, add the EXACT test from H-11's "Tests" section
6. Run: pytest tests/unit/test_session.py::test_get_recent_session_summaries -v

DO NOT implement any other item from PRIORITY.md.
```

---

## MEDIUM Priority

---

### M-1: Implement .mcp.json MCP Server Configuration

```
/goal m1

You are implementing M-1 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-1 in /home/suraj/ash/PRIORITY.md
2. Create ash/mcp/server.py with the EXACT code from M-1's "How to Fix"
3. In ash/config.py: add the mcp_servers field loaded from .mcp.json
4. Run: ruff check ash/mcp/server.py && ruff format ash/mcp/server.py
5. Create tests/unit/test_mcp.py with the EXACT tests from M-1's "Tests" section
6. Run: pytest tests/unit/test_mcp.py -v

DO NOT implement any other item from PRIORITY.md.
```

---

### M-2: Hook System with Matcher-Based Registration

```
/goal m2

You are implementing M-2 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-2 in /home/suraj/ash/PRIORITY.md
2. Create ash/hooks/registry.py with the EXACT code from M-2's "How to Fix" — this includes:
   - Hook ABC class
   - PreToolUseHook, PostToolUseHook, SessionStartHook dataclasses
   - HookRegistry class with register_* and fire_* methods
   - get_injected_prompt() method (IMPORTANT: must be included per M-2 spec)
3. Run: ruff check ash/hooks/registry.py && ruff format ash/hooks/registry.py
4. Create tests/unit/test_hooks.py with the EXACT tests from M-2's "Tests" section
5. Run: pytest tests/unit/test_hooks.py -v

DO NOT implement any other item from PRIORITY.md.
```

---

### M-3: Add SessionStart Hook for System Prompt Injection

```
/goal m3

You are implementing M-3 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-3 in /home/suraj/ash/PRIORITY.md
2. This builds on M-2 (ash/hooks/registry.py must exist first with HookRegistry)
3. In ash/core/loop.py:
   a. Add hooks: HookRegistry | None = None parameter to AshLoop.__init__
   b. In start_session(), before creating a new session, add the hooks.fire_session_start() block shown in M-3
4. Run: ruff check ash/core/loop.py && ruff format ash/core/loop.py
5. In tests/unit/test_hooks.py, add the EXACT test from M-3's "Tests" section
6. Run: pytest tests/unit/test_hooks.py::test_session_start_hook_injects_prompt -v

DO NOT implement any other item from PRIORITY.md.
```

---

### M-4: Add PostToolUse Hook for Logging/Metrics

```
/goal m4

You are implementing M-4 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-4 in /home/suraj/ash/PRIORITY.md
2. This builds on M-2 (ash/hooks/registry.py must exist first)
3. In ash/core/loop.py _execute_tool_calls: after "tool_result: ToolResult = await tool.run(**arguments)", add:
   "if self.hooks is not None: await self.hooks.fire_post_tool(tool_name, arguments, tool_result)"
4. Run: ruff check ash/core/loop.py && ruff format ash/core/loop.py
5. In tests/unit/test_hooks.py, add the EXACT test from M-4's "Tests" section
6. Run: pytest tests/unit/test_hooks.py::test_post_tool_hook_fires -v

DO NOT implement any other item from PRIORITY.md.
```

---

### M-5: Plugin Manifest Schema (plugin.json)

```
/goal m5

You are implementing M-5 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-5 in /home/suraj/ash/PRIORITY.md
2. Create ash/plugins/manifest.py with the EXACT code from M-5's "How to Fix"
3. Run: ruff check ash/plugins/manifest.py && ruff format ash/plugins/manifest.py
4. Create tests/unit/test_plugin_manifest.py with the EXACT tests from M-5's "Tests" section
5. Run: pytest tests/unit/test_plugin_manifest.py -v

DO NOT implement any other item from PRIORITY.md.
```

---

### M-6: In-Memory Context Object for Turn-Level State Sharing

```
/goal m6

You are implementing M-6 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-6 in /home/suraj/ash/PRIORITY.md
2. Create ash/context/turn.py with the EXACT code from M-6's "How to Fix"
3. In ash/core/loop.py:
   a. Add turn_context: TurnContext | None = None parameter to AshLoop.__init__
   b. Initialize it at the start of run_turn with the TurnContext shown in M-6
4. Run: ruff check ash/context/turn.py ash/core/loop.py && ruff format ash/context/turn.py ash/core/loop.py
5. Create tests/unit/test_turn_context.py with the EXACT tests from M-6's "Tests" section
6. Run: pytest tests/unit/test_turn_context.py -v

DO NOT implement any other item from PRIORITY.md.
```

---

### M-7: Per-Agent Sandbox Modes

```
/goal m7

You are implementing M-7 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-7 in /home/suraj/ash/PRIORITY.md
2. In ash/agents/subprocess_agent.py: add sandbox_tier parameter to __init__ and store it
3. In ash/agents/orchestrator.py:
   a. Add "from ash.sandbox._base import SANDBOX_TIER_SCOPED, SANDBOX_TIER_SANDBOX_EXEC, SANDBOX_TIER_DOCKER" import
   b. Add sandbox_tier field to SubagentSpec with default SANDBOX_TIER_SCOPED
   c. In SubagentOrchestrator.fanout_for_goal, add the ROLE_SANDBOX_TIER dict and set spec.sandbox_tier per role
4. Run: ruff check ash/agents/subprocess_agent.py ash/agents/orchestrator.py && ruff format ash/agents/subprocess_agent.py ash/agents/orchestrator.py
5. In tests/unit/test_subprocess_agent.py, add the EXACT tests from M-7's "Tests" section
6. Run: pytest tests/unit/test_subprocess_agent.py::test_subagent_spec_sandbox_tier_default tests/unit/test_subprocess_agent.py::test_subagent_spec_sandbox_tier_override -v

DO NOT implement any other item from PRIORITY.md.
```

---

### M-8: Agent-to-Agent IPC Messages

```
/goal m8

You are implementing M-8 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-8 in /home/suraj/ash/PRIORITY.md
2. In ash/agents/shared_state.py:
   a. Add the send_to_agent method shown in M-8's "How to Fix"
   b. The broadcast method ALREADY EXISTS at lines 291-311 of shared_state.py — do NOT add it again. Only add send_to_agent if it doesn't exist yet.
   c. If adding send_to_agent, note that broadcast returns int (count of messages sent), not None
3. Run: ruff check ash/agents/shared_state.py && ruff format ash/agents/shared_state.py
4. In tests/unit/test_shared_state.py, add the EXACT tests from M-8's "Tests" section
   IMPORTANT: shared_state.broadcast() and shared_state.register_agent() are SYNC methods. Write tests WITHOUT async/await for these:
   def test_agent_to_agent_messages(shared_state):
       shared_state.register_agent("agent-a", role="general")
       ...
       shared_state.send_to_agent("agent-a", "agent-b", "test", "hello from a")
5. Run: pytest tests/unit/test_shared_state.py::test_agent_to_agent_messages tests/unit/test_shared_state.py::test_broadcast -v

DO NOT implement any other item from PRIORITY.md.
```

---

### M-9: Retry with Exponential Backoff for Transient Tool Failures

> Historical and superseded. Do not implement these instructions: Ash now
> forbids automatic replay after tool dispatch because the prior operation may
> already have performed an external side effect.

```
/goal m9

You are implementing M-9 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-9 in /home/suraj/ash/PRIORITY.md
2. In ash/core/loop.py:
   a. Add "import asyncio" at the top of the file with the other imports
   b. In _execute_tool_calls, replace the direct await tool.run() with the _execute_with_retry function shown in M-9
   c. The retry loop uses MAX_RETRIES=2 and exponential backoff: 1s, 2s, 4s
3. Run: ruff check ash/core/loop.py && ruff format ash/core/loop.py
4. In tests/unit/test_loop.py, add the EXACT test from M-9's "Tests" section
5. Run: pytest tests/unit/test_loop.py::test_retry_on_transient_failure -v

DO NOT implement any other item from PRIORITY.md.
```

---

### M-10: Recovery Suggestions When Circuit Breaker Trips

```
/goal m10

You are implementing M-10 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-10 in /home/suraj/ash/PRIORITY.md
2. In ash/core/recovery.py:
   a. Add the CircuitBreakerError dataclass shown in M-10
   b. Add the SUGGESTIONS dict and suggest_alternatives method to CircuitBreaker
   c. In record_failure, raise CircuitBreakerError when failure_counter >= max_failures
3. In ash/core/loop.py:
   a. Add CircuitBreakerError to imports
   b. In _execute_tool_calls, add "except CircuitBreakerError: raise" (re-raise immediately to halt turn)
4. Run: ruff check ash/core/recovery.py ash/core/loop.py && ruff format ash/core/recovery.py ash/core/loop.py
5. Create tests/unit/test_recovery.py with the EXACT tests from M-10's "Tests" section
6. Run: pytest tests/unit/test_recovery.py -v

DO NOT implement any other item from PRIORITY.md.
```

---

### M-11: Implement OpenAI Provider

```
/goal m11

You are implementing M-11 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-11 in /home/suraj/ash/PRIORITY.md
2. Create ash/providers/openai.py with the EXACT code from M-11's "How to Fix"
3. Update ash/providers/__init__.py to export OpenAIProvider
4. Update ash/__main__._build_provider() to support provider="openai" with the code from M-11
5. Run: ruff check ash/providers/openai.py ash/__main__.py && ruff format ash/providers/openai.py
6. In tests/unit/test_providers.py, add the EXACT tests from M-11's "Tests" section
7. Run: pytest tests/unit/test_providers.py::test_openai_provider_initializes tests/unit/test_providers.py::test_openai_provider_stream_chat_signature -v

DO NOT implement any other item from PRIORITY.md.
```

---

### M-12: Implement Ollama Provider for Local Models

```
/goal m12

You are implementing M-12 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-12 in /home/suraj/ash/PRIORITY.md
2. Create ash/providers/ollama.py with the EXACT code from M-12's "How to Fix"
3. Update ash/providers/__init__.py to export OllamaProvider
4. Update ash/__main__._build_provider() to support provider="ollama" with the code from M-12
5. Run: ruff check ash/providers/ollama.py && ruff format ash/providers/ollama.py
6. In tests/unit/test_providers.py, add the EXACT test from M-12's "Tests" section
7. Run: pytest tests/unit/test_providers.py::test_ollama_provider_initializes -v

DO NOT implement any other item from PRIORITY.md.
```

---

### M-13: Context Filters for RepoMap

```
/goal m13

You are implementing M-13 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-13 in /home/suraj/ash/PRIORITY.md
2. In ash/config.py: add repo_map_exclude_patterns field with the defaults shown in M-13
3. In ash/repo/repomap.py:
   a. Add "import fnmatch" at the top
   b. Add exclude_patterns to RepoMap.__init__
   c. Add _is_excluded helper method
   d. In rank(), filter results with: return [p for p in ranked if not self._is_excluded(p)]
   CRITICAL: Do NOT use super().rank() — RepoMap has no parent class with a rank() method. Copy the existing rank() implementation and filter its output.
4. Run: ruff check ash/repo/repomap.py ash/config.py && ruff format ash/repo/repomap.py
5. In tests/unit/test_repomap.py, add the EXACT test from M-13's "Tests" section
6. Run: pytest tests/unit/test_repomap.py::test_repomap_excludes_node_modules -v

DO NOT implement any other item from PRIORITY.md.
```

---

### M-14: Memory Nudge Intervals

```
/goal m14

You are implementing M-14 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-14 in /home/suraj/ash/PRIORITY.md
2. In ash/core/loop.py:
   a. Add memory_nudge_interval parameter to AshLoop.__init__
   b. Add _build_memory_nudge method
   c. In run_turn, add the nudge check INSIDE the for loop (after processing tool results from _execute_tool_calls), NOT after the loop
3. Run: ruff check ash/core/loop.py && ruff format ash/core/loop.py

DO NOT implement any other item from PRIORITY.md.
```

---

### M-15: Skill Nudge Intervals

```
/goal m15

You are implementing M-15 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-15 in /home/suraj/ash/PRIORITY.md
2. In ash/core/loop.py:
   a. Add tools_registry: ToolRegistry | None = None parameter to AshLoop.__init__
   b. Add skill_nudge_interval parameter to AshLoop.__init__
   c. Add _build_skill_nudge method
   d. In _execute_tool_calls, add the skill nudge check INSIDE the tool execution loop (after tool.run()), NOT after the turn loop
3. Run: ruff check ash/core/loop.py && ruff format ash/core/loop.py

DO NOT implement any other item from PRIORITY.md.
```

---

### M-16: Whole-Edit Format for Code Modifications

```
/goal m16

You are implementing M-16 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-16 in /home/suraj/ash/PRIORITY.md
2. In ash/tools/filesystem.py: add WholeEditTool and WholeEditArgs with the EXACT code from M-16's "How to Fix"
3. In ash/__main__._build_tools(): add "whole_edit": WholeEditTool(safety_guard) to the dict
4. Run: ruff check ash/tools/filesystem.py ash/__main__.py && ruff format ash/tools/filesystem.py
5. In tests/unit/test_tools.py, add the EXACT test from M-16's "Tests" section
6. Run: pytest tests/unit/test_tools.py::test_whole_edit_tool -v

DO NOT implement any other item from PRIORITY.md.
```

---

## LOW Priority

---

### L-1: ClawHub-Style Remote Skill Registry

```
/goal l1

You are implementing L-1 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section L-1 in /home/suraj/ash/PRIORITY.md
2. Create ash/plugins/registry.py with the EXACT code from L-1's "How to Fix"
3. Run: ruff check ash/plugins/registry.py && ruff format ash/plugins/registry.py

DO NOT implement any other item from PRIORITY.md.
```

---

### L-2: Environment Variable Expansion in Plugin Configs

```
/goal l2

You are implementing L-2 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section L-2 in /home/suraj/ash/PRIORITY.md
2. This builds on M-1 (ash/mcp/server.py must exist first)
3. In ash/mcp/server.py: add expand_env_vars function and __post_init__ to MCPServerConfig with the EXACT code from L-2's "How to Fix"
   NOTE: MCPServerConfig is frozen=True, so __post_init__ uses object.__setattr__ to set fields — this is correct and necessary
4. Run: ruff check ash/mcp/server.py && ruff format ash/mcp/server.py

DO NOT implement any other item from PRIORITY.md.
```

---

### L-3: Plugin Dependency Management

```
/goal l3

You are implementing L-3 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section L-3 in /home/suraj/ash/PRIORITY.md
2. This builds on M-5 (ash/plugins/manifest.py must exist first)
3. In ash/plugins/manifest.py: add dependencies field and check_dependencies method with the EXACT code from L-3's "How to Fix"
4. Run: ruff check ash/plugins/manifest.py && ruff format ash/plugins/manifest.py

DO NOT implement any other item from PRIORITY.md.
```

---

### L-4: MCP CLI Management Commands

```
/goal l4

You are implementing L-4 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section L-4 in /home/suraj/ash/PRIORITY.md
2. In ash/__main__.py:
   a. First add "subparsers = parser.add_subparsers(dest='command')" after the main parser is created
   b. Then add "mcp_subparser = subparsers.add_parser('mcp')"
   c. Add the args.command == "mcp" handling block shown in L-4
3. Run: ruff check ash/__main__.py && ruff format ash/__main__.py

DO NOT implement any other item from PRIORITY.md.
```

---

### L-5: SSE and WebSocket MCP Transport Support

```
/goal l5

You are implementing L-5 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section L-5 in /home/suraj/ash/PRIORITY.md
2. Create ash/mcp/client.py with the EXACT code from L-5's "How to Fix"
3. Run: ruff check ash/mcp/client.py && ruff format ash/mcp/client.py

DO NOT implement any other item from PRIORITY.md.
```

---

### L-6: RepoMap Dependency Graph Visualization

```
/goal l6

You are implementing L-6 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section L-6 in /home/suraj/ash/PRIORITY.md
2. In ash/repo/repomap.py: add to_dot_graph method with the EXACT code from L-6's "How to Fix"
3. Run: ruff check ash/repo/repomap.py && ruff format ash/repo/repomap.py

DO NOT implement any other item from PRIORITY.md.
```

---

### L-7: Context Window Usage Indicator

```
/goal l7

You are implementing L-7 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section L-7 in /home/suraj/ash/PRIORITY.md
2. In ash/ui/terminal.py:
   a. Add show_token_meter parameter to TerminalUI.__init__
   b. Add the token progress bar initialization shown in L-7
   c. Add _render_token_meter method — implement the full method with actual progress bar rendering code (not just a placeholder)
   d. Update _render to show the token bar alongside the main content
3. Run: ruff check ash/ui/terminal.py && ruff format ash/ui/terminal.py

DO NOT implement any other item from PRIORITY.md.
```

---

### L-8: JSON-Configurable Dynamic Agent Spawning

```
/goal l8

You are implementing L-8 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section L-8 in /home/suraj/ash/PRIORITY.md
2. In ash/agents/orchestrator.py:
   a. Add from_dict classmethod to SubagentSpec with the EXACT code from L-8
   b. Include all fields: mode (from H-9) and sandbox_tier (from M-7) if those exist:
      mode=data.get("mode", "execute"),
      sandbox_tier=data.get("sandbox_tier", 1),
   c. Add run_batch_from_config method to SubagentOrchestrator with the EXACT code from L-8
3. Run: ruff check ash/agents/orchestrator.py && ruff format ash/agents/orchestrator.py

DO NOT implement any other item from PRIORITY.md.
```

---

### L-9: Agent Workspace Isolation

```
/goal l9

You are implementing L-9 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section L-9 in /home/suraj/ash/PRIORITY.md
2. In ash/agents/orchestrator.py: add workspace_root field to SubagentSpec with the EXACT code from L-9
3. In ash/agents/subprocess_agent.py: in spawn_subprocess(), add the ASH_WORKSPACE_ROOT env var with the EXACT code from L-9
4. Run: ruff check ash/agents/subprocess_agent.py ash/agents/orchestrator.py && ruff format ash/agents/subprocess_agent.py ash/agents/orchestrator.py

DO NOT implement any other item from PRIORITY.md.
```

---

### L-10: Agent-Created Self-Improving Skills

```
/goal l10

You are implementing L-10 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section L-10 in /home/suraj/ash/PRIORITY.md
2. Create a new module (e.g., ash/tools/skill_writer.py) with:
   - A SkillWriter class that can write .skill.md files to disk
   - Integration with the agent success callback system
   The exact code should be derived from L-10's "How to Fix" section — create a skill_writer.py file with the SkillWriter class and the on_agent_success function
3. Run: ruff check ash/tools/skill_writer.py && ruff format ash/tools/skill_writer.py

DO NOT implement any other item from PRIORITY.md.
```

---

### L-11: Memory as Markdown Files

```
/goal l11

You are implementing L-11 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section L-11 in /home/suraj/ash/PRIORITY.md
2. Create ash/memory/markdown_store.py with the EXACT code from L-11's "How to Fix"
3. Run: ruff check ash/memory/markdown_store.py && ruff format ash/memory/markdown_store.py

DO NOT implement any other item from PRIORITY.md.
```

---

### L-12: Continuous Autonomous Agent Mode

```
/goal l12

You are implementing L-12 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section L-12 in /home/suraj/ash/PRIORITY.md
2. In ash/core/loop.py:
   a. Add continuous_mode: bool = False and max_continuous_turns: int = 10 to AshLoop.__init__
   b. In run_turn, after "if not tool_calls:" add the continuous mode follow-up shown in L-12
3. Run: ruff check ash/core/loop.py && ruff format ash/core/loop.py

DO NOT implement any other item from PRIORITY.md.
```

---

### L-13: Streamable HTTP Extension for Remote Control

```
/goal l13

You are implementing L-13 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section L-13 in /home/suraj/ash/PRIORITY.md
2. Create ash/server/http.py with the EXACT code from L-13's "How to Fix"
   CRITICAL: The FastAPI app needs ash_loop wired in. Add at the bottom of the file:
   app.state.ash_loop = ash_loop  # wire the loop into app state for the /turn endpoint to access
   Or inside a startup event:
   @app.on_event("startup")
   async def startup():
       app.state.ash_loop = ash_loop
3. Run: ruff check ash/server/http.py && ruff format ash/server/http.py

DO NOT implement any other item from PRIORITY.md.
```

---

### L-14: JSON-RPC Client/Server Protocol

```
/goal l14

You are implementing L-14 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section L-14 in /home/suraj/ash/PRIORITY.md
2. Create ash/server/jsonrpc.py with the EXACT code from L-14's "How to Fix"
3. Run: ruff check ash/server/jsonrpc.py && ruff format ash/server/jsonrpc.py

DO NOT implement any other item from PRIORITY.md.
```

---

### L-15: LSP Diagnostics Integration

Superseded on 2026-07-13 by the managed LSP 3.18 implementation in
`lsp/config.py`, `lsp/client.py`, `lsp/manager.py`, `lsp/middleware.py`,
`tools/lsp.py`, and `cli/lsp.py`. Do not recreate the former diagnostic-only
emitter.

---

### L-16: Autonomous --auto-approve Mode

```
/goal l16

You are implementing L-16 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section L-16 in /home/suraj/ash/PRIORITY.md
2. In ash/config.py: add safety_tier: str = "interactive" field
3. In ash/core/loop.py: in _execute_tool_calls, replace the approval check with the conditional from L-16
   When safety_tier == "auto_approve", approved = True unconditionally
4. Run: ruff check ash/config.py ash/core/loop.py && ruff format ash/config.py ash/core/loop.py

DO NOT implement any other item from PRIORITY.md.
```

---

### L-17: Agent Teams and Spawn Commands

```
/goal l17

You are implementing L-17 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section L-17 in /home/suraj/ash/PRIORITY.md
2. Create ash/tools/agent.py with ALL the following imports and code:
   import uuid
   from typing import Any
   from ash.tools.base import BaseTool, ToolResult
   from ash.agents.subprocess_agent import SubprocessAgent, make_simple_text_task

   class SpawnAgentArgs(BaseModel):
       role: str
       task: str
       agent_id: str | None = None

   class SpawnAgentTool(BaseTool):
       name = "spawn_agent"
       description = "Spawn a new subagent to handle a subtask."
       args_schema: type[BaseModel] = SpawnAgentArgs  # IMPORTANT: assign the class directly, not tuple syntax

       def __init__(self, safety_guard: SafetyGuard, shared_state: "SharedState") -> None:
           super().__init__(safety_guard)
           self._shared_state = shared_state  # Store shared_state so run() can use it
           self._shared_state = shared_state

       async def run(self, **kwargs: Any) -> ToolResult:
           args = SpawnAgentArgs(**kwargs)
           agent = SubprocessAgent(
               agent_id=args.agent_id or f"spawned-{uuid.uuid4().hex[:8]}",
               role=args.role, task=args.task,
               shared_state=self._shared_state,
               runner=make_simple_text_task("done"),
           )
           report = await agent.run_in_process()
           return ToolResult(success=report.success, output=report.summary)
3. Register SpawnAgentTool in the tool registry for the orchestrator agent (update _build_tools in __main__.py to include it)
4. Run: ruff check ash/tools/agent.py && ruff format ash/tools/agent.py

DO NOT implement any other item from PRIORITY.md.
```

---

### H-12: Wire Vector Memory Pipeline into AshLoop

```
/goal h12

You are implementing H-12 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section H-12 in /home/suraj/ash/PRIORITY.md
2. Update ash/memory/__init__.py to export all memory classes (see H-12's "How to Fix" step 1 — exports VectorSearchPipeline, ChromaIndex, InMemoryVectorIndex, all Embedding adapters, MarkdownMemoryStore, FTS5Index)
3. In ash/config.py: add memory_backend, chroma_persist_dir, embedding_provider, openai_api_key, onnx_model_path fields
4. In ash/core/loop.py:
   a. Add enable_semantic_memory, memory_backend, embedding_provider, openai_api_key, onnx_model_path, chroma_persist_dir params to __init__
   b. Initialize VectorSearchPipeline in __init__ when enable_semantic_memory=True
   c. Add index_file_for_memory(), semantic_search(), _chunk_file() methods
   d. In run_turn, inject semantic search results into the system prompt before provider call
5. Run: ruff check ash/memory/__init__.py ash/config.py ash/core/loop.py && ruff format
6. Create tests/unit/test_memory_pipeline.py with the EXACT tests from H-12's "Tests" section
7. Run: pytest tests/unit/test_memory_pipeline.py -v

DO NOT implement any other item from PRIORITY.md.
```

---

### H-13: MCP Server Lifecycle Management (Start/Stop Servers)

```
/goal h13

You are implementing H-13 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section H-13 in /home/suraj/ash/PRIORITY.md
2. In ash/mcp/server.py:
   a. Add MCPServerInstance dataclass with name, config, process, transport fields
   b. Add MCPServerManager class with start_server(), stop_server(), stop_all(), get_server(), list_servers() methods
   c. start_server() spawns the subprocess for stdio transport, stores config for network transports
3. In ash/core/loop.py:
   a. Add mcp_config_path: Path | None param to __init__
   b. Initialize MCPServerManager in __init__ and start all servers from .mcp.json
   c. Add __aenter__ and __aexit__ for async context manager
   d. In __del__ (or close()), call stop_all()
4. Run: ruff check ash/mcp/server.py ash/core/loop.py && ruff format
5. Create tests/unit/test_mcp_manager.py with the EXACT tests from H-13's "Tests" section
6. Run: pytest tests/unit/test_mcp_manager.py -v

DO NOT implement any other item from PRIORITY.md.
```

---

### M-17: Implement `check_dependencies()` in PluginManifest

```
/goal m17

You are implementing M-17 from /home/suraj/ash/PRIORITY.md.

Steps:
1. Read section M-17 in /home/suraj/ash/PRIORITY.md
2. In ash/plugins/manifest.py:
   a. Add imports: importlib, importlib.metadata, importlib.util, packaging.version, packaging.specifiers
   b. Replace the empty check_dependencies() stub with the FULL implementation from M-17's "How to Fix" that:
      - Iterates over self.dependencies
      - Uses importlib.util.find_spec() to check if package exists
      - Uses packaging.version.satisfied() to check version specifiers
      - Returns a list of error strings for unmet dependencies
3. Run: ruff check ash/plugins/manifest.py && ruff format ash/plugins/manifest.py
4. In tests/unit/test_plugin_manifest.py, add the EXACT tests from M-17's "Tests" section
5. Run: pytest tests/unit/test_plugin_manifest.py -v

DO NOT implement any other item from PRIORITY.md.
```

---

## Phase Commands

Run multiple items in order:

```
/goal phase1
Read /home/suraj/ash/PRIORITY.md. Implement Phase 1 ONLY: H-7 (init files) then H-8 (missing tests). Do each one using its /goal command above. Do NOT implement any other item.
```

```
/goal phase2
Read /home/suraj/ash/PRIORITY.md. Implement Phase 2 ONLY: H-1 (concurrency bug) then H-2 (async SharedState). Do each one using its /goal command above. Do NOT implement any other item.
```

```
/goal phase3
Read /home/suraj/ash/PRIORITY.md. Implement Phase 3 ONLY in order: H-3, H-4, H-6, M-2, M-3, M-4, M-16. Do each one using its /goal command above. Do NOT implement any other item.
```

```
/goal phase4
Read /home/suraj/ash/PRIORITY.md. Implement Phase 4 ONLY: H-5 (tool allowlist enforcement). Do it using its /goal command above. Do NOT implement any other item.
```

```
/goal phase5
Read /home/suraj/ash/PRIORITY.md. Implement Phase 5 ONLY in order: H-9, H-10, M-7, M-8, L-8, L-9, L-17. Do each one using its /goal command above. Do NOT implement any other item.
```

```
/goal phase6
Read /home/suraj/ash/PRIORITY.md. Implement Phase 6 ONLY: M-11 (OpenAI provider) then M-12 (Ollama provider). Do each one using its /goal command above. Do NOT implement any other item.
```

```
/goal phase7
Read /home/suraj/ash/PRIORITY.md. Implement Phase 7 ONLY in order: M-1 (.mcp.json) then M-4 (PostToolUse hook) then M-5 (plugin manifest). Do each one using its /goal command above. Do NOT implement any other item.
```

```
/goal phase8
Read /home/suraj/ash/PRIORITY.md. Implement Phase 8 ONLY in order: H-12 (wire vector memory) then H-11 (memory recall) then M-13, M-14, M-15 then L-10, L-11. Do each one using its /goal command above. Do NOT implement any other item.
```

```
/goal phase9
Read /home/suraj/ash/PRIORITY.md. Implement Phase 9 ONLY in order: M-9 (retry backoff) then M-10 (circuit breaker suggestions) then M-17 (check_dependencies) then L-16 (auto-approve). Do each one using its /goal command above. Do NOT implement any other item.
```

```
/goal phase10
Read /home/suraj/ash/PRIORITY.md. Implement Phase 10 ONLY in order: H-13 (MCP server lifecycle) then M-6 (TurnContext) then L-7 (token meter) then L-6 (RepoMap graph). Do each one using its /goal command above. Do NOT implement any other item.
```

```
/goal phase11
Read /home/suraj/ash/PRIORITY.md. Implement Phase 11 ONLY in order: L-13 (HTTP) then L-14 (JSON-RPC) then L-15 (LSP) then L-12 (continuous mode). Do each one using its /goal command above. Do NOT implement any other item.
```

```
/goal phase12
Read /home/suraj/ash/PRIORITY.md. Implement Phase 12 ONLY in order: M-5 (plugin manifest) then L-1, L-2, L-3. Do each one using its /goal command above. Do NOT implement any other item.
```

---

## Group Commands

```
/goal high
Read /home/suraj/ash/PRIORITY.md. Implement all HIGH priority items in order: H-1, H-2, H-3, H-4, H-5, H-6, H-7, H-8, H-9, H-10, H-11, H-12, H-13. Use each item's /goal command above. Do NOT implement MEDIUM or LOW items.
```

```
/goal medium
Read /home/suraj/ash/PRIORITY.md. Implement all MEDIUM priority items in order: M-1 through M-17. Use each item's /goal command above. Do NOT implement HIGH or LOW items.
```

```
/goal low
Read /home/suraj/ash/PRIORITY.md. Implement all LOW priority items in order: L-1 through L-17. Use each item's /goal command above. Do NOT implement HIGH or MEDIUM items.
```

```
/goal all
Read /home/suraj/ash/PRIORITY.md. Implement everything: run Phase 1 through Phase 12 in order using the phase commands above. Implements all 47 items.
```

```
/goal safety
Read /home/suraj/ash/PRIORITY.md. Implement safety items: H-5, H-6, M-9, M-10, L-16. Use each item's /goal command above.
```

```
/goal providers
Read /home/suraj/ash/PRIORITY.md. Implement provider items: M-11, M-12. Use each item's /goal command above.
```

```
/goal memoryitems
Read /home/suraj/ash/PRIORITY.md. Implement memory items: H-11, M-14, M-15, L-10, L-11. Use each item's /goal command above.
```

```
/goal hooksitems
Read /home/suraj/ash/PRIORITY.md. Implement hook items: H-4, H-6, M-2, M-3, M-4. Use each item's /goal command above.
```

```
/goal subagentsitems
Read /home/suraj/ash/PRIORITY.md. Implement subagent items: H-1, H-2, H-9, H-10, M-7, M-8. Use each item's /goal command above.
```
