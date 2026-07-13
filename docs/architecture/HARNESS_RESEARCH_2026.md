# Ash Harness Research and Target Architecture

Status: active architecture record  
Research date: 2026-07-10  
Scope: local coding harness plus extensible general-purpose agent platform

This document supersedes earlier project plans as a source of architectural
direction. Earlier Markdown files remain historical inputs, not requirements.
Decisions here come from the running Ash implementation, current protocol
specifications, and the reference repositories pinned below.

No source code from a reference repository was copied during this audit. Ash
does not yet declare a license, so source adaptation is blocked until the
project owner selects one and attribution requirements can be satisfied.

## Executive Conclusion

An AI harness is not just a model loop. A production harness is the control
plane between users, models, tools, state, external services, and execution
environments. It must make model behavior useful, bounded, observable,
recoverable, and extensible.

Ash already has a credible coding-harness base: a streamed agent loop, native
tool calling with an XML fallback, filesystem and command tools, granular
permissions, OS/container sandbox selection, durable SQLite sessions,
checkpoints and rewind, provider retries/failover, MCP tools/resources/prompts,
skills, declarative plugins, subagents with worktree isolation, a TUI, SDK, and
authenticated HTTP/JSON-RPC adapters. The initial full suite contained 925
passing tests before this audit.

Ash is not yet the universal harness described by the project goal. The largest
missing domains are:

1. A stable canonical event and provider contract independent of any one model
   API or the legacy XML protocol.
2. Current MCP client capabilities and authentication.
3. Broader executable plugin capabilities for providers, storage, interfaces,
   channels, and services. Plugin API v1 now covers isolated tool contributions
   without importing Ash internals.
4. Durable session trees and resumable operations rather than only linear
   transcripts and session-level forks.
5. Remote agent interoperability through A2A and editor/client interoperability
   through Agent Client Protocol.
6. Browser/computer control, web search, multimodal generation/understanding,
   scheduling, background events, and messaging channels.
7. OpenTelemetry-compatible traces, metrics, evaluation fixtures, and
   performance/regression budgets.

The target should therefore be a narrow core with capability packs. Sending
every tool and integration to every model call would increase prompt cost,
reduce tool selection accuracy, and make the security boundary unreviewable.

## Research Inputs

### Protocols and safety sources

