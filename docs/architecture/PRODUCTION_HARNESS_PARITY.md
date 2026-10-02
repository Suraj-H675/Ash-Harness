# Ash Production Harness Parity

**Scope:** native Linux and macOS coding harness. Windows users run Ash inside
WSL2 through the supported Linux runtime path; native Windows is intentionally
outside the supported/tested host matrix.
Authentication is limited to API keys, custom OpenAI-compatible endpoints,
local model runtimes, and optional service-verified OpenAI ChatGPT-plan
sign-in. MCP OAuth is part of the remote MCP transport boundary.

This is the authoritative product checklist. Older roadmap files describe
historical intent and do not prove that a feature works.

This checklist measures Ash against its own required behavior; it is not a
claim that Ash is feature-equivalent to every public harness. Competitor scope
is evidence, not an automatic backlog: gateway/chat-channel/mobile/voice
surfaces matter only when they solve a real Ash product requirement. “Verified
locally” means the Ash implementation and its available tests support the
listed contract. It does not imply production maturity, ecosystem breadth,
native client coverage, or interoperability with every comparator.

## Evidence Rules

- **Verified:** wired into the installed product and covered by appropriate
  end-to-end/integration evidence.
- **Verified hosted:** behavior that specifically depends on a supported host
  or CI environment has also passed on that real hosted environment.
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
- Codex Goals: <https://developers.openai.com/cookbook/examples/codex/using-goals-in-codex>
- OpenAI Sign in with ChatGPT for open-source apps:
  <https://developers.openai.com/siwc/token-sharing-open-source>

Research is clean-room: proprietary or leaked source is not used. Public
benchmark framing was refreshed on 2026-10-01 against current official/public
documentation.

## Mission Finish Lines

These are the finite gates for deciding whether the Ash mission is actually
complete. They supersede the previous hardening-phase F1-F5 gates without
reopening already-verified areas mechanically.

1. **M1 — Evidence and claims:** current comparator research, support scope,
   parity statuses, and public claims agree with verified reality. Evidence-only
   gaps are either closed or explicitly scoped.
2. **M2 — Supported-host production core:** Linux/macOS safety, sandboxing,
   terminal UX/accessibility, resource containment, recovery, and security
   boundaries have realistic supported-host evidence and no material known
   production gap.
3. **M3 — Interoperability confidence:** provider, local-model, MCP, LSP,
   browser, and IDE/remote protocol claims have enough real implementation or
   vendor/runtime conformance evidence to justify their support claims.
4. **M4 — High-value harness parity:** evaluate remaining comparator advantages
   by real coding-harness value. Implement the ones that materially strengthen
   Ash (for example durable long-horizon objectives if Ash lacks an equivalent),
   and explicitly reject unrelated scope rather than copying feature counts.
5. **M5 — Final mission audit:** realistic first-run, advanced, interrupted,
   hostile/failure, packaging, performance, security, and maintenance journeys
   are revalidated; docs are truthful; supported-host CI is green; and the final
   benchmark no longer identifies a material core-harness weakness that should
   be fixed before calling Ash first-class and production-worthy.

Current production-foundation gate state: **M1-M5 are closed/current.** They
establish that Ash is production-worthy within its documented terminal
coding-harness scope; they do **not** establish literal whole-product parity
with every leading harness. Partial capability rows below remain deliberately
qualified and do not imply broader support than their recorded evidence.

## Whole-Product Leading-Harness Parity Program

The next finite program asks a stronger question than M1-M5:

> Is there any important user need for which a leading current harness makes
> Ash materially second-rate, unless Ash deliberately and defensibly chooses a
> different product boundary?

This is not a feature-count exercise. A comparator capability closes a gap when
Ash matches or exceeds the underlying user outcome, solves it differently with
comparable quality, or explicitly rejects it after current evidence shows that
it would not materially improve Ash. A gate is not closed merely because a
document says so; current implementation, realistic tests, real service/runtime
evidence where warranted, and truthful user-facing claims must support closure.

The program is intentionally finite:

0. **P0 — Production foundation — CLOSED.** M1-M5, immutable release
   ash-v0.1.0, supported-host CI, packaging/install/repair, safety,
   recovery, observability, and production-core evidence remain the completed
   baseline. Reopen P0 only for new contradictory evidence or a regression that
   directly invalidates that baseline.
1. **P1 — Provider/model parity — OPEN.** Close material gaps in provider
   breadth and correctness, authentication/subscription paths, discovery and
   switching, capability metadata, tool/reasoning/multimodal semantics,
   streaming/cancellation, usage/cost normalization, failover/routing, local
   runtimes, onboarding, and real-provider/runtime conformance. Closure requires
   a current comparator pass plus enough real vendor/runtime evidence for every
   support claim that materially depends on external behavior.
2. **P2 — Agent/workspace parity — NOT STARTED.** Evaluate and close material
   gaps in subagent orchestration, durable/background work, worktree/workspace
   isolation, delegation, steering, recovery, remote execution where it solves
   real coding workflows, and multi-workspace ergonomics.
3. **P3 — Web/computer interaction parity — NOT STARTED.** Evaluate browser
   control breadth, signed-in browser workflows, web research/fetch ergonomics,
   browser attachment/remote operation, and whether general computer use is a
   justified Ash capability rather than copying broader assistant products.
4. **P4 — Extensibility/ecosystem parity — NOT STARTED.** Evaluate plugins,
   skills, MCP, hooks, custom commands, discovery, installation, updating,
   provenance/trust, and marketplace/ecosystem experience. Hosted ecosystem
   breadth is work only when it materially improves real Ash usage.
5. **P5 — Interfaces and anywhere-access parity — NOT STARTED.** Determine
   whether remote access, web UI, messaging surfaces, companion clients, or
   other access modalities solve important Ash workflows; implement justified
   gaps and explicitly reject unrelated product categories.
6. **P6 — User-experience parity — NOT STARTED.** Benchmark the complete
   journey from installation and onboarding through model selection, coding,
   approvals, sessions, debugging, interruption/recovery, updates, and
   troubleshooting. Close material friction even when the underlying backend
   capability already exists.
7. **P7 — Final adversarial comparator pass — NOT STARTED.** Re-run current
   evidence against the strongest relevant harnesses, including OpenClaw,
   Hermes, Claude Code, Codex CLI, Gemini CLI, OpenCode, Aider, and any newer
   serious comparator. Confirm no material user-value gap was missed or hidden
   by Ash's existing architecture or docs.
8. **P8 — Whole-product parity decision gate — NOT STARTED.** Every remaining
   material comparator advantage must be classified with evidence as
   matched/exceeded, solved differently to comparable quality, explicitly
   out-of-scope for a defensible product reason, or still open. Only when no
   important unresolved user need remains may Ash claim whole-product
   leading-harness parity. Literal feature identity is never the criterion.

**Active gate: P1 — Provider/model parity.** Do not start P2 merely because P1
work is slow or because another interesting gap is discovered. Record
cross-gate findings for later and close P1 first.

### P1 finite closure checklist

P1 is split into bounded sub-gates so provider work cannot become an endless
catalog-expansion exercise:

1. **P1A — Provider and enterprise route coverage — OPEN.** Re-evaluate the
   direct providers, gateways, and enterprise-hosted model routes that serious
   coding-harness users actually need. Ash already covers Anthropic, OpenAI,
   Google AI Studio, OpenRouter, Hugging Face Inference Providers, Vercel AI
   Gateway, DeepSeek, Groq, Mistral, xAI, Together, Fireworks, Cerebras,
   NVIDIA, Ollama, LM Studio, vLLM, and explicit custom OpenAI-compatible
   endpoints. Current comparator evidence makes enterprise cloud routes such as
   Amazon Bedrock and Google Vertex AI a real remaining question rather than a
   provider-count target. Add only routes whose governance, billing, deployment,
   or authentication value cannot already be met cleanly through Ash's existing
   surfaces.
2. **P1B — Authentication and credential resilience — OPEN.** Preserve the
   verified ChatGPT-plan multi-account path and cross-provider model fallback,
   then close any material same-provider credential/profile rotation,
   subscription/OAuth, expiry, quota, or account-selection gap. Do not borrow
   another product's private credentials or unsupported OAuth flow merely to
   increase auth-method count.
