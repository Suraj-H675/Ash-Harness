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

- Claude Code official docs: <https://code.claude.com/docs/en/overview>
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
| Dependency separation | Partial | Runtime, dev, provider, vector, and server extras with lockfile consistency |
| First-run wizard | Partial | No-key detection, cancel/back, retry, verification, non-TTY guidance |
| API-key providers | Partial | Anthropic, OpenAI, Groq, DeepSeek and custom endpoints tested from fresh process |
| Local models | Partial | Ollama discovery, health checks, pull guidance, tool-capability detection |
| Custom endpoints | Verified locally | Per-provider credentials are stored in mode-0600 env storage, not TOML |
| Config precedence | Partial | `ash config explain` shows env/dotenv/TOML/default sources; project config and full CLI override reporting remain |
| Config migration | Partial | Legacy file migration and config schema future-version refusal exist; config backups remain |
| `ash doctor` | Verified locally | Human/JSON diagnostics; optional endpoint connectivity probe |
| Update/version check | Verified locally | Explicit GitHub release check with no background telemetry or self-modification |
| Uninstall/reset | Verified locally | Confirmed selective reset for config, sessions, cache, or all local state |

## 2. Core Agent Runtime

| Capability | Ash status | Required production behavior |
|---|---|---|
| Streaming model loop | Partial | Cancellation-safe, typed events, clean terminal finalization |
| Native tool calling | Partial | Canonical calls translated correctly for every provider |
| XML fallback tool protocol | Partial | Keep only for models without native tools; strict parser and validation |
| Parallel tool calls | Partial | Independent read-only calls run concurrently with deterministic result order |
| Turn steering | Missing | Queue or inject user guidance while a turn runs |
| Interrupt/cancel | Partial | Process trees and provider streams cancel; CLI exits 130; interactive steering remains |
| Retry policy | Partial | Retry only classified transient failures; jitter and provider retry headers |
| Circuit breaker | Partial | Consistent state, actionable recovery, tested public behavior |
| Long-running process control | Verified locally | Managed start/list/poll/stdin/stop with process-tree cleanup |
| Structured output mode | Verified locally | One-shot JSON Schema injection, parsing, validation, and machine output |
| Model capability negotiation | Partial | Tools, vision, reasoning, local status, and known context/output limits; dynamic manifests remain |
| Provider failover | Verified locally | Ordered fallback before first emitted chunk with visible configured models/failures |

## 3. Context And Memory

| Capability | Ash status | Required production behavior |
|---|---|---|
| Token accounting | Partial | Provider/model-aware counts and visible uncertainty |
| Context budget allocation | Partial | Configurable system/tool/history/repo-map/memory budgets are enforced before compaction and shown by `/context`; file attachment and provider cache budgets remain |
| Automatic compaction | Verified locally | Threshold-based extractive summary retains recent tool call/result pairs |
| Manual `/compact` | Verified locally | Forces compaction while preserving the durable transcript |
| Tool-output pruning | Verified locally | Stale large results are pruned in provider context while preserving call identity and durable data |
| Prompt caching | Missing | Provider-supported cache controls and hit metrics |
| Repository map | Partial/unwired | Incremental index, ignores, active-file ranking, production injection |
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
| Resume by ID | Partial | Error if missing unless user explicitly requests a new session |
| Session picker/list/search | Partial | Recent cwd-scoped session list with model/title/query; full-screen picker remains |
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
| Responsive full-screen TUI | Missing | Narrow/wide layouts, scrollback, stable redraw, Windows terminal support |
| Streaming transcript | Partial | Distinct assistant, tool, error, diff, and status events |
| Markdown/code rendering | Verified locally | Streamed Rich Markdown with fenced-code highlighting and bounded repaint frequency |
| Diff preview | Partial | Bounded unified previews for writes/replacements/patches; side-by-side view remains |
| Approval dialog | Partial | Allow once/session/persist, deny with feedback, editable scope |
| Status line | Verified locally | Cached model, mode, branch, context budget, cost, sandbox, session, and cwd state |
| Themes | Partial | ANSI-safe no-color mode is wired; selectable light/dark themes remain |
| Configurable keybindings | Verified locally | Cross-platform newline/editor actions with collision validation |
| Vim input mode | Verified locally | Optional Emacs or Vim prompt-toolkit editing mode |
| External editor | Verified locally | Prompt-toolkit external-editor integration |
| Image/file attachments | Partial | Safe bounded text/directory `@path` expansion and completion; image/provider blocks remain |
| Prompt history search | Verified locally | Persistent prompt-toolkit file history and reverse search |
| Desktop notifications | Missing | Optional completion/approval notifications |
| Accessibility | Partial | Reduced-redraw and no-color controls are wired; dedicated screen-reader output remains |

