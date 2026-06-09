# ASH — Incremental Goal Sprints Playbook (Sprints 1–15)

This playbook divides the construction of the Ash agent harness (Versions 1 through 7) into 15 isolated sprints. 
Copy and paste each sprint's `/goal` command *one at a time* to your builder AI. Do not paste Sprint N+1 until Sprint N is fully complete and all its tests pass.

---

## SPRINT 1: Project Setup & Core Configuration
*   **Specifications Referenced**: 
    - [ASH_MASTER_PLAN_V2.md](file:///c:/Users/Suraj%20H/Desktop/ash/ASH_MASTER_PLAN_V2.md) (Section 16: File Structure)
    - [ARCHITECTURAL_SPECIFICATION.md](file:///c:/Users/Suraj%20H/Desktop/ash/ARCHITECTURAL_SPECIFICATION.md) (Section 2.7: Configuration Settings)
*   **Prompt to Copy/Paste**:
    ```markdown
    /goal Implement Sprint 1: Project Setup & Core Configuration.
    
    ### SCOPE BOUNDARY (STRICT RULE)
    - ONLY implement the project setup, dependencies, and configuration settings loader.
    - DO NOT create folders or write code for database connections, tool actions, XML parsers, safety checks, or main loops. 
    - DO NOT create any files under the directories `ash/core/`, `ash/tools/`, `ash/safety/`, `ash/context/`, `ash/memory/`, or `ash/ui/` yet. Keep your focus strictly on this base setup.
    
    ### Tasks:
    1. Create a Python 3.12+ project structure using `uv` or `poetry`. Add these dependencies in `pyproject.toml`: `pydantic`, `pydantic-settings`, `rich`, `tiktoken`, `toml`, `pytest`, `pytest-asyncio`.
    2. Create `ash/__init__.py` and `ash/exceptions.py` for custom exception handling.
    3. Create `ash/config.py`. Read Section 2.7 of ARCHITECTURAL_SPECIFICATION.md and implement the `AshConfig` settings loader using Pydantic Settings (supporting `env_prefix="ASH_"` and loading configurations from `ash.toml` if it exists).
    4. Create a default `.env.example` file and a default configuration template `ash.toml` in the project root containing default settings.
    5. Write unit tests in `tests/unit/test_config.py` verifying that all config fields load correctly from both environment variables and `ash.toml` fallbacks. Run the tests via pytest and ensure they pass.
    ```

---

## SPRINT 2: Database Storage & WAL Persistence
*   **Specifications Referenced**:
    - [ARCHITECTURAL_SPECIFICATION.md](file:///c:/Users/Suraj%20H/Desktop/ash/ARCHITECTURAL_SPECIFICATION.md) (Section 2.2: Session Persistence, Section 3: Database Schema)
*   **Prompt to Copy/Paste**:
    ```markdown
    /goal Implement Sprint 2: Database Storage & WAL Persistence.
    
    ### PREREQUISITES & FLOW
    - Sprint 1 configuration code is fully written, tested, and passing. 
    - Continue building directly on top of the existing codebase. Do not rewrite or modify Sprint 1 configuration files.
    
    ### SCOPE BOUNDARY (STRICT RULE)
    - ONLY implement the SQLite persistence layer and thread-safe locks.
    - DO NOT write code for file-handling tools, CLI execution runs, XML streaming parsers, context builders, or loops.
    - DO NOT create files under `ash/tools/`, `ash/safety/`, `ash/context/`, or `ash/ui/` yet.
    
    ### Tasks:
    1. Read Section 2.2 and Section 3 of ARCHITECTURAL_SPECIFICATION.md.
    2. Implement Pydantic data schemas `Message`, `ToolCallRecord`, and `Session` in `ash/core/session.py`.
    3. Implement `get_db_connection(db_path)` using `check_same_thread=False` and execute connection PRAGMAs (`journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`).
    4. Implement the `write_transaction(db_path)` async lock registry to serialize write transactions across asynchronous executions.
    5. Define SQLite schemas matching Section 3.1 & 3.4 of the Architectural Specification to create tables for `sessions`, `messages`, `tool_calls`, and `audit_logs`.
    6. Write unit tests in `tests/unit/test_session.py` verifying session creation, message storage, tool logs insertion, and WAL concurrency lock behavior. Ensure all tests pass.
    ```

---

## SPRINT 3: Safety Guard Containment Checks
*   **Specifications Referenced**:
    - [ARCHITECTURAL_SPECIFICATION.md](file:///c:/Users/Suraj%20H/Desktop/ash/ARCHITECTURAL_SPECIFICATION.md) (Section 2.6: Safety Guard)
*   **Prompt to Copy/Paste**:
    ```markdown
    /goal Implement Sprint 3: Safety Guard Containment Checks.
    
    ### PREREQUISITES & FLOW
    - Assume Sprints 1 and 2 are complete and fully passing tests.
    - Continue building on top of the established codebase without editing or refactoring configuration or database session files.
    
    ### SCOPE BOUNDARY (STRICT RULE)
    - ONLY implement the safety validation logic.
    - DO NOT implement workspace file editors, token bucket structures, or prompt builders.
    - DO NOT create files under `ash/tools/`, `ash/context/`, `ash/memory/`, or `ash/ui/`.
    
    ### Tasks:
    1. Read Section 2.6 of ARCHITECTURAL_SPECIFICATION.md.
    2. Create `ash/safety/guard.py` containing the `SafetyGuard` class and custom `SafetyViolation` exception.
    3. Implement `validate_path(target_path)` to resolve and scope all files strictly inside the project root and allowed directories. Throw `SafetyViolation` on traversal bypass attempts.
    4. Implement `validate_command(command_str)` to scan command parameters against Windows and Linux blocklists.
    5. Create `ash/safety/path_scope.py` to enforce project root safety boundary checks.
    6. Write unit tests in `tests/unit/test_safety.py` mapping path escapes, allowed folders, and dangerous command blocklist triggers. Verify tests pass.
    ```

---

## SPRINT 4: Core Workspace & Subprocess Tools
*   **Specifications Referenced**:
    - [TOOL_SPECIFICATIONS.md](file:///c:/Users/Suraj%20H/Desktop/ash/TOOL_SPECIFICATIONS.md) (Section 1: Tool Base, Section 2: Core Tool Definitions, Section 4: Platform-Specific Command Safety)
*   **Prompt to Copy/Paste**:
    ```markdown
    /goal Implement Sprint 4: Core Workspace & Subprocess Tools.
    
    ### PREREQUISITES & FLOW
    - Continue building on top of Sprints 1-3. Use the `SafetyGuard` implemented in Sprint 3.
    - Do not modify or refactor config, persistence, or safety logic.
    
    ### SCOPE BOUNDARY (STRICT RULE)
    - ONLY implement the tool schemas and executors.
    - DO NOT build token compactors, FTS5 indexed search, or XML prompt tags parsers.
    - DO NOT write code under `ash/context/`, `ash/memory/` (except FTS5 migrations), or `ash/ui/`.
    
    ### Tasks:
    1. Read TOOL_SPECIFICATIONS.md. Implement the `BaseTool` abstract class and `ToolResult` schemas in `ash/tools/base.py`.
    2. Implement `read_file` (with 8KB null-byte binary detection), `write_file`, and `replace_file_content` in `ash/tools/filesystem.py`.
    3. For `replace_file_content`, ensure execution is confined strictly to start/end line bounds, line ending normalization (\r\n to \n), and atomic temporary-swap writes to protect against file corruption.
    4. Implement `run_command` in `ash/tools/command.py` executing subprocesses. Enforce the Windows CP1252/UTF-8 stream decoding fallback, timeouts (default 300s), and PowerShell `-LiteralPath` parameters.
    5. Write unit tests in `tests/unit/test_tools.py` for read, write, replace, and execution. Verify atomic edits work on files containing Windows line breaks (`\r\n`).
    ```

---

## SPRINT 5: Token Calculators & Token Bucket Rate Limiter
*   **Specifications Referenced**:
    - [CONTEXT_AND_MEMORY_SPECIFICATION.md](file:///c:/Users/Suraj%20H/Desktop/ash/CONTEXT_AND_MEMORY_SPECIFICATION.md) (Section 3: Token Counting & Rate Limiting)
*   **Prompt to Copy/Paste**:
    ```markdown
    /goal Implement Sprint 5: Token Calculators & Token Bucket Rate Limiter.
    
    ### PREREQUISITES & FLOW
    - Continue building on top of Sprints 1-4.
    - Do not modify previous config, database, safety, or tool files.
    
    ### SCOPE BOUNDARY (STRICT RULE)
    - ONLY implement token counting and token-bucket rate limits.
    - DO NOT write code for LLM client loops or terminal UI renders.
    - DO NOT create files under `ash/memory/` or `ash/ui/`.
    
    ### Tasks:
    1. Read Section 3 of CONTEXT_AND_MEMORY_SPECIFICATION.md.
    2. Create `ash/context/tokens.py`. Implement token counting adapters for Anthropic and OpenAI (using tiktoken mappings).
    3. Create `ash/providers/rate_limiter.py`. Implement the `TokenBucketRateLimiter` class using monotonic system clocks.
    4. Implement async rate backoff routines to trigger delay wait intervals when token quotas are exhausted.
    5. Write unit tests in `tests/unit/test_tokens.py` to confirm counting accuracy and rate consumption delays. Run tests and verify success.
    ```

---

## SPRINT 6: Document Chunker & FTS5 Lexical Search Pipeline
*   **Specifications Referenced**:
    - [CONTEXT_AND_MEMORY_SPECIFICATION.md](file:///c:/Users/Suraj%20H/Desktop/ash/CONTEXT_AND_MEMORY_SPECIFICATION.md) (Section 5: Semantic Memory & FTS5 Lookup)
*   **Prompt to Copy/Paste**:
    ```markdown
    /goal Implement Sprint 6: Document Chunker & FTS5 Lexical Search.
    
    ### PREREQUISITES & FLOW
    - Continue building on top of Sprints 1-5.
    - Maintain existing codebase integrity; do not change previous modules.
    
    ### SCOPE BOUNDARY (STRICT RULE)
    - ONLY implement sliding-window chunking and FTS5 database retrieval.
    - DO NOT write LLM client calls, loop orchestration, or XML command parsers.
    - DO NOT write code in `ash/ui/` or `ash/core/loop.py`.
    
    ### Tasks:
    1. Read Section 5 of CONTEXT_AND_MEMORY_SPECIFICATION.md.
    2. Create `ash/context/compaction.py`. Implement a Line-Preserving Sliding Window chunker (30 lines window, 5 lines overlap).
    3. Create `ash/memory/fts5.py`. Implement the FTS5 virtual table schemas (`fts_index`, `document_metadata`).
    4. Implement `query_lexical_fallback()` using SQLite BM25 ranking.
    5. Write unit tests in `tests/unit/test_fts5.py` verifying document indexing and lexical keyword matching. Run tests and ensure they pass.
    ```

---

## SPRINT 7: Streaming XML Parser State Machine
*   **Specifications Referenced**:
    - [SYSTEM_PROMPTS_AND_TEMPLATES.md](file:///c:/Users/Suraj%20H/Desktop/ash/SYSTEM_PROMPTS_AND_TEMPLATES.md) (Section 3: Core XML Tagging Specifications)
*   **Prompt to Copy/Paste**:
    ```markdown
    /goal Implement Sprint 7: Streaming XML Parser State Machine.
    
    ### PREREQUISITES & FLOW
    - Continue building on top of Sprints 1-6.
    - Maintain base settings, tools, DB and safety libraries.
    
    ### SCOPE BOUNDARY (STRICT RULE)
    - ONLY implement the stream token parser.
    - DO NOT connect it to the LLM client or UI terminals yet.
    - DO NOT create or edit files in `ash/core/loop.py` or `ash/ui/terminal.py`.
    
    ### Tasks:
    1. Read Section 3.2 of SYSTEM_PROMPTS_AND_TEMPLATES.md.
    2. Create `ash/ui/parser.py`. Implement the character-buffered `StreamingXMLParser` state machine.
    3. Ensure parser yields (`token`, text), (`thought`, reasoning), or (`tool_call`, dict) events *while streaming* without waiting for complete packet completions.
    4. Write unit tests in `tests/unit/test_parser.py` feeding fragmented tool sequences (e.g. half-sent tags like `<call_t`) and verify events are output correctly. Run tests and verify success.
    ```

---

## SPRINT 8: Provider Manager & Main Loop Integration (TUI)
*   **Specifications Referenced**:
    - [ARCHITECTURAL_SPECIFICATION.md](file:///c:/Users/Suraj%20H/Desktop/ash/ARCHITECTURAL_SPECIFICATION.md) (Section 2.1: Core Loop, Section 2.5: Provider, Section 4: Sequence Diagram)
    - [SYSTEM_PROMPTS_AND_TEMPLATES.md](file:///c:/Users/Suraj%20H/Desktop/ash/SYSTEM_PROMPTS_AND_TEMPLATES.md) (Section 1: Primary System Prompt, Section 2: Mode-Specific Prompts)
*   **Prompt to Copy/Paste**:
    ```markdown
    /goal Implement Sprint 8: Provider Manager & Main Loop Integration.
    
    ### PREREQUISITES & FLOW
    - Combine all modules from Sprints 1-7.
    - Build the core loop orchestration that connects all previously written components.
    
    ### SCOPE BOUNDARY (STRICT RULE)
    - ONLY implement the V1 Minimal loop.
    - DO NOT implement tree-sitter PPR maps, git auto-commits, or subagent tasks.
    - DO NOT create files under `ash/repo/`.
    
    ### Tasks:
    1. Implement the LLM API adapter in `ash/providers/base.py` and `ash/providers/anthropic.py` mapping streaming client configurations.
    2. Create `ash/core/loop.py` class `AshLoop` running the cycle: Ingest user prompt -> build context -> stream chat -> parse XML -> call tools -> save DB -> compact history. Ingest the prompt structures from Sections 1 and 2 of SYSTEM_PROMPTS_AND_TEMPLATES.md.
    3. Create `ash/core/recovery.py` implementing the consecutive tool error `CircuitBreaker`.
    4. Create `ash/ui/terminal.py` console using `rich.live` to stream thoughts and outputs. Implement key intercept confirmations for tool runs.
    5. Expose entry point in `ash/__main__.py` to launch via `python -m ash`.
    6. Run integration tests in `tests/integration/test_loop.py` and ensure they pass.
    ```

---

## SPRINT 9: Git Commits & Personalized PageRank Repo Maps (V2 Upgrade)
*   **Specifications Referenced**:
    - [CONTEXT_AND_MEMORY_SPECIFICATION.md](file:///c:/Users/Suraj%20H/Desktop/ash/CONTEXT_AND_MEMORY_SPECIFICATION.md) (Section 4: Repository Mapping)
    - [TOOL_SPECIFICATIONS.md](file:///c:/Users/Suraj%20H/Desktop/ash/TOOL_SPECIFICATIONS.md) (Section 2.6: Git Tools)
*   **Prompt to Copy/Paste**:
    ```markdown
    /goal Implement Sprint 9: Git Commits & Personalized PageRank Repo Maps.
    
    ### PREREQUISITES & FLOW
    - Assume V1 (Sprints 1-8) is complete, functional, and fully tested.
    - This sprint upgrades the working loop to support V2 features.
    
    ### Tasks:
    1. Read Section 4 of CONTEXT_AND_MEMORY_SPECIFICATION.md. Create `ash/repo/repomap.py` and `ash/repo/parser.py`.
    2. Set up tree-sitter grammar parsers for Python. Extract symbols (classes, functions, imports).
    3. Implement `calculate_personalized_pagerank` transition math using numpy as specified in Section 4.2 of the Context & Memory Specification.
    4. Create `ash/tools/git.py` implementing auto-commits on turn completion.
    5. Wire PPR repo map outputs and git auto-commits into the active `AshLoop`.
    6. Verify all unit and integration tests across the workspace pass.
    ```

---

## SPRINT 10: ChromaDB & Local Vector Persistence (V3 Upgrade)
*   **Specifications Referenced**:
    - [CONTEXT_AND_MEMORY_SPECIFICATION.md](file:///c:/Users/Suraj%20H/Desktop/ash/CONTEXT_AND_MEMORY_SPECIFICATION.md) (Section 5: Semantic Memory & FTS5 Lookup Pipeline)
*   **Prompt to Copy/Paste**:
    ```markdown
    /goal Implement Sprint 10: ChromaDB & Local Vector Persistence.
    
    ### PREREQUISITES & FLOW
    - Sprints 1-9 are complete and passing. This sprint implements Version 3 vector lookup capabilities.
    
    ### SCOPE BOUNDARY (STRICT RULE)
    - ONLY implement ChromaDB collection integrations and embedding pipeline hooks.
    - DO NOT write code for process sandboxing, planning modules, or subagents.
    - DO NOT create directories under `ash/sandbox/` or `ash/agents/`.
    
    ### Tasks:
    1. Read Section 5 of CONTEXT_AND_MEMORY_SPECIFICATION.md. Create `ash/memory/vector.py`.
    2. Add `chromadb` as an optional dependency in `pyproject.toml` (`[vector]` extra).
    3. Implement the `EmbeddingAdapter` base interface. Implement `ONNXLocalEmbedding` (loading a local sentence-transformers model via ONNX runtime) and `OpenAIEmbedding` as subclasses.
    4. Set up the dynamic vector database interface for ChromaDB: index text chunks, generate embeddings, and query matches using cosine similarity equations.
    5. Write fallback handlers: if ChromaDB or ONNX loading fails, fall back to the SQLite FTS5 search index from Sprint 6.
    6. Write unit tests in `tests/unit/test_vector.py` verifying document indexing and vector queries. Ensure all tests pass.
    ```

---

## SPRINT 11: Sandboxing & Subprocess Isolation (V4 Upgrade)
*   **Specifications Referenced**:
    - [TOOL_SPECIFICATIONS.md](file:///c:/Users/Suraj%20H/Desktop/ash/TOOL_SPECIFICATIONS.md) (Section 2.5: run_command Security Policies)
    - [ASH_MASTER_PLAN_V2.md](file:///c:/Users/Suraj%20H/Desktop/ash/ASH_MASTER_PLAN_V2.md) (Version 4 roadmap)
*   **Prompt to Copy/Paste**:
    ```markdown
    /goal Implement Sprint 11: Sandboxing & Subprocess Isolation.
    
    ### PREREQUISITES & FLOW
    - Sprints 1-10 are complete. This sprint upgrades execution safety to Sandbox Tiering.
    
    ### SCOPE BOUNDARY (STRICT RULE)
    - ONLY implement command execution sandboxing wrappers.
    - DO NOT write code for planner checklists or subagent IPC states.
    - DO NOT create files under `ash/agents/` or `ash/core/planner.py`.
    
    ### Tasks:
    1. Read the Version 4 roadmap in the Master Plan and the run_command security directives. Create `ash/sandbox/manager.py`, `ash/sandbox/bwrap.py`, and `ash/sandbox/docker.py`.
    2. Implement bubblewrap (Linux namespace boundary locking) command wrappers in `ash/sandbox/bwrap.py`.
    3. Implement Docker container execution managers in `ash/sandbox/docker.py`.
    4. Implement sandboxing diagnostics: fallback gracefully to path-scoped subprocesses (Sprint 4) if Docker/bubblewrap are not installed locally on the host machine.
    5. Integrate sandboxing wrappers into `run_command` in `ash/tools/command.py`.
    6. Write unit tests in `tests/unit/test_sandbox.py` verifying file containment and command boundaries under sandboxed execution. Ensure all tests pass.
    ```

---

## SPRINT 12: Planner & Sprint Checklist Engine (V5 Upgrade)
*   **Specifications Referenced**:
    - [SYSTEM_PROMPTS_AND_TEMPLATES.md](file:///c:/Users/Suraj%20H/Desktop/ash/SYSTEM_PROMPTS_AND_TEMPLATES.md) (Section 2.1: Architect Mode Prompt)
    - [ASH_MASTER_PLAN_V2.md](file:///c:/Users/Suraj%20H/Desktop/ash/ASH_MASTER_PLAN_V2.md) (Version 5 roadmap)
*   **Prompt to Copy/Paste**:
    ```markdown
    /goal Implement Sprint 12: Planner & Sprint Checklist Engine.
    
    ### PREREQUISITES & FLOW
    - Sprints 1-11 are complete. This sprint implements Version 5 planner architectures.
    
    ### SCOPE BOUNDARY (STRICT RULE)
    - ONLY implement prompt checklist compilation and planning states.
    - DO NOT build multi-agent subprocesses or SQLite IPC worker queues.
    - DO NOT create directories under `ash/agents/`.
    
    ### Tasks:
    1. Read the Version 5 roadmap in the Master Plan and Section 2.1 of SYSTEM_PROMPTS_AND_TEMPLATES.md.
    2. Create `ash/core/planner.py` and `ash/core/sprint.py`.
    3. Implement planning decomposition: when a user inputs a major request, Ash intercepts and enters a planning phase, sending the request to the LLM (in Architect Mode) to produce a structural check list.
    4. Implement sprint execution state variables (`planning`, `active`, `complete`, `aborted`) in `sprint.py` to trace progress.
    5. Wire the planner checklist output to show in the terminal UI and ask the user for confirmation before executing.
    6. Write integration tests in `tests/integration/test_planner.py` verifying that checklist items are generated and tracked in the database. Ensure all tests pass.
    ```

---

## SPRINT 13: Subagent Process Orchestration (V6 Upgrade)
*   **Specifications Referenced**:
    - [ARCHITECTURAL_SPECIFICATION.md](file:///c:/Users/Suraj%20H/Desktop/ash/ARCHITECTURAL_SPECIFICATION.md) (Section 3.3: Subagent Shared State Schema)
    - [ASH_MASTER_PLAN_V2.md](file:///c:/Users/Suraj%20H/Desktop/ash/ASH_MASTER_PLAN_V2.md) (Version 6 roadmap)
*   **Prompt to Copy/Paste**:
    ```markdown
    /goal Implement Sprint 13: Subagent Process Orchestration.
    
    ### PREREQUISITES & FLOW
    - Sprints 1-12 are complete. This sprint implements Version 6 subagent orchestration.
    
    ### SCOPE BOUNDARY (STRICT RULE)
    - ONLY implement the subagent subprocess execution and IPC shared state.
    - DO NOT write code for the plugin loading SDK or DSPy skill evolution.
    
    ### Tasks:
    1. Read the Version 6 roadmap in the Master Plan and Section 3.3 of ARCHITECTURAL_SPECIFICATION.md.
    2. Create `ash/agents/orchestrator.py`, `ash/agents/subprocess_agent.py`, and `ash/agents/shared_state.py`.
    3. Set up the `shared_state.db` database connection hooks. Implement tables for `agent_status`, `ipc_messages`, and `sprints`.
    4. Implement subprocess spawning: the lead orchestrator launches background subagents as separate processes, running their own `AshLoop`.
    5. Implement the IPC channel using SQLite poll queries or database triggers in WAL mode to transfer JSON-RPC coordination packets.
    6. Write integration tests in `tests/integration/test_subagents.py` verifying that subagents spawn, report completion status, and communicate. Ensure all tests pass.
    ```

---

## SPRINT 14: Plugin SDK & Dynamic Skill Evolution (V7 Upgrade)
*   **Specifications Referenced**:
    - [ASH_MASTER_PLAN_V2.md](file:///c:/Users/Suraj%20H/Desktop/ash/ASH_MASTER_PLAN_V2.md) (Version 7 roadmap)
*   **Prompt to Copy/Paste**:
    ```markdown
    /goal Implement Sprint 14: Plugin SDK & Dynamic Skill Evolution.
    
    ### PREREQUISITES & FLOW
    - Sprints 1-13 are complete. This sprint implements Version 7 plugin frameworks.
    
    ### Tasks:
    1. Read the Version 7 roadmap in the Master Plan. Create `ash/tools/registry.py` updates to dynamically load external skill files.
    2. Implement the Skill compiler: parse markdown recipe files (like those under `skills/`) into active Pydantic tool structures.
    3. Implement self-extension: when told to build a new capability, Ash writes a new python tool script to the `skills/` folder and loads it on-the-fly.
    4. Write unit tests in `tests/unit/test_skills.py` verifying dynamic markdown skill compilation and tool execution. Ensure all tests pass.
    ```

---

## SPRINT 15: Full Integration & E2E Validation
*   **Specifications Referenced**:
    - [ASH_MASTER_PLAN_V2.md](file:///c:/Users/Suraj%20H/Desktop/ash/ASH_MASTER_PLAN_V2.md) (Section 14: Testing Strategy)
*   **Prompt to Copy/Paste**:
    ```markdown
    /goal Implement Sprint 15: Full Integration & E2E Validation.
    
    ### PREREQUISITES & FLOW
    - All previous Sprints 1-14 are complete. This is the final verification phase.
    
    ### Tasks:
    1. Read Section 14 of the Master Plan. Implement mock provider classes in `tests/unit/test_providers.py` simulating rate limits, context overflows, and tool callbacks.
    2. Create end-to-end integration tests in `tests/e2e/test_real_session.py` to run a mock CLI session (making file edits, running test commands, committing to git).
    3. Ensure all tests across the entire repository pass with zero errors. Run standard checks: linting (`ruff`), and type compliance (`mypy`).
    4. Package the application: verify `python -m ash` runs seamlessly from any scoped project workspace.
    ```
