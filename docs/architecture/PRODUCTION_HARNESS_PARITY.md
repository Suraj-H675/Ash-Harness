# Ash Production Harness Parity

**Scope:** cross-platform coding harness for Linux, macOS, and Windows.
Authentication is limited to API keys, custom OpenAI-compatible endpoints,
and local model runtimes. Subscription model-provider login is out of scope;
MCP OAuth is part of the remote MCP transport boundary.

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
| Local models | Partial | Ollama URL validation, installed-model discovery, health failure detail, fresh-process configuration checks, safe bounded pull execution, dynamic tool/context capability probing with fail-closed fallback, and explicit `/capabilities --refresh` are wired; broader runtime capability refresh remains |
| Custom endpoints | Verified locally | Per-provider credentials are stored in mode-0600 env storage, not TOML |
| Config precedence | Verified locally | CLI > process env > trusted hierarchical project TOML > user TOML > user dotenv > defaults, with exact masked provenance and project security restrictions |
| Config migration | Verified locally | Complete legacy mapping, conflict preservation, strict destination parsing, verified private source/destination backups, exact-content migration records, and future-version refusal |
| `ash doctor` | Verified locally | Human/JSON diagnostics, extension config validation, optional endpoint connectivity probe |
| Update/version check | Verified locally | Explicit GitHub release check with no background telemetry or self-modification |
| Uninstall/reset | Verified locally | Confirmed selective reset for config, sessions, cache, or all local state |

## 2. Core Agent Runtime

| Capability | Ash status | Required production behavior |
|---|---|---|
| Streaming model loop | Verified locally | Cancellation-safe typed events, provider-neutral completion outcomes, mandatory terminal chunks, post-terminal output rejection, fail-closed EOF/truncation/filter handling, and no-replay retry/failover after output |
| Native tool calling | Verified locally | Capability-selected native schemas/calls are isolated from text parsing, malformed/truncated/cross-protocol calls are refused, and native Anthropic reasoning/redacted/web-search citation blocks are captured in provider-neutral outcomes |
| XML fallback tool protocol | Verified locally | Enabled only for explicitly non-native providers, with incremental text/reasoning, literal-markup finalization, incomplete-control rejection, and no native-call crossover |
| Tool execution boundary | Verified locally | Approved intents persist before dispatch; pre-hooks and middleware can stop an unstarted call; every dispatched local, plugin, and MCP tool call runs exactly once by default. Returned failures are terminal, and exceptions or lost results after dispatch are durably marked ambiguous rather than replayed. |
| Parallel tool calls | Verified locally | Independent read-only calls run concurrently with deterministic result order; cancellation preserves already persisted dispatch intent |
| Turn steering | Verified locally | Bounded durable guidance queue applies at safe iteration boundaries through interactive CLI, SDK, and HTTP |
| Interrupt/cancel | Verified locally | `/cancel` preempts live interactive turns, propagates cancellation, clears pending steering, and finalizes the turn journal |
| Retry policy | Verified locally | One harness-owned policy retries only classified pre-output transient failures, honors bounded Retry-After, adds jittered exponential backoff, preserves cancellation, emits redacted events, and disables nested SDK retries |
| Circuit breaker | Verified locally | Exhausted transient requests open provider-keyed state, fail fast during cooldown, expose `/status` and events, allow a half-open probe, and reset on success |
| Long-running process control | Verified locally | Managed start/list/poll/stdin/stop with process-tree cleanup |
| Structured output mode | Verified locally | One-shot JSON Schema injection, parsing, validation, and machine output |
| Model capability negotiation | Verified locally | Tools, vision, reasoning, local status, and known context/output limits; active sessions expose the negotiated manifest through `/capabilities`, with dynamic Ollama evidence distinguished from static/default registry metadata |
| Provider failover | Verified locally | Ordered fallback before first emitted chunk with visible configured models/failures |

## 3. Context And Memory