3. **P1C — Model discovery and capability semantics — OPEN.** Verify that
   model catalogs, aliases, context/output limits, vision, reasoning, native
   tools, usage, prompt caching, streaming terminal semantics, and model
   switching remain provider-owned and fail conservatively when metadata is
   absent or contradictory. Fix concrete incorrect assumptions rather than
   hard-coding fast-changing model lists.
4. **P1D — Local runtime parity — OPEN.** Close the material difference between
   merely connecting to Ollama/LM Studio/vLLM and a strong local-model user
   journey. Decide with evidence whether Ash should manage runtime/model
   lifecycle itself or integrate cleanly with runtime-owned lifecycle commands;
   validate capability discovery, context sizing, tool calling, streaming,
   cancellation, health/readiness, and realistic coding turns on supported
   local runtimes.
5. **P1E — Real service/runtime conformance and claims — OPEN.** Run the
   smallest set of live provider/runtime journeys that materially changes
   confidence, keep unsupported claims qualified, and finish with a current
   comparator pass. P1 closes only when no important provider/model user need
   remains materially weaker without a deliberate product reason.

Work P1A-P1E in evidence-driven slices; several may advance together when one
implementation legitimately spans them, but do not declare P1 closed until all
five are resolved.

### P1 progress — provider correctness slice 1

The first P1 implementation slice fixed confirmed correctness gaps in provider
surfaces Ash already claims rather than expanding the catalog for feature-count
parity:

- OpenAI-compatible streaming now preserves current `reasoning` and legacy
  `reasoning_content` deltas as bounded canonical reasoning instead of silently
  dropping them. The shared path covers the generic OpenAI-compatible adapter,
  DeepSeek, and Groq while retaining Ash's existing no-replay-after-output
  boundary by exposing the accumulated reasoning only with the provider's
  terminal chunk.
- DeepSeek usage now preserves provider-reported prompt-cache hits so cached
  input is not treated as ordinary uncached input by downstream accounting.
- Ollama capability negotiation consumes current declared tool, vision, and
  thinking metadata, retains older metadata/template probing only when the
  current capability field is absent, fails conservatively on malformed
  declared capability metadata, accepts architecture-qualified context-length
  metadata, preserves streamed `message.thinking`, and treats an omitted model
  tag as the documented `:latest` alias during readiness verification.
- Failover capability aggregation no longer advertises a context or output
  ceiling when any child limit is unknown. Failover also binds active
  provider/model identity before each child attempt, so runtime events,
  pricing selection, and observability identify the provider that actually
  served a successful backup rather than pairing the primary identity with a
  backup model. OpenTelemetry retains the requested model separately from the
  response model.

Focused validation after the final boundary changes passes **165 provider,
readiness, failover, and observability tests**. The complete affected
core-loop/failover files additionally pass **184 tests**. Ruff, targeted Mypy,
and `git diff --check` are green.

This slice advances P1C/P1D/P1E but does not close them. Explicitly remaining:
unified live model discovery/switching across API-key and ChatGPT-plan auth,
provider-owned capability truth for fast-changing hosted models rather than
stale name heuristics, correct representation of unknown model pricing instead
of implying zero cost, the P1A enterprise-route decision, P1B same-provider
credential/account resilience, supported local-runtime lifecycle and real
Ollama/LM Studio/vLLM conformance, and adapter-level cancellation/stream-cleanup
verification.

### M4 product decisions

The remaining comparator differences have now been reduced to explicit product
decisions rather than an open-ended feature inventory:

- **Service-verified — optional OpenAI ChatGPT-plan sign-in.** OpenAI now provides an
  official open-source OAuth/PKCE flow that lets eligible users run supported
  Responses API requests using their ChatGPT plan without configuring an API
  key. Ash now implements this as a distinct auth/transport path: profile-aware
  descriptor-anchored private multi-account credentials; stable host identity;
  exact loopback state/nonce/PKCE validation; OIDC signature/issuer/audience/
  nonce checks; issued-client-ID reuse; bounded recovery from first-exchange
  `invalid_grant`; plan-scope enforcement; cross-process serialized rotating
  refresh tokens; revocation-aware logout; live account model discovery;
  explicit status/login/logout/accounts/use/models CLI surfaces; setup wizard
  selection; and public Responses API requests with `store=false`,
  `stream=true`, full history replay, strict terminal handling, canonical
  native function calls, and bounded opaque encrypted reasoning replay. The
  existing API-key route remains unchanged. Deterministic local tests cover the
  auth, replay, setup, routing, recovery, and stream contracts. On 2026-10-01,
  a real interactive OpenAI sign-in completed successfully, live model
  discovery returned the account's selectable catalog, `ash providers test
  openai/gpt-6-astra` verified a real plan-backed Responses completion, and a
  separate two-turn live native-function journey completed through canonical
  assistant/tool replay with provider-reported usage.
- **No separate M4 gap — remote browser control.** Ash already owns a managed
  Playwright browser and explicit loopback CDP attachment with guarded browser
  policy. Cross-machine browser relays/control planes are useful to gateway or
  remote-operations products, but are not a material missing coding-harness
  primitive while ACP/A2A/HTTP/SDK integration remains available for remote
  hosts.
- **No separate M4 gap — richer remote-agent modalities.** Ash already has
  durable local agent DAGs, official ACP client/server interoperability, and
  authenticated A2A delegation/task recovery. Additional presentation or
  deployment modalities become work only when a concrete coding workflow
  requires them.
- **Explicitly rejected for Ash core — gateway/chat-channel/mobile/voice/media
  breadth.** Those are central to general assistant/gateway products such as
  OpenClaw, but do not materially improve Ash's terminal-first coding-harness
  mission enough to justify the product, security, and maintenance surface.

M4 is closed: the ChatGPT-plan path passed the required real interactive OpenAI
sign-in/model-discovery/Responses completion journey, the stronger live
function-call continuation also passed, and the final comparator pass did not
identify another material Ash coding-core gap.

### M5 final Charter audit — closed 2026-10-02

The 2026-10-01 post-closure audit correctly reopened M5 and surfaced several
material production gaps. Those blockers have now been resolved or
scope-qualified with stronger evidence:

- **Production distribution — verified.** GitHub immutable releases are enabled,
  `ash-v0.1.0` is published and reports `isImmutable: true`, and
  `gh release verify` plus `gh release verify-asset` both succeed against the
  published release. The tag resolves exactly to commit
  `01f29c282ed3d34d01433b2a8c79ed778087d0f9`. Release CI completed
  successfully after running the supported-host/conformance matrix, building a
  fresh sdist and wheel, smoke-testing the artifacts, generating checksums and
  provenance, publishing the draft, and verifying immutability. A separate
  post-publication journey downloaded and verified the immutable
  `install-ash.py`, installed `ash-v0.1.0` into an isolated pipx environment
  with the `observability` capability pack, verified `ash 0.1.0`, simulated
  a missing app link, and proved same-ref repair restored the executable while
  preserving the capability pack. A true cross-version upgrade/rollback cannot
  be exercised until a second immutable release exists; the installer lifecycle
  remains covered by deterministic stateful upgrade/repair/rollback tests.
- **Production observability gap — addressed locally.** Ash now has an opt-in,
  user-owned OpenTelemetry/OTLP HTTP trace-and-metrics plane with locally owned
  SDK providers, parented turn/model/tool spans, retry/circuit/context/token
  metrics, bounded sampling/export controls, setup/doctor diagnostics, and
  fail-open exporter behavior. The observer boundary is structurally
  content-free: prompts, assistant output, tool arguments/results, paths,
  credential material, and provider error text are stripped before observers
  receive events. A real loopback OTLP collector test verifies trace and metric
  protobuf export and the absence of injected secret content on the wire.
- **Interoperability breadth remains explicitly partial by evidence.** The API-key
  provider row and local-model row remain Partial because live vendor/runtime
  conformance is much narrower than the deterministic protocol coverage,
  especially for LM Studio/vLLM and multi-vendor hosted providers.
- **Several whole-product surfaces remain intentionally scoped relative to
  broader products.** Browser control is loopback/isolated-context rather than
  remote/direct selected-tab or general computer-use breadth; the plugin
  marketplace lacks a first-party hosted/curated public ecosystem; MCP, ACP,
  and A2A intentionally leave unsupported protocol modalities unadvertised.
  These remain documented scope differences rather than hidden production
  deficiencies in Ash's terminal coding-harness mission.
  differences, but the Charter requires explicit evidence that they do not
  create a material whole-product weakness before closure.

Evidence that remains valid from the previous audit:

- **First-run, interrupted, maintenance, and performance journeys:** a bounded
  representative pack passed **42/42** tests covering fresh setup/process
  behavior, a real session journey, turn/crash recovery, maintenance,
  long-session memory behavior, and viewport performance.
- **Advanced journeys:** a separate pack passed **307/307** tests across the
  SDK, HTTP/JSON-RPC, modern MCP, plugin lifecycle, agent-task orchestration,
  and browser command/tool contracts.
- **Hostile/failure/security journeys:** anchored I/O, sandbox/policy,
  environment scrubbing, secret middleware, logging/redaction,
  background-process race defenses, and process-tree cleanup passed
  **345 tests** with **4 environment-dependent skips**.
- **Real provider journey:** optional ChatGPT-plan auth passed a real browser
  OAuth sign-in, live model discovery, `ash providers test
  openai/gpt-6-astra`, and a two-turn native function-call continuation with
  canonical tool-result replay and provider-reported usage.
- **Supported-host CI and packaging:** commit `1304bab` is fully green in
  hosted run `36876005830`, including Linux/macOS,
  Python 3.12/3.13/3.14, quality/packaging, minimum-dependencies, browser,
  Docker/native sandbox, PTY, MCP, and LSP lanes.
- **Comparator sanity check:** current official OpenClaw, Hermes, Claude Code,
  and Codex evidence does not identify another material terminal
  coding-harness-core weakness that should block production readiness. Their
  remaining advantages are primarily ecosystem scale, hosted/desktop/browser
  presentation, gateway/channels/mobile/media breadth, or managed cloud
  execution; Ash's documented scope decisions remain valid.
- **Documentation truthfulness:** the authoritative checklist is the single
  current verdict, while older research/audit files are explicitly labeled as
  dated evidence rather than competing product truth.

**M5 is closed.** The supported-host CI matrix is green, the production release
path is real and immutable, packaging/install/repair evidence is live rather
than simulated, observability and maintenance gaps are addressed, and the final
comparator pass does not identify a material terminal coding-harness-core
weakness that should block production readiness. Qualified Partial rows remain
truthful evidence boundaries, not open mission blockers.

## 1. Installation And Setup

| Capability | Ash status | Required production behavior |
|---|---|---|
| `ash` console command | Verified locally | Wheel and sdist build; clean-environment console smoke test passes |
| `python -m ash` | Verified locally | Keep as supported fallback |
| Dependency separation | Verified locally | Lean default runtime/provider install, standardized dev group, explicit server/local-embeddings/browser/ACP/A2A capability extras, actionable missing-extra errors, and lockfile/artifact checks |
| First-run wizard | Verified locally | No-key detection, deterministic cancel/back, endpoint retry/save-unverified choices, non-billable model discovery, secret input, atomic related settings, non-TTY guidance, secret-free JSON status, and fresh-process API/local checks |
| API-key providers | Partial | Every built-in cloud route now assembles from a fresh non-interactive process with its provider credential contract (including Google `GEMINI_API_KEY` fallback), while custom endpoints retain fresh-process coverage; deterministic fresh-process CLI loopback E2E proves real streamed completion across the previously covered built-in cloud catalog: Anthropic through the native Messages/SSE protocol with `x-api-key`, protocol-version, and provider-usage assertions, plus the OpenAI-wire routes for OpenAI, Google, OpenRouter, Vercel AI Gateway, DeepSeek, Groq, Mistral, xAI, Together, Fireworks, Cerebras, and NVIDIA with provider-specific bearer credentials, dynamic model-catalog probes where applicable, Together's list-shaped catalog, and Google's client-identification header. Hugging Face Inference Providers is additionally wired as a first-class gateway with `HF_TOKEN`, official `/v1/models` discovery, model/provider routing aliases, and conservative capability-floor parsing across its live upstream providers; Vercel AI Gateway is wired as a first-class `AI_GATEWAY_API_KEY` route using its official OpenAI-compatible `https://ai-gateway.vercel.sh/v1` endpoint and `/v1/models` catalog, giving Ash another high-leverage gateway to hundreds of models without widening the transport surface. Both still need real service conformance before joining the live-verified subset. A bounded real OpenRouter service run additionally proved live catalog discovery, a no-tools completion with provider-reported usage, and a coding journey that edited and externally tested a project using an ephemeral credential that was not persisted by Ash; interactive generic onboarding consumes the same readiness-owned catalog shape used at runtime; custom OpenAI-compatible routes fail closed unless exact per-model metadata is explicitly declared; OpenRouter, Hugging Face, Vercel AI Gateway, Mistral, xAI, Together, Fireworks, and Cerebras start conservatively and recover only capabilities proven by bounded provider-owned metadata (including multi-source xAI metadata that fails closed on direct or follow-up alias disagreement about canonical model identity, and selected-model Fireworks management metadata); runtime provider/model switches own retired-provider closure through loop shutdown, surface cleanup failures, and preserve close-once semantics for successful resources across shutdown retries; real vendor cross-version/service interoperability remains broader than the exercised OpenRouter service plus deterministic protocol coverage |
| OpenAI ChatGPT-plan auth | Verified live | Optional first-party `openai/*` auth mode uses OpenAI's open-source Sign in with ChatGPT flow with exact loopback state/nonce/PKCE and ID-token validation, private profile-scoped multi-account registration storage, stable host identity, rotating refresh serialization, revocation-aware logout, live model discovery, user-owned setup/CLI controls, project-config exclusion, and a dedicated stateless Responses adapter enforcing `store=false`, `stream=true`, full canonical history/tool replay, bounded encrypted reasoning replay, and success only on `response.completed`; deterministic auth/provider/runtime regressions pass, and a real 2026-10-01 account journey verified sign-in, catalog discovery, plan-backed completion, native function call, canonical tool-result continuation, and provider-reported usage |
| Provider route verification | Verified locally | `ash providers test` separates catalog/model discovery from a bounded no-tools model completion, requires a safe terminal response before declaring the route ready, redacts completion failures, and closes the probe provider; `ash doctor --connect` remains a non-billable catalog/model check |
| Local models | Partial | Ollama URL validation, discovery, health detail, safe bounded pulls, and dynamic tool/context probing are wired; LM Studio and vLLM no longer inherit OpenAI capabilities from wire compatibility, LM Studio consumes its native per-model tool/vision/reasoning/loaded-context metadata, and vLLM preserves served context while generic `supported_parameters=tools` does not enable native auto-tool calling without explicit server capability evidence; deterministic fresh-process CLI loopback E2E now proves each route's native catalog endpoint, streamed completion, provider/model identity, and absence of bearer auth; `/capabilities --refresh` re-probes dynamic manifests, while real cross-version LM Studio/vLLM runtime conformance remains |
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
| Structured output mode | Verified locally | One-shot JSON Schema injection with strict JSON parsing/validation; JSON and stream-JSON terminal output is released only after validation, successful completions include parsed `structured_output`, and invalid model output is a stable `output` error rather than an internal failure |
| Model capability negotiation | Verified locally | Tools, vision, reasoning, local status, and known context/output limits use provider/model evidence rather than wire-protocol identity; Ollama, OpenRouter, Hugging Face, Mistral, xAI, Together, Fireworks, Cerebras, LM Studio, vLLM, and generic OpenAI-compatible routes negotiate bounded provider-owned metadata before use where available; exact IDs and unique provider aliases resolve consistently, conflicting/missing/malformed/ambiguous/different-model evidence stays conservative, and `/capabilities` distinguishes dynamic evidence even inside failover chains |
| Provider failover | Verified locally | Ordered fallback only before first emitted chunk; static/dynamic chains enforce tool-protocol parity, expose a conservative whole-chain capability envelope, use the maximum child token estimate, forward per-turn output ceilings to every child, and keep configured models/failures visible |

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
| Project instructions | Verified locally | Trusted hierarchical project discovery with ordered `ASH.md` → `AGENTS.md` → `CLAUDE.md` compatibility fallbacks per scope; instructions refresh between model iterations while preserving session prompt layers; successful file reads activate the applicable nested rule chains for the next model iteration, including deterministic union for parallel reads without unrelated sibling scopes; bounded `@import` expansion, diagnostics, and conservative conflict lint across user/project/imported instructions |
| User instructions | Verified locally | Global `~/.ash/ASH.md` is loaded with a bounded size |
| Session memory | Verified locally | Redacted summaries are searchable across title, ID, and persisted summary metadata without raw transcript concatenation |
| Project memory | Verified locally | Explicit project-scoped index/search/export/clear controls plus opt-in trusted-workspace auto-indexing bounded by file count, file size, and configured exclusions; relative persistence is anchored to the selected workspace and workspace re-indexing prunes missing/unsafe indexed files |
| Project-memory retrieval | Verified locally | One private per-workspace SQLite index owns canonical chunks, FTS5 lexical rows, and optional embedding vectors. Lexical retrieval is always available when project memory is enabled; OpenAI or local ONNX embeddings are explicit opt-ins, query-time embedding failure falls back to lexical search, incompatible embedding identities discard only stale vectors, and hybrid retrieval fuses lexical/vector ranks without mixing raw score scales |
| Memory privacy | Verified locally | Recalled project memory is explicitly framed to the model as untrusted data rather than instructions/authorization; memory database and parent identity checks reject redirection/substitution, POSIX database sidecars are private, terminal search output is control-safe, and bounded redacted project-memory export remains available |

