# Ash

> A terminal-native coding-agent harness for serious, long-running work.

Ash turns a language model into a durable engineering workspace. It combines a
streaming agent loop, real coding tools, persistent sessions, provider
failover, safe execution, extensibility, subagents, automation, and editor or
remote-agent protocols in one Python application.

Ash is designed for the space occupied by [Hermes Agent](https://github.com/NousResearch/hermes-agent),
[OpenClaw](https://github.com/openclaw/openclaw), and
[OpenCode](https://github.com/anomalyco/opencode): a harness around models,
tools, state, policy, and integrations rather than a thin chat client.

## What Ash is

Ash is a local-first, provider-neutral coding environment that can:

- work interactively in a rich terminal UI or run bounded one-shot turns;
- inspect, modify, test, review, and version-control a real workspace;
- keep conversations, plans, usage, tool activity, and recovery state in
  durable project-scoped storage;
- use hosted APIs, local model servers, or custom OpenAI-compatible routes;
- delegate work to isolated subagents or communicate with remote agents;
- connect external capabilities through MCP, plugins, skills, LSP, ACP, A2A,
  HTTP, JSON-RPC, and the Python SDK; and
- keep every model action inside explicit approval, trust, path, network,
  sandbox, redaction, and audit boundaries.

## Capability overview

### Provider and model layer

Ash has a descriptor-driven provider catalog and a provider-neutral runtime.
Supported built-in routes include:

| Route | Models and behavior |
| --- | --- |
| Anthropic | Claude models through the Anthropic Messages API |
| OpenAI | GPT models through the OpenAI API |
| OpenRouter | Multi-provider gateway routing |
| DeepSeek | Chat and reasoning models |
| Groq | Fast hosted open models |
| Mistral | Mistral models through an OpenAI-compatible endpoint |
| xAI | Grok models through an OpenAI-compatible endpoint |
| Together AI | Hosted open models |
| Fireworks AI | Hosted inference |
| Cerebras | Fast hosted models |
| Ollama | Local models with no API key requirement |
| LM Studio | Local OpenAI-compatible model server |
| vLLM | Self-hosted OpenAI-compatible model server |
| Custom routes | User-defined OpenAI-compatible providers with explicit auth mode |

The provider layer also provides:

- model strings in `provider/model` form;
- live model discovery and endpoint readiness checks;
- provider and model capability metadata for native tools, vision,
  reasoning, locality, context, and output budgets;
- isolated named profiles for separate model, provider, credential, and local
  state contexts;
- ordered model failover before the first streamed response chunk;
- one harness-owned retry policy for classified pre-output transient failures;
- `Retry-After` handling, jittered exponential backoff, and provider-keyed
  circuit breakers with half-open recovery;
- local Ollama health, model metadata, native-tool, and context probing;
- safe, bounded local-model pull execution;
- Anthropic and OpenAI prompt caching with normalized cache usage and cost
  reporting; and
- provider usage normalization with explicit provider, estimated, or mixed
  accounting when a service does not return token usage.

Subscription-based model-provider login is not currently part of the provider
surface. MCP OAuth is supported separately for protected MCP servers.

### Agent runtime

The core loop supports:

- streaming provider-neutral text, reasoning, tool, usage, status, error, and
  completion events;
- native tool calling for capable providers;
- an incremental XML tool protocol for explicitly non-native providers;
- strict parsing and validation of tool calls and provider responses;
- durable approved-intent recording before dispatch;
- exactly-once local, plugin, and MCP dispatch by default;
- explicit ambiguous-outcome handling when a dispatched side effect times out,
  crashes, or loses its result;
- concurrent independent read-only tool calls with deterministic result order;
- bounded turn iteration and steering-message queues;
- cancellation that propagates through provider and tool work;
- structured JSON and JSON-Schema output for machine consumers;
- capability-aware tool and vision negotiation; and
- bounded long-running process management with process-tree cleanup.

The model receives an explicit untrusted-content boundary. Workspace files,
tool output, memory, citations, and project-derived instructions are treated
as evidence and cannot override user instructions or runtime policy.

### Coding workspace tools

Ash includes a complete coding-tool surface:

| Area | Capabilities |
| --- | --- |
| Files | Ranged reads, file creation, atomic writes, exact replacement, multi-edit operations, whole-file edits, and patch application |
| Text and structure | Directory listing, bounded globbing, ripgrep-backed text/regex search, Python search fallback, symbol lookup, and reference lookup |
| Code intelligence | Incremental Tree-sitter repository maps for Python, JavaScript/JSX, TypeScript/TSX, Go, Rust, Java, C, C++, and C# |
| Git | Status, bounded diffs, log inspection, explicit-scope commits, secret scanning, Git-hook error reporting, and worktree-aware review |
| Processes | Foreground commands, managed background jobs, live bounded stdout/stderr, stdin, polling, stopping, and cleanup |
| Interaction | Typed ask-user questions, persisted plans, and model-visible compiler, linter, test, MyPy, and Ruff diagnostics |

File operations are workspace-scoped and protect against symlinks, junctions,
path traversal, stale reads, encoding loss, binary misclassification, and
concurrent no-overwrite races. UTF-8 and BOM-tagged UTF-8/16/32 text can be
read and preserved during edits.

### Context, memory, and instructions

Ash manages context as a first-class runtime resource:

- configurable system, tool, history, repository-map, and memory budgets;
- provider-aware token counting, cost tracking, and cache accounting;
- automatic and manually requested compaction;
- extractive summaries that retain recent tool-call/result pairs;
- bounded pruning of stale large tool results;
- persistent global and trusted hierarchical `ASH.md` instructions with
  bounded imports, diagnostics, and conflict linting;
- project-root-aware repository maps with active-file ranking;
- searchable redacted session memory;
- explicit project memory indexing, search, clear, and privacy export;
- deterministic in-memory semantic search;
- optional Chroma vector search;
- SQLite FTS5 lexical fallback; and
- bounded batch indexing for large projects without one database transaction
  per file.

Attachments and context expansion support workspace files, directories,
images, fuzzy repository symbols, and live MCP resources. Text and directory
content is bounded by model context; PNG, JPEG, GIF, and WebP images are
bounded, require advertised vision support, and are persisted as metadata and
digests rather than base64 payloads.

### Durable sessions and recovery

Every project can have durable, inspectable conversation state backed by
versioned SQLite storage. Ash supports:

- new, continue, resume, list, search, rename, and project-scoped session
  picking;
- exact session IDs and human-readable session names;
- parent/root lineage, forks, conversation trees, and branch summaries;
- complete-turn transcript rewind;
- optional conflict-aware restoration of checkpointed file edits;
- per-turn file checkpoints, hashes, and undo protection;
- redacted JSONL and Markdown session export and validated JSONL import;
- configurable retention, pruning, vacuum, backups, restore, and integrity
  checks;
- crash recovery based on persisted tool intent and hash-proven file state;
- recovery reports for in-flight, incomplete, conflicting, or ambiguous work;
  and
- persisted token, cache, usage, and cost history with estimated portions
  clearly labeled.

Recovery is conservative: completed work is retained, direct file edits are
compensated only when hashes prove what happened, and non-file side effects
are never guessed or silently replayed.

### Terminal experience

The interactive interface is built for sustained terminal work:

- responsive full-screen transcript viewport with page navigation and live
  tailing;
- inline rendering fallback for terminals that do not support a viewport;
- Rich Markdown and fenced-code rendering with bounded repaint frequency;
- streaming user, assistant, reasoning, tool, approval, status, error, and
  recovery entries;
- searchable full-screen help with a linear fallback for redirected input and
  screen readers;
- multiline editing, persistent prompt history, reverse history search, Vim or
  Emacs input modes, and external-editor support;
- `/` command registry with aliases, parsing, completion, and stable help;
- `@` file, directory, image, symbol, and MCP-resource completion;
- unified or side-by-side bounded approval diffs;
- status surfaces for model, branch, context, cache, cost, sandbox, session,
  and working directory;
- validated dark and light themes;
- reduced-motion, no-color, inline, and dedicated screen-reader modes;
- configurable cross-platform keybindings; and
- opt-in OSC 9, BEL, and terminal-aware desktop notifications with bounded
  previews and failure isolation.

Structured text, JSON, and stream-JSON surfaces are available for scripts,
CI systems, editor hosts, and other automation clients.

### Safety, trust, and policy

Safety is part of the harness contract rather than an optional prompt:

- interactive approvals for tool calls with once, session, project, and
  scoped decisions;
- ordered allow, ask, and deny rules with stable IDs;
- exact, contains, prefix, enum, numeric maximum, path-prefix, suffix, domain,
  and command-prefix matchers;
- deny precedence and fail-closed behavior for unattended work;
- bounded approval previews and deny-with-feedback;
- separate user-owned policy from repository-controlled configuration;
- explicit project trust before project instructions, extensions, MCP, or LSP
  configuration can affect the runtime;
- workspace path and domain restrictions;
- secret and private-key detection before auto-commit;
- incremental redaction of sensitive output;
- tamper-evident session audit records with verification and export;
- untrusted-content labels and provenance metadata;
- no automatic replay of dispatched side effects; and
- clean error classification with actionable diagnostics.

### Sandboxed execution

Shell and executable extension work can use a fail-closed isolation layer:

- Bubblewrap on Linux;
- `sandbox-exec` on macOS;
- Docker with the packaged `ash-sandbox` baseline, including Docker Desktop on
  Windows; or
- explicitly reported direct execution when no isolation backend is available.

Isolated commands default to disabled network access, a scrubbed environment,
workspace-scoped filesystem access, bounded output, and process-tree cleanup.
User-owned sandbox configuration cannot be weakened by project configuration.
Git hooks, MCP stdio servers, and plugin runtimes use the same conservative
environment boundary. Unsafe auto-approval is disabled unless an operator
explicitly opts into the compatibility escape hatch.

### Web and browser automation

The web surface includes:

- guarded HTTP(S) fetching;
- Brave and Tavily search with provider provenance;
- automatic search-provider fallback or an explicitly pinned provider;
- public-host, private-network, loopback, content-type, size, and domain
  restrictions;
- durable citations for search and fetch results; and
- normalized source records exposed to both the model and structured clients.

The optional browser surface owns an isolated Playwright Chromium context and
provides:

- navigation, click, type, scroll, back, and stable element references;
- bounded ARIA snapshots rather than unbounded DOM dumps;
- bounded vision screenshots;
- safe workspace uploads;
- bounded atomic workspace downloads with no-overwrite-by-default behavior;
- public-host and allowed-domain policy for navigation, subresources, and
  WebSockets;
- disabled service workers and blocked password-field filling;
- ephemeral contexts by default;
- optional Ash-owned persistent browser profiles; and
- deterministic browser cleanup and health diagnostics.

Attaching to an already-running browser through CDP is not currently exposed.

### MCP integration

Ash connects local and remote Model Context Protocol servers over stdio,
Streamable HTTP, and the SSE configuration alias. MCP support includes:

- server add, remove, list, status, login, logout, and targeted refresh;
- live tools, resources, resource templates, prompts, and task operations;
- OAuth 2.1 discovery, protected-resource metadata, S256 PKCE, resource
  indicators, callback-state validation, and dynamic client registration;
- explicit operator authorization rather than surprise browser prompts during
  agent runs;
- resource-bound access and refresh tokens with private local storage;
- automatic token refresh and explicit insufficient-scope step-up handling;
- exact draft-aware schema validation in a bounded secret-free subprocess;
- preservation of rich content blocks, structured data, metadata, and protocol
  errors;
- no replay of a rejected or ambiguously dispatched side effect;
- session-expiry recovery without reposting the rejected tool call;
- paginated capability listing and live catalog reconciliation;
- atomic publication of validated tool-list changes;
- catalog quarantine when refresh fails; and
- in-flight tool-snapshot and contract verification before sending a call.

MCP configuration can be user-owned or enabled for a trusted workspace. MCP
resource mentions can be selected directly from terminal completion.

### Extensions, skills, and hooks

Ash is extensible without changing the core runtime:

- local plugins with a root `plugin.json` manifest;
- plugin-provided skills, commands, agents, hooks, MCP servers, and executable
  tools;
- namespacing and dependency constraints to prevent collisions;
- local-directory and trusted HTTPS Git repository sources;
- publisher-pinned, signed catalogs with bounded redirect-free caching;
- validation for traversal, links, malformed manifests, oversized components,
  missing dependencies, and unsafe replacements;
- enable, disable, uninstall, inventory, search, and atomic live reload;
- isolated versioned JSON-RPC stdio for executable plugins;
- lazy plugin startup with no ambient secrets or network access;
- ordinary approval, audit, hook, sandbox, and dry-run policy around plugins;
- modern Markdown `SKILL.md` instruction skills that do not execute embedded
  code;
- opt-in legacy executable-skill compatibility; and
- bounded lifecycle hooks for sessions, turns, models, tools, errors, and
  policy gates.

Critical pre-tool hooks fail closed. Observer-hook failures cannot corrupt a
completed turn. Custom Markdown commands support arguments, namespaces,
completion, and trusted user or project sources.

### Subagents and delegation

Ash can run provider-backed workers as bounded, role-specific agents:

- researchers and reviewers with read-only tools;
- coders with scoped edit tools;
- testers with shell access only when a full OS sandbox is active;
- isolated Git worktrees and retained `ash-agent/*` branches;
- live status, stop, resume, steering, messages, reports, and branch review;
- durable task IDs, ownership leases, capacity admission, crash recovery,
  cancellation, time/token/cost budgets, results, and artifacts;
- dependency graphs with atomic submission and ready-task scheduling;
- redacted predecessor results treated as untrusted evidence;
- event replay with type and cursor filtering; and
- embedded delegation through the Python client.

The remote-agent tools can discover configured A2A peers and delegate to them
with operator-owned bearer credentials held outside model arguments.

### Protocols and integrations

Ash exposes or consumes the following integration surfaces:

| Surface | Capability |
| --- | --- |
| ACP v1 | Stdio editor/agent host integration with session lifecycle, prompts, cancellation, tool progress, usage, text, resource links, and stdio/HTTP/SSE MCP support |
| A2A 1.0 | Authenticated Agent Card, JSON-RPC, HTTP+JSON routes, task polling, streaming, cancellation, context continuation, inspection, and outbound delegation |
| HTTP API | Authenticated synchronous turns, live SSE turn events, session fork, and session tree endpoints |
| JSON-RPC | Structured runtime and session integration for external hosts |
| LSP 3.18 | Managed lazy language servers for diagnostics, hover, definitions, references, implementations, symbols, and call hierarchy |
| Python SDK | Async client access to turns, sessions, plans, steering, events, usage, storage, automation, and agent delegation |

Managed LSP detects installed basedpyright/pyright,
typescript-language-server, gopls, rust-analyzer, clangd, and
lua-language-server processes. It never downloads a server, rejects
out-of-workspace semantic results, and does not expose rename or code-action
operations yet.

### Durable automation

Ash can persist unattended prompts as:

- one-shot jobs at an explicit future instant;
- elapsed intervals; or
- five-field cron schedules in named IANA time zones.

Automation includes validated schedule parsing, misfire grace, whole-turn
timeouts, token budgets, cancellation, run history, pause/resume, soft delete,
worker liveness heartbeats, crash recovery, and external-supervisor-friendly
workers. Unattended calls continue to obey the same permission, trust, and
fail-closed approval rules as interactive work.

### Operations and lifecycle

The operational surface includes:

- local health diagnostics covering credentials, providers, web search,
  browser, storage, automation, extensions, A2A, MCP, LSP, and workspace
  trust;
- secret-free JSON status for setup, providers, capabilities, sandbox, and
  diagnostics;
- database integrity checks, consistent backups, validated restore, redacted
  debug bundles, metrics, and tamper-evident audit export;
- explicit update/version checks with no background telemetry;
- selective reset of configuration, sessions, cache, or all local state;
- idempotent installation and repair behavior that preserves selected optional
  capability packs; and
- lazy loading so lightweight version/help/status paths do not initialize the
  full provider, browser, server, repository, or TUI stack.

## Project shape

Ash is a typed Python package with a public `ash.*` namespace and a console
entry point. Optional capability packs keep server, vector, browser, ACP, and
A2A dependencies separate from the lean core. The repository contains:

- the installable harness and runtime under `src/ash/`;
- unit, integration, packaging, and real-browser coverage under `tests/`;
- architecture and parity analysis under `docs/architecture/`;
- operational guides under `docs/guides/`; and
- versioned protocol and extension contracts under `docs/reference/`.

The [documentation index](docs/README.md) links to the maintained guides,
including [permissions and managed policy](docs/guides/PERMISSIONS.md),
[durable session branching](docs/guides/SESSION_BRANCHING.md),
[durable automation](docs/guides/DURABLE_AUTOMATION.md), and the
[production parity checklist](docs/architecture/PRODUCTION_HARNESS_PARITY.md).

## Current boundaries

Ash deliberately reports unsupported or partial surfaces instead of pretending
they are complete:

- subscription-based provider authentication is not included;
- browser CDP attachment is not exposed;
- ACP image/audio/embedded-resource and advanced session capabilities are not
  advertised until their full behavior is implemented;
- A2A push notifications, file/data modalities, extended cards, gRPC, and
  signed-card trust policy are not currently advertised;
- LSP rename and code actions are not exposed; and
- a broad messaging-channel gateway is not currently part of Ash.

Ash is a work in progress (WIP).
