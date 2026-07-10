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
3. A versioned executable plugin SDK that can add providers, tools, storage,
   interfaces, channels, and services without growing the core.
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
- [Agent Skills specification](https://agentskills.io/specification): `SKILL.md` metadata, naming, optional resources, validation, and progressive disclosure.
- [Agent2Agent specification 1.0](https://a2a-protocol.org/latest/specification): discovery, modalities, task lifecycle, streaming, and independent agent interoperability.
- [Agent Client Protocol](https://agentclientprotocol.com/): client/agent initialization, sessions, terminal and filesystem capabilities, modes, session resume/list/close, and registry distribution.
- [OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/): standard traces, metrics, logs, errors, and GenAI attributes. Prompt/tool content is sensitive and must be opt-in.
- [OWASP Agentic AI threats and mitigations](https://genai.owasp.org/resource/agentic-ai-threats-and-mitigations/): goal hijacking, tool misuse, identity abuse, supply-chain compromise, unexpected code execution, memory poisoning, and insecure inter-agent communication.
- [NIST AI RMF Generative AI Profile](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence): governance and test/evaluation/verification/validation guidance across the AI lifecycle.

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
- LSP client management for diagnostics, definitions, references, symbols,
  rename, and code actions. Tree-sitter remains a fallback, not an LSP
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

### Pi

Adopt the narrow core, comprehensive extension event API, provider-portable
messages, tree sessions, explicit branch summaries, custom compaction, and
fake-provider tests. Pi intentionally leaves subagents and plan mode to
extensions; Ash already has these and should keep them as optional core packs,
not remove them.

## Current Ash Capability Matrix

| Domain | State on 2026-07-10 | Key gap |
|---|---|---|
| Agent loop and streaming | Partial | Public event schema v1 now spans runtime, tools, SDK, stream JSON, SSE, and JSON-RPC discovery; the loop remains monolithic and legacy XML fallback is mixed into canonical behavior. |
| Provider layer | Strong partial | Built-ins, embedders, declared model capabilities, and custom OpenAI-compatible endpoints resolve through thread-safe linked registries; auth/discovery and non-text modalities remain limited. |
| Coding tools | Strong | No PTY terminal session, tool search, browser, web search, media, scheduler, or messaging. |
| Context budgets | Strong partial | Provider totals now include reserved tool schemas; typed hashed fragments expose source/trust provenance; deterministic compaction preserves task/path/action/outcome state and redacts persisted summaries. Model-assisted compaction remains. |
| Sessions/recovery | Strong linear | SQLite schema v8 now persists redacted, cursor-replayable canonical events across SDK/HTTP/JSON-RPC; within-session trees, labels, and branch summaries remain. |
| Memory | Partial | FTS/vector/Markdown exist; lifecycle, provenance, expiry, poisoning controls, and user workflow are incomplete. |
| Subagents | Strong local | Worktrees, roles, shared state, steering, and reports exist; no remote A2A or workflow DAG runtime. |
| Agent Skills | Strong after `5dcbf51` | Standard parsing and progressive disclosure now exist; controlled script execution and compatibility diagnostics remain. |
| Plugins | Partial | Declarative skills/commands/agents/hooks/MCP only; no versioned executable SDK or providers/channels/services. |
| Hooks | Partial | Pre-tool, post-tool, session-start only; no full lifecycle, ordering, mutation contract, or isolation. |
| MCP | Strong partial | 2025-11-25 negotiation, tools/resources/templates/prompts, roots, sampling, elicitation, progress, logging, cancellation, pagination, and server requests exist; OAuth and experimental tasks remain. |
| Safety/sandbox | Strong | Needs remote/plugin identity capabilities, network proxy policy, stronger resource limits, and platform CI. |
| CLI/TUI/SDK/API | Strong local | CLI, SDK, and HTTP server now share one trusted runtime assembler and versioned events; no ACP adapter, WebSocket gateway, multi-conversation daemon ownership, web/desktop UI, or channel adapters. |
| LSP | Absent in practice | `lsp/diagnostics.py` is 26 lines and is not a managed language-server client. |
| Browser/media | Absent | Must be optional capability packs with explicit profiles, costs, and policy. |
| Automation/channels | Absent | No durable scheduler, event bus, gateway routing, pairing, or delivery semantics. |
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
- Current verification: 948 passed, 8 environment-dependent skips on Python
  3.12; Ruff and mypy are clean. The managed PID-namespace subprocess waiter
  race is covered by Ash's portable process completion helper.

### Phase 1: runtime contracts

1. Extract canonical events/content/capabilities from the loop and providers.
2. Add schema versioning and golden compatibility tests for SDK/JSON-RPC/HTTP:
   event schema v1 is complete; result/request schemas remain.
3. Introduce provider and capability registries; move CLI provider branching
   behind them: provider registry and shared runtime assembly are complete;
   capability registry and real SDK runtime registration are complete.
4. Add typed context fragments and a model-assisted compactor with deterministic
   fallback: typed provenance and strengthened deterministic fallback are
   complete; model-assisted compaction remains.
5. Add session-tree/event-log foundations without breaking existing SQLite
   sessions: schema v8 event storage and replay are complete; session trees
   remain.

Exit criterion: all current behavior runs through versioned contracts, old
sessions migrate, and provider/tool fakes cover the state machine.

### Phase 2: standards and extension platform

1. Upgrade MCP incrementally with negotiated capabilities, server request
   dispatch, progress/cancellation/notifications, roots, sampling, elicitation,
   and OAuth; gate experimental tasks.
2. Define plugin API v1 and an out-of-process plugin host; migrate declarative
   plugins as a zero-code plugin type.
3. Expand lifecycle hooks and add tool search/profiles.
4. Implement ACP server mode and a conformance test client.

Exit criterion: external clients and extensions can use Ash without importing
internal modules, and untrusted contributions cannot bypass policy.

### Phase 3: durable multi-agent runtime

1. Make local agents use the canonical operation/event/task contracts.
2. Add workflow DAGs, artifact handoff, budgets, ownership, and cancellation
   propagation.
3. Implement A2A server/client adapters with agent cards and policy mapping.
4. Add remote execution backends after identities and audit semantics exist.

Exit criterion: local and remote agents share one observable lifecycle and can
resume after process interruption.

### Phase 4: general-purpose capability packs

1. Web search/extraction and dedicated browser profile automation.
2. Media/document understanding and image/audio/video generation adapters.
3. Durable scheduler, cron/heartbeat, webhooks, and event triggers.
4. Gateway identity/routing plus initial WebChat, Telegram, Discord, and Slack
   adapters; other channels remain plugins.
5. Managed LSP clients and richer code intelligence.

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
