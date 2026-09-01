# Ash Harness Research and Target Architecture

Status: active architecture record  
Research date: 2026-08-27<br>
Latest parity refresh: 2026-08-30
Scope: local coding harness plus extensible general-purpose agent platform

This document supersedes earlier project plans as a source of architectural
direction. Earlier Markdown files remain historical inputs, not requirements.
Decisions here come from the running Ash implementation, current protocol
specifications, and the reference repositories pinned below.

No source code from a reference repository was copied during this audit. Ash
is distributed under the MIT license; reference behavior is used for design
comparison only, with source adaptation still subject to attribution review.

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
authenticated HTTP/JSON-RPC adapters. The current full suite contains 1,497
passing tests, with 4 environment-dependent skips on this host.

Ash is not yet the universal harness described by the project goal. The largest
remaining domains are:

1. Richer provider-neutral content blocks and model-assisted compaction beyond
   the deterministic, provenance-preserving fallback.
2. Full multi-vendor MCP OAuth conformance and the remaining edge behavior of
   experimental tasks and resumable transports.
3. Broader executable plugin capabilities for providers, storage, interfaces,
   channels, and services. Plugin API v1 currently covers isolated tool
   contributions without importing Ash internals.
4. Non-Git artifact materialization and richer A2A modalities/auth, plus
   per-turn configuration snapshots and richer branch summaries.
5. Browser downloads/CDP attachment and additional media, gateway routing, and
   messaging channels.
6. OpenTelemetry-compatible traces, evaluation fixtures, Windows CI, signed
   release provenance, and public PyPI publication.

The target should therefore be a narrow core with capability packs. Sending
every tool and integration to every model call would increase prompt cost,
reduce tool selection accuracy, and make the security boundary unreviewable.

## Research Inputs

### Protocols and safety sources

- [Model Context Protocol 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25): lifecycle and capability negotiation; draft-aware JSON Schema 2020-12 tool inputs and outputs; content, structured results, metadata, and application errors; server tools, resources, and prompts; client roots, sampling, and elicitation; progress, cancellation, logging, authorization, and experimental durable tasks.
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

## First-Party Harness Parity Refresh — 2026-08-30

### Method and Status Semantics

This refresh uses only current first-party material: official Hermes Agent,
OpenClaw, and OpenCode documentation, repositories, and release pages. The
sources were checked on **2026-08-30**. Blogs, Reddit, comparison posts,
community implementations, mirrors, and AI-generated summaries were excluded.
No additional harness was needed because the three named comparators
collectively cover every requested category.

Capability status uses these values:

- `implemented`: first-party evidence establishes a usable capability.
- `partial`: a narrower capability exists, but material parts of the category
  are absent or explicitly incomplete.
- `missing`: first-party evidence explicitly establishes that the harness does
  not provide the capability, or its documented scope explicitly excludes it.
  Absence of evidence alone remains `unknown`.
- `unknown`: first-party evidence is insufficient. This is not a defect claim.

The `Parity status` column uses the same vocabulary:

- `implemented`: Ash materially matches the strongest evidenced comparator
  behavior for the capability.
- `partial`: Ash has the capability, but serious comparator behavior remains.
- `missing`: comparator capability is established and Ash lacks it.
- `unknown`: evidence is insufficient to make the parity call.

### Current Reference Snapshots

These rows supplement, rather than silently replace, the older historical
reference snapshots above.

