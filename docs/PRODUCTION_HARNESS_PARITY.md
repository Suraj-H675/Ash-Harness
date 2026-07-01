# Ash Production Harness Parity

**Scope:** cross-platform coding harness for Linux, macOS, and Windows.
Authentication is limited to API keys, custom OpenAI-compatible endpoints,
and local model runtimes. OAuth and subscription login are out of scope.

This is the authoritative product checklist. Older roadmap files describe
historical intent and do not prove that a feature works.

## Evidence Rules

- **Verified:** wired into the installed CLI and covered by an end-to-end test.
- **Partial:** code exists, but behavior, wiring, portability, or tests are incomplete.
- **Placeholder:** surface exists but returns canned data or targets a nonexistent service.
- **Missing:** no usable implementation exists.
- A feature is not complete until install, CLI/TUI, persistence, and failure paths work.

## Public Benchmarks

- Claude Code official docs: <https://code.claude.com/docs/en/overview>,
  <https://code.claude.com/docs/en/sessions>
- Codex CLI docs and source: <https://developers.openai.com/codex/cli/features>,
  <https://github.com/openai/codex>
- Gemini CLI docs and source: <https://geminicli.com/docs/>,
  <https://github.com/google-gemini/gemini-cli>
- OpenClaw source: <https://github.com/openclaw/openclaw>
- Hermes Agent source: <https://github.com/NousResearch/hermes-agent>
- OpenCode source: <https://github.com/anomalyco/opencode>
- Aider source: <https://github.com/Aider-AI/aider>

Research is clean-room: proprietary or leaked source is not used.

## 1. Installation And Setup

| Capability | Ash status | Required production behavior |
|---|---|---|
| `ash` console command | Verified locally | Wheel and sdist build; clean-environment console smoke test passes |
| `python -m ash` | Verified locally | Keep as supported fallback |
| Dependency separation | Verified locally | Lean default runtime/provider install, standardized dev group, published vector/server extras, actionable missing-extra errors, and lockfile/artifact checks |
| First-run wizard | Verified locally | No-key detection, deterministic cancel/back, endpoint retry/save-unverified choices, non-billable model discovery, secret input, atomic related settings, non-TTY guidance, and fresh-process API/local checks |
| API-key providers | Partial | Anthropic, OpenAI, Groq, DeepSeek and custom endpoints tested from fresh process |
| Local models | Partial | Ollama URL validation, installed-model discovery, health failure detail, start/pull guidance, and fresh-process configuration checks are wired; pull execution and dynamic tool-capability detection remain |
| Custom endpoints | Verified locally | Per-provider credentials are stored in mode-0600 env storage, not TOML |
| Config precedence | Verified locally | CLI > process env > trusted hierarchical project TOML > user TOML > user dotenv > defaults, with exact masked provenance and project security restrictions |
| Config migration | Verified locally | Complete legacy mapping, conflict preservation, strict destination parsing, verified private source/destination backups, exact-content migration records, and future-version refusal |
| `ash doctor` | Verified locally | Human/JSON diagnostics, extension config validation, optional endpoint connectivity probe |
| Update/version check | Verified locally | Explicit GitHub release check with no background telemetry or self-modification |
| Uninstall/reset | Verified locally | Confirmed selective reset for config, sessions, cache, or all local state |

## 2. Core Agent Runtime