| Capability | Ash status | Required production behavior |
|---|---|---|
| Token accounting | Verified locally | Provider usage and cache tokens are normalized; missing usage is estimated from the compacted prompt and streamed/native response, marked provider/estimated/mixed across TUI and structured surfaces, and estimated token/cost portions are persisted separately |
| Context budget allocation | Verified locally | Configurable system/tool/history/repo-map/memory budgets are enforced before compaction and shown by `/context`; text, directory, and image attachments have a model-counted combined cap that defaults to 25% of usable input context and refuses overflow without silent clipping |
| Automatic compaction | Verified locally | Threshold-based extractive summary retains recent tool call/result pairs |
| Manual `/compact` | Verified locally | Forces compaction while preserving the durable transcript |
| Tool-output pruning | Verified locally | Stale large results are pruned in provider context while preserving call identity and durable data |
| Prompt caching | Verified locally | First-party Anthropic/OpenAI automatic controls and retention mapping; normalized reads/writes/hit rate persist and surface through CLI, SDK, HTTP, and JSON-RPC; custom/local endpoints remain untouched ([OpenAI](https://platform.openai.com/docs/guides/prompt-caching), [Anthropic](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)) |
| Repository map | Verified locally | Incremental Tree-sitter symbols/imports for Python, JS/JSX, TS/TSX, Go, Rust, Java, C/C++, and C#; configured/Git ignores, active-file ranking, and CLI/SDK injection |
| Project instructions | Verified locally | Trusted hierarchical `ASH.md` discovery, bounded `@import` expansion, diagnostics, and conservative conflict lint across user/project/imported instructions |
| User instructions | Verified locally | Global `~/.ash/ASH.md` is loaded with a bounded size |
| Session memory | Verified locally | Redacted summaries are searchable across title, ID, and persisted summary metadata without raw transcript concatenation |
| Project memory | Verified locally | Explicit project-scoped index/search/clear controls plus opt-in trusted-workspace auto-indexing bounded by file count, file size, and configured exclusions |
| Semantic memory | Verified locally | In-memory, Chroma, and FTS5 backend selection with index/search/delete lifecycle and bounded automatic project indexing |
| Memory privacy | Verified locally | Project scope, explicit delete, bounded redacted semantic-memory export across in-memory and FTS-backed indexes |

## 4. Sessions And Recovery

| Capability | Ash status | Required production behavior |
|---|---|---|
| Durable sessions | Verified locally | SQLite schema migrations, lifecycle, integrity checks, backup, and restore |
| Resume and continue | Verified locally | `-c` resumes the latest project session; `-r` and `/resume` accept exact IDs or case-insensitive names with ambiguity and wrong-project refusal |
| Session picker/list/search | Verified locally | Bare `-r` and `/resume` open a searchable, keyboard-navigable, project-scoped picker with metadata-only filtering and on-demand bounded transcript preview; top-level list/search also supports JSON output ([Claude Code session behavior](https://code.claude.com/docs/en/sessions)) |
| Session naming | Verified locally | Rename and stable persisted display names |
| Fork session | Verified locally | New durable session from a selected message boundary |
| Rewind conversation | Verified locally | Complete-turn transcript rewind plus optional conflict-preflighted multi-turn file restoration; usage totals are adjusted and filesystem changes roll forward if the database phase fails |
| File checkpoints/undo | Verified locally | Direct edit tools capture per-turn bytes/hashes and refuse conflict overwrites |
| Session export/import | Verified locally | Versioned redacted JSONL/Markdown export and validated JSONL import |
| Crash recovery | Verified locally | Approved tool intent is persisted before execution; resume and cancellation compensate only hash-proven in-flight direct edits, preserve earlier completed edits, persist idempotent outcomes, and flag non-file or conflicting effects for inspection |
| Session retention | Verified locally | Configurable automatic cleanup plus explicit project prune and vacuum |
| Cost/token history | Verified locally | Built-in pricing defaults for current major Anthropic, OpenAI, DeepSeek, and Groq models; explicit user pricing overrides remain authoritative |

## 5. CLI And TUI Experience

| Capability | Ash status | Required production behavior |
|---|---|---|
| Multiline editor | Verified locally | Prompt-toolkit editor with persistent history and multiline bindings |
| Responsive full-screen TUI | Partial | Transcript-owned prompt-toolkit viewport, narrow/wide resize reflow, page navigation, live tail, terminal restoration, and inline fallback are locally verified; native Windows terminal runtime remains |
| Streaming transcript | Verified locally | Immutable semantic user/assistant/reasoning/tool/approval/status/error entries, bounded mutable live cells, rich cached Markdown, command output routing, and durable-session hydration |
| Markdown/code rendering | Verified locally | Streamed Rich Markdown with fenced-code highlighting and bounded repaint frequency |
| Diff preview | Verified locally | Bounded unified and selectable persisted side-by-side previews for writes/replacements/patches are wired into the interactive approval flow |
| Approval dialog | Verified locally | Allow once, exact/broad session, exact/edited project scopes, persisted exact deny, verified command-prefix project scopes, bounded deny-with-feedback, and the full-screen scope editor are wired |
| Status line | Verified locally | Cached model, mode, branch, context budget, prompt-cache totals, cost, sandbox, session, and cwd state |
| Themes | Verified locally | Validated dark/light palettes are wired through config precedence, streamed Rich panels, approvals/status output, inline prompts, and the responsive viewport; screen-reader/no-color fallback remains ANSI-safe |
| Configurable keybindings | Verified locally | Cross-platform newline/editor actions with collision validation |
| Vim input mode | Verified locally | Optional Emacs or Vim prompt-toolkit editing mode |
| External editor | Verified locally | Prompt-toolkit external-editor integration |
| Image/file attachments | Verified locally | Scoped text/directory and bounded PNG/JPEG/GIF/WebP `@path` inputs, vision capability refusal, model-relative attachment token budgets, native Anthropic/OpenAI blocks, fixed image token estimates, and metadata-only persistence |
| Prompt history search | Verified locally | Persistent prompt-toolkit file history and reverse search |
| Desktop notifications | Verified locally | Opt-in completion/approval events, conservative OSC 9/BEL auto detection, tmux passthrough, TTY gating, control-character stripping, bounded optional previews, and failure isolation ([Claude Code behavior](https://code.claude.com/docs/en/terminal-config), [Codex backend](https://github.com/openai/codex/tree/main/codex-rs/tui/src/notifications)) |
| Accessibility | Partial | Dedicated screen-reader mode forces linear non-rewriting output, inline rendering, no color/token bar, reduced motion, and a reduced-dynamic prompt; native assistive-technology runtime evidence remains |

## 6. Commands And Modes

| Capability | Ash status | Required production behavior |
|---|---|---|
| Central slash-command registry | Verified locally | Metadata, aliases, parsing, completion, and stable help |
| `/help` | Verified locally | Filterable text fallback plus full-screen searchable overlay with navigation, details, aliases, and screen-reader/non-TTY fallback |
| `/status` | Verified locally | Runtime model/mode/workspace/session plus persisted token, cache, and cost diagnostics |
| `/model` and `/models` | Verified locally | Configured custom/local/built-in catalogs, capability labels, context/output budgets, and opt-in live endpoint discovery through `/models --refresh` are wired |
| Runtime `/capabilities` | Verified locally | Active-loop manifest reports negotiated tools/vision/reasoning/local status, context/output budgets, and whether evidence is dynamic or from the default registry |
| `/new`, `/resume`, `/sessions` | Verified locally | Durable session lifecycle and missing-ID errors |
| `/rename`, `/fork`, `/tree` | Verified locally | Atomic complete-turn transcript forks, durable parent/root lineage, redacted branch metadata, stable parent-first navigation, and tree-aware retention across CLI/SDK/HTTP/JSON-RPC |
| `/compact`, `/context` | Verified locally | Context compaction, budget inspection, and last-turn cache hit metrics; explicit bounded workspace memory indexing is available from the REPL |
| `/cancel` | Verified locally | Cancel the active provider/tool turn while retaining already completed work |
| `/clear`, `/rewind`, `/undo` | Verified locally | New-session clear, complete-turn transcript rewind with optional `--files`, and conflict-aware latest file undo |
| `/diff` | Verified locally | Current, staged, path-scoped Git diff plus latest per-turn checkpoint diff with conflict refusal |
| `/review` | Verified locally | Bounded reviews for worktree/untracked, staged, commit, or current branch versus base |
| `/plan` mode | Verified locally | Explicit toggle, editable sprint contract, approval gate, and execution transition |
| `/permissions` | Verified locally | Inspect/change mode plus versioned project allow/ask/deny rules with stable IDs, exact/string-prefix/argv-prefix matchers, JSON output, precise removal, and legacy migration ([Claude Code rule model](https://code.claude.com/docs/en/permissions)) |
| `/sandbox` | Verified locally | Active backend, tier, capabilities, and network policy |
| `/mcp` | Partial | Top-level add/remove/list/status config supports JSON and env/header metadata plus safe OAuth credential health (`missing`, `usable`, `expired`, or `invalid`) without exposing secrets; REPL status/tools/resources/prompts use live clients, explicit login/logout atomically reloads affected tooling, and targeted `/mcp refresh SERVER` reconnects one server while preserving others; richer lifecycle remains |
| `/skills`, `/plugins`, `/hooks` | Verified locally | Namespaced components and executable tool protocols are inspectable; local install/enable/disable/confirmed-uninstall and live reload close and replace plugin hosts |
| `/agents` | Verified locally | Slash status/stop/resume, persisted status/report/message inspection, live steering/stop acknowledgement, isolated branch lifecycle, durable task filtering, and cursor-based replay of redacted v1 task events |
| `/doctor` | Verified locally | Runs the local health report in-session |
| Shell escape (`!`) | Verified locally | Commands use the normal policy, sandbox, persistence, and audit path |
| File mention/completion (`@`) | Verified locally | Workspace file/directory/text/vision expansion plus fuzzy symbol and live MCP-resource mention expansion are wired through bounded, provenance-marked attachments with fail-closed lookup and budget enforcement |
| Custom commands | Verified locally | User/trusted-project Markdown commands, arguments, namespacing, listing, completion |

## 7. Built-In Coding Tools

| Capability | Ash status | Required production behavior |
|---|---|---|
| Read file/ranges | Verified locally | Ranged reads support UTF-8 plus BOM-tagged UTF-8/16/32, report raw-byte SHA-256 and encoding, block binary content, and expose stable truncation metadata |
| Write/create file | Verified locally | New files use UTF-8; atomic overwrites and exact edits preserve BOM-tagged UTF-8/16/32 encodings, supplied newlines, modes, path scope, and stale-read race checks |
| Exact replace/edit | Verified locally | Bounded exact replacement has diff diagnostics, optional stale-read SHA-256 protection, and atomic multi-edit support |
| Patch application | Verified locally | Validated multi-file Git patch with dry-run and atomic check/apply |
| List/glob files | Verified locally | Bounded production tools with workspace scoping |
| Text/regex search | Verified locally | Ripgrep-backed bounded search with Python fallback |
| Symbol/code search | Verified locally | Read-only `find_symbol` and `find_references` tools use the incremental Tree-sitter index, exact locations, case controls, globs, and bounded results |
| Deferred tool search | Verified locally | Large built-in/plugin/MCP catalogs expose an essential set plus `search_tools`; ranked matches return exact schemas, activate on the next iteration, reset per session, and alone consume schema budget |
| Shell execution | Verified locally | Foreground and managed background commands share fail-closed sandbox injection, process-tree termination, and child env scrubbing; foreground stdout/stderr stream as call-correlated, incrementally redacted, bounded events through inline TUI, viewport, SDK, and stream-JSON surfaces |
| Git status/diff/log | Verified locally | Read-only bounded Git inspection tools |
| Git commit | Verified locally | Explicit staging scope is required, unrelated pre-staged paths are refused, staged additions are secret-scanned, and Git hook/stdout/stderr failures are surfaced with exit codes |
| Tests/build/lint diagnostics | Verified locally | `run_command` parses bounded compiler/lint/pytest diagnostics into model-visible path, line, symbol, code, and message fields and aggregates pytest/MyPy/Ruff summary counts |
| Web fetch/search | Verified locally | Guarded HTTP(S) fetch plus Brave/Tavily live search with credential auto-detection, auto fallback, fixed endpoints, freshness, bounded normalized sources, provider provenance, shared domain filtering, and durable citation objects are wired |
| Browser automation | Strong partial | Optional Playwright pack owns an isolated Chromium context with public-host/domain routing for requests and WebSockets, blocked service workers/downloads/password fills, bounded ARIA snapshots, stable refs, navigation/click/type/scroll/history actions, bounded vision screenshots, safe workspace uploads, setup/doctor support, and deterministic cleanup; persistent profiles and CDP attachment remain |
| Ask-user tool | Verified locally | Typed blocking question with bounded options and explicit empty-answer failure |
| Todo/plan tracking | Verified locally | Persisted sprint checklists are inspectable and updatable from the top-level CLI; active plan state is injected into every runtime model request with bounded context accounting |

## 8. Safety, Trust, And Permissions

| Capability | Ash status | Required production behavior |
|---|---|---|
| Workspace path boundary | Verified locally | Canonical scope checks plus symlink/junction rejection; POSIX reads, directory listings, creates, no-clobber writes, edits, and patches use descriptor-anchored no-follow operations, with identity/content revalidation fallback on Windows |
| Trusted-folder prompt | Verified locally | Project instructions, skills, hooks, plugins, and MCP are trust-gated |
| Fine-grained policy engine | Partial | Deny-first then ask/allow precedence, mode circuit breakers, stable scoped rules, conservative command-prefix parsing, managed policy layers, tool-specific path/domain/suffix grammar with safe case-insensitive filename extensions plus bounded text containment matching are enforced; richer argument grammars remain |
| Approval persistence | Verified locally | Atomic mode-0600 versioned rules, version-1 migration, stable IDs, exact session/project scopes, command-prefix project scopes, scoped denial, slash/top-level inspection, removal, and clear |
| Read-only plan mode | Verified locally | Non-read tools are denied by the central policy |
| Auto-edit mode | Verified locally | Reads/edits allowed while commands and external tools remain gated |
| Full auto mode | Verified locally | CLI startup, runtime mode switching, and SDK reject unisolated auto-approve unless the explicit unsafe override is set |
| Dry-run mode | Verified after fix | No side effects, including hooks and subagents |
| OS sandbox | Partial | Fail-closed workspace-write Bubblewrap on Linux, scoped `sandbox-exec` profile on macOS, verified Docker image fallback on all platforms, and clearly labeled approval-controlled direct mode; native Windows AppContainer remains |
| Network isolation | Verified locally | Bubblewrap network namespace, macOS profile rules, and Docker `none` networking default to blocked; `web_fetch` separately enforces public-host validation and optional domains |
| Environment scrubbing | Verified locally | Foreground/background commands and Git hooks receive only a scrubbed operational env plus an explicit user-owned variable-name allowlist; MCP stdio servers receive only explicit server env; project config cannot grant command variables, isolated backends preserve the policy, and Docker arguments never contain values |
| Prompt-injection isolation | Verified locally | Every provider request declares an explicit untrusted-content boundary, tool responses carry provenance/policy guidance, and structured per-fragment provenance is inspectable through `/context --provenance`; permissions, sandboxing, redaction, and audit enforcement remain independent |
| Secret scanning/redaction | Verified locally | Runtime logs, persisted messages/tool calls, tool output, and exports are redacted; auto-commit blocks high-confidence secrets in staged additions without echoing values |
| Audit log | Verified locally | Tool approvals, blocks, outcomes, and interactive permission-mode decisions are persisted with a tamper-evident hash chain plus CLI list/verify/export |
| Enterprise policy layers | Verified locally | Platform-managed deny/ask files load before user rules, survive mode changes, appear in status, and invalid/unreadable policy fails startup closed |

## 9. Extensibility

| Capability | Ash status | Required production behavior |
|---|---|---|
| MCP stdio client | Verified locally | Current negotiation, bounded 8 MiB framing with immediate pending-request failure, tools, resources/templates, prompts, pagination, progress, bidirectional cancellation, logging, roots, sampling, elicitation, coalesced atomic list-change refresh with under-write-lock contract proof, experimental required-tool task execution with optional task status notifications plus polling fallback and validated tasks/list observability are wired |
| MCP HTTP transport | Verified locally | Strict JSON-RPC JSON/buffered-SSE POST responses, case-safe headers, initialization-only validated session IDs, generation-locked 404 reinitialization, cross-session pagination restart, catalog reconciliation, no replay of rejected tool calls, cleanup, OAuth 2.1 protected-resource plus ordered OAuth/OIDC discovery, S256 PKCE, resource indicators, configured Client ID Metadata Documents/preregistered clients or DCR fallback, private resource-bound token storage, refresh de-duplication, explicit login/logout, insufficient-scope step-up guidance, experimental required-tool task execution, version-aware pre-2026 GET/SSE with Last-Event-ID plus retry handling, and legacy HTTP+SSE endpoint discovery on one persistent stream are wired; live multi-vendor OAuth conformance remains |
| MCP `2026-07-28` request headers | Verified locally | Static `x-mcp-header` validation, duplicate/limit/name/location rejection, method and identity mirroring, nested primitive parameter extraction, safe/Base64 header encoding, HTTP-only enforcement, and tool-call wiring are covered by focused tests; full modern per-request protocol migration remains |
| MCP `2026-07-28` stdio detection | Verified locally | `server/discover` probing uses modern metadata and validates results; modern-only/newer-version responses fail deterministically, unsupported-version errors do not trigger fallback, and unrecognized errors or timeout safely continue with the legacy handshake; modern per-request operation remains future work |
| MCP `2026-07-28` HTTP detection | Verified locally | A one-shot modern ping probe with required metadata recognizes modern JSON-RPC compatibility errors; modern-only/newer versions, header mismatch, and missing capability fail deterministically, while unrecognized responses, auth rejection, timeouts, or network failures safely continue with the legacy handshake |
| MCP schemas/results | Verified locally | Exact version-aware input/output schemas, isolated non-coercing validation with no remote fetches, bounded content checks, complete rich result envelopes, output-schema checks, application-error semantics, and exact JSON-RPC error data are preserved without unsafe call replay |
| MCP tool namespacing | Verified locally | `mcp__server__tool` adapters prevent collisions |
| MCP resources/prompts | Verified locally | Browse in TUI, invoke/read through policy-gated model tools, declared revisioned list changes, explicit live lists, and atomic `/mcp refresh` lifecycle reporting are wired |
| Skills | Verified locally | Bounded standard `SKILL.md` discovery and progressive activation with trust; legacy in-process executable skills are explicit unsafe compatibility only and cannot fabricate tools |
| Plugins/extensions | Verified locally | Transactional local lifecycle, dependencies, inventory, project trust, namespacing, declarative components, and Plugin API v1 tools through strict bounded JSON-RPC subprocesses with read-only/no-network isolation, ordinary policy/audit/events, no side-effect replay, live replacement, and deterministic cleanup |
| Plugin marketplace | Partial | Trust-reviewed HTTPS Git source installation uses explicit refs, shallow temporary checkouts, metadata removal, all existing lifecycle/component safety gates, and optional signed catalogs with pinned Ed25519 keys plus exact revision binding; signed catalog search and name-driven installs preserve catalog source/ref/digest pinning; curated remote discovery remains |
| Hooks | Verified locally | Versioned session/turn/model/tool/error lifecycle plus context-compaction/config/permission-change observers; structured pre-tool denial and session context, critical-versus-observer failure semantics, redacted diagnostics/events, plugin cwd, scrubbed env, timeout, bounded I/O, and process-tree cleanup are wired |
| Custom agents | Verified locally | User/project/plugin Markdown roles, namespacing, instructions, base-role isolation, and non-escalating tool restrictions |
| Hot reload | Verified locally | Commands/completion, skills, agents, hooks, and MCP runtimes validate before live replacement |

## 10. Subagents And Work Isolation

| Capability | Ash status | Required production behavior |
|---|---|---|
| Spawn subagent tool | Verified locally | Real bounded provider-backed Ash loop, persisted reports, role-scoped tool manifests, background execution, cancellation, and no recursive spawning |
| Parallel agents | Verified locally | Atomic DAG submission, dependency-ready parallel dispatch, cross-process capacity, bounded retries, foreground/background operation, restart recovery, and durable graph-wide token and USD ceilings with atomic overrun accounting |
| Agent status/output | Verified locally | Live basic and full slash status with durable task identity, token budgets, and USD cost usage; top-level persisted status/report/message inspection |
| Agent messaging | Verified locally | Typed SQLite IPC is persisted and inspectable; running workers consume steer/stop messages, acknowledge delivery, and enforce pending-message backpressure |
| Role tool policies | Verified locally | Read/search baseline, coder-only scoped edits, sandbox-required tester commands, and no recursive spawn tool |
| Worktree isolation | Verified locally | Clean-lead precondition, locked `ash-agent/*` branches, exact branch/commit verification, conflict-safe dependent merges, bounded commits, deterministic cleanup, and explicit full-branch squash/discard |
| Result consolidation | Verified locally | Foreground DAG calls return typed terminal results and errors plus evidence-linked synthesis, bounded summaries, workspace-relative path evidence, cross-agent claim-conflict detection, and durable graph-consolidation artifacts |
| Agent steering/stop | Verified locally | In-process stop, persisted stop, atomic graph cancellation with active-turn revocation, live steering at safe iteration boundaries, delivery state, and report-based resume; isolated changes must be applied before continuation |

## 11. Automation And Integration

| Capability | Ash status | Required production behavior |
|---|---|---|
| One-shot prompt (`ash -p`) | Verified locally | Session-aware one-shot mode with meaningful exit codes |
| JSON output | Verified locally | Machine-clean completion objects with normalized usage and structured error objects |
| Streaming JSONL | Verified locally | Typed token, reasoning, context, usage, tool lifecycle, completion, and structured error events |
| Stdin prompts | Verified locally | Piped stdin and `-p -` enter machine-clean one-shot mode |
| CI mode | Verified locally | `--ci` disables interactive prompts/ANSI and defaults one-shot output to stream-json |
| SDK/library API | Verified locally | Async create/prompt/steer/session/lifecycle/delegation API with explicit subagent provider injection and normalized usage independent of the TUI |
| JSON-RPC server | Verified locally | Validated stdio and authenticated HTTP methods share one SDK adapter; remote `/rpc` supports JSON-RPC 2.0 requests, notifications, bounded batches, strict JSON parsing, bearer auth, rate limits, payload limits, and cancellation |
| HTTP server | Verified locally | Bearer auth, rate limits, lifecycle, session/turn/steering routes, live SSE events, cancellation, and safe CLI binding |
| IDE/ACP integration | Verified locally | Official ACP Python SDK v1 over bounded JSONL stdio; initialize/new/load/list/prompt/cancel, durable message and redacted tool replay, permission requests, text/resource links, tool/usage streaming, editor-supplied stdio/HTTP/SSE MCP, isolated runtimes, and an official-client wire test. Images/audio/embedded context, extra directories, modes, fork/resume, terminal/filesystem callbacks, and registry publication remain unadvertised. |
| Remote-agent/A2A integration | Verified locally | Official A2A SDK/spec 1.0 Agent Card, JSON-RPC and HTTP+JSON routes, bearer auth, rate limits, durable SQLite tasks and project-scoped context/session mapping, text artifact streaming, polling/get/list/cancel, CLI inspect/send, trusted configured delegation tools, bounded I/O, origin pinning, and official-client end-to-end tests. Push notifications, files/data, extended cards, gRPC, signed-card verification, and OAuth/mTLS remain unadvertised. |
| Managed LSP | Verified locally | Trusted, lazy per-root LSP 3.18 stdio clients; negotiated full/incremental sync and UTF positions; push/pull diagnostics; hover, definition, references, implementation, symbols, and call hierarchy; advisory post-edit diagnostics; bounded framing/documents/results/stderr; external-URI filtering; refused server edits; deterministic process cleanup. Rename, code actions, and cross-vendor conformance remain. |
| Durable scheduler | Verified locally | Trusted one-shot, interval, and timezone-aware cron prompts; SQLite WAL persistence; atomic workspace-scoped multi-worker claims; per-job overlap prevention; renewable leases; isolated process termination; bounded timeout/token/output; misfire/coalescing and DST contracts; terminal crash recovery; CLI/SDK/model tools; read-only doctor checks; worker-liveness diagnostics; installed-wheel smoke coverage |

## 12. Reliability And Operations

| Capability | Ash status | Required production behavior |
|---|---|---|
| Structured logging | Verified locally | Rotation, redaction, correlation IDs, and bounded redacted structured debug bundles |
| Telemetry | Verified locally | No outbound telemetry; `ash metrics` exposes aggregate local-only token, cache, session, and cost metrics |
| Error taxonomy | Verified locally | Shared config/provider/tool/policy/sandbox/context/storage classifier is wired into headless/CI errors, interactive slash commands, imports, model switches, and normal turn failures |
| Graceful shutdown | Verified locally | Providers, MCP clients, command trees, background agents, SDK, and servers close deterministically |
| Database migrations | Verified locally | Schema version table, transactional ordered migration, future-version refusal, and backups |
| Corruption recovery | Verified locally | Read-only integrity diagnostics plus validated backup and pre-restore preservation |
| Offline test suite | Partial | No hidden home-directory mutation or live network dependency |
| Cross-platform CI | Partial | Linux/macOS CI workflow and platform-neutral installed-wheel smoke gate exist; Windows workflow is intentionally deferred until parity work resumes |
| Packaging CI | Verified locally | Wheel/sdist build, minimal clean install, artifact metadata/content, CLI/config/trust, repo-map import, optional dependency absence, and missing-extra behavior are exercised |
| Security tests | Partial | Command/path bypass corpus, sandbox escape assumptions, secret leaks |
| Performance tests | Partial | Lightweight CLI import graph and installed version startup are regression-tested under one second; bounded large-repository memory indexing is covered by a 170-file offline benchmark; long-session memory proves 1,000-file indexing plus 20 exact-recall rounds with bounded indexing and sub-50 ms recall; redraw latency benchmarks remain |
| Compatibility policy | Verified locally | Config, session, and plugin manifest schemas are versioned with future-version refusal, minimum-version enforcement, and plugin deprecation notices |

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