| Reference | Current source snapshot | Current release evidence | Verified date |
|---|---|---|---|
| Hermes Agent | [`2a598aad`](https://github.com/NousResearch/hermes-agent/commit/2a598aad1c398e95b3325a0f100f5c28efa63d12), `main` | [`v2026.8.27` / v0.20.6](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.27), marked latest | 2026-08-30 |
| OpenClaw | [`f9b55d4c`](https://github.com/openclaw/openclaw/commit/f9b55d4c44bff79b2fbfbbf64b149714e2f8beac), `main` | Official release channels showed [`v2026.9.1-beta.1`](https://github.com/openclaw/openclaw/releases/tag/v2026.9.1-beta.1), stable [`v2026.7.1-2`](https://github.com/openclaw/openclaw/releases/tag/v2026.7.1-2), and extended-stable [`v2026.6.34`](https://github.com/openclaw/openclaw/releases/tag/v2026.6.34) | 2026-08-30 |
| OpenCode | [`10765ff2`](https://github.com/anomalyco/opencode/commit/10765ff2a9da8c3b88e4de873aa383a49c318912), default `dev` branch | Immutable [`v1.18.25`](https://github.com/anomalyco/opencode/releases/tag/v1.18.25) | 2026-08-30 |

### Capability Parity Matrix

| Capability | Ash status | Hermes | OpenClaw | OpenCode | Source URL / evidence | Verified date | Parity status |
|---|---|---|---|---|---|---|---|
| Providers, models, and API keys | partial | implemented | implemented | implemented | Ash [production checklist](./PRODUCTION_HARNESS_PARITY.md#1-installation-and-setup); Hermes [model configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuring-models); OpenClaw [model providers](https://docs.openclaw.ai/concepts/model-providers); OpenCode [providers](https://opencode.ai/docs/providers) | 2026-08-30 | partial |
| Provider login and OAuth | missing | implemented | implemented | implemented | Ash explicitly limits provider auth to API keys in the [authoritative scope](./PRODUCTION_HARNESS_PARITY.md); Hermes [configuration and Portal OAuth](https://hermes-agent.nousresearch.com/docs/user-guide/configuration); OpenClaw [OAuth](https://docs.openclaw.ai/concepts/oauth); OpenCode documents browser, device, cloud-identity, and API-key flows in [providers](https://opencode.ai/docs/providers) | 2026-08-30 | missing |
| Custom endpoints | implemented | implemented | implemented | implemented | Ash [custom endpoints](./PRODUCTION_HARNESS_PARITY.md#1-installation-and-setup); Hermes [provider runtime](https://hermes-agent.nousresearch.com/docs/developer-guide/provider-runtime); OpenClaw [model providers](https://docs.openclaw.ai/concepts/model-providers); OpenCode [`baseURL`](https://opencode.ai/docs/providers#base-url) | 2026-08-30 | implemented |
| Isolated profiles | partial | implemented | implemented | unknown | Ash now provides isolated named config/credential state through `ash profile` and `--profile`; Hermes [profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles); OpenClaw [multi-agent workspaces and routing](https://docs.openclaw.ai/concepts/multi-agent); no qualifying OpenCode harness-profile evidence found | 2026-08-30 | partial |
| Fallbacks, key rotation, and model switching | partial | implemented | implemented | partial | Ash [provider failover and model switching](./PRODUCTION_HARNESS_PARITY.md#2-core-agent-runtime); Hermes [credential pools](https://hermes-agent.nousresearch.com/docs/user-guide/features/credential-pools) and [fallback providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers); OpenClaw [model failover](https://docs.openclaw.ai/concepts/model-failover); OpenCode [model selection](https://opencode.ai/docs/models) without an evidenced harness fallback/key-rotation chain | 2026-08-30 | partial |
| Context and compaction | partial | implemented | implemented | implemented | Ash [context management](../guides/CONTEXT_MANAGEMENT.md); Hermes [compression and caching](https://hermes-agent.nousresearch.com/docs/developer-guide/context-compression-and-caching); OpenClaw [context](https://docs.openclaw.ai/concepts/context) and [compaction](https://docs.openclaw.ai/concepts/compaction); OpenCode [automatic compaction agent](https://opencode.ai/docs/agents#use-compaction) | 2026-08-30 | partial |
| Sessions, resume, fork, and export | implemented | implemented | partial | implemented | Ash [session branching](../guides/SESSION_BRANCHING.md) and [production checklist](./PRODUCTION_HARNESS_PARITY.md#4-sessions-and-recovery); Hermes [CLI commands](https://hermes-agent.nousresearch.com/docs/reference/cli-commands), [`/branch` and `/fork`](https://hermes-agent.nousresearch.com/docs/reference/slash-commands), and [REST session fork](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server); OpenClaw [session management](https://docs.openclaw.ai/concepts/session) and [resume](https://docs.openclaw.ai/cli/resume), without general conversation fork/export evidence; OpenCode [CLI continue/fork/export/import](https://opencode.ai/docs/cli) and [session API](https://opencode.ai/docs/server#sessions) | 2026-08-30 | implemented |
| Memory and instructions | partial | implemented | implemented | partial | Ash [context and memory status](./PRODUCTION_HARNESS_PARITY.md#3-context-and-memory); Hermes [memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory) and [context files](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files); OpenClaw [memory architecture](https://docs.openclaw.ai/concepts/memory-architecture); OpenCode [rules](https://opencode.ai/docs/rules) and session summaries, without evidenced durable cross-session memory | 2026-08-30 | partial |
| Core coding tools | implemented | implemented | implemented | implemented | Ash [built-in tools](./PRODUCTION_HARNESS_PARITY.md#7-built-in-coding-tools); Hermes [tools and toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools); OpenClaw [tools overview](https://docs.openclaw.ai/tools); OpenCode [tools](https://opencode.ai/docs/tools) | 2026-08-30 | implemented |
| Browser automation | partial | implemented | implemented | unknown | Ash [browser status](./PRODUCTION_HARNESS_PARITY.md#7-built-in-coding-tools); Hermes [browser automation](https://hermes-agent.nousresearch.com/docs/user-guide/features/browser); OpenClaw [managed browser](https://docs.openclaw.ai/tools/browser); OpenCode Web UI was not counted as browser automation and no qualifying first-party implementation evidence was found | 2026-08-30 | partial |
| Web search and fetch | implemented | implemented | implemented | implemented | Ash [web search/fetch](./PRODUCTION_HARNESS_PARITY.md#7-built-in-coding-tools); Hermes [web search and extract](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search); OpenClaw [web search/fetch](https://docs.openclaw.ai/tools/web); OpenCode [`webfetch` and `websearch`](https://opencode.ai/docs/tools) | 2026-08-30 | implemented |
| MCP lifecycle and OAuth | partial | implemented | implemented | implemented | Ash [MCP status](./PRODUCTION_HARNESS_PARITY.md#9-extensibility); Hermes [MCP OAuth and lifecycle](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference#oauth-21-authentication); OpenClaw [MCP lifecycle, transports, policy, and OAuth](https://docs.openclaw.ai/tools/mcp); OpenCode [local/remote MCP and OAuth](https://opencode.ai/docs/mcp-servers) | 2026-08-30 | partial |
| Plugins and extensions | partial | implemented | implemented | implemented | Ash [Plugin API v1](../reference/PLUGIN_API_V1.md); Hermes [plugins](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins); OpenClaw [plugin architecture](https://docs.openclaw.ai/plugins/architecture); OpenCode [plugins and event hooks](https://opencode.ai/docs/plugins) | 2026-08-30 | partial |
| Skills and marketplace | partial | implemented | implemented | partial | Ash [skills and marketplace status](./PRODUCTION_HARNESS_PARITY.md#9-extensibility); Hermes [skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills); OpenClaw [skills](https://docs.openclaw.ai/tools/skills) and [ClawHub](https://docs.openclaw.ai/clawhub); OpenCode [Agent Skills](https://opencode.ai/docs/skills) but no first-party marketplace comparable to ClawHub | 2026-08-30 | partial |
| Permissions and approvals | implemented | implemented | implemented | implemented | Ash [managed permissions](../guides/PERMISSIONS.md); Hermes [security and approvals](https://hermes-agent.nousresearch.com/docs/user-guide/security); OpenClaw [permission modes](https://docs.openclaw.ai/gateway/permission-modes); OpenCode [ordered allow/ask/deny rules](https://opencode.ai/docs/permissions) | 2026-08-30 | implemented |
| Sandbox and trust | partial | partial | implemented | unknown | Ash [safety status](./PRODUCTION_HARNESS_PARITY.md#8-safety-trust-and-permissions); Hermes [container isolation and authorization](https://hermes-agent.nousresearch.com/docs/user-guide/security), without an equivalent explicit project-trust boundary; OpenClaw [sandboxing](https://docs.openclaw.ai/gateway/sandboxing) and [security](https://docs.openclaw.ai/gateway/security); no qualifying OpenCode OS/container sandbox plus project-trust evidence found | 2026-08-30 | partial |
| Hooks and events | implemented | implemented | implemented | implemented | Ash [Hook Contract v1](../reference/HOOKS_V1.md); Hermes [hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks); OpenClaw [automation hooks](https://docs.openclaw.ai/automation/hooks); OpenCode [plugin events](https://opencode.ai/docs/plugins#events) | 2026-08-30 | implemented |
| Subagents and delegation | implemented | implemented | implemented | implemented | Ash [Agent Tasks v1](../reference/AGENT_TASKS_V1.md); Hermes [delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation); OpenClaw [subagents](https://docs.openclaw.ai/tools/subagents); OpenCode [primary agents and subagents](https://opencode.ai/docs/agents) | 2026-08-30 | implemented |
| A2A | implemented | implemented | implemented | unknown | Ash [A2A status and boundaries](./PRODUCTION_HARNESS_PARITY.md#11-automation-and-integration); Hermes [bidirectional A2A 1.0](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a); OpenClaw [A2A 1.0](https://docs.openclaw.ai/channels/a2a); no qualifying OpenCode A2A evidence found | 2026-08-30 | partial |
| ACP | implemented | implemented | implemented | implemented | Ash [ACP status and boundaries](./PRODUCTION_HARNESS_PARITY.md#11-automation-and-integration); Hermes [ACP host integration](https://hermes-agent.nousresearch.com/docs/user-guide/features/acp); OpenClaw [ACP bridge](https://docs.openclaw.ai/cli/acp); OpenCode [ACP support](https://opencode.ai/docs/acp) | 2026-08-30 | partial |
| LSP | partial | implemented | implemented | implemented | Ash [managed LSP](./PRODUCTION_HARNESS_PARITY.md#11-automation-and-integration); Hermes [LSP diagnostics](https://hermes-agent.nousresearch.com/docs/user-guide/features/lsp); OpenClaw [embedded bundle-configured LSP](https://docs.openclaw.ai/plugins/bundles#embedded-openclaw-lsp) plus [runtime source](https://github.com/openclaw/openclaw/blob/f9b55d4c44bff79b2fbfbbf64b149714e2f8beac/src/agents/agent-bundle-lsp-runtime.ts); OpenCode [LSP servers](https://opencode.ai/docs/lsp) | 2026-08-30 | partial |
| Durable automation | partial | implemented | implemented | partial | Ash [durable automation](../guides/DURABLE_AUTOMATION.md); Hermes [cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron); OpenClaw [automation](https://docs.openclaw.ai/automation) and [cron/webhooks/triggers](https://docs.openclaw.ai/automation/cron-jobs); OpenCode [GitHub Actions automation](https://opencode.ai/docs/github), without a built-in durable scheduler/gateway | 2026-08-30 | partial |
| Messaging channels and gateway | missing | implemented | implemented | unknown | Ash explicitly records channel adapters and gateway routing as remaining; Hermes [messaging gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging); OpenClaw [channels](https://docs.openclaw.ai/channels) and [gateway architecture](https://docs.openclaw.ai/concepts/architecture); no qualifying OpenCode messaging-gateway evidence found | 2026-08-30 | missing |
| HTTP and API surfaces | implemented | implemented | implemented | implemented | Ash [HTTP and JSON-RPC](./PRODUCTION_HARNESS_PARITY.md#11-automation-and-integration); Hermes [OpenAI-compatible API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server); OpenClaw [OpenAI-compatible HTTP API](https://docs.openclaw.ai/gateway/openai-http-api) and [OpenResponses API](https://docs.openclaw.ai/gateway/openresponses-http-api); OpenCode [server](https://opencode.ai/docs/server) and [SDK](https://opencode.ai/docs/sdk) | 2026-08-30 | implemented |
| Setup, onboarding, and config | implemented | implemented | implemented | implemented | Ash [setup and config](./PRODUCTION_HARNESS_PARITY.md#1-installation-and-setup); Hermes [installation](https://hermes-agent.nousresearch.com/docs/getting-started/installation) and [configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration); OpenClaw [onboarding](https://docs.openclaw.ai/start/onboarding-overview) and [configuration](https://docs.openclaw.ai/gateway/configuration); OpenCode [intro/setup](https://opencode.ai/docs) and [config](https://opencode.ai/docs/config) | 2026-08-30 | implemented |
| Doctor, recovery, backup, migration, reset, and uninstall | implemented | partial | implemented | partial | Ash [setup/recovery status](./PRODUCTION_HARNESS_PARITY.md#12-reliability-and-operations); Hermes [updates, snapshots, recovery, and uninstall](https://hermes-agent.nousresearch.com/docs/getting-started/updating) and [imports](https://hermes-agent.nousresearch.com/docs/user-guide/import-from-other-agents), without one documented doctor surface; OpenClaw [doctor](https://docs.openclaw.ai/cli/doctor), [backup](https://docs.openclaw.ai/cli/backup), [migration](https://docs.openclaw.ai/cli/migrate), [reset](https://docs.openclaw.ai/cli/reset), and [uninstall](https://docs.openclaw.ai/cli/uninstall); OpenCode [troubleshooting, uninstall, and recovery](https://opencode.ai/docs/troubleshooting) without evidenced doctor/backup/migration/selective reset | 2026-08-30 | implemented |
| Updates, install, and platform support | partial | implemented | implemented | implemented | Ash [packaging and platform status](./PRODUCTION_HARNESS_PARITY.md#12-reliability-and-operations); Hermes [updates](https://hermes-agent.nousresearch.com/docs/getting-started/updating) and [platform support](https://hermes-agent.nousresearch.com/docs/getting-started/platform-support); OpenClaw [updating](https://docs.openclaw.ai/install/updating) and [platforms](https://docs.openclaw.ai/platforms); OpenCode [install](https://opencode.ai/docs#install), [upgrade CLI](https://opencode.ai/docs/cli#upgrade), and official [releases](https://github.com/anomalyco/opencode/releases) | 2026-08-30 | partial |
| Logging, metrics, tracing, retries, secrets, and audit | partial | partial | implemented | partial | Ash [operations status](./PRODUCTION_HARNESS_PARITY.md#12-reliability-and-operations); Hermes [gateway monitoring](https://github.com/NousResearch/hermes-agent/blob/2a598aad1c398e95b3325a0f100f5c28efa63d12/docs/observability/monitoring.md), [provider runtime retries](https://hermes-agent.nousresearch.com/docs/developer-guide/provider-runtime), and [configuration storage](https://hermes-agent.nousresearch.com/docs/user-guide/configuration), without equivalent audit breadth; OpenClaw [logging](https://docs.openclaw.ai/gateway/logging), [OpenTelemetry](https://docs.openclaw.ai/gateway/opentelemetry), [Prometheus](https://docs.openclaw.ai/gateway/prometheus), [retry policy](https://docs.openclaw.ai/concepts/retry), [secrets](https://docs.openclaw.ai/gateway/secrets), and [audit](https://docs.openclaw.ai/cli/audit); OpenCode [logs/debugging](https://opencode.ai/docs/troubleshooting) and experimental [OpenTelemetry source](https://github.com/anomalyco/opencode/blob/10765ff2a9da8c3b88e4de873aa383a49c318912/packages/opencode/src/session/llm.ts), without comparable first-party metrics/audit breadth | 2026-08-30 | partial |
| CLI, TUI, visual surfaces, and docs polish | partial | implemented | implemented | implemented | Ash [CLI/TUI status](./PRODUCTION_HARNESS_PARITY.md#5-cli-and-tui-experience); Hermes [CLI/TUI](https://hermes-agent.nousresearch.com/docs/user-guide/cli), [Desktop](https://hermes-agent.nousresearch.com/docs/user-guide/desktop), and [dashboard](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard); OpenClaw [TUI](https://docs.openclaw.ai/cli/tui) and [Control UI](https://docs.openclaw.ai/web/control-ui); OpenCode [TUI](https://opencode.ai/docs/tui), [Web](https://opencode.ai/docs/web), and [official README](https://github.com/anomalyco/opencode) | 2026-08-30 | partial |

### Claim-Source Ledger

| ID | Harness | Capability/claim | First-party source | Evidence |
|---|---|---|---|---|
| H1 | Hermes | Models, auth, custom endpoints | [Configuring Models](https://hermes-agent.nousresearch.com/docs/user-guide/configuring-models), [Configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration) | Provider/model slots, API-key and OAuth credential stores, base URL and API mode. |
| H2 | Hermes | Profiles, key rotation, fallback | [Profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles), [Credential Pools](https://hermes-agent.nousresearch.com/docs/user-guide/features/credential-pools), [Fallback Providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers) | Isolated homes; same-provider key/token rotation; cross-provider fallback. |
| H3 | Hermes | Context, sessions, export/fork | [Compression and Caching](https://hermes-agent.nousresearch.com/docs/developer-guide/context-compression-and-caching), [CLI Commands](https://hermes-agent.nousresearch.com/docs/reference/cli-commands), [Slash Commands](https://hermes-agent.nousresearch.com/docs/reference/slash-commands), [API Server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server) | Pluggable compaction; resume and JSONL export; `/branch` alias `/fork`; REST session fork. |
| H4 | Hermes | Memory and instructions | [Persistent Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory), [Context Files](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files) | Bounded cross-session memory and hierarchical project/global instruction files. |
| H5 | Hermes | Tools, browser, web | [Tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools), [Browser](https://hermes-agent.nousresearch.com/docs/user-guide/features/browser), [Web Search](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search) | Built-in toolsets, accessibility-tree browser control, multiple search/extract backends. |
| H6 | Hermes | MCP OAuth and lifecycle | [MCP Guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp), [MCP Config Reference](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference#oauth-21-authentication) | OAuth 2.1 discovery, PKCE, CIMD/DCR, persisted refreshable tokens, login/reload, dynamic tool-list refresh. |
| H7 | Hermes | Extensions, safety, hooks | [Plugins](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins), [Skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills), [Security](https://hermes-agent.nousresearch.com/docs/user-guide/security), [Hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks) | Executable plugins, progressive skills, approvals/isolation, control and event hooks. |
| H8 | Hermes | Agents and protocols | [Delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation), [A2A](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a), [ACP](https://hermes-agent.nousresearch.com/docs/user-guide/features/acp), [LSP](https://hermes-agent.nousresearch.com/docs/user-guide/features/lsp) | Parallel isolated subagents, bidirectional A2A, ACP server, managed language-server diagnostics. |
| H9 | Hermes | Automation, channels, API | [Cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron), [Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging), [API Server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server) | Scheduled jobs, broad channel gateway, OpenAI-compatible HTTP endpoint. |
| H10 | Hermes | Operations and interfaces | [Updating](https://hermes-agent.nousresearch.com/docs/getting-started/updating), [Platform Support](https://hermes-agent.nousresearch.com/docs/getting-started/platform-support), [Gateway Monitoring](https://github.com/NousResearch/hermes-agent/blob/2a598aad1c398e95b3325a0f100f5c28efa63d12/docs/observability/monitoring.md), [Desktop](https://hermes-agent.nousresearch.com/docs/user-guide/desktop) | Backups/rollback during updates, tiered platforms, OTLP monitoring, shared-state desktop UI. |
| OC1 | OpenClaw | Models, auth, failover | [Model Providers](https://docs.openclaw.ai/concepts/model-providers), [OAuth](https://docs.openclaw.ai/concepts/oauth), [Model Failover](https://docs.openclaw.ai/concepts/model-failover) | Hosted/local providers, subscription/API-key auth, auth-profile rotation and model fallback. |
| OC2 | OpenClaw | Context, sessions, memory | [Compaction](https://docs.openclaw.ai/concepts/compaction), [Sessions](https://docs.openclaw.ai/concepts/session), [Resume](https://docs.openclaw.ai/cli/resume), [Memory Architecture](https://docs.openclaw.ai/concepts/memory-architecture) | Durable compaction summaries, gateway sessions/resume, tiered memory with provenance. General fork/export was not established. |
| OC3 | OpenClaw | Browser, web, MCP | [Browser](https://docs.openclaw.ai/tools/browser), [Web](https://docs.openclaw.ai/tools/web), [MCP](https://docs.openclaw.ai/tools/mcp) | Isolated managed browser, web tools, MCP transports/OAuth/lifecycle and server mode. |
| OC4 | OpenClaw | Plugins, skills, marketplace | [Plugin Architecture](https://docs.openclaw.ai/plugins/architecture), [Skills](https://docs.openclaw.ai/tools/skills), [ClawHub](https://docs.openclaw.ai/clawhub) | Broad runtime contributions, skill lifecycle, public marketplace and trust guidance. |
| OC5 | OpenClaw | Safety and audit | [Permission Modes](https://docs.openclaw.ai/gateway/permission-modes), [Sandboxing](https://docs.openclaw.ai/gateway/sandboxing), [Secrets](https://docs.openclaw.ai/gateway/secrets), [Audit](https://docs.openclaw.ai/cli/audit) | Policy modes, sandbox controls, secret lifecycle, metadata-only activity and identity records. |
| OC6 | OpenClaw | Hooks, agents, A2A, ACP | [Hooks](https://docs.openclaw.ai/automation/hooks), [Subagents](https://docs.openclaw.ai/tools/subagents), [A2A](https://docs.openclaw.ai/channels/a2a), [ACP](https://docs.openclaw.ai/cli/acp) | Lifecycle hooks, subagents, A2A 1.0 and ACP bridge. |
| OC7 | OpenClaw | LSP | [Plugin Bundles](https://docs.openclaw.ai/plugins/bundles#embedded-openclaw-lsp), [LSP runtime](https://github.com/openclaw/openclaw/blob/f9b55d4c44bff79b2fbfbbf64b149714e2f8beac/src/agents/agent-bundle-lsp-runtime.ts), [LSP tests](https://github.com/openclaw/openclaw/blob/f9b55d4c44bff79b2fbfbbf64b149714e2f8beac/src/agents/agent-bundle-lsp-runtime.test.ts) | Embedded, bundle-configured stdio runtime; verified hover, definition and references. This does not prove a broad default server catalog. |
| OC8 | OpenClaw | Automation, channels, gateway/API | [Automation](https://docs.openclaw.ai/automation), [Channels](https://docs.openclaw.ai/channels), [Gateway Architecture](https://docs.openclaw.ai/concepts/architecture), [OpenAI HTTP API](https://docs.openclaw.ai/gateway/openai-http-api) | Durable tasks/triggers, channel routing, WebSocket gateway and compatible HTTP endpoints. |
| OC9 | OpenClaw | Recovery and operations | [Doctor](https://docs.openclaw.ai/cli/doctor), [Backup](https://docs.openclaw.ai/cli/backup), [Migration](https://docs.openclaw.ai/cli/migrate), [Updating](https://docs.openclaw.ai/install/updating) | Guided repair, verified archives/SQLite snapshots, migrations and channel-aware updates. |
| OC10 | OpenClaw | Observability and UI | [OpenTelemetry](https://docs.openclaw.ai/gateway/opentelemetry), [Prometheus](https://docs.openclaw.ai/gateway/prometheus), [Retry](https://docs.openclaw.ai/concepts/retry), [Control UI](https://docs.openclaw.ai/web/control-ui) | OTLP traces/metrics/logs, Prometheus, bounded retries and browser administration UI. |
| OD1 | OpenCode | Providers, auth, endpoints | [Providers](https://opencode.ai/docs/providers), [Models](https://opencode.ai/docs/models), [CLI auth](https://opencode.ai/docs/cli#auth) | Broad provider registry, API keys and provider-specific OAuth/device/cloud identity, custom `baseURL`, model switching. No harness fallback/key-rotation chain established. |
| OD2 | OpenCode | Context and sessions | [Agents](https://opencode.ai/docs/agents), [CLI](https://opencode.ai/docs/cli), [Server](https://opencode.ai/docs/server#sessions) | Automatic compaction; continue, fork, JSON export/import; child/session APIs. |
| OD3 | OpenCode | Instructions, tools, MCP | [Rules](https://opencode.ai/docs/rules), [Tools](https://opencode.ai/docs/tools), [MCP Servers](https://opencode.ai/docs/mcp-servers) | AGENTS.md instructions, coding/web tools, local/remote MCP with automatic OAuth and DCR. No durable memory or browser automation was established. |
| OD4 | OpenCode | Plugins, skills, permissions | [Plugins](https://opencode.ai/docs/plugins), [Skills](https://opencode.ai/docs/skills), [Permissions](https://opencode.ai/docs/permissions) | Local/npm plugins and event hooks, on-demand skills, ordered allow/ask/deny rules. No first-party marketplace or OS sandbox/project-trust boundary was established. |
| OD5 | OpenCode | Agents, ACP, LSP | [Agents](https://opencode.ai/docs/agents), [ACP](https://opencode.ai/docs/acp), [LSP](https://opencode.ai/docs/lsp) | Primary/subagents, ACP stdio, broad LSP integration. No A2A evidence was established. |
| OD6 | OpenCode | Automation and API | [GitHub](https://opencode.ai/docs/github), [Server](https://opencode.ai/docs/server), [SDK](https://opencode.ai/docs/sdk) | GitHub Actions automation plus HTTP/OpenAPI and typed SDK. No built-in durable scheduler or messaging gateway was established. |
| OD7 | OpenCode | Install, recovery, UI | [Intro](https://opencode.ai/docs), [CLI](https://opencode.ai/docs/cli), [Troubleshooting](https://opencode.ai/docs/troubleshooting), [TUI](https://opencode.ai/docs/tui), [Web](https://opencode.ai/docs/web) | Cross-platform install, upgrade/uninstall, logs/cache recovery, TUI and web UI. No doctor/backup/migration/selective reset was established. |
| OD8 | OpenCode | Tracing | [Experimental OpenTelemetry source](https://github.com/anomalyco/opencode/blob/10765ff2a9da8c3b88e4de873aa383a49c318912/packages/opencode/src/session/llm.ts) | Model calls wire optional experimental OpenTelemetry; no comparable first-party metrics/audit breadth was established. |

### Evidence Gaps / Blocked Categories

No requested category is blocked collectively: at least one named comparator
has first-party evidence for every category. The unresolved per-product cells
remain `unknown` rather than being converted into unsupported `missing` claims:

- OpenClaw: general conversation fork/tree and general session export/import.
- OpenCode: isolated harness profiles; credential/key rotation and fallback
  chains beyond model switching; durable cross-session memory; browser
  automation; OS/container sandbox plus explicit project trust; A2A; messaging
  gateway/channels; built-in durable scheduler/webhooks; doctor, backup,
  migration and selective reset; full metrics/tracing/secrets/audit breadth.
- Hermes: an explicit project-trust boundary comparable to Ash and a single
  documented doctor/audit surface. Its narrower operations are recorded as
  `partial`, not inferred absent.

### Ash Parity Priorities

The strongest evidence-backed parity gaps are provider subscription login and
OAuth, isolated profiles with credential rotation, richer model-assisted
compaction and memory lifecycle controls, browser completion, live multi-vendor
MCP conformance, broader plugin/marketplace contributions, sandbox and platform
completion, a messaging gateway and channel packs, broader event-driven
automation, OpenTelemetry/evaluation infrastructure, and signed public release
and platform polish. These are comparison findings, not an implementation
commitment; the phased architecture below remains the decision record.

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

| Domain | State on 2026-08-27 | Key gap |
|---|---|---|
| Agent loop and streaming | Strong | Public event schema v1, validated provider-portable messages, typed terminal outcomes, and capability-selected native/XML paths span the runtime and adapters. EOF, truncation, filtering, post-terminal output, and cross-protocol calls fail closed; richer provider content blocks and a smaller loop remain. |
| Provider layer | Strong partial | Built-ins, embedders, declared model capabilities, and custom OpenAI-compatible endpoints resolve through thread-safe linked registries; auth/discovery and non-text modalities remain limited. |
| Coding tools | Strong | Exact-schema deferred tool search, guarded Brave/Tavily live search, an optional isolated Playwright browser, and policy-routed automation management are live; PTY terminal sessions, media, and messaging remain. |
| Context budgets | Strong partial | Provider totals reserve only currently visible tool schemas; large catalogs defer nonessential tools behind session-scoped search activation. Typed hashed fragments and deterministic compaction preserve provenance and task/path/action/outcome state. Model-assisted compaction remains. |
| Sessions/recovery | Strong partial | SQLite schema v9 persists redacted cursor-replayable events plus atomic parent-first session trees, bounded branch metadata, safe fork boundaries, tree-aware retention, and SDK/CLI/HTTP/JSON-RPC access. Per-turn config snapshots and richer branch navigation/summarization remain. |
| Memory | Partial | FTS/vector/Markdown exist; lifecycle, provenance, expiry, poisoning controls, and user workflow are incomplete. |
| Subagents | Strong local/remote | Provider-backed local roles, shared state, steering, atomic DAG dispatch, bounded retries, restart recovery, redacted result context, and branch-verified Git artifact handoff exist. Official A2A 1.0 adds authenticated independent-agent discovery/delegation, durable tasks and context continuation, streaming, cancellation, CLI client/server, and policy-gated configured tools; non-Git artifact materialization and richer A2A modalities/auth remain. |
| Agent Skills | Strong after `5dcbf51` | Standard parsing and progressive disclosure now exist; controlled script execution and compatibility diagnostics remain. |
| Plugins | Strong partial | Versioned, isolated executable tool plugins plus declarative skills/commands/agents/hooks/MCP are live; provider/storage/interface/channel/service contribution kinds remain future capabilities. |
| Hooks | Strong partial | Hook contract v1 covers bounded/redacted session, turn, model, tool, and error lifecycle with fail-closed pre-tool denial and isolated observers. Context-compaction/config/permission-change events and explicit ordering remain. |
| MCP | Strong partial | 2025-11-25 negotiation, strict JSON-RPC envelopes, bounded large stdio messages, exact version-aware input/output schemas, process-isolated non-coercing validation with no remote-reference retrieval, version-specific content checks, complete structured result envelopes, protocol error data, bidirectional cancellation, tools/resources/templates/prompts, roots, sampling, elicitation, progress, logging, restart-safe pagination, server requests, bounded atomic declared list-change refresh with stale-call quarantine, generation-locked Streamable HTTP POST session recovery with catalog reconciliation, experimental task listing/cancellation/execution, pre-2026 GET/SSE, legacy HTTP+SSE discovery, and explicit OAuth 2.1 authorization/refresh/scope step-up exist. Live multi-vendor OAuth conformance remains. |
| Safety/sandbox | Strong | Needs remote/plugin identity capabilities, network proxy policy, stronger resource limits, and platform CI. |
| CLI/TUI/SDK/API | Strong local/remote | CLI, SDK, HTTP server, ACP v1, and A2A 1.0 adapters share the trusted runtime and versioned events. ACP provides bounded editor sessions and an official wire test. A2A provides authenticated/durable JSON-RPC and HTTP+JSON tasks, streaming/cancel/continuation, origin-pinned clients, and trusted delegation tools. WebSocket gateway, multi-conversation daemon ownership, web/desktop UI, and channel adapters remain. |
| LSP | Verified locally | Bounded LSP 3.18 stdio clients start lazily per root, honor negotiated full/incremental sync and UTF position units, support push/pull diagnostics and semantic queries, filter external URIs, require workspace trust, use scrubbed environments, bound caches/results, and shut down process trees deterministically. Rename, code actions, broader server coverage, and multi-vendor conformance remain. |
| Browser/media | Partial | Optional Playwright/Chromium navigation, ARIA snapshots, stable refs, form/click/scroll/history actions, network policy, screenshots/vision, bounded uploads, private profiles, and deterministic cleanup are live. Downloads, CDP attachment, and other media remain. |
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
- Current verification: 1497 passed, 4 environment-dependent skips on Python
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

1. Upgrade MCP incrementally with negotiated capabilities, exact draft-aware
   tool schemas, structured result validation, server request dispatch,
   progress/cancellation/notifications, roots, sampling, elicitation, and OAuth:
   complete through explicit OAuth 2.1 login, private resource-bound
   credentials, refresh, atomic declared tool-list refresh, and bounded
   Streamable HTTP POST session recovery without tool-call replay. Incremental resumable SSE,
   deprecated HTTP+SSE discovery, and experimental tasks remain.
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
   recovery matters. Once a tool dispatch starts, never replay it
   automatically: a lost result is an ambiguous outcome unless an explicit,
   future idempotency contract proves a retry is safe.
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