| Capability | Ash status | Required production behavior |
|---|---|---|
| Streaming model loop | Partial | Cancellation-safe, typed events, clean terminal finalization |
| Native tool calling | Partial | Canonical calls translated correctly for every provider |
| XML fallback tool protocol | Partial | Keep only for models without native tools; strict parser and validation |
| Parallel tool calls | Partial | Independent read-only calls run concurrently with deterministic result order |
| Turn steering | Verified locally | Bounded durable guidance queue applies at safe iteration boundaries through interactive CLI, SDK, and HTTP |
| Interrupt/cancel | Verified locally | `/cancel` preempts live interactive turns, propagates cancellation, clears pending steering, and finalizes the turn journal |
| Retry policy | Verified locally | One harness-owned policy retries only classified pre-output transient failures, honors bounded Retry-After, adds jittered exponential backoff, preserves cancellation, emits redacted events, and disables nested SDK retries |
| Circuit breaker | Verified locally | Exhausted transient requests open provider-keyed state, fail fast during cooldown, expose `/status` and events, allow a half-open probe, and reset on success |
| Long-running process control | Verified locally | Managed start/list/poll/stdin/stop with process-tree cleanup |
| Structured output mode | Verified locally | One-shot JSON Schema injection, parsing, validation, and machine output |
| Model capability negotiation | Partial | Tools, vision, reasoning, local status, and known context/output limits; dynamic manifests remain |
| Provider failover | Verified locally | Ordered fallback before first emitted chunk with visible configured models/failures |

## 3. Context And Memory