## 4. Sessions And Recovery

| Capability | Ash status | Required production behavior |
|---|---|---|
| Durable sessions | Verified locally | SQLite schema migrations, lifecycle, integrity checks, backup, and restore |
| Resume and continue | Verified locally | `-c` resumes the latest project session; `-r` and `/resume` accept exact IDs or case-insensitive names with ambiguity and wrong-project refusal |
| Session picker/list/search | Verified locally | Bare `-r` and `/resume` open a searchable, keyboard-navigable, project-scoped picker with metadata-only filtering and on-demand bounded transcript preview; top-level list/search also supports JSON output ([Claude Code session behavior](https://code.claude.com/docs/en/sessions)) |
| Session naming | Verified locally | Rename and stable persisted display names |
| Fork session | Verified locally | New durable session from a selected message boundary |
| Rewind conversation | Verified locally | Complete-turn transcript rewind plus optional conflict-preflighted multi-turn file restoration; usage totals are adjusted and filesystem changes roll forward if the database phase fails |
| File checkpoints/undo | Verified locally | Direct edit tools capture per-call pre-edit bytes and finalized hashes under the durable provider tool-call ID; provider call identity is available before checkpoint middleware runs; participating Ash workspace mutations are serialized across processes for the checkpoint/edit/finalization lifecycle; undo/rewind rollback only states owned by the failed operation and preserve conflicting later edits |
| Session export/import | Verified locally | Versioned redacted JSONL/Markdown export and validated JSONL import |
| Crash recovery | Verified locally | Approved tool intent is persisted before dispatch. Real Linux SIGKILL/restart probes prove pre-dispatch calls remain not-run, dispatched non-file side effects are marked ambiguous without replay, hash-proven in-flight edits are compensated, pre-finalization or independently changed files are preserved for inspection, active sessions are protected by a cross-process runtime lease so a second process cannot misclassify a live turn as crashed, and a later resume remains idempotent after the active owner exits |
| Session retention | Verified locally | Configurable automatic cleanup plus explicit project prune and vacuum |
| Cost/token history | Verified locally | Built-in pricing defaults for current major Anthropic, OpenAI, DeepSeek, and Groq models; explicit user pricing overrides remain authoritative |
| Persistent long-horizon Goals | Verified locally | One durable foreground Goal per session persists objective, bounded redacted evidence, lifecycle, and continuation usage in SQLite; model-visible Goal context survives ordinary turn boundaries; automatic continuation requires a real non-Goal tool action and is bounded by a configurable 1–100-turn window; completion requires explicit evidence; cancellation/runtime failure pauses conservatively; pause/resume/clear are host-owned controls; SDK and interactive `/goal` surfaces share the same state |

## 5. CLI And TUI Experience

| Capability | Ash status | Required production behavior |
|---|---|---|
| Multiline editor | Verified locally | Prompt-toolkit editor with persistent history and multiline bindings |
| Responsive full-screen TUI | Verified hosted | Transcript-owned prompt-toolkit viewport, narrow/wide resize reflow, page navigation, live tail, terminal restoration, and inline fallback are covered by real tmux PTY E2E on both supported native hosts (Linux and macOS) in hosted CI; native Windows is outside the supported host matrix and Windows users run the Linux path through WSL2 |
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
| Accessibility | Verified hosted | Dedicated screen-reader mode forces linear non-rewriting output, inline rendering, no color/token bar, reduced motion, and a reduced-dynamic prompt; real tmux PTY E2E for that Ash-owned terminal contract passes on both supported native hosts (Linux and macOS) in hosted CI. Ash does not claim certification of the external terminal emulator or VoiceOver/Orca stack, which is outside the harness process boundary |

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
| `/goal` | Verified locally | Create/status/pause/resume/clear one durable foreground Goal; resume grants a fresh bounded continuation window and immediately returns to normal permissioned turn execution |
| `/clear`, `/rewind`, `/undo` | Verified locally | New-session clear, complete-turn transcript rewind with optional `--files`, and conflict-aware latest file undo |
| `/diff` | Verified locally | Current, staged, path-scoped Git diff plus latest per-turn checkpoint diff with conflict refusal |
| `/review` | Verified locally | Bounded reviews for worktree/untracked, staged, commit, or current branch versus base |
| `/plan` mode | Verified locally | Explicit toggle, editable sprint contract, approval gate, and execution transition |
| `/permissions` | Verified locally | Inspect/change mode plus versioned project allow/ask/deny rules with stable IDs, exact/string-prefix/argv-prefix matchers, JSON output, precise removal, and legacy migration ([Claude Code rule model](https://code.claude.com/docs/en/permissions)) |
| `/sandbox` | Verified locally | Active backend, tier, capabilities, network policy, and effective Docker resource-limit diagnostics |
| `/mcp` | Partial | Top-level add/remove/list/status config supports JSON and env/header metadata plus safe OAuth credential health (`missing`, `usable`, `expired`, `invalid`, or `unavailable`) without exposing secrets; `ash mcp probe SERVER` performs an explicit bounded live connection, reports negotiated protocol/server capability/tool-count metadata in text or JSON, and redacts failure diagnostics; POSIX OAuth persistence is descriptor-anchored with no pathname fallback, while unsupported platforms fail closed before credential access; local stdio configs snapshot an existing cwd identity when trusted and both client/server launchers require that same directory identity at spawn while retaining descriptor-scoped post-open cwd protection; REPL status reports live transport/auth/tool/error summaries in text and structured JSON, tools/resources/prompts use live clients, explicit login/logout atomically reloads affected tooling, targeted `/mcp refresh SERVER` reconnects one server while preserving others, replacement-server list-change notifications are accepted only when the replacement declared `listChanged`, valid notifications clear prior protocol-status errors, and explicit `watch`/`unwatch`/`watches` commands manage bounded session-scoped resource subscriptions with watch mutations serialized against targeted reconnect so concurrent changes are not lost or resurrected; MCP 2026-07-28 stateless discovery/MRTR support now negotiates the official `io.modelcontextprotocol/tasks` extension, transparently drives server-directed tool tasks through bounded `tasks/get` polling, deduplicated `tasks/update` input responses, JSON-RPC failure propagation, cooperative `tasks/cancel`, and task-ID HTTP routing while retaining the 2025-11-25 legacy task wire separately; active modern tasks open best-effort task-ID `subscriptions/listen` streams so valid `notifications/tasks` updates can wake long polls immediately, with declined/lost streams falling back to the same bounded polling path; modern task IDs plus latest task state, answered-input fingerprints, original Ash call/turn/session identity, verified MCP tool contract metadata, and a v13 non-secret fingerprint of the originating resolved server configuration plus negotiated `serverInfo` are persisted durably in the session database; persistence failure requests task cancellation rather than continuing unrecoverably, restart resumes the same task ID through `tasks/get` without replaying `tools/call`, preserves already-answered input-request fingerprints, validates the recovered result against the same tool contract and server identity, atomically reconstructs at most one model-visible tool result, cleans stale durable rows after normal/local completion, refuses to send old task IDs to a repointed server alias, and fails closed while the original server or exact tool contract is temporarily unavailable; fingerprintless v12 task rows fall back to Ash's non-replay ambiguous-outcome recovery rather than being sent to an unverified server; 2026 tool output schemas and `structuredContent` accept arbitrary JSON roots while legacy protocol revisions retain their object-root requirement |
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
| Text/regex search | Verified locally | Descriptor-scoped bounded Python text/regex search with file-size, scan-entry, capture-size, result-count, regex, and glob controls |
| Symbol/code search | Verified locally | Read-only `find_symbol` and `find_references` tools use the incremental Tree-sitter index, exact locations, case controls, globs, and bounded results |
| Deferred tool search | Verified locally | Large built-in/plugin/MCP catalogs expose an essential set plus `search_tools`; ranked matches return exact schemas, activate on the next iteration, reset per session, and alone consume schema budget |
| Shell execution | Verified locally | Foreground and managed background commands share fail-closed sandbox injection, process-tree termination, and child env scrubbing; foreground stdout/stderr stream as call-correlated, incrementally redacted, bounded events through inline TUI, viewport, SDK, and stream-JSON surfaces |
| Git status/diff/log | Verified locally | Read-only bounded Git inspection tools |
| Git commit | Verified locally | Explicit commits require a path scope and refuse any pre-existing staged index state before staging; staged additions are secret-scanned and Git hook/stdout/stderr failures are surfaced. Automatic per-turn commits are stricter: candidates come only from successful Ash file mutations, `auto_commit_paths` acts only as an allowlist, paths dirty at turn start are skipped, post-edit SHA-256 ownership is rechecked before and after staging, and real dirty-repo journeys preserve same-file user edits instead of absorbing them. Explicit approved commits remain full-path scoped rather than hunk-owned. |
| Tests/build/lint diagnostics | Verified locally | `run_command` parses bounded compiler/lint/pytest diagnostics into model-visible path, line, symbol, code, and message fields and aggregates pytest/MyPy/Ruff summary counts |
| Web fetch/search | Verified locally | Guarded HTTP(S) fetch plus Brave/Tavily live search with credential auto-detection, auto fallback, fixed endpoints, freshness, bounded normalized sources, provider provenance, shared domain filtering, and durable citation objects are wired |
| Browser automation | Strong partial | Optional Playwright pack owns an isolated Chromium context with public-host/domain routing for requests and WebSockets, blocked service workers/password fills, bounded ARIA snapshots, stable refs, navigation/click/type/scroll/history actions, bounded vision screenshots, safe workspace uploads and atomic bounded workspace downloads, setup/doctor support, deterministic cleanup, opt-in private Ash-owned profiles, and loopback-only CDP attachment to an existing Chromium browser through a separate Ash-owned policy context with optional bounded storage-state reuse. `/browser inspect` performs bounded read-only source-browser inventory (context/tab counts, terminal-safe titles, redacted URLs) without reading storage or page content; `/browser status`, `/browser connect`, and `/browser disconnect` hot-swap the browser family after candidate preflight without persisting config or taking ownership of the source browser. Real Chromium E2E proves inspection, isolated state reuse, hot-swap, and source-browser survival; remote CDP and direct ownership of pre-existing tabs remain intentionally unsupported |
| Ask-user tool | Verified locally | Typed blocking question with bounded options and explicit empty-answer failure |
| Todo/plan tracking | Verified locally | Persisted sprint checklists are inspectable and updatable from the top-level CLI; active plan state is injected into every runtime model request with bounded context accounting |

## 8. Safety, Trust, And Permissions

| Capability | Ash status | Required production behavior |
|---|---|---|
| Workspace path boundary | Verified locally | Canonical scope checks plus symlink/junction rejection; the selected workspace-root directory identity is pinned for the runtime lifetime and descriptor-scoped operations verify the root they actually opened, so whole-root replacement requires restart; POSIX reads, directory listings, creates, no-clobber writes, edits, and patches use descriptor-anchored no-follow operations, with identity/content revalidation fallback on Windows |
| Trusted-folder prompt | Verified locally | Project instructions, skills, hooks, plugins, and MCP are trust-gated |
| Fine-grained policy engine | Verified locally | Deny-first then ask/allow precedence, mode circuit breakers, stable scoped rules, conservative tokenized command-prefix parsing, managed policy layers, AND-composable same-argument constraints, traversal-safe path prefixes and whole-path `*`/`?` globs, exact-vs-wildcard domain scoping, safe case-insensitive filename extensions, bounded text containment, strict positive-integer maxima, and bounded string-set membership are enforced |
| Approval persistence | Verified locally | Atomic mode-0600 versioned rules, version-1 migration, stable IDs, exact session/project scopes, command-prefix project scopes, scoped denial, slash/top-level inspection, removal, and clear |
| Read-only plan mode | Verified locally | Non-read tools are denied by the central policy |
| Auto-edit mode | Verified locally | Reads/edits allowed while commands and external tools remain gated |
| Full auto mode | Verified locally | CLI startup, runtime mode switching, SDK, sandbox diagnostics, and subagent workers require both full OS isolation and aggregate CPU/memory containment for safe auto-approve; `auto` selects Docker with non-zero limits, native-only/unbounded configurations fail closed, and the explicit unsafe override remains an operator-owned escape hatch |
| Dry-run mode | Verified after fix | No side effects, including hooks and subagents |
| OS sandbox | Partial | Fail-closed workspace-write Bubblewrap **0.12.0+** on Linux (older or unparseable versions are rejected because CVE-2026-87766 affects `<0.12.0`), scoped `sandbox-exec` profile on macOS, verified Docker image fallback on supported native hosts, and clearly labeled approval-controlled direct mode. Bubblewrap requires a private user namespace, disables nested user namespaces, and exposes a compatibility allowlist under `/etc` rather than the whole host directory. Docker execution defaults to cgroup-backed **4096 MiB RAM**, **2 CPU**, and **256 PID** limits; RAM/CPU are user-owned validated controls (`0` disables the corresponding limit), flow through ordinary commands, subagents, and executable-plugin runtime/staging, and project config cannot weaken them. Safe `auto_approve` treats resource containment as a capability boundary: `auto` skips native backends and selects Docker only when both CPU and memory limits are non-zero, otherwise startup/worker execution fails closed unless the operator explicitly enables the unsafe override. Bubblewrap and macOS remain approval-gated native isolation and do not claim aggregate CPU/memory containment. The hosted Linux Docker E2E now passes against the real container cgroup files, verifying the configured memory, CPU, and PID ceilings. Docker's full-isolation claim remains scoped to code running inside the container under Ash's documented trusted-host/OS-account boundary; its ordinary workspace bind is a daemon-resolved host pathname and is not claimed to resist concurrent replacement by an independently hostile same-account host process. Windows users run the supported Linux sandbox path through WSL2; native Windows sandboxing is outside the supported host matrix |
| Network isolation | Verified locally | Bubblewrap network namespace, macOS profile rules, and Docker `none` networking default to blocked; `web_fetch` separately enforces public-host validation and optional domains |
| Environment scrubbing | Verified locally | Foreground/background commands and Git hooks receive only a scrubbed operational env plus an explicit user-owned variable-name allowlist; MCP stdio servers receive only explicit server env; project config cannot grant command variables, isolated backends preserve the policy, and Docker arguments never contain values |
| Prompt-injection isolation | Verified locally | Every provider request declares an explicit untrusted-content boundary, tool responses carry provenance/policy guidance, and structured per-fragment provenance is inspectable through `/context --provenance`; permissions, sandboxing, redaction, and audit enforcement remain independent |
| Secret scanning/redaction | Verified locally | Runtime logs, persisted messages/tool calls, tool output, and exports are redacted; auto-commit blocks high-confidence secrets in staged additions without echoing values |
| Audit log | Verified locally | Tool approvals, blocks, outcomes, and interactive permission-mode decisions are persisted with a tamper-evident hash chain plus CLI list/verify/export |
| Enterprise policy layers | Verified locally | Platform-managed deny/ask files load before user rules, survive mode changes, appear in status, and invalid/unreadable policy fails startup closed |

## 9. Extensibility

| Capability | Ash status | Required production behavior |
|---|---|---|
| MCP stdio client | Verified hosted | Current negotiation, bounded 8 MiB framing with immediate pending-request failure, tools, resources/templates, prompts, pagination, progress, bidirectional cancellation, logging, roots, and coalesced atomic list-change refresh with under-write-lock contract proof are wired. A pinned official MCP Python SDK **2.2.0** server passes real stdio `server/discover` negotiation for **2026-07-28**, `tools/list`, and `tools/call` through Ash both locally and in hosted CI; that conformance probe also exposed and fixed explicit virtualenv-launcher canonicalization that previously stripped the configured venv path. Client sampling and form elicitation are user-owned opt-ins with associated-request enforcement, bounded payloads, explicit review/decline, no hidden tool/context/task use, sensitive-form rejection, and headless callback requirements. URL elicitation and sampling tools/context/task augmentation remain unadvertised. Experimental required-tool task execution with optional task status notifications plus polling fallback and validated tasks/list observability is also wired |
| MCP HTTP transport | Verified hosted | Strict JSON-RPC JSON/buffered-SSE POST responses, case-safe headers, initialization-only validated session IDs, generation-locked 404 reinitialization, cross-session pagination restart, catalog reconciliation, no replay of rejected tool calls, cleanup, OAuth 2.1 protected-resource plus ordered OAuth/OIDC discovery, S256 PKCE, resource indicators, configured Client ID Metadata Documents/preregistered clients or DCR fallback, descriptor-anchored private resource-bound token storage on supported POSIX systems with fail-closed unavailable diagnostics elsewhere, cross-process serialized credential mutation, refresh de-duplication, explicit login/logout, insufficient-scope step-up guidance, and CLI project-config persistence that requires environment indirection for credential-bearing env/header/command-option values instead of writing literal secrets into `.mcp.json`; a pinned official MCP Python SDK **2.2.0** Streamable HTTP server passes real **2026-07-28** `server/discover`, `tools/list`, and `tools/call` conformance through Ash both locally and in hosted CI; experimental required-tool task execution, version-aware pre-2026 GET/SSE with Last-Event-ID plus retry handling, and legacy HTTP+SSE endpoint discovery on one persistent stream are wired; live multi-vendor OAuth conformance remains |
| MCP `2026-07-28` request headers | Verified locally | Static `x-mcp-header` validation, duplicate/limit/name/location rejection, method and identity mirroring, nested primitive parameter extraction, safe/Base64 header encoding, HTTP-only enforcement, and tool-call wiring are covered by focused tests; modern stateless requests also send required protocol/method/name routing metadata, consume mandatory `resultType`, and preserve opaque MRTR `requestState` across bounded retries |
| MCP `2026-07-28` stdio detection | Verified locally | `server/discover` probing uses modern metadata and validates results; valid 2026 servers activate the stateless per-request core without `initialize`, support bounded MRTR plus correlated `subscriptions/listen` list/resource streams, and use `notifications/cancelled` for stdio subscription teardown; unsupported/newer versions fail deterministically while non-modern probes fall back to the legacy handshake |
| MCP `2026-07-28` HTTP detection | Verified locally | `server/discover` over Streamable HTTP uses modern routing/version metadata, activates stateless requests without protocol sessions, consumes bounded long-lived `subscriptions/listen` SSE streams for list/per-resource updates, and tears HTTP subscriptions down by closing the stream rather than POSTing cancellation; malformed modern results fail closed while non-modern compatibility responses fall back to legacy initialization |
| MCP schemas/results | Verified locally | Exact version-aware input/output schemas, isolated non-coercing validation with no remote fetches, bounded content checks, complete rich result envelopes, output-schema checks, application-error semantics, and exact JSON-RPC error data are preserved without unsafe call replay |
| MCP tool namespacing | Verified locally | Simple identities retain `mcp__server__tool`; non-portable or over-64-character identities receive deterministic provider-portable aliases while the exact remote MCP name remains the wire/recovery identity. Collision checks remain fail-closed across and within servers |
| MCP resources/prompts | Verified locally | Browse in TUI, invoke/read through policy-gated model tools, declared revisioned list changes, explicit live lists, and atomic `/mcp refresh` lifecycle reporting are wired. Resource watching is explicit and session-scoped: modern 2026 servers use bounded per-URI `resourceSubscriptions`, legacy servers use `resources/subscribe`, valid updates emit `mcp.resource.updated`, and active watches are restored across targeted/full reloads and legacy HTTP session recovery without silently auto-subscribing ordinary reads |
| Skills | Verified locally | Bounded standard `SKILL.md` discovery and progressive activation with trust are wired into the runtime. Legacy in-process executable skills remain a library-only unsafe compatibility API rather than a runtime/CLI feature; their standalone boundary is explicit opt-in, cannot fabricate missing tools, and validates secured Python source/declared identity before executing reloads |
| Plugins/extensions | Verified locally | Transactional local lifecycle, dependency and reverse-dependency validation, lifecycle-serialized enable/disable/uninstall topology decisions, and a durable per-root crash journal that rolls back interrupted pre-commit mutations or finalizes committed cleanup before later plugin discovery/use. Journal recovery binds the plugin root, managed activation-state parent, provenance, activation bit, and transaction-owned tree identities; ambiguous replacements fail closed, and staging/backup/quarantine/conflict artifacts are excluded from discovery. Inventory, project trust, namespacing, declarative components, and Plugin API v1 tools use strict bounded JSON-RPC subprocesses with read-only/no-network isolation, ordinary policy/audit/events, no side-effect replay, live replacement, and deterministic cleanup. Safe executable-plugin runtime additionally requires bounded Docker (full read isolation plus aggregate CPU/memory containment); Bubblewrap/native-only execution is available only through the explicit unsafe plugin compatibility override. Runtime-tool reloads are serialized, refuse active-turn/shutdown replacement, retain ownership of rejected hosts whose cleanup fails, retry that cleanup before later publication and during shutdown, and report persisted plugin mutations separately from retryable live-reload failures |
| Plugin marketplace | Partial | Trust-reviewed HTTPS Git source installation uses explicit refs, shallow temporary checkouts, metadata removal, all existing lifecycle/component safety gates, and an isolated non-interactive Git environment that strips unrelated secrets/proxy/Git overrides, disables user/system Git configuration and credential prompts, and uses an Ash-owned empty home/config directory; optional local or HTTPS signed catalogs use pinned Ed25519 keys plus exact revision binding; HTTPS catalogs use bounded no-redirect fetches and a private cache; catalog v1 remains compatible for one-catalog workflows, while signed catalog v2 binds a lowercase publisher namespace, repeatable `--catalog` selection aggregates distinct publishers, bare duplicate plugin names fail as ambiguous, and `@publisher/plugin` selects the exact signed source/ref/digest; duplicate publisher namespaces and mixed v1/v2 multi-catalog selections fail closed; profile-scoped `ash marketplace list/add/remove` persists a user-owned publisher-to-source registry only after signed v2 verification, pins both signing key ID and verified Ed25519 public-key SHA-256 fingerprint, re-verifies both on every search/install/update, rejects publisher/signer drift (including key-material replacement under a reused key ID), requires legacy registrations without a fingerprint to be explicitly re-registered, preserves explicit `--catalog` precedence and the legacy single-catalog environment fallback, and cannot be injected by project config; managed installs persist Ash-owned source/ref/resolved-commit/version/publisher plus explicit `git` versus `catalog` origin under the descriptor-anchored lifecycle lock and durable crash journal, keep the previous tree recoverable until the committed tree/provenance/activation checkpoint, roll back interrupted pre-commit replacement, finalize post-commit cleanup after restart, reject replacements/updates that would newly violate installed reverse-dependency constraints, conservatively treat publisher-less v1 provenance as update-ineligible rather than guessing its trust source, and clear stale remote provenance on local replacements; `ash extensions update NAME` plus `/plugins update NAME` provide first-class single-plugin updates with direct-Git commit no-op detection, signed-catalog re-resolution, candidate identity/dependency/component validation, provenance-anchored recovery of damaged/missing local payloads when upstream changed, exact-tree rejection of tampered no-op updates, atomic replacement, disabled-state preservation, provenance-only update reporting, and unchanged outcomes without reload; `ash extensions update --all` plus `/plugins update --all` select tracked installs deterministically, keep unrelated plugins on independent atomic update boundaries, continue after ordinary per-plugin failures, retry dependency-ordering failures after the first pass, and coordinate genuine dependency-version deadlocks by atomically persisting a bounded quiesce intent for the connected component, temporarily disabling that component, applying each verified candidate through the existing per-plugin crash-safe lifecycle, validating the final enabled graph, and restoring the previous activation set only after it is valid. Crashes or invalid candidates leave the cohort disabled with durable resume intent; rerunning update-all resumes the same targets without exposing an enabled broken graph. This is deliberately not claimed as a simultaneous cross-tree atomic swap; Ash still lacks a first-party hosted/curated public marketplace ecosystem comparable to ClawHub |
| Hooks | Verified locally | Versioned session/turn/model/tool/error lifecycle plus context-compaction/config/permission-change observers; structured pre-tool denial and session context, critical-versus-observer failure semantics, redacted diagnostics/events, plugin cwd, scrubbed env, timeout, bounded I/O, and process-tree cleanup are wired |
| Custom agents | Verified locally | User/project/plugin Markdown roles, namespacing, instructions, base-role isolation, and non-escalating tool restrictions |
| Hot reload | Verified locally | Commands/completion, skills, agents, hooks, and MCP runtimes validate before live replacement |

## 10. Subagents And Work Isolation

| Capability | Ash status | Required production behavior |
|---|---|---|
| Spawn subagent tool | Verified locally | Real bounded provider-backed Ash loop, persisted reports, role-scoped tool manifests, background execution, cancellation, no recursive spawning, and start-time inheritance of the parent permission mode plus managed/session/persistent rules; the default remains in-process, while optional config-backed subprocess execution serializes the durable task/config/policy plus only selected provider environment into a scrubbed child that rebuilds the provider there, with source-tree regressions and installed-wheel smoke proving the parent provider factory is not used; direct foreground child `ASK` decisions broker through the active TUI or explicit runtime approval callback, including the subprocess approval channel, while autonomous workers persist attempt-scoped approval requests for later operator resolution and direct foreground headless workers still fail closed when no broker exists |
| Parallel agents | Verified locally | Atomic DAG submission, dependency-ready dispatch, bounded retries, foreground/background operation, restart recovery, independent child sessions/workspaces, durable graph-wide token and USD ceilings, safe foreground approval brokerage, and a durable asynchronous approval inbox for background/queued DAG workers. Approval responses are single-resolution, task/attempt/owner/digest correlated, cancellation-aware, and stale requests are retired before retried attempts can run |
| Agent status/output | Verified locally | Live basic and full slash status with durable task identity, token budgets, and USD cost usage; top-level persisted status/report/message inspection |
| Agent messaging | Verified locally | Typed SQLite IPC is persisted and inspectable; running workers consume steer/stop messages, acknowledge delivery, enforce pending-message backpressure, and use correlated `approval_request`/`approval_response` messages for asynchronous child permissions |
| Role tool policies | Verified locally | Read/search baseline, coder-only scoped edits, sandbox-required tester commands, and no recursive spawn tool |
| Worktree isolation | Verified locally | Clean-lead precondition, locked `ash-agent/*` branches, exact branch/commit verification, conflict-safe dependent merges, bounded commits, deterministic cleanup, explicit full-branch squash/discard, and Ash-owned storage-root/worktree inode pinning carried into the worker `SafetyGuard` and sandbox. If a concurrently hostile same-account host process redirects the destination pathname during external `git worktree add`, Ash revalidates after Git returns and refuses the lease before any worker execution; Git may already have written to the redirected host path because the Git CLI exposes only a pathname destination, so that host-side race is not claimed as a strict same-principal namespace guarantee. |
| Result consolidation | Verified locally | Foreground DAG calls return typed terminal results and errors plus evidence-linked synthesis, bounded summaries, workspace-relative path evidence, cross-agent claim-conflict detection, and durable graph-consolidation artifacts |
| Agent steering/stop | Verified locally | In-process stop, persisted stop, atomic graph cancellation with active-turn revocation, live steering at safe iteration boundaries, delivery state, and report-based resume; isolated changes must be applied before continuation |

## 11. Automation And Integration

| Capability | Ash status | Required production behavior |
|---|---|---|
| One-shot prompt (`ash -p`) | Verified locally | Session-aware one-shot mode with meaningful exit codes |
| JSON output | Verified locally | Machine-clean single terminal completion objects with normalized usage and structured error objects; schema failures emit only the structured error |
| Streaming JSONL | Verified locally | Typed token, reasoning, context, usage, tool lifecycle, completion, and structured error events; schema-gated turns defer `turn.completed` until validation so a failed contract cannot publish a premature success terminal |
| Stdin prompts | Verified locally | Piped stdin and `-p -` enter machine-clean one-shot mode |
| CI mode | Verified locally | `--ci` disables interactive prompts/ANSI and defaults one-shot output to stream-json |
| SDK/library API | Verified locally | Async create/prompt/steer/session/lifecycle/delegation API with explicit subagent provider injection and normalized usage independent of the TUI |
| JSON-RPC server | Verified locally | Validated stdio and authenticated HTTP methods share one SDK adapter; remote `/rpc` supports JSON-RPC 2.0 requests, notifications, bounded batches, strict JSON parsing, bearer auth, rate limits, payload limits, a 64-request in-flight ownership budget, duplicate in-flight request-ID rejection, and cancellation. Direct `/v1/turn` and SSE turn routes have a separate default 16-turn admission budget while steering remains available at capacity. |
| HTTP server | Verified locally | Bearer auth, rate limits, lifecycle, session/turn/steering routes, live SSE events, cancellation, safe CLI binding, and mandatory TLS for non-loopback bearer-token transport |
| IDE/ACP integration | Verified locally | Official ACP Python SDK v1 over bounded JSONL stdio; initialize/new/load/list/close/prompt/cancel plus durable fork/resume lifecycle, durable message and redacted tool replay, permission requests, text/resource links, ordered bounded inline image prompts, tool/usage streaming, editor-supplied stdio/HTTP/SSE MCP, isolated runtimes, and official-client plus shipped-process wire tests. Fork (an ACP unstable capability) creates a durable independent child while preserving the parent runtime, rejects unfinished source turns and prompt/fork races, reserves session capacity and exact IDs before publication, confines source/resume ownership to the requested workspace, and rolls back only provably untouched unpublished children; resume attaches an existing durable session without replaying prior transcript updates. ACP images are validated through Ash's canonical PNG/JPEG/GIF/WebP contract, limited to 5 MiB each / 10 MiB per prompt, passed as native provider image blocks in protocol order, rejected before provider I/O when the negotiated model lacks vision, and stripped of raw bytes before session persistence. Audio/embedded context, session delete, extra directories, modes, terminal/filesystem callbacks, and registry publication remain unadvertised. |
| Remote-agent/A2A integration | Verified locally | Official A2A SDK/spec 1.0 Agent Card, JSON-RPC and HTTP+JSON routes, bearer auth, rate limits, a default 16-task in-flight executor budget, durable SQLite tasks and project-scoped context/session mapping, text artifact streaming, polling/get/list/cancel, CLI inspect/send, trusted configured delegation tools, bounded I/O, origin pinning, mandatory TLS for non-loopback serving, and official-client end-to-end tests. Push notifications, files/data, extended cards, gRPC, signed-card verification, and OAuth/mTLS remain unadvertised. |
| Managed LSP | Verified hosted | Trusted, lazy per-root LSP 3.18 stdio clients whose existing workspace root identity is snapshotted at client construction and required again at process launch while descriptor-scoped cwd protection covers later swaps; built-in autodetection resolves only host-installed servers, while a trusted `.ash/lsp.json` may explicitly opt into a workspace-local executable; negotiated full/incremental sync and UTF positions; push/pull diagnostics; hover, definition, references, implementation, symbols, call hierarchy, prepare-rename, advisory rename WorkspaceEdits, and advisory code actions; WorkspaceEdit paths/resource operations are normalized to workspace-relative data, unsafe/ambiguous edits fail closed, LSP commands are reported but never executed, `workspace/applyEdit` remains refused, and framing/documents/results/stderr/process cleanup stay bounded. Real conformance against pinned Microsoft **Pyright 1.1.414** proves initialization, hover, definition, and diagnostics through Ash's actual manager both locally and in hosted CI, while broader multi-vendor LSP coverage remains unclaimed. |
| Durable scheduler | Verified locally | Trusted one-shot, interval, and timezone-aware cron prompts; corruption-safe SQLite journaling (WAL on fixed SQLite runtimes, rollback-journal fallback on affected versions); atomic workspace-scoped multi-worker claims; per-job overlap prevention; renewable leases; long-lived workers detect replacement of an existing workspace inode and require restart, while Ash's default unattended prompt subprocess carries the pinned workspace identity through descriptor-scoped launch; isolated process termination; bounded timeout/token/output; misfire/coalescing and DST contracts; terminal crash recovery; CLI/SDK/model tools; read-only doctor checks; worker-liveness diagnostics; installed-wheel smoke coverage |

## 12. Reliability And Operations

| Capability | Ash status | Required production behavior |
|---|---|---|
| Structured logging | Verified locally | Human stderr logging is centrally secret-redacted and terminal-safe; `ASH_DEBUG=1` additionally writes bounded JSONL records to a private descriptor-anchored `~/.ash/logs/ash.jsonl` sink with size rotation/retention and session/turn/tool-operation correlation IDs; debug bundles remain bounded and privacy-redacted |
| Telemetry | Verified locally | No outbound telemetry; `ash metrics` exposes aggregate local-only token, cache, session, and cost metrics |
| Error taxonomy | Verified locally | Shared config/provider/tool/policy/sandbox/context/storage/output classifier is wired into headless/CI errors, structured-output validation, interactive slash commands, imports, model switches, and normal turn failures |
| Graceful shutdown | Verified locally | Providers, MCP clients, command trees, background agents, SDK, and servers close deterministically |
| Database migrations | Verified locally | Schema version table, transactional ordered migration, future-version refusal, and backups |
| Corruption recovery | Verified locally | Read-only integrity diagnostics plus validated backup and pre-restore preservation |
| Offline test suite | Verified locally | Session-wide test isolation establishes a synthetic HOME/USERPROFILE/XDG/AppData profile before collection, isolates global Git config, and blocks non-loopback Python DNS/socket connections while preserving loopback/Unix-socket integration tests; the complete local suite passes under those guards without live-network or real-home dependencies |
| Cross-platform CI | Verified hosted | The configured Linux and macOS matrix covers Python 3.12, 3.13, and 3.14 on both supported native hosts, with packaging smoke gates in CI. The full supported-host matrix is green in hosted CI at commit `50e066e` (run `36848303087`). Native Windows CI is intentionally absent because native Windows is outside the supported host matrix |
| Packaging CI | Verified hosted | The hosted quality gate builds an sdist, rebuilds a wheel from that fresh sdist, smoke-installs both wheel and sdist in clean environments, exercises minimal CLI/import/config/trust/repo-map behavior and missing-extra handling, and smoke-runs the installed wheel under Python 3.14 |
| Security tests | Verified locally | The bounded supported-host/path-bypass review is closed. Command bypass coverage includes shell-expanded destructive options, dynamic executables, common process/shell wrappers, entry-time workspace identity pinning plus descriptor-anchored POSIX cwd race regressions across foreground/background command, direct/scoped sandbox execution (including nested cwd after whole-workspace replacement), direct Git, patch, managed worktree Git, LSP, MCP stdio, durable automation, command-hook execution, direct executable-plugin launch, and repository-map Git-ignore evaluation. Ash-owned read-only Git disables pagers/fsmonitor/signature verification/external diff/textconv, discovers effective clean/smudge/process filter driver names (including trusted user-global includes), shadows executable filters to no-op command-scope values, pins the worktree to the selected workspace, and rejects repository-local/worktree config that redirects attributes, excludes, diff ordering, mailmap, or includes through host paths while preserving system/global user policy. Sandboxed Git now carries the original pinned cwd inode through the sandbox manager instead of re-snapshotting a replaced nested cwd. Repository-map `check-ignore` performs the same local/worktree provenance check before Git ignore evaluation; malformed or repository-controlled external ignore/include policy fails closed rather than shaping model context from host files. Ash-managed agent worktree Git uses a scrubbed environment, an Ash-owned empty hooks directory for every operation, disabled fsmonitor/signature/external-diff/textconv behavior, the original repository/cwd identity across config preflight and mutation, and fails closed when local/worktree config defines executable filters, merge drivers, diff helpers, local includes, or host-path indirections. MCP stdio launch resolves every bare command to an absolute host executable before spawn while excluding its trusted cwd/source/current workspace from PATH shadowing; explicit relative project commands are canonicalized against the pinned project source root when no cwd is declared, and manager/client share the same resolver and cwd identity. Linux Bubblewrap requires security-supported 0.12.0+, a private user namespace with nested-userns creation disabled, descriptor-bound workspace/extra mounts, and a minimal `/etc` compatibility set that excludes host machine identity; safe autonomous execution and safe executable-plugin runtime additionally require Docker-backed aggregate CPU/memory/PID containment while native backends remain approval-gated, and hosted Linux Docker E2E verifies the real cgroup ceilings. Docker executable-plugin code requires descriptor-anchored immutable capture from the discovered plugin inode, is streamed into an Ash-owned daemon volume mounted read-only at runtime, and fails closed instead of falling back to a live host bind when anchored capture is unavailable; ordinary Docker workspace binds remain daemon-resolved host paths, while concurrent replacement by an independently hostile same-account host process is explicitly outside Ash's supported local application boundary. Descriptor-scoped custom-command reads reject post-validation link swaps; whole-workspace replacement is rejected across normal turns, scoped I/O, project memory, REPL workspace-aware commands, MCP reload/reconnect, executable-plugin runtime reload, live project-instruction refresh, read-only Git multi-stage preflight, repo-map Git ignore evaluation, and managed worktree Git orchestration. Mixed-generation A→B→A regressions additionally reject transient project hook config and bind discovered plugin manifests to their plugin-root inode, skill metadata/resources to the discovered package inode, custom-agent instructions and custom-command templates to their source-file inode, trusted project MCP configs to their source-root identity, and trusted project LSP/A2A config reads directly to the runtime's pinned workspace guard; built-in LSP autodetection no longer treats executable workspace dependencies as installed servers without explicit trusted project configuration; tool/event redaction covers assigned/header secrets, supported provider keys, GitHub/Slack/Stripe bearer credentials, and complete or chunk-split PEM private keys, including background-process streaming and durable runtime-event replay. No additional P0-P2 defect was confirmed in the final native-mount, Docker bind/cwd, launch environment/PATH, or nested-caller downgrade sweep beyond the plugin resource-containment and sandboxed-Git cwd-identity defects fixed in this phase. |
| Performance tests | Verified locally | Lightweight CLI import graph and installed version startup are regression-tested under one second; bounded large-repository memory indexing is covered by a 170-file offline benchmark; long-session memory proves 1,000-file indexing plus 20 exact-recall rounds with bounded indexing and sub-50 ms recall; cached transcript redraw tests prove only changed Markdown is re-rendered and enforce generous latency ceilings for both a 200-entry interactive transcript and the 1,000-entry bounded history |
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

## Overall parity verdict

Ash is now a **production-worthy, first-class local-first terminal coding
harness within its documented scope**. M1-M5 are closed/current.

Current comparators also cover materially different product categories:

- OpenClaw is a gateway product with many chat channels, multi-agent routing,
  mobile nodes, media/voice surfaces, a browser Control UI, dozens of providers,
  and plugin channels. See its [official feature list](https://docs.openclaw.ai/concepts/features).
- Hermes combines a terminal TUI with remote deployment targets, messaging
  channels, self-improving memory/skills, web backends, and parallel delegated
  work. See the [official repository overview](https://github.com/NousResearch/hermes-agent).
- OpenCode, Claude Code, Codex CLI, Gemini CLI, and Aider each document mature
  combinations of agent profiles, provider ecosystems, IDE/remote or web
  surfaces, extensions, review/checkpoint workflows, and/or voice/media that
  Ash does not provide as a single product. See the [OpenCode agents](https://opencode.ai/docs/agents/),
  [Claude Code extension model](https://code.claude.com/docs/en/features-overview),
  [Codex CLI overview](https://developers.openai.com/codex/cli/features),
  [Gemini CLI feature index](https://geminicli.com/docs/), and
  [Aider feature overview](https://aider.chat/).

It is still not truthful to claim literal feature identity "in everything" with
products whose scope includes hosted gateways, large public marketplaces,
desktop/mobile clients, messaging/channel ecosystems, or general remote
computer-use infrastructure. Those differences are explicit scope boundaries.
Within Ash's mission—terminal-first coding, agent/runtime safety, provider and
protocol interoperability, browser-assisted coding workflows, plugin
lifecycle, observability, packaging, and supported-host production
operation—the final audit no longer identifies a material blocker to calling
Ash first-class and production-worthy.