## 6. Commands And Modes

| Capability | Ash status | Required production behavior |
|---|---|---|
| Central slash-command registry | Verified locally | Metadata, aliases, parsing, completion, and stable help |
| `/help` | Partial | Complete command reference; searchable overlay remains |
| `/status` | Verified locally | Runtime model/mode/workspace/session diagnostics |
| `/model` and `/models` | Partial | Dynamic catalogs, custom/local models, capability display |
| `/new`, `/resume`, `/sessions` | Verified locally | Durable session lifecycle and missing-ID errors |
| `/rename`, `/fork` | Verified locally | Session organization and transcript branching |
| `/compact`, `/context` | Verified locally | Context compaction and budget inspection |
| `/clear`, `/rewind`, `/undo` | Verified locally | New-session clear, transcript rewind, and conflict-aware file undo |
| `/diff` | Partial | Current/staged/path Git diff; per-turn checkpoint diff remains |
| `/review` | Verified locally | Bounded reviews for worktree/untracked, staged, commit, or current branch versus base |
| `/plan` mode | Verified locally | Explicit toggle, editable sprint contract, approval gate, and execution transition |
| `/permissions` | Partial | Inspect/change active mode; persisted argument grants remain |
| `/sandbox` | Verified locally | Active backend, tier, capabilities, and network policy |
| `/mcp` | Placeholder | Add/remove/auth/list/status/tools/resources with live clients |
| `/skills`, `/plugins`, `/hooks` | Partial/unwired | Discover, inspect, enable, disable, trust, reload |
| `/agents` | Placeholder | Spawn real workers, inspect status/output, steer, stop, resume |
| `/doctor` | Verified locally | Runs the local health report in-session |
| Shell escape (`!`) | Verified locally | Commands use the normal policy, sandbox, persistence, and audit path |
| File mention/completion (`@`) | Partial | Workspace file/directory completion and bounded text expansion; fuzzy symbols/images/MCP remain |
| Custom commands | Verified locally | User/trusted-project Markdown commands, arguments, namespacing, listing, completion |

## 7. Built-In Coding Tools

| Capability | Ash status | Required production behavior |
|---|---|---|
| Read file/ranges | Partial | UTF-8 text reads, binary blocking, and stable truncation metadata exist; broader encoding/media handling remains |
| Write/create file | Partial | Atomic UTF-8 writes preserve supplied newlines and validate path/permissions; broader encoding preservation remains |
| Exact replace/edit | Partial | Bounded exact replacement has diff diagnostics and optional stale-read SHA-256 protection; multiple edits remain |
| Patch application | Verified locally | Validated multi-file Git patch with dry-run and atomic check/apply |
| List/glob files | Verified locally | Bounded production tools with workspace scoping |
| Text/regex search | Verified locally | Ripgrep-backed bounded search with Python fallback |
| Symbol/code search | Partial/unwired | Tree-sitter/LSP-backed definitions and references |
| Shell execution | Partial/unsafe default | Policy engine, sandbox injection, process groups, streaming output |
| Git status/diff/log | Verified locally | Read-only bounded Git inspection tools |
| Git commit | Partial | Explicit staging scope, no unrelated changes, hooks/errors surfaced |
| Tests/build/lint diagnostics | Partial | Parse diagnostics and feed concise structured failures |
| Web fetch/search | Partial | Guarded HTTP(S) `web_fetch` includes optional domain allowlist; search and citation workflow remain |
| Ask-user tool | Verified locally | Typed blocking question with bounded options and explicit empty-answer failure |
| Todo/plan tracking | Partial/unwired | Runtime-visible and persisted progress updates |

## 8. Safety, Trust, And Permissions