| Capability | Ash status | Required production behavior |
|---|---|---|
| Token accounting | Partial | Provider usage, cached input, completion, and configured cache-aware costs are normalized and persisted; visible estimate uncertainty remains |
| Context budget allocation | Partial | Configurable system/tool/history/repo-map/memory budgets are enforced before compaction and shown by `/context`; file attachment budgets remain |
| Automatic compaction | Verified locally | Threshold-based extractive summary retains recent tool call/result pairs |
| Manual `/compact` | Verified locally | Forces compaction while preserving the durable transcript |
| Tool-output pruning | Verified locally | Stale large results are pruned in provider context while preserving call identity and durable data |
| Prompt caching | Verified locally | First-party Anthropic/OpenAI automatic controls and retention mapping; normalized reads/writes/hit rate persist and surface through CLI, SDK, HTTP, and JSON-RPC; custom/local endpoints remain untouched ([OpenAI](https://platform.openai.com/docs/guides/prompt-caching), [Anthropic](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)) |
| Repository map | Verified locally | Incremental Tree-sitter symbols/imports for Python, JS/JSX, TS/TSX, Go, Rust, Java, C/C++, and C#; configured/Git ignores, active-file ranking, and CLI/SDK injection |
| Project instructions | Partial | Trusted hierarchical `ASH.md` discovery plus bounded `@import` expansion and diagnostics; conflict lint remains |
| User instructions | Verified locally | Global `~/.ash/ASH.md` is loaded with a bounded size |
| Session memory | Partial | Searchable summaries rather than raw transcript concatenation |
| Project memory | Partial | Explicit project-scoped index/search/clear controls; automatic policy remains |
| Semantic memory | Partial | In-memory, Chroma, and FTS5 backend selection with index/search/delete lifecycle |
| Memory privacy | Partial | Project scope and explicit delete; retention/redacted export remain |

## 4. Sessions And Recovery

| Capability | Ash status | Required production behavior |
|---|---|---|
| Durable sessions | Verified locally | SQLite schema migrations, lifecycle, integrity checks, backup, and restore |
| Resume and continue | Verified locally | `-c` resumes the latest project session; `-r` and `/resume` accept exact IDs or case-insensitive names with ambiguity and wrong-project refusal |
| Session picker/list/search | Verified locally | Bare `-r` and `/resume` open a searchable, keyboard-navigable, project-scoped picker with metadata-only filtering and on-demand bounded transcript preview; top-level list/search also supports JSON output ([Claude Code session behavior](https://code.claude.com/docs/en/sessions)) |
| Session naming | Verified locally | Rename and stable persisted display names |
| Fork session | Verified locally | New durable session from a selected message boundary |
| Rewind conversation | Partial | Transcript rewind is durable; combined transcript-plus-file rewind remains |
| File checkpoints/undo | Verified locally | Direct edit tools capture per-turn bytes/hashes and refuse conflict overwrites |
| Session export/import | Verified locally | Versioned redacted JSONL/Markdown export and validated JSONL import |
| Crash recovery | Partial | Durable turn journal reconciles interrupted turns; interrupted-tool compensation remains |
| Session retention | Verified locally | Configurable automatic cleanup plus explicit project prune and vacuum |
| Cost/token history | Partial | Model-specific pricing, no hardcoded provider assumptions |

## 5. CLI And TUI Experience

| Capability | Ash status | Required production behavior |
|---|---|---|
| Multiline editor | Verified locally | Prompt-toolkit editor with persistent history and multiline bindings |
| Responsive full-screen TUI | Partial | Transcript-owned prompt-toolkit viewport, narrow/wide resize reflow, page navigation, live tail, terminal restoration, and inline fallback are locally verified; native Windows terminal runtime remains |
| Streaming transcript | Verified locally | Immutable semantic user/assistant/reasoning/tool/approval/status/error entries, bounded mutable live cells, rich cached Markdown, command output routing, and durable-session hydration |
| Markdown/code rendering | Verified locally | Streamed Rich Markdown with fenced-code highlighting and bounded repaint frequency |
| Diff preview | Partial | Bounded unified previews for writes/replacements/patches; side-by-side view remains |
| Approval dialog | Partial | Allow once, exact session, broad session, exact project, persisted exact deny, and verified command-prefix project scopes are wired; deny-with-feedback and a full-screen scope editor remain |
| Status line | Verified locally | Cached model, mode, branch, context budget, prompt-cache totals, cost, sandbox, session, and cwd state |
| Themes | Partial | ANSI-safe no-color mode is wired; selectable light/dark themes remain |
| Configurable keybindings | Verified locally | Cross-platform newline/editor actions with collision validation |
| Vim input mode | Verified locally | Optional Emacs or Vim prompt-toolkit editing mode |
| External editor | Verified locally | Prompt-toolkit external-editor integration |
| Image/file attachments | Verified locally | Scoped text/directory and bounded PNG/JPEG/GIF/WebP `@path` inputs, vision capability refusal, native Anthropic/OpenAI blocks, fixed image token estimates, and metadata-only persistence |
| Prompt history search | Verified locally | Persistent prompt-toolkit file history and reverse search |
| Desktop notifications | Verified locally | Opt-in completion/approval events, conservative OSC 9/BEL auto detection, tmux passthrough, TTY gating, control-character stripping, bounded optional previews, and failure isolation ([Claude Code behavior](https://code.claude.com/docs/en/terminal-config), [Codex backend](https://github.com/openai/codex/tree/main/codex-rs/tui/src/notifications)) |
| Accessibility | Partial | Dedicated screen-reader mode forces linear non-rewriting output, inline rendering, no color/token bar, reduced motion, and a reduced-dynamic prompt; native assistive-technology runtime evidence remains |

## 6. Commands And Modes

| Capability | Ash status | Required production behavior |
|---|---|---|
| Central slash-command registry | Verified locally | Metadata, aliases, parsing, completion, and stable help |
| `/help` | Partial | Filterable command reference with aliases is wired; full-screen searchable overlay remains |
| `/status` | Verified locally | Runtime model/mode/workspace/session plus persisted token, cache, and cost diagnostics |
| `/model` and `/models` | Partial | Dynamic catalogs, custom/local models, capability display |
| `/new`, `/resume`, `/sessions` | Verified locally | Durable session lifecycle and missing-ID errors |
| `/rename`, `/fork` | Verified locally | Session organization and transcript branching |
| `/compact`, `/context` | Verified locally | Context compaction, budget inspection, and last-turn cache hit metrics |
| `/cancel` | Verified locally | Cancel the active provider/tool turn while retaining already completed work |
| `/clear`, `/rewind`, `/undo` | Verified locally | New-session clear, transcript rewind, and conflict-aware file undo |
| `/diff` | Partial | Current/staged/path Git diff; per-turn checkpoint diff remains |
| `/review` | Verified locally | Bounded reviews for worktree/untracked, staged, commit, or current branch versus base |
| `/plan` mode | Verified locally | Explicit toggle, editable sprint contract, approval gate, and execution transition |
| `/permissions` | Verified locally | Inspect/change mode plus versioned project allow/ask/deny rules with stable IDs, exact/string-prefix/argv-prefix matchers, JSON output, precise removal, and legacy migration ([Claude Code rule model](https://code.claude.com/docs/en/permissions)) |
| `/sandbox` | Verified locally | Active backend, tier, capabilities, and network policy |
| `/mcp` | Partial | Top-level add/remove/list/status config supports JSON and env/header metadata; REPL status/tools/resources/prompts use live clients; auth and richer lifecycle remain |
| `/skills`, `/plugins`, `/hooks` | Verified locally | Namespaced components are inspectable; local install/enable/disable/confirmed-uninstall and live reload are wired |
| `/agents` | Verified locally | Slash status/stop/resume, persisted status/report/message inspection, live queued steering/stop consumption with delivery acknowledgement, and isolated branch list/apply/discard lifecycle |
| `/doctor` | Verified locally | Runs the local health report in-session |
| Shell escape (`!`) | Verified locally | Commands use the normal policy, sandbox, persistence, and audit path |
| File mention/completion (`@`) | Partial | Workspace file/directory completion plus bounded text, directory, and vision image expansion; fuzzy symbols and MCP resources remain |
| Custom commands | Verified locally | User/trusted-project Markdown commands, arguments, namespacing, listing, completion |

## 7. Built-In Coding Tools

| Capability | Ash status | Required production behavior |
|---|---|---|
| Read file/ranges | Partial | UTF-8 text reads, binary blocking, and stable truncation metadata exist; broader encoding/media handling remains |
| Write/create file | Partial | Atomic UTF-8 writes preserve supplied newlines and validate path/permissions; broader encoding preservation remains |
| Exact replace/edit | Verified locally | Bounded exact replacement has diff diagnostics, optional stale-read SHA-256 protection, and atomic multi-edit support |
| Patch application | Verified locally | Validated multi-file Git patch with dry-run and atomic check/apply |
| List/glob files | Verified locally | Bounded production tools with workspace scoping |
| Text/regex search | Verified locally | Ripgrep-backed bounded search with Python fallback |
| Symbol/code search | Verified locally | Read-only `find_symbol` and `find_references` tools use the incremental Tree-sitter index, exact locations, case controls, globs, and bounded results |
| Shell execution | Verified locally | Foreground and managed background commands share fail-closed sandbox injection, process-tree termination, and child env scrubbing; foreground stdout/stderr stream as call-correlated, incrementally redacted, bounded events through inline TUI, viewport, SDK, and stream-JSON surfaces |
| Git status/diff/log | Verified locally | Read-only bounded Git inspection tools |
| Git commit | Partial | Explicit staging scope, no unrelated changes, hooks/errors surfaced |
| Tests/build/lint diagnostics | Partial | Parse diagnostics and feed concise structured failures |
| Web fetch/search | Partial | Guarded HTTP(S) `web_fetch` includes optional domain allowlist; search and citation workflow remain |
| Ask-user tool | Verified locally | Typed blocking question with bounded options and explicit empty-answer failure |
| Todo/plan tracking | Partial | Persisted sprint checklists are inspectable and updatable from the top-level CLI; automatic runtime progress injection remains |

## 8. Safety, Trust, And Permissions

| Capability | Ash status | Required production behavior |
|---|---|---|
| Workspace path boundary | Verified locally | Canonical scope checks plus symlink/junction rejection; POSIX reads, directory listings, creates, no-clobber writes, edits, and patches use descriptor-anchored no-follow operations, with identity/content revalidation fallback on Windows |
| Trusted-folder prompt | Verified locally | Project instructions, skills, hooks, plugins, and MCP are trust-gated |
| Fine-grained policy engine | Partial | Deny-first then ask/allow precedence, mode circuit breakers, stable scoped rules, and conservative command-prefix parsing are enforced; tool-specific path/domain grammar and managed policy layers remain |
| Approval persistence | Verified locally | Atomic mode-0600 versioned rules, version-1 migration, stable IDs, exact session/project scopes, command-prefix project scopes, scoped denial, slash/top-level inspection, removal, and clear |
| Read-only plan mode | Verified locally | Non-read tools are denied by the central policy |
| Auto-edit mode | Verified locally | Reads/edits allowed while commands and external tools remain gated |
| Full auto mode | Verified locally | CLI startup, runtime mode switching, and SDK reject unisolated auto-approve unless the explicit unsafe override is set |
| Dry-run mode | Verified after fix | No side effects, including hooks and subagents |
| OS sandbox | Partial | Fail-closed workspace-write Bubblewrap on Linux, scoped `sandbox-exec` profile on macOS, verified Docker image fallback on all platforms, and clearly labeled approval-controlled direct mode; native Windows AppContainer remains |
| Network isolation | Verified locally | Bubblewrap network namespace, macOS profile rules, and Docker `none` networking default to blocked; `web_fetch` separately enforces public-host validation and optional domains |
| Environment scrubbing | Partial | Child processes receive a scrubbed operational env and tool outputs are redacted; explicit secret allowlist remains |
| Prompt-injection isolation | Partial | Untrusted content provenance and tool-policy enforcement |
| Secret scanning/redaction | Partial | Runtime logs, persisted messages/tool calls, tool output, and exports are redacted; pre-commit scanning remains |
| Audit log | Partial | Tool approvals, blocks, and outcomes are persisted with a tamper-evident hash chain plus CLI list/verify/export; broader non-tool decision coverage remains |
| Enterprise policy layers | Missing | System/admin policy that lower scopes cannot override |

## 9. Extensibility

| Capability | Ash status | Required production behavior |
|---|---|---|
| MCP stdio client | Partial | Initialize/negotiate/list/call tools wired; resources/prompts remain |
| MCP HTTP transport | Partial | JSON/SSE HTTP, headers, session IDs, and cleanup; OAuth remains out of scope |
| MCP tool namespacing | Verified locally | `mcp__server__tool` adapters prevent collisions |
| MCP resources/prompts | Partial | Browse in TUI and invoke/read through policy-gated model tools |
| Skills | Verified locally | Bounded standard `SKILL.md` discovery and progressive activation with trust; legacy in-process executable skills are explicit unsafe compatibility only and cannot fabricate tools |
| Plugins/extensions | Verified locally | Transactional local install/replace, enable/disable/uninstall state, dependency checks, component inventory, project trust, namespacing, and live reload |
| Plugin marketplace | Missing | Signed or trust-reviewed sources; no hardcoded nonexistent registry |
| Hooks | Partial | User/project/plugin command hooks, JSON protocol, plugin cwd, scrubbed env, timeout, and process cleanup; more lifecycle events remain |
| Custom agents | Verified locally | User/project/plugin Markdown roles, namespacing, instructions, base-role isolation, and non-escalating tool restrictions |
| Hot reload | Verified locally | Commands/completion, skills, agents, hooks, and MCP runtimes validate before live replacement |

## 10. Subagents And Work Isolation

| Capability | Ash status | Required production behavior |
|---|---|---|
| Spawn subagent tool | Verified locally | Real bounded provider-backed Ash loop, persisted reports, role-scoped tool manifests, background execution, cancellation, and no recursive spawning |
| Parallel agents | Partial | Background workers and bounded orchestrator scheduling are verified; aggregate token/cost ceilings remain |
| Agent status/output | Partial | Live slash status plus top-level persisted status/report/message inspection; full TUI view remains |
| Agent messaging | Verified locally | Typed SQLite IPC is persisted and inspectable; running workers consume steer/stop messages, acknowledge delivery, and enforce pending-message backpressure |
| Role tool policies | Verified locally | Read/search baseline, coder-only scoped edits, sandbox-required tester commands, and no recursive spawn tool |
| Worktree isolation | Verified locally | Clean-lead precondition, locked `ash-agent/*` branches, bounded commit, deterministic cleanup, safe list/cherry-pick/discard commands, and conflict abort |
| Result consolidation | Partial test harness only | Evidence-linked summaries and conflict handling |
| Agent steering/stop | Verified locally | In-process stop, persisted stop, live steering at safe iteration boundaries, delivery state, and report-based resume; isolated changes must be applied before continuation |

## 11. Automation And Integration

| Capability | Ash status | Required production behavior |
|---|---|---|
| One-shot prompt (`ash -p`) | Verified locally | Session-aware one-shot mode with meaningful exit codes |
| JSON output | Verified locally | Machine-clean completion objects with normalized usage and structured error objects |
| Streaming JSONL | Verified locally | Typed token, reasoning, context, usage, tool lifecycle, completion, and structured error events |
| Stdin prompts | Verified locally | Piped stdin and `-p -` enter machine-clean one-shot mode |
| CI mode | Verified locally | `--ci` disables interactive prompts/ANSI and defaults one-shot output to stream-json |
| SDK/library API | Verified locally | Async create/prompt/steer/session/lifecycle API with normalized usage independent of the TUI |
| JSON-RPC server | Partial | Validated stdio methods, cancellation, lifecycle, and tests; remote transport remains |
| HTTP server | Verified locally | Bearer auth, rate limits, lifecycle, session/turn/steering routes, live SSE events, cancellation, and safe CLI binding |
| IDE/ACP integration | Missing | Protocol-based editor integration after CLI core is stable |

## 12. Reliability And Operations

| Capability | Ash status | Required production behavior |
|---|---|---|
| Structured logging | Partial | Rotation, redaction, correlation IDs, debug bundle |
| Telemetry | Missing | Off by default or explicit opt-in; local metrics still available |
| Error taxonomy | Partial | Shared config/provider/tool/policy/sandbox/context/storage classifier is wired into headless/CI errors; broader interactive command adoption remains |
| Graceful shutdown | Verified locally | Providers, MCP clients, command trees, background agents, SDK, and servers close deterministically |
| Database migrations | Verified locally | Schema version table, transactional ordered migration, future-version refusal, and backups |
| Corruption recovery | Verified locally | Read-only integrity diagnostics plus validated backup and pre-restore preservation |
| Offline test suite | Partial | No hidden home-directory mutation or live network dependency |
| Cross-platform CI | Partial | Linux/macOS/Windows workflow and platform-neutral installed-wheel smoke gate exist; remote runs are not yet observed |
| Packaging CI | Verified locally | Wheel/sdist build, minimal clean install, artifact metadata/content, CLI/config/trust, repo-map import, optional dependency absence, and missing-extra behavior are exercised |
| Security tests | Partial | Command/path bypass corpus, sandbox escape assumptions, secret leaks |
| Performance tests | Partial | Lightweight CLI import graph and installed version startup are regression-tested under one second; large-repo indexing, long-session memory, and redraw latency benchmarks remain |
| Compatibility policy | Partial | Config, session, and plugin manifest schemas are versioned with future-version refusal; plugin deprecation windows remain |

## Delivery Gates

1. **Foundation:** clean install, package layout, config, credentials, doctor,
   lifecycle, test isolation, CI.
2. **Safe core:** canonical provider events, policy engine, sandbox wiring,
   process control, file/search/patch tools, interrupt handling.
3. **Context and sessions:** budgeting, compaction, instructions, checkpoints,
   session picker/fork/rewind/export.
4. **CLI/TUI:** full editor, command registry, status/diff/approval views,
   headless JSON modes.
5. **Extensibility:** real MCP, skills, plugins, hooks, custom commands.
6. **Agents:** provider-backed subagents, worktrees, status/steering/consolidation.
7. **Release:** cross-platform CI, packaging artifacts, security/performance tests,
   documentation that matches verified behavior.