- [Model Context Protocol 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25): lifecycle and capability negotiation; server tools, resources, and prompts; client roots, sampling, and elicitation; progress, cancellation, logging, authorization, and experimental durable tasks.
- [MCP Authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization): OAuth 2.1 protected-resource discovery, OAuth/OIDC metadata fallback order, Client ID Metadata Documents, preregistration, DCR fallback, S256 PKCE, resource indicators, step-up scope challenges, and token handling requirements.
- [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728), [RFC 8414](https://www.rfc-editor.org/rfc/rfc8414), [RFC 7591](https://www.rfc-editor.org/rfc/rfc7591), [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707), and [RFC 7636](https://www.rfc-editor.org/rfc/rfc7636): protected-resource metadata, authorization-server metadata, dynamic registration, resource indicators, and PKCE details used by the MCP authorization profile.
- [Agent Skills specification](https://agentskills.io/specification): `SKILL.md` metadata, naming, optional resources, validation, and progressive disclosure.
- [Agent2Agent specification 1.0](https://a2a-protocol.org/latest/specification): discovery, modalities, task lifecycle, streaming, and independent agent interoperability.
- [Official A2A Python SDK](https://github.com/a2aproject/a2a-python): typed 1.0 protobuf models, Agent Card resolution, JSON-RPC/HTTP+JSON transports, server routing, task aggregation, cancellation, and durable database stores used by Ash's adapter and conformance client.
- [Agent Client Protocol](https://agentclientprotocol.com/): client/agent initialization, sessions, terminal and filesystem capabilities, modes, session resume/list/close, and registry distribution.
- [ACP Python SDK](https://github.com/agentclientprotocol/python-sdk): official typed protocol models, newline-delimited JSON stdio transport, agent/client routers, capability negotiation, and wire-test primitives used by Ash's v1 adapter.
- [Language Server Protocol 3.18](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.18/specification/): lifecycle, bounded stdio framing, negotiated document synchronization and position encodings, diagnostics, navigation, symbols, and call hierarchy used by Ash's managed client.
- [OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/): standard traces, metrics, logs, errors, and GenAI attributes. Prompt/tool content is sensitive and must be opt-in.
- [OWASP Agentic AI threats and mitigations](https://genai.owasp.org/resource/agentic-ai-threats-and-mitigations/): goal hijacking, tool misuse, identity abuse, supply-chain compromise, unexpected code execution, memory poisoning, and insecure inter-agent communication.
- [NIST AI RMF Generative AI Profile](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence): governance and test/evaluation/verification/validation guidance across the AI lifecycle.
- [Brave Web Search API](https://api-dashboard.search.brave.com/api-reference/web/search/get): bounded result counts, freshness filters, safe search, authentication, and rate-limit behavior for source-oriented live search.
- [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search): agent-oriented normalized search, freshness ranges, bounded results, and bearer authentication.
- [Playwright Python](https://playwright.dev/python/docs/library): maintained cross-platform browser automation and version-pinned browser binaries used by Ash's optional isolated browser capability.
- [croniter](https://pypi.org/project/croniter/): maintained, timezone-aware cron iteration used for fire-time calculation; Ash owns persistence, claims, leases, and execution recovery.

### Reference repository snapshots

| Reference | Commit | Date | License | Primary lesson |
|---|---:|---:|---|---|
| Claude Code | `be02c39` | 2026-07-08 | Proprietary | Polished permission, hook, subagent, plugin, and workflow UX. Behavior may be studied; source must not be copied. |
| Codex | `dc23c7bcc8` | 2026-07-08 | Apache-2.0 | Protocol-first event runtime, app server, sandboxing, approvals, contextual fragments, tool routing, rollouts, multi-agent controls, and focused crates. |
| Hermes Agent | `a4ba8c964` | 2026-07-09 | MIT | One agent core across CLI/TUI/desktop/messaging, broad toolsets, browser/media, memory, scheduling, and delegation. |
| OpenClaw | `7e0b67d7ea` | 2026-07-09 | MIT | Gateway architecture, channels/nodes, multi-agent routing, cron/heartbeat, provider breadth, browser profiles, and capability-oriented plugins. |
| OpenCode | `6b41ae910` | 2026-07-09 | MIT | Client/server separation, provider/model registry, ordered permission patterns, LSP, snapshots, agents, plugins, and multiple frontends. |
| Pi | `cb222bf9` | 2026-07-09 | MIT | Small core, strong extension events, tree-shaped JSONL sessions, branch summaries, compaction hooks, and provider portability. |

Useful local entry points include:

- Codex: `ref/codex/codex-rs/core/src`, `app-server-protocol`, `skills`,
  `execpolicy`, `linux-sandbox`, and `thread-store`.
- Claude Code: `ref/claude-code/plugins` and `examples/settings`.
- Hermes: `ref/hermes-agent/agent`, `tools`, `providers`, `plugins`, `gateway`,
  and `cron`.
- OpenClaw: `ref/openclaw/packages/agent-core`, `packages/ai`,
  `packages/gateway-protocol`, `src/agents`, `src/plugins`, and `src/channels`.
- OpenCode: `ref/opencode/packages/opencode/src` areas `session`, `agent`,
  `provider`, `permission`, `tool`, `mcp`, `plugin`, `skill`, `snapshot`, and
  `lsp`.
- Pi: `ref/pi/packages/agent` and `ref/pi/packages/coding-agent/src/core`.

## What a Harness Must Contain

The first group is mandatory for any reliable harness. Later groups are
required for Ash's universal-platform goal but can be installed as capability
packs.

### 1. Canonical agent runtime

- A bounded turn state machine: accept input, assemble context, call a model,
  dispatch tool calls, feed results back, and terminate or continue.
- Typed input/output events for text, reasoning summaries, tool calls, tool
  updates/results, usage, errors, approvals, elicitation, and completion.
- Cancellation, timeout, retry, backpressure, steering, iteration limits, loop
  detection, and deterministic terminal states.
- Provider-neutral messages and content blocks for text, images, audio, files,
  structured data, citations, reasoning, and tool calls.
- Idempotency keys and operation journals for calls that may outlive a process.
- Structured output validation and repair without silently accepting invalid
  output.

### 2. Provider and model layer

- A provider registry rather than provider-specific branching in the CLI.
- Streaming adapters that normalize finish reasons, errors, usage, caching,
  reasoning, citations, and native tool calls.
- Model metadata and discovery: context/output limits, modalities, tool support,
  reasoning controls, pricing, deprecation, and aliases.
- API key, local endpoint, cloud identity, and OAuth/subscription auth through a
  credential broker. Credentials never enter prompts, logs, argv, or persisted
  session content.
- Retry classification, rate limiting, circuit breakers, fallbacks before any
  visible output, and explicit model handoff after output.
- First-class adapters should eventually cover Anthropic, OpenAI, Google,
  OpenRouter, Azure OpenAI, AWS Bedrock, Vertex AI, Ollama, and generic
  OpenAI/Anthropic-compatible endpoints. Other providers belong in plugins.

### 3. Tool runtime

- JSON-schema-described tools with stable names, validation, cancellation,
  streaming updates, progress, structured results, and content blocks.
- A registry with namespaces, provenance, conflict handling, dynamic discovery,
  health, versioning, and tool search so large catalogs are not always sent.
- Middleware for policy, approvals, secrets, checkpoints, tracing, budgets,
  caching, and hooks.
- Foreground and managed background processes with PTY support, process-tree
  termination, output bounds, and resumable handles.
- Built-in capability groups: files, search, patches, shell/process, Git,
  repository symbols/LSP, web, browser, media, memory, scheduling, messaging,
  user questions, and delegation.
- Explicit contracts for side effects, idempotency, read-only behavior,
  external network use, and irreversible actions.

### 4. Context engineering

- Ordered, typed context fragments with provenance, priority, token cost,
  freshness, sensitivity, and hard size limits.
- Durable user/project instructions with hierarchy and trust boundaries.
- Prompt-cache-stable prefixes; mutable information appears late and only when
  needed.
- Per-section token budgets for system instructions, tool schemas, history,
  attachments, repository context, skills, memory, and completion reserve.
- Provider-aware token counting and image/audio/file estimates.
- Tool-result pruning that preserves call/result validity.
- Model-assisted compaction with a deterministic fallback, explicit summaries,
  split-turn handling, preserved file/task state, and compaction audit entries.
- Context inspection explaining exactly what was included, omitted, truncated,
  or summarized and why.

### 5. Durable sessions and memory

- Append-only event persistence with schema migrations and crash recovery.
- Tree-shaped sessions: branch, fork, clone, label, navigate, summarize an
  abandoned branch, rewind, and export/import without rewriting history.
- Per-turn model/config snapshots so replay and resume preserve semantics.
- Durable tasks, pending approvals, tool intents/results, checkpoints, and
  recovery reports.
- Short-term working state separated from long-term user/project memory.
- Memory provenance, scope, expiry, conflict resolution, user inspection,
  deletion, poisoning defenses, and opt-in semantic retrieval.
- Keyword and semantic retrieval with ingestion budgets and reproducible
  embedding configuration.

### 6. Agents and multi-agent orchestration

- Named agent definitions with instructions, model, tools, permission profile,
  budget, concurrency, workspace, and lifecycle.
- In-process and isolated-process workers, plus optional worktree/container/
  remote execution.
- Parent/child lineage, depth/concurrency/cost limits, cancellation propagation,
  heartbeats, steering, mailboxes, artifacts, and structured reports.
- Parallel fan-out, sequential handoff, review loops, consensus/debate, and DAG
  workflows without forcing every task into a fixed pattern.
- Shared state with ownership and conflict rules; never let concurrent workers
  write the same workspace without isolation or coordination.
- A2A client/server support for remote independent agents. Local subagents are
  an implementation detail, not a substitute for interoperability.

### 7. Skills, plugins, hooks, and workflows

- Agent Skills-compatible progressive discovery: metadata at startup,
  instructions on activation, resources only on demand.
- Skill resources scoped to the skill root. `allowed-tools` is experimental
  metadata and must not silently weaken host policy.
- A versioned plugin manifest and SDK capable of registering tools, providers,
  agents, commands, hooks, context sources, memory stores, transports, channels,
  UI contributions, and background services.
- Plugin trust, signatures/checksums where available, dependency locking,
  atomic installation/update/rollback, capability declarations, and isolation.
- Lifecycle hooks around session, turn, model request/response, context,
  approval, tool call/result, compaction, and shutdown. Hooks require timeouts,
  ordering, error policy, and recursion protection.
- Declarative workflows for repeated DAGs; use skills for instructions, tools
  for actions, and plugins for executable lifecycle capability.

### 8. Interoperability protocols

- MCP host/client with negotiated versions and capabilities, stdio and
  Streamable HTTP, tools/resources/prompts, notifications, pagination,
  progress, cancellation, roots, sampling, elicitation, logging, OAuth 2.1
  discovery/PKCE, and optional experimental tasks behind a feature flag.
- MCP server mode for exposing selected Ash tools/resources/prompts with the
  same policy boundary as local calls.
- Agent Client Protocol server mode so editors and agent clients can create,
  resume, list, configure, prompt, cancel, and close Ash sessions.
- A2A client/server mode with agent cards, task lifecycle, streaming,
  authentication, artifacts, and policy mapping.
- LSP client management is implemented for diagnostics, hover, definitions,
  references, implementations, symbols, and call hierarchy. Rename and code
  actions remain future work. Tree-sitter remains a fallback, not an LSP
  replacement.
- Stable SDK, JSON-RPC, HTTP/SSE or WebSocket event schemas with explicit
  version negotiation.

### 9. Safety, trust, and isolation

- Default least privilege with ordered allow/ask/deny rules over tool,
  arguments, path, command prefix, network destination, agent, and plugin.
- Distinct user, workspace, plugin, remote-agent, MCP-server, and model trust
  domains.
- Project trust before loading repository-controlled instructions, config,
  skills, hooks, plugins, or MCP processes.
- OS sandboxing with truthful capability detection, network policy, read-only
  mounts, writable roots, resource limits, and fail-closed behavior.
- Secure path traversal resistant to symlinks/junctions and time-of-check/time-
  of-use races.
- Secret brokerage and redaction across prompts, events, files, process env,
  logs, checkpoints, audit trails, and telemetry.
- Prompt-injection defenses based on provenance and policy, not claims that
  untrusted text can be made harmless.
- Supply-chain controls for plugins, skills, MCP servers, model endpoints, and
  browser extensions.
- Tamper-evident audit records and human-readable explanations for every
  boundary crossing.

### 10. Interfaces and operation

- TUI/CLI, non-interactive CI mode, embeddable SDK, daemon/server, and editor
  protocol adapters over the same runtime.
- Accessible linear output plus responsive terminal UI; structured JSON event
  streams for automation.
- Local gateway for multiple users/conversations, identity, rate limits,
  routing, and channel adapters.
- Messaging channels with pairing/allowlists, group mention policy, attachment
  handling, chunking, retry, deduplication, and delivery receipts.
- Scheduler, cron and heartbeat triggers, webhooks, file/queue events, durable
  jobs, missed-run policy, and notification delivery.
- Browser automation using a dedicated agent profile by default, deterministic
  DOM/accessibility snapshots, screenshots, downloads, and explicit attachment
  to a user's signed-in browser only with consent.
- Image/audio/video/document understanding and generation through provider or
  plugin capabilities, with content, size, cost, and safety limits.

### 11. Observability, evaluation, and release engineering

- OpenTelemetry traces for turns, model calls, tool calls, approvals,
  compaction, retrieval, delegation, and protocol requests.
- Metrics for latency, tokens, cache hits, cost, retries, errors, queue depth,
  context utilization, tool success, and sandbox/policy decisions.
- Content capture off by default; redacted metadata-only telemetry is the safe
  default.
- Deterministic fake providers and tools, protocol conformance fixtures,
  transcript snapshots, security tests, crash/recovery tests, and platform
  matrices.
- Task evaluations for correctness, tool choice, context retention, recovery,
  safety, and cost, plus performance budgets for startup and steady-state use.
- Reproducible wheels/installers, signed releases, SBOM/provenance, migrations,
  doctor/update/rollback, and one-command installation.

## Lessons from Each Harness

### Claude Code

Adopt the interaction concepts: scoped permissions, project trust, lifecycle
hooks, reusable agents/skills/commands, plugin composition, checkpoints, and a
strong non-interactive interface. Do not copy source: the reference license is
proprietary. Avoid binding Ash to one provider or subscription system.

### Codex

Adopt the protocol-first split between core runtime and clients, typed
contextual fragments, narrow tool router, resumable thread/rollout state,
structured approvals, hardened execution, and focused modules. Avoid allowing a
single `core` package to become the default home for every capability. Codex's
Apache license is compatible with many choices but still requires notices when
source is adapted.

### Hermes Agent

Adopt capability toolsets, the same agent core across interfaces, persistent
memory with explicit tools, provider breadth, browser/media integrations,
scheduling, and channel delivery. Keep provider-specific paid gateways optional
and ensure every toolset passes through Ash policy and tracing.

### OpenClaw

Adopt the gateway/plugin boundary, per-agent workspaces and session routing,
channel isolation, pairing, cron/heartbeat, dedicated browser profiles, nodes,
and plugin contributions beyond tools. Avoid putting channel and device
dependencies into the base install.

### OpenCode

Adopt ordered wildcard permissions, client/server APIs, model/provider
discovery, LSP integration, session snapshots, configurable primary/subagents,
and multiple frontends over one backend. Avoid configuration semantics where
rule ordering is implicit; Ash should expose the winning rule and provenance.
OpenCode's lazy per-root server lifecycle informed Ash's manager, while Ash
deliberately requires installed or explicit servers instead of downloading
language-server binaries during a session.

### Pi

Adopt the narrow core, comprehensive extension event API, provider-portable
messages, tree sessions, explicit branch summaries, custom compaction, and
fake-provider tests. Pi intentionally leaves subagents and plan mode to
extensions; Ash already has these and should keep them as optional core packs,
not remove them.

## Current Ash Capability Matrix

| Domain | State on 2026-07-13 | Key gap |
|---|---|---|
| Agent loop and streaming | Partial | Public event schema v1 and validated provider-portable message/content/tool-call inputs now span the runtime and adapters; the loop remains monolithic and legacy XML fallback is mixed into canonical behavior. |
| Provider layer | Strong partial | Built-ins, embedders, declared model capabilities, and custom OpenAI-compatible endpoints resolve through thread-safe linked registries; auth/discovery and non-text modalities remain limited. |
| Coding tools | Strong | Exact-schema deferred tool search, guarded Brave/Tavily live search, an optional isolated Playwright browser, and policy-routed automation management are live; PTY terminal sessions, media, and messaging remain. |
| Context budgets | Strong partial | Provider totals reserve only currently visible tool schemas; large catalogs defer nonessential tools behind session-scoped search activation. Typed hashed fragments and deterministic compaction preserve provenance and task/path/action/outcome state. Model-assisted compaction remains. |
| Sessions/recovery | Strong partial | SQLite schema v9 persists redacted cursor-replayable events plus atomic parent-first session trees, bounded branch metadata, safe fork boundaries, tree-aware retention, and SDK/CLI/HTTP/JSON-RPC access. Per-turn config snapshots and richer branch navigation/summarization remain. |
| Memory | Partial | FTS/vector/Markdown exist; lifecycle, provenance, expiry, poisoning controls, and user workflow are incomplete. |
| Subagents | Strong local/remote | Provider-backed local roles, shared state, steering, atomic DAG dispatch, bounded retries, restart recovery, redacted result context, and branch-verified Git artifact handoff exist. Official A2A 1.0 adds authenticated independent-agent discovery/delegation, durable tasks and context continuation, streaming, cancellation, CLI client/server, and policy-gated configured tools; non-Git artifact materialization and richer A2A modalities/auth remain. |
| Agent Skills | Strong after `5dcbf51` | Standard parsing and progressive disclosure now exist; controlled script execution and compatibility diagnostics remain. |
| Plugins | Partial | Declarative skills/commands/agents/hooks/MCP only; no versioned executable SDK or providers/channels/services. |
| Hooks | Strong partial | Hook contract v1 covers bounded/redacted session, turn, model, tool, and error lifecycle with fail-closed pre-tool denial and isolated observers. Context-compaction/config/permission-change events and explicit ordering remain. |
| MCP | Strong partial | 2025-11-25 negotiation, tools/resources/templates/prompts, roots, sampling, elicitation, progress, logging, cancellation, pagination, server requests, and explicit OAuth 2.1 authorization/refresh/scope step-up exist; experimental tasks and live multi-vendor OAuth conformance remain. |
| Safety/sandbox | Strong | Needs remote/plugin identity capabilities, network proxy policy, stronger resource limits, and platform CI. |
| CLI/TUI/SDK/API | Strong local/remote | CLI, SDK, HTTP server, ACP v1, and A2A 1.0 adapters share the trusted runtime and versioned events. ACP provides bounded editor sessions and an official wire test. A2A provides authenticated/durable JSON-RPC and HTTP+JSON tasks, streaming/cancel/continuation, origin-pinned clients, and trusted delegation tools. WebSocket gateway, multi-conversation daemon ownership, web/desktop UI, and channel adapters remain. |
| LSP | Verified locally | Bounded LSP 3.18 stdio clients start lazily per root, honor negotiated full/incremental sync and UTF position units, support push/pull diagnostics and semantic queries, filter external URIs, require workspace trust, use scrubbed environments, bound caches/results, and shut down process trees deterministically. Rename, code actions, broader server coverage, and multi-vendor conformance remain. |
| Browser/media | Partial | Optional Playwright/Chromium navigation, ARIA snapshots, stable refs, form/click/scroll/history actions, network policy, and deterministic cleanup are live. Screenshots/vision, downloads/uploads, profiles, and other media remain. |
| Automation/channels | Partial | Durable one-shot/interval/cron prompts, atomic multi-worker claims, renewable leases, cancellation, coalescing, misfire records, CLI/SDK/model tools, liveness diagnostics, and restart recovery are verified. Webhooks, event triggers, gateway routing, pairing, and channel delivery remain. |
| Observability/evals | Absent | Logging and audit exist, but no OTel traces/metrics or task evaluation harness. |
| Packaging | Improved | Distribution renamed to available `ash-ai`; public PyPI release/signing still required. |

## Target Architecture

### Narrow core

`ash.runtime` should own only:

1. Canonical message, content, event, usage, error, and capability types.
2. The bounded agent/turn state machine and cancellation model.
3. Context fragment assembly and budgets.
4. Tool registry/router and middleware interfaces.
5. Session/event/task storage interfaces and migrations.
6. Policy decisions, trust identities, approvals, and audit events.
7. Extension discovery and lifecycle contracts.

The core must not import a specific provider SDK, terminal UI, web framework,
browser, channel, or vector database.

### Adapters

- `ash.providers`: model/auth adapters and model registry.
- `ash.tools`: capability packs registered through the tool contract.
- `ash.protocols`: MCP, ACP, A2A, HTTP/JSON-RPC, and LSP adapters.
- `ash.interfaces`: CLI/TUI, SDK, daemon, and future web/desktop clients.
- `ash.extensions`: skills, plugins, hooks, workflows, install/update/trust.
- `ash.storage`: SQLite defaults plus optional stores and artifact blobs.
- `ash.observability`: audit, OpenTelemetry, diagnostics, and evaluations.

Existing top-level packages can migrate incrementally behind compatibility
aliases. A repository-wide rename is not a prerequisite for behavior changes.

### Event-first execution

Every runtime action should produce a versioned event envelope containing an
event ID, timestamp, session/turn/operation IDs, parent event ID, source
identity, type, payload, and schema version. Interfaces render events; storage
persists them; telemetry observes them; recovery reconstructs state from them.
Provider-native payloads remain adapter details.

### Capability-based extension model

Extensions declare capabilities and required privileges. The host resolves an
extension into contributions and grants, starts it only when needed, and can
run untrusted executable plugins out of process. Tool schemas are selected per
agent/turn through profiles and search rather than always injected.

### Trust identities

Policy decisions need an actor and provenance, not only a tool name. Minimum
identities are user, model, local agent, remote agent, built-in tool, plugin,
skill, MCP server, hook, channel sender, and scheduled job. The same command may
be allowed for an interactive user turn and denied for an untrusted channel or
scheduled task.

## Implementation Sequence

### Phase 0: verified baseline

- Fix sandbox fail-closed behavior when Docker disappears: complete in
  `9a3536e`.
- Establish public distribution metadata, Python 3.11 support, complete dev
  dependencies, Agent Skills validation/progressive loading, and scoped skill
  resources: complete in `5dcbf51`.
- Current verification: 1140 passed, 9 environment-dependent skips on Python
  3.12; Ruff and mypy are clean. The managed PID-namespace subprocess waiter
  race is covered by Ash's portable process completion helper.

### Phase 1: runtime contracts

1. Extract canonical events/content/capabilities from the loop and providers:
   event v1, request message/content models, and capability declarations are
   complete, including normalized native tool calls; richer canonical streamed
   media/reasoning blocks remain.
2. Add schema versioning and golden compatibility tests for SDK/JSON-RPC/HTTP:
   event schema v1 is complete; result/request schemas remain.
3. Introduce provider and capability registries; move CLI provider branching
   behind them: provider registry and shared runtime assembly are complete;
   capability registry and real SDK runtime registration are complete.
4. Add typed context fragments and a model-assisted compactor with deterministic
   fallback: typed provenance and strengthened deterministic fallback are
   complete; model-assisted compaction remains.
5. Add session-tree/event-log foundations without breaking existing SQLite
   sessions: schema v9 event replay and durable atomic session-node trees are
   complete, including migration backup, branch metadata, boundary validation,
   tree-aware retention, and public runtime adapters.

Exit criterion: all current behavior runs through versioned contracts, old
sessions migrate, and provider/tool fakes cover the state machine.

### Phase 2: standards and extension platform

1. Upgrade MCP incrementally with negotiated capabilities, server request
   dispatch, progress/cancellation/notifications, roots, sampling, elicitation,
   and OAuth: complete through explicit OAuth 2.1 login, private resource-bound
   credentials, refresh, and one-retry runtime recovery. Experimental tasks
   remain gated and unimplemented.
2. Define plugin API v1 and an out-of-process plugin host: complete for tool
   contributions, including manifest-time Draft 2020-12 schema and namespace
   validation, strict bounded JSON-RPC stdio, lazy shared hosts, no call replay,
   secret-free environments, read-only plugin mounts, network and host-read
   isolation, ordinary approval/audit/hook/event/persistence paths, fail-closed
   errors, hot reload, deterministic shutdown, and zero-code declarative
   plugins. Provider/storage/interface/channel/service contribution kinds remain
   future versioned capabilities.
3. Expand lifecycle hooks and add tool search/profiles: bounded hook contract v1
   covers session/turn/model/tool/error events and failure semantics; exact
   schema tool search now defers large live catalogs and activates matches per
   session. Context-compaction events, explicit ordering, and profiles remain.
4. Extend the implemented ACP v1 server and official-client wire test with
   media/editor callbacks and registry publication only as those capabilities
   gain complete behavior.

Exit criterion: external clients and extensions can use Ash without importing
internal modules, and untrusted contributions cannot bypass policy.

### Phase 3: durable multi-agent runtime

1. Make local agents use the canonical operation/event/task contracts: durable
   task v1 now backs provider workers with cross-process capacity, renewable
   hashed leases, crash recovery, stale-owner rejection, result persistence,
   operator inspection, transactionally durable canonical event envelopes, and
   live runtime lifecycle emission.
2. Add workflow DAGs, artifact handoff, budgets, ownership, and cancellation:
   creation-order dependency DAGs, recursive cancellation, dependency-failure
   propagation, enforced token/time budgets, parent/sprint lineage, and typed
   artifact records now feed atomic DAG submission, automatic parallel
   ready-task dispatch, task-level retry, foreground aggregation, restart
   recovery, redacted dependency context, and conflict-safe verified Git
   artifact handoff; non-Git materialization and graph-wide budgets remain.
3. A2A server/client adapters now provide an official 1.0 Agent Card, durable
   authenticated tasks, streaming, cancellation, CLI calls, project-scoped
   context continuation, and approval-gated configured delegation tools.
4. Add remote execution backends after identities and audit semantics exist.

Exit criterion: local and remote agents share one observable lifecycle and can
resume after process interruption.

### Phase 4: general-purpose capability packs

1. Web search and an isolated browser automation baseline are live; richer
   extraction, persistent browser profiles, vision, uploads, and downloads remain.
2. Media/document understanding and image/audio/video generation adapters.
3. Durable scheduler and cron/worker heartbeats are live; add webhooks and
   external event triggers.
4. Gateway identity/routing plus initial WebChat, Telegram, Discord, and Slack
   adapters; other channels remain plugins.
5. The managed LSP baseline is live. Add rename, code actions, broader server
   coverage, and cross-vendor conformance without automatic binary downloads.

Exit criterion: each pack installs independently, declares privileges, passes
policy/sandbox/tracing, and does not inflate unrelated model contexts.

### Phase 5: production operations and release

1. OpenTelemetry traces/metrics with content capture off by default.
2. Evaluation runner, benchmark budgets, crash/fault injection, protocol
   conformance, and multi-platform CI.
3. Signed releases, SBOM/provenance, upgrade/rollback, installer smoke tests,
   and PyPI publication under `ash-ai`.
4. Security review against OWASP agentic risks and documented threat models for
   plugins, MCP, A2A, browser, gateway, memory, and scheduled work.

Exit criterion: a clean host can install Ash with one command, configure a
provider or local model, pass `ash doctor`, execute a real end-to-end task, and
upgrade or roll back without losing sessions.

## Non-Negotiable Engineering Rules

1. No capability may bypass the central tool policy, secret broker, audit, or
   cancellation path.
2. All model-visible context has a hard bound and provenance.
3. Project-controlled executable content requires trust; remote content remains
   untrusted even when its transport is authenticated.
4. Persist intent before side effects and result after side effects when crash
   recovery matters.
5. Never claim a sandbox, protocol feature, provider capability, or install path
   works without an automated or end-to-end verification.
6. Keep protocol and public SDK schemas versioned and backward-compatible or
   provide an explicit migration.
7. Prefer original implementation from specifications. Copy or adapt reference
   code only after license selection, attribution review, and a recorded source
   notice.
8. A feature is not complete until it has policy behavior, cancellation,
   persistence/recovery where applicable, structured events, tests, diagnostics,
   and user-facing configuration.