| Capability | Ash status | Required production behavior |
|---|---|---|
| Workspace path boundary | Partial | File tools and command effects; symlink/race-safe behavior |
| Trusted-folder prompt | Verified locally | Project instructions, skills, hooks, plugins, and MCP are trust-gated |
| Fine-grained policy engine | Partial | Central allow/ask/deny by tool and mode; persisted argument rules remain |
| Approval persistence | Partial | Session grants and persistent project tool grants with inspection/revocation |
| Read-only plan mode | Verified locally | Non-read tools are denied by the central policy |
| Auto-edit mode | Verified locally | Reads/edits allowed while commands and external tools remain gated |
| Full auto mode | Partial | Explicit dangerous-mode warning and sandbox requirement |
| Dry-run mode | Verified after fix | No side effects, including hooks and subagents |
| OS sandbox | Partial/unwired | Linux bwrap, macOS sandbox/container, Windows restricted execution |
| Network isolation | Partial | `web_fetch` supports public-host validation plus domain allowlist; per-command sandbox network enforcement remains |
| Environment scrubbing | Partial | Secret allowlist, redacted logs, child-process policy |
| Prompt-injection isolation | Partial | Untrusted content provenance and tool-policy enforcement |
| Secret scanning/redaction | Partial | Runtime logs, persisted messages/tool calls, tool output, and exports are redacted; pre-commit scanning remains |
| Audit log | Partial/unwired | Every decision/tool side effect with tamper-evident export |
| Enterprise policy layers | Missing | System/admin policy that lower scopes cannot override |

## 9. Extensibility

| Capability | Ash status | Required production behavior |
|---|---|---|
| MCP stdio client | Partial | Initialize/negotiate/list/call tools wired; resources/prompts remain |
| MCP HTTP transport | Partial | JSON/SSE HTTP, headers, session IDs, and cleanup; OAuth remains out of scope |
| MCP tool namespacing | Verified locally | `mcp__server__tool` adapters prevent collisions |
| MCP resources/prompts | Partial | Browse in TUI and invoke/read through policy-gated model tools |
| Skills | Verified locally | Safe standard `SKILL.md` discovery and progressive activation with trust |
| Plugins/extensions | Partial | Local validated manifests and plugin skills; full lifecycle remains |
| Plugin marketplace | Missing | Signed or trust-reviewed sources; no hardcoded nonexistent registry |
| Hooks | Partial | Trusted command hooks, JSON protocol, scrubbed env, timeout; more lifecycle events remain |
| Custom agents | Missing in production | Declarative roles, model/tools/policy/instructions |
| Hot reload | Partial | Clear errors and atomic registry updates |

## 10. Subagents And Work Isolation

| Capability | Ash status | Required production behavior |
|---|---|---|
| Spawn subagent tool | Partial | Real bounded provider-backed worker with persisted report; worker tools remain restricted |
| Parallel agents | Partial test harness only | Bounded scheduling, cancellation, token/cost limits |
| Agent status/output | Partial/unwired | Live TUI view and persisted reports |
| Agent messaging | Partial/unwired | Typed IPC, backpressure, delivery/ack semantics |
| Role tool policies | Partial | Enforced by central policy engine |
| Worktree isolation | Missing in production | Create/merge/cleanup Git worktrees safely |
| Result consolidation | Partial test harness only | Evidence-linked summaries and conflict handling |
| Agent steering/stop | Missing | User can redirect or terminate workers |

## 11. Automation And Integration

| Capability | Ash status | Required production behavior |
|---|---|---|
| One-shot prompt (`ash -p`) | Verified locally | Session-aware one-shot mode with meaningful exit codes |
| JSON output | Verified locally | Machine-clean completion objects and structured error objects |
| Streaming JSONL | Verified locally | Typed token, reasoning, context, tool lifecycle, completion, and structured error events |
| Stdin prompts | Verified locally | Piped stdin and `-p -` enter machine-clean one-shot mode |
| CI mode | Verified locally | `--ci` disables interactive prompts/ANSI and defaults one-shot output to stream-json |
| SDK/library API | Verified locally | Async create/prompt/session/lifecycle API independent of the TUI |
| JSON-RPC server | Partial | Validated stdio methods, cancellation, lifecycle, and tests; remote transport remains |
| HTTP server | Verified locally | Bearer auth, rate limits, lifecycle, session/turn routes, live SSE events, cancellation, and safe CLI binding |
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
| Cross-platform CI | Partial | Linux/macOS/Windows workflow added; remote runs are not yet observed |
| Packaging CI | Verified locally | Wheel/sdist build, clean install, console and module command smoke tests pass |
| Security tests | Partial | Command/path bypass corpus, sandbox escape assumptions, secret leaks |
| Performance tests | Missing | Startup, large repo indexing, long session, memory use, redraw latency |
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
