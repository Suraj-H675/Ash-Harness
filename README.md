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

### Platform support

Ash supports native **Linux** and **macOS** on Python 3.12 through 3.14.
Windows users should run Ash inside **WSL2**, which follows the supported Linux
runtime path. Native Windows is intentionally not supported yet; the CLI and
public installer fail fast there instead of entering partially tested platform
branches.

### Installation

Ash's intended production install path is an **immutable GitHub Release**, not
a moving branch. The release workflow builds and attests the artifacts and the
installer verifies the selected release before installation.

**Current status:** the first production release,
[`ash-v0.1.0`](https://github.com/Suraj-H675/Ash-Harness/releases/tag/ash-v0.1.0),
is published and GitHub reports it as immutable. Its release attestation and
installer asset verify successfully. A clean isolated pipx install from the
published wheel and a same-ref repair journey both pass, including preservation
of the `observability` capability pack.

The production install flow is:

```bash
repo="Suraj-H675/Ash-Harness"
tag="$(gh release view -R "$repo" --json tagName,isImmutable --jq 'select(.isImmutable == true) | .tagName')"
test -n "$tag" || { echo >&2 "No immutable Ash release is available."; exit 1; }
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
gh release verify "$tag" -R "$repo"
gh release download "$tag" -R "$repo" --pattern install-ash.py --dir "$tmp"
gh release verify-asset "$tag" "$tmp/install-ash.py" -R "$repo"
python3 -I "$tmp/install-ash.py" --ref "$tag"
```

This requires the GitHub CLI (`gh`). The installer preserves an existing
pipx/uv-managed Ash installation when possible, resolves the wheel from that
same immutable release, downloads it into a private temporary directory, and
verifies the exact byte count and GitHub SHA-256 digest itself before invoking
the package manager. pipx/uv then install that already-verified local wheel;
the hash fragment is retained only as defense in depth. Optional capability
packs such as `--extra browser` remain available on that wheel install.
Development setup and release-maintainer instructions are intentionally
separate; see [Contributing](CONTRIBUTING.md) and
[Releasing Ash](docs/guides/RELEASING.md).

## Capability overview

### Provider and model layer

Ash has a descriptor-driven provider catalog and a provider-neutral runtime.
Supported built-in routes include:

| Route | Models and behavior |
| --- | --- |
| Anthropic | Claude models through the Anthropic Messages API |
| OpenAI | GPT models through the OpenAI API using an API key or optional Sign in with ChatGPT plan auth |
| Google Gemini API | Gemini models through Google AI Studio's OpenAI-compatible endpoint |
| Google Vertex AI | Enterprise OpenAI-compatible inference through explicit project/location scope and Google Application Default Credentials (gcp extra) |
| Amazon Bedrock | Bedrock Runtime OpenAI Chat Completions through the AWS credential chain and SigV4 (aws extra) |
| OpenRouter | Multi-provider gateway routing |
| Hugging Face | Inference Providers gateway to hundreds of hosted chat models |
| Vercel AI Gateway | One API key for hundreds of routed models through an OpenAI-compatible gateway |
| DeepSeek | Chat and reasoning models |
| Groq | Fast hosted open models |
| Mistral | Mistral models through an OpenAI-compatible endpoint |
| xAI | Grok models through an OpenAI-compatible endpoint |
| Together AI | Hosted open models |
| Fireworks AI | Hosted inference |
| Cerebras | Fast hosted models |
| NVIDIA API Catalog | Hosted NVIDIA and partner models through an OpenAI-compatible endpoint |
| Ollama | Local models with no API key requirement |
| LM Studio | Local OpenAI-compatible model server |
| vLLM | Self-hosted OpenAI-compatible model server |
| Custom routes | User-defined OpenAI-compatible providers with explicit auth mode |

The provider layer also provides:

- model strings in `provider/model` form;
- live model discovery plus explicit bounded completion verification through
  `ash providers test`, with catalog and model-call readiness reported separately;
- provider and model capability metadata for native tools, vision,
  reasoning, locality, context, and output budgets;
- isolated named profiles for separate model, provider, credential, and SQLite
  runtime state; user-installed extensions, trust, and permission policy remain
  global to the Ash user;
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

Enterprise cloud routes keep cloud identity outside Ash configuration. Vertex
requires an explicit user-owned project and location and refreshes short-lived
Application Default Credentials in memory. Bedrock uses the AWS default
credential/profile chain through the official OpenAI Bedrock provider and the
recommended bedrock-runtime endpoint. AWS native model/profile discovery is
shown as candidate discovery only; ash providers test performs the bounded
completion that actually verifies Runtime Chat Completions compatibility.

OpenAI additionally supports optional **Sign in with ChatGPT** for eligible
ChatGPT plans. Run `ash auth chatgpt login` to register/sign in, inspect saved
registrations with `ash auth chatgpt accounts`, switch with
`ash auth chatgpt use CLIENT_ID`, and clear the active session with
`ash auth chatgpt logout`. This path uses OpenAI's public Responses API with
stateless history replay; the existing OpenAI API-key route remains the default.
MCP OAuth is supported separately for protected MCP servers.

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
| Text and structure | Directory listing, bounded globbing, descriptor-scoped bounded text/regex search, symbol lookup, and reference lookup |
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
- persistent global `~/.ash/ASH.md` plus trusted hierarchical project
  instructions with ordered `ASH.md` → `AGENTS.md` → `CLAUDE.md`
  compatibility fallbacks, live refresh between model iterations, and path-sensitive
  nested-scope activation after file reads, with bounded imports, diagnostics, and
  conflict linting;
- project-root-aware repository maps with active-file ranking;
- searchable redacted session memory;
- explicit project memory indexing, search, clear, and privacy export;
- private per-workspace SQLite project memory under Ash-owned state;
- SQLite FTS5 lexical retrieval by default, with transactional document
  replacement and bounded stale-file reconciliation;
- optional semantic retrieval only when the user explicitly configures OpenAI
  embeddings or the `local-embeddings` ONNX capability pack, with lexical
  fallback when embeddings are unavailable and no hidden embedding API calls
  by default; choosing OpenAI embeddings sends the text being indexed and
  memory-search queries to OpenAI for embedding, while `none` and ONNX keep
  embedding work local; and
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
- one durable foreground Goal per session, with persisted objective/evidence,
  bounded automatic continuation, no-spin suppression, explicit pause/resume/
  clear controls, and interruption-safe pausing; and
- persisted token, cache, usage, and cost history with estimated portions
  clearly labeled.

Recovery is conservative: completed work is retained, direct file edits are
compensated only when hashes prove what happened, and non-file side effects
are never guessed or silently replayed.

### Persistent Goals

Use `/goal <objective>` for a foreground objective that should keep advancing
across ordinary turn boundaries until it is verified complete, paused, blocked,
or reaches its continuation window. `/goal` shows status; `/goal pause`,
`/goal resume`, and `/goal clear` control the lifecycle. The default automatic
continuation window is 10 turns and is configurable with
`max_goal_continuations` from 1 through 100.

The model can only record bounded progress evidence or mark the current Goal
complete; creation, pause, resume, and clear remain user/host-owned controls.
Automatic continuation requires a real non-Goal tool action in the preceding
turn, so bookkeeping or an empty answer cannot create a spin loop. Cancellation
or a runtime failure pauses the Goal conservatively. Goals are session-scoped
foreground work; use durable automation for scheduled or unattended jobs.

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

- Bubblewrap 0.12.0 or newer on Linux;
- `sandbox-exec` on macOS;
- Docker with the packaged `ash-sandbox` baseline; or
- explicitly reported direct execution when no isolation backend is available.

Isolated commands default to disabled network access, a scrubbed environment,
workspace-scoped filesystem access, bounded output, and process-tree cleanup.
User-owned sandbox configuration cannot be weakened by project configuration.
Git hooks, MCP stdio servers, and plugin runtimes use the same conservative
environment boundary. Unsafe auto-approval is disabled unless an operator
explicitly opts into the compatibility escape hatch. Safe `auto_approve`
requires both full filesystem/network isolation and aggregate CPU/memory
containment; with `sandbox_backend = "auto"`, Ash therefore selects Docker for
autonomous execution and fails closed if that bounded backend is unavailable.

Docker sandbox execution also defaults to a **4096 MiB RAM limit, 2 CPU cores,
and 256 processes**. `sandbox_docker_memory_mb` and `sandbox_docker_cpus` are
user-owned settings; setting either numeric limit to `0` disables that Docker
limit. Docker's memory setting is a RAM cap; additional swap availability
follows the Docker daemon/kernel policy. Bubblewrap and macOS `sandbox-exec` do
not claim equivalent aggregate CPU/memory containment and remain appropriate
for approval-gated interactive execution rather than safe autonomous mode.

For ordinary mutable Docker command sessions, `/workspace` is a Docker-daemon
host bind mount so edits flow back to the real repository. That boundary assumes
the local OS account and Docker daemon host are trusted against concurrent
replacement of the workspace pathname. Executable plugins use the stronger
snapshot path instead: validated plugin bytes are staged into an Ash-owned
Docker volume and mounted read-only at execution time.

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
- runtime `/browser inspect` plus `connect` / `disconnect` / `status`
  switching for a
  loopback Chromium CDP endpoint, with candidate preflight and an Ash-owned
  isolated context;
- read-only source-browser inspection showing bounded, terminal-safe tab titles
  and redacted URLs before attachment; inspection does not read cookies,
  storage, DOM content, or take control of those tabs;
- explicit `--reuse-storage-state` copying of bounded cookies/local storage
  from the attached browser into Ash's isolated context; and
- deterministic browser cleanup and health diagnostics.

Remote CDP endpoints and direct ownership/control of pre-existing browser tabs
remain intentionally unsupported.

### MCP integration

Ash connects local and remote Model Context Protocol servers over stdio,
Streamable HTTP, and the SSE configuration alias. MCP support includes:

- server add, remove, list, status, live connection/capability probing, login,
  logout, and targeted refresh;
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
- catalog quarantine when refresh fails;
- opt-in client-side sampling with explicit review before model execution and
  again before the sampled response is released to the server;
- opt-in form elicitation with typed review/edit/decline/cancel flows and
  credential-like fields rejected in favor of future URL-mode handling; and
- in-flight tool-snapshot and contract verification before sending a call.

MCP configuration can be user-owned or enabled for a trusted workspace. The
client-interaction controls `mcp_sampling_enabled`, `mcp_elicitation_enabled`,
and `mcp_sampling_max_tokens` are user-owned only: project configuration cannot
enable or widen them. Headless/SDK use requires explicit review callbacks. Ash
does not currently advertise MCP sampling tools/context/task augmentation or URL
elicitation. MCP resource mentions can be selected directly from terminal
completion.

### Extensions, skills, and hooks

Ash is extensible without changing the core runtime:

- local plugins with a root `plugin.json` manifest;
- plugin-provided skills, commands, agents, hooks, MCP servers, and executable
  tools;
- namespacing and dependency constraints to prevent collisions;
- local-directory and trusted HTTPS Git repository sources;
- signed catalogs with pinned Ed25519 trust keys and bounded redirect-free
  caching; persistent marketplace registrations bind both the signing key ID
  and the SHA-256 fingerprint of the verified public key, so replacing key
  material under a reused ID requires explicit re-registration/replacement;
  catalog v2 binds a lowercase publisher namespace into the signed
  payload, repeatable `--catalog` inputs can be searched together, and
  `@publisher/plugin` selects an exact publisher when names overlap;
- persistent, profile-scoped `ash marketplace list/add/remove` registration
  for signed catalog v2 publishers; registered sources are re-verified on use,
  publisher drift fails closed, and explicit `--catalog` still overrides the
  saved registry;
- managed Git/catalog installs persist Ash-owned source, ref, resolved commit,
  version, signed publisher, and source-origin provenance transactionally with
  plugin replacement/uninstall; local replacements clear stale remote
  provenance;
- plugin Git acquisition runs with a scrubbed non-interactive environment, an
  Ash-owned empty home/config directory, and user/system Git configuration,
  credential prompts, askpass, templates, and inherited proxy/secret overrides
  disabled;
- lifecycle mutations serialize dependency-topology decisions with activation
  state changes, reject replacements/updates that would newly break installed
  reverse dependencies, and keep provenance aligned with the live tree across
  ordinary failures and process crashes. A durable per-root lifecycle journal
  records the exact tree/provenance/activation transition before publication;
  interrupted pre-commit work rolls back, committed work is finalized on the
  next lifecycle/runtime discovery, transaction-owned stage/backup/quarantine
  directories are never discovered as plugins, and recovery verifies the
  plugin-root plus managed activation-state parent identities before mutation;
- `ash extensions update NAME` and `/plugins update NAME` update one tracked
  plugin through that provenance: direct Git refs use resolved-commit no-op
  detection, catalog installs are re-resolved through the currently verified
  signed catalog/marketplace source, changed candidates reuse atomic replacement,
  disabled state is preserved, and ambiguous legacy provenance fails closed;
- `ash extensions update --all` and `/plugins update --all` process every
  tracked plugin in deterministic order, preserve each plugin's independent
  atomic update boundary, continue after per-plugin failures, and report
  updated/unchanged/error outcomes; the CLI returns a failing status when any
  tracked update fails. Dependency migrations that cannot be made valid one
  plugin at a time are handled as a coordinated quiesced cohort: Ash
  atomically records and temporarily disables the connected component, applies
  each verified plugin through its existing crash-safe replacement boundary,
  validates the final enabled graph, then restores the component's prior
  activation. A crash or failed candidate leaves a durable resumable quiesce
  record instead of exposing a broken enabled graph;
- validation for traversal, links, malformed manifests, oversized components,
  missing dependencies, and unsafe replacements;
- update, enable, disable, uninstall, inventory, search, and atomic live reload;
- isolated versioned JSON-RPC stdio for executable plugins;
- lazy plugin startup with no ambient secrets or network access;
- ordinary approval, audit, hook, sandbox, and dry-run policy around plugins;
- modern Markdown `SKILL.md` instruction skills that do not execute embedded
  code;
- a library-only legacy executable-skill compatibility API that remains outside
  the default runtime/CLI because it executes user-authored code in-process; and
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
| ACP v1 | Stdio editor/agent host integration with new/load/list/close plus durable fork/resume session lifecycle, prompts, cancellation, tool progress, usage, text, bounded inline images, resource links, and stdio/HTTP/SSE MCP support |
| A2A 1.0 | Authenticated Agent Card, JSON-RPC, HTTP+JSON routes, bounded concurrent task execution, task polling, streaming, cancellation, context continuation, inspection, and outbound delegation |
| HTTP API | Authenticated bounded-concurrency synchronous turns, live SSE turn events, steering, session fork, and session tree endpoints |
| JSON-RPC | Structured runtime and session integration for external hosts |
| LSP 3.18 | Managed lazy language servers for diagnostics, hover, definitions, references, implementations, symbols, and call hierarchy |
| Python SDK | Async client access to turns, durable Goals, sessions, plans, steering, events, usage, storage, automation, and agent delegation |

Managed LSP detects host-installed basedpyright/pyright,
typescript-language-server, gopls, rust-analyzer, clangd, and
lua-language-server processes. Trusted project configuration can explicitly
select a workspace-local server, but project executables are not implicitly
auto-detected. It never downloads a server, rejects
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
- opt-in `ASH_DEBUG=1` structured diagnostics in private rotating
  `~/.ash/logs/ash.jsonl`, with secret redaction and session/turn/tool
  correlation while normal human logs remain on stderr;
- explicit update/version checks with no background telemetry;
- selective reset of configuration, sessions, cache, or all local state;
- idempotent installation and repair behavior that preserves selected optional
  capability packs; and
- lazy loading so lightweight version/help/status paths do not initialize the
  full provider, browser, server, repository, or TUI stack.

## Project shape

Ash is a typed Python package with a public `ash.*` namespace and a console
entry point. Optional capability packs keep server, local embeddings, browser,
ACP, and A2A dependencies separate from the lean core. The repository contains:

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

- native Windows execution is not currently supported; use WSL2;
- OpenAI ChatGPT-plan authentication is service-verified through real browser
  sign-in, model discovery, plan-backed Responses completion, and native
  function-call continuation;
- remote browser CDP and direct takeover of pre-existing tabs are not exposed;
- ACP audio/embedded-resource, session delete, additional directories, modes,
  terminal/filesystem callbacks, and registry publication are not advertised
  until their full behavior is implemented;
- A2A push notifications, file/data modalities, extended cards, gRPC, and
  signed-card trust policy are not currently advertised;
- LSP rename and code actions are not exposed; and
- a broad messaging-channel gateway is not currently part of Ash.

Ash is not yet justified as fully production-ready under the project Charter.
The current implementation is strong, but the post-closure Charter audit
reopened the final mission gate because the documented production release does
not yet exist and whole-product parity still has material gaps. Production
observability is now available as an explicit opt-in OpenTelemetry/OTLP
trace-and-metrics path; qualified boundaries above remain truthful support
limits rather than claims of feature identity with every comparator.

### Observability

Ash can export **content-free OpenTelemetry traces and metrics** over OTLP/HTTP
when the optional `observability` extra is installed and the user explicitly
enables it. It is off by default and project configuration cannot enable it or
redirect its collector.

Typical user-owned configuration:

```toml
observability_enabled = true
observability_otlp_endpoint = "http://127.0.0.1:4318"
observability_sample_rate = 1.0
```

The same settings are available as `ASH_OBSERVABILITY_*` environment
variables. Standard `OTEL_EXPORTER_OTLP_ENDPOINT` or per-signal trace/metric
endpoint variables are honored only after Ash observability has been explicitly
enabled.

Exported telemetry covers turn, provider-request, tool, retry/circuit, token,
context, outcome, and duration signals. Ash does **not** export prompts,
assistant responses, tool arguments/results, file paths, credential material,
or provider error messages. Run `ash setup status` or `ash doctor` to inspect
the effective state. See
[Observability](docs/reference/OBSERVABILITY.md) for the full contract.
