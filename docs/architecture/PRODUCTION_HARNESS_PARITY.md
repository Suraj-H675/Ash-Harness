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
1. **P1 — Provider/model parity — CLOSED.** Close material gaps in provider
   breadth and correctness, authentication/subscription paths, discovery and
   switching, capability metadata, tool/reasoning/multimodal semantics,
   streaming/cancellation, usage/cost normalization, failover/routing, local
   runtimes, onboarding, and real-provider/runtime conformance. Closure requires
   a current comparator pass plus enough real vendor/runtime evidence for every
   support claim that materially depends on external behavior.
2. **P2 — Agent/workspace parity — CLOSED.** Evaluate and close material
   gaps in subagent orchestration, durable/background work, worktree/workspace
   isolation, delegation, steering, recovery, remote execution where it solves
   real coding workflows, and multi-workspace ergonomics.
3. **P3 — Web/computer interaction parity — OPEN.** Evaluate browser
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

**Active gate: P3 — Web/computer interaction parity.** P1 and P2 are closed at
their bounded finish conditions. Do not reopen either for feature-count churn;
reopen only for concrete evidence that an important supported provider/model or
agent/workspace user need or claim is wrong.

### P1 finite closure checklist

P1 is split into bounded sub-gates so provider work cannot become an endless
catalog-expansion exercise:

1. **P1A — Provider and enterprise route coverage — CLOSED LOCALLY.** Re-evaluate the
   direct providers, gateways, and enterprise-hosted model routes that serious
   coding-harness users actually need. Ash already covers Anthropic, OpenAI,
   Google AI Studio, OpenRouter, Hugging Face Inference Providers, Vercel AI
   Gateway, DeepSeek, Groq, Mistral, xAI, Together, Fireworks, Cerebras,
   NVIDIA, Ollama, LM Studio, vLLM, and explicit custom OpenAI-compatible
   endpoints. Current comparator evidence established Amazon Bedrock and Google
   Vertex AI as material enterprise routes whose governance and authentication
   could not be represented cleanly by generic bearer endpoints; first-class
   implementations now close that gap without turning provider count into the
   target.
2. **P1B — Authentication and credential resilience — CLOSED LOCALLY.** Preserve the
   verified ChatGPT-plan multi-account path and cross-provider model fallback,
   then close any material same-provider credential/profile rotation,
   subscription/OAuth, expiry, quota, or account-selection gap. Do not borrow
   another product's private credentials or unsupported OAuth flow merely to
   increase auth-method count. Ash now has first-class Azure Entra identity,
   ChatGPT-plan multi-account auth, ordered same-provider API-key profiles, and
   bounded user-owned dynamic credential helpers with forced auth refresh and
   static-profile fallback.
3. **P1C — Model discovery and capability semantics — CLOSED.** Verify that
   model catalogs, aliases, context/output limits, vision, reasoning, native
   tools, usage, prompt caching, streaming terminal semantics, and model
   switching remain provider-owned and fail conservatively when metadata is
   absent or contradictory. Fix concrete incorrect assumptions rather than
   hard-coding fast-changing model lists.
4. **P1D — Local runtime parity — CLOSED.** Close the material difference between
   merely connecting to Ollama/LM Studio/vLLM and a strong local-model user
   journey. Decide with evidence whether Ash should manage runtime/model
   lifecycle itself or integrate cleanly with runtime-owned lifecycle commands;
   validate capability discovery, context sizing, tool calling, streaming,
   cancellation, health/readiness, and realistic coding turns on supported
   local runtimes.
5. **P1E — Real service/runtime conformance and claims — CLOSED.** Run the
   smallest set of live provider/runtime journeys that materially changes
   confidence, keep unsupported claims qualified, and finish with a current
   comparator pass. P1 closes only when no important provider/model user need
   remains materially weaker without a deliberate product reason.

Work P1A-P1E in evidence-driven slices; several may advance together when one
implementation legitimately spans them, but do not declare P1 closed until all
five are resolved.

### P1 finish flags

These are the bounded stop conditions for the provider/model parity phase. Do
not extend P1 with unrelated work once every flag is closed.

- `P1A_PROVIDER_BREADTH = CLOSED`
- `P1B_AUTH_RESILIENCE = CLOSED`
- `P1C_MODEL_CAPABILITY_TRUTH = CLOSED`
- `P1D_LOCAL_RUNTIME_PARITY = CLOSED`
- `P1E_LIVE_CONFORMANCE = CLOSED`
- `P1_PROVIDER_MODEL_PARITY = CLOSED`

P1C's finish condition is satisfied: supported hosted-provider model discovery,
capability/limit semantics, usage, prompt caching, and model switching now use
provider-owned metadata or exact first-party declarations, while missing,
ambiguous, conflicting, or deployment-opaque evidence fails conservative.
P1D's implementation-side finish condition is also satisfied. P1E now has a
current live post-refactor OpenAI journey, the earlier independent OpenRouter
service journey, and a current comparator/claims audit with unexercised routes
still explicitly qualified. All five bounded P1 flags are closed; P2 is next.

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

This slice advances P1C/P1D/P1E but does not close them.

### P1 progress — discovery and switching slice 2

Provider catalog discovery now has one auth-aware dispatch path that returns the
same `ProviderVerification` contract for ordinary API-key/local/custom routes
and first-party OpenAI ChatGPT-plan auth. `ash providers test` and REPL live
catalog refresh therefore cannot silently diverge on which OpenAI credential
path they use.

The REPL keeps successful `/models --refresh` results in a session-local
per-provider cache. Plain `/model` remains instant and network-independent while
merging those refreshed models into its normal numbered picker, so a model that
was discovered live is directly switchable without hand-typing its identifier.
The picker and renderer share one grouped ordering, preventing displayed
numbers from resolving to a different model when a refreshed model belongs to
an earlier provider group. Static/configured models remain available when live
discovery fails.

The complete affected CLI/provider unit files pass **95 tests**. Ruff, targeted
Mypy, and `git diff --check` are green.

P1 remains open. Explicitly remaining: provider-owned capability truth for
fast-changing hosted models rather than stale name heuristics, correct
representation of unknown model pricing instead of implying zero cost, the P1A
enterprise-route decision, supported local-runtime lifecycle and real
Ollama/LM Studio/vLLM conformance, and adapter-level
cancellation/stream-cleanup verification.

### P1 progress — pricing provenance slice 3

Ash no longer treats missing trusted model pricing as evidence that inference
was free. Runtime usage now carries an explicit `cost_known` signal while
retaining numeric `cost_usd` as the subtotal that can actually be calculated
from known rates. If any completion in a turn lacks pricing, the turn and its
SDK/result surfaces report pricing as unknown rather than presenting `$0` as a
complete cost. Automatic Goal continuations preserve the same aggregate rule.

Session storage schema v17 adds a rewind-safe `pricing_unknown_turns` counter.
New turns increment it only when pricing is incomplete; rewinds subtract the
removed turns' persisted provenance and a fully rewound empty session returns
to known-zero state. Pre-v17 sessions with historical token usage are
conservatively migrated as pricing-unknown because older schemas did not retain
enough provenance to prove their stored cost total is complete. Forked sessions
continue to own usage totals independently.

The distinction propagates through `SessionUsage`, SDK results, the terminal
status line, local metrics JSON/text, and durable automations. Automation
schema v4 persists `cost_known`, conservatively marks historical runs with token
usage as unknown, carries the flag through subprocess execution, worker
persistence, CLI/JSON output, and webhook delivery, and never renders an
unknown-priced run as `$0.000000`.

Validation passes **124 session/storage/status tests**, **198 loop/SDK tests**,
and **130 automation tests**. Ruff, targeted Mypy, and `git diff --check` are
green.

P1 remains open. Explicitly remaining: provider-owned capability truth for
fast-changing hosted models, supported local-runtime lifecycle and real
Ollama/LM Studio/vLLM conformance, and adapter-level
cancellation/stream-cleanup verification.

### P1 progress — DeepSeek current-agent compatibility slice 4

DeepSeek's first-party API contract changed materially during 2026, so Ash no
longer advertises the retired `deepseek-chat` / `deepseek-reasoner` model IDs.
The built-in picker now exposes `deepseek-flash` and `deepseek-v4-pro`; still
accepted V4 Flash compatibility aliases retain the same conservative mapping
when entered manually. The exact current manifest used by both runtime and
picker capability resolution records native tools and reasoning for both
models, a 1M context window and 384K maximum output, with vision enabled only
for Flash. Unknown DeepSeek IDs remain conservative instead of inheriting
features from name substrings.

DeepSeek thinking-mode tool use requires every prior assistant
`reasoning_content` value to be replayed exactly on later requests that carry
tools. Ash now captures that provider-required state on all DeepSeek reasoning
completions when durable replay is available, seals the exact UTF-8 text with
AES-GCM, and persists only a bounded opaque canonical `provider_state` envelope.
The profile-scoped seal key is stored through Ash's existing descriptor-anchored
private-store boundary. Restarted provider instances reopen the same key and
restore the exact `reasoning_content` field before network I/O; missing,
malformed, oversized, or tampered replay state fails closed rather than sending
an invalid continuation. Generic persistence keeps sealed state byte-exact
while continuing to redact provider-state variants that may contain plaintext
summaries.

DeepSeek Flash vision remains enabled because its current Chat Completions
contract accepts the same user-only OpenAI `image_url` / base64 data-URL shape
Ash already emits. Built-in DeepSeek dollar pricing was removed because current
first-party prices vary by peak/off-peak window; absent an explicit user rate,
Ash now reports pricing as unknown instead of applying a stale universal value.

Evidence was rechecked against DeepSeek's first-party documentation on
2026-10-02, including:
`https://api-docs.deepseek.com/quick_start/pricing/`,
`https://api-docs.deepseek.com/guides/thinking_mode/`,
`https://api-docs.deepseek.com/guides/vision/`, and
`https://api-docs.deepseek.com/guides/anthropic_api/`.

The full affected provider/loop/CLI gate passes **461 tests**. Ruff, targeted
Mypy, and `git diff --check` are green.

P1 remains open. Explicitly remaining: provider-owned capability truth for
other fast-changing hosted routes such as Groq, the P1A enterprise-route
decision, supported local-runtime lifecycle and real Ollama/LM Studio/vLLM
conformance, and adapter-level cancellation/stream-cleanup verification.

### P1 progress — Groq current-model truth slice 5

Ash no longer treats Groq's model catalog as a timeless static list. The
generic picker now exposes only the current generally available production
text models `openai/gpt-oss-120b` and `openai/gpt-oss-20b`. The older
`llama-3.1-8b-instant` and `llama-3.3-70b-versatile` IDs were removed from
generic defaults because Groq retired them for free/developer tiers in August
2026, while still permitting committed-spend Enterprise access. Those IDs
remain recognized when an entitled account returns them through live model
discovery. `groq/compound-mini` is no longer advertised because Groq
decommissioned it on 2026-09-21. `qwen/qwen3.8-27b` is recognized accurately
when live-discovered but remains non-default because Groq labels it Preview.

Groq capability resolution now combines an exact first-party semantic manifest
with live provider-owned model metadata. Known GPT-OSS models advertise native
tools and reasoning; Enterprise Llama models advertise native tools without
invented reasoning/vision support; Qwen 3.8 advertises tools, reasoning and
vision. Unknown IDs remain conservative. At session capability negotiation the
adapter retrieves Groq's own model record, fails closed for inactive models,
and adopts provider-reported context and max-completion limits instead of
assuming the static documentation value is still current.

The Groq wire path now uses `max_completion_tokens` rather than the deprecated
`max_tokens` request field. Provider-reported prompt-cache hits from
`prompt_tokens_details.cached_tokens` flow into Ash usage accounting. Built-in
pricing for the two production GPT-OSS defaults reflects Groq's current
published input/output rates and 50%-discount cached-input rates; Enterprise
and Preview routes without stable general pricing continue to surface as
unknown unless the user configures an explicit rate.

Evidence was rechecked against Groq's first-party documentation on 2026-10-02,
including:
`https://console.groq.com/docs/models`,
`https://console.groq.com/docs/deprecations`,
`https://console.groq.com/docs/reasoning`,
`https://console.groq.com/docs/prompt-caching`,
`https://console.groq.com/docs/api-reference`,
`https://console.groq.com/docs/model/openai/gpt-oss-120b`, and
`https://console.groq.com/docs/model/openai/gpt-oss-20b`.

The full affected provider/registry/readiness/CLI/loop gate passes **389
tests**. Ruff, targeted Mypy, and `git diff --check` are green.

P1 remains open. Explicitly remaining: supported local-runtime lifecycle and
real Ollama/LM Studio/vLLM conformance, and adapter-level
cancellation/stream-cleanup verification.

### P1 progress — enterprise cloud routes slice 6

P1A is closed at the product/implementation level. Ash now has first-class
enterprise-hosted routes where generic bearer custom endpoints were not an
adequate substitute:

- Google Vertex AI uses an explicit user-owned project and location plus
  Google Application Default Credentials. The optional gcp extra installs
  google-auth with its requests transport. Ash lazily loads ADC, serializes
  refresh, passes an async rotating bearer credential into the shared
  OpenAI-compatible adapter, and retains recent tokens only in memory for
  provider-error redaction. No implicit global location is selected.
- Amazon Bedrock uses the optional aws extra and the official OpenAI Bedrock
  provider rather than an Ash-owned SigV4 implementation. The route targets
  AWS's recommended bedrock-runtime endpoint, supports explicit Region/profile
  scope, resolves partition-aware Runtime URLs through public botocore endpoint
  data, and scopes AWS credential-chain variables into isolated provider
  workers. Native foundation-model and inference-profile listings are exposed
  only as candidates because AWS documents API compatibility separately per
  model; a real Runtime Chat Completions request is the authoritative readiness
  check.

Both enterprise scopes are excluded from project-controlled configuration and
neither route persists cloud credentials. Core Ash remains lightweight:
OpenAI stays >=1.82,<4, while gcp and aws raise only their own optional
requirements. The locked current environment uses OpenAI 3.23; real provider
constructors were exercised with the installed extras, and wheel metadata was
verified to publish the expected optional dependencies.

The full affected provider/message/registry/readiness/CLI/loop/runtime/config/
setup gate passes **704 tests**. The narrower enterprise lifecycle gate passes
**388 tests**. Ruff, targeted Mypy, pinned uv lock validation, diff checks, and
wheel/sdist build validation are green.

P1A closure does not claim live AWS/GCP service conformance; credentialed cloud
calls remain in P1E. Azure OpenAI/Foundry identity work is tracked under P1B
because it is an authentication/credential concern rather than provider-count
coverage.

Evidence was rechecked against current first-party AWS and Google cloud
documentation on 2026-10-02. AWS recommends bedrock-runtime for new
applications, documents SigV4 on the OpenAI Chat Completions route, and states
that native model/profile discovery must be combined with per-model API
compatibility. Vertex's OpenAI-compatible endpoint uses explicit
project/location scope plus short-lived Google Cloud OAuth/ADC credentials.
Primary references:
https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html
https://docs.aws.amazon.com/bedrock/latest/userguide/models-api-compatibility.html
https://docs.cloud.google.com/vertex-ai/generative-ai/docs/samples/generativeaionvertexai-gemini-chat-completions-non-streaming

### P1 progress — Azure identity and failover commit boundary slice 7

The first P1B implementation slice closes two prerequisites without declaring
the full authentication-resilience gate complete.

First, automatic provider failover now treats every retained model output/state
as a commit point. In addition to visible text and tool calls, reasoning,
reasoning blocks, and provider replay state now prohibit switching to another
provider after a partial response. Usage/model/diagnostic metadata that does not
become assistant history remains non-committing, so a provider can still fail
over safely before any model output/state has been exposed. This is required
before adding same-provider credential rotation; otherwise a reasoning-only
partial response could be silently mixed with a backup provider.

Second, Ash now has a first-class Azure OpenAI / Microsoft Foundry v1 route:

- the user owns the public Azure v1 resource/project endpoint and authentication
  mode; project config cannot redirect either;
- API-key mode uses the core OpenAI dependency and needs no Azure SDK;
- Entra/managed-identity mode uses the optional azure capability pack,
  azure-identity, and the official async bearer-token provider with
  https://ai.azure.com/.default;
- async Azure Identity transport is explicit through azure-core[aio], after a
  real optional-extra constructor smoke exposed that plain azure-identity alone
  does not install aiohttp;
- the provider owns and closes credentials it creates, setup fails closed on
  credential cleanup errors, and recent access tokens exist only in a bounded
  in-memory redaction window;
- isolated provider workers forward only API-key material in API-key mode and
  only Entra/workload/managed-identity environment in Entra mode;
- Azure model/deployment discovery remains non-authoritative, so readiness is
  established by a bounded real completion just like the other enterprise
  routes whose management inventory does not prove OpenAI-wire compatibility;
- capabilities remain conservative because Azure wire compatibility does not
  prove tools, vision, reasoning, or model limits for the selected deployment.

The complete affected Azure/failover/provider/config/setup/installer/loop gate
passes **695 tests**. The focused Azure/failover gate passes **57 tests**.
Ruff, targeted Mypy, pinned uv lock validation, git diff checks, a real
Azure-Identity async constructor/close smoke, and wheel/sdist metadata
verification are green. The built wheel publishes the azure extra with
azure-core[aio], azure-identity, and the compatible OpenAI range while keeping
those dependencies out of the base install.

At this checkpoint P1B still had the dynamic external-credential-helper portion
open. The following slices add ordered user-owned API-key references,
stickiness, bounded cooldowns, Retry-After-aware health, classified pre-output
rotation, and finally the bounded helper path. Existing ChatGPT multi-account
auth remains intact and unsupported private subscription credentials are not
borrowed.

Evidence was rechecked against Microsoft first-party Azure v1 documentation on
2026-10-02:
https://learn.microsoft.com/en-us/azure/ai-foundry/openai/api-version-lifecycle
https://learn.microsoft.com/en-us/azure/ai-services/reference/sdk-package-resources

### P1 progress — same-provider API-key resilience slice 8

P1B now has a production-grade static credential-rotation layer for API-key
routes without turning Ash into a second secret vault. User-owned
provider_api_key_envs stores only an ordered list of environment-variable
references for a provider. References are bounded, validated, deduplicated,
excluded from project config, and all must resolve before any child provider is
constructed. Raw key values remain in the process environment or private Ash
profile dotenv and are not written to TOML, SQLite, session history, runtime
events, or telemetry.

The registry nests a CredentialPoolProvider inside each model route and leaves
the existing FailoverProvider outside it. That gives the intended ordering:
same-provider credential recovery first, then model/provider fallback. The
public ProviderRegistry factory contract remains unchanged, so third-party
registrations are not silently opted into credential semantics they did not
declare.

The pool uses one shared provider-neutral failure classifier with the core
retry loop. Authentication failures (401/403), explicit billing/quota failures
and rate limits can rotate credentials only before retained model output/state.
Successful credentials become sticky. Authentication, billing and default
rate-limit cooldowns are bounded and session-local; provider Retry-After hints
are honored up to the safety ceiling. Request/format/model errors and ordinary
transient network/server failures do not rotate credentials and continue
through Ash's established retry/failover policy. If every credential is
cooling down, the pool emits a bounded retry hint instead of polling.

Replay safety is now consistent across all automatic recovery layers:
CredentialPoolProvider, FailoverProvider and the core request retry loop all
use the same retained-output/state commit predicate covering text, XML/native
tool output, reasoning, reasoning blocks and provider replay state. Metadata/
usage-only chunks remain safe to abandon before a retry.

Subprocess isolation is least-privilege for pools: only explicitly referenced
credential variables cross the worker boundary, while stale provider defaults
and unrelated secrets are omitted. Azure API-key pools additionally exclude
Entra identity variables. Secret-free request events and OpenTelemetry expose
only the active reference name, pool size and failure category. ash config
explain shows the validated reference names while continuing to mask actual
secret-bearing config fields.

The complete affected provider/runtime/config/CLI/observability gate passes
**859 tests**; focused credential/security/telemetry checks are also green with
Ruff and targeted Mypy. No
dependency or config-schema migration is required for this additive user-owned
field.

At this checkpoint P1B still had one deliberately separate capability open: a
bounded external credential helper for short-lived/vault/SSO-generated API
keys. Current comparator evidence showed this was materially useful: OpenClaw
resolves named auth/secret references and rotates profiles before model
fallback, while Claude Code supports apiKeyHelper for externally generated
credentials. The following slice closes that gap without accepting arbitrary
shell command strings.

Current comparator references rechecked on 2026-10-02:
https://docs.openclaw.ai/models
https://docs.openclaw.ai/gateway/config-secrets-env
https://code.claude.com/docs/en/settings

### P1 progress — dynamic credential helper slice 9

P1B is now closed locally. API-key routes can configure one user-owned dynamic
credential helper ahead of ordered static env profiles through
`provider_api_key_helpers`. The helper definition is excluded from
project-controlled config, bounded and validated, and deliberately stores no
credential value.

The execution boundary is narrower than a generic shell hook:

- helper commands are argv lists, never shell strings;
- the executable must be a bare host command resolved outside the active
  workspace or an explicit absolute executable outside the workspace;
- execution cwd is user-owned Ash state, not repository content;
- child environment starts from Ash's scrubbed baseline and adds only the
  helper's explicit env allowlist;
- helper authentication belongs in that env allowlist, not argv, because
  process arguments may be visible to the host OS;
- execution has bounded timeout/output and managed descendant cleanup;
- stderr/helper output is never copied into failure messages;
- stdout must be exactly one bounded non-whitespace UTF-8 credential line.

Helper credentials are cached only in memory for a bounded TTL. Registry
construction is lazy and never executes the helper. Normal capability refresh
reuses a healthy credential; TTL expiry refreshes it naturally. A pre-output
401/403 forces one helper refresh, after which a second auth failure advances
to ordered static env backups inside the same model request attempt. Helper
execution failures use the explicit `credential_source` classification and can
also advance to static backups. Once text, tool output, reasoning, reasoning
blocks or provider replay state has been retained, helper refresh/rotation is
forbidden by the same shared commit predicate used by request retry and model
failover.

Provider construction remains extension-safe: helper-backed and static
credential entries use the same Ash-owned credential factory and capability
resolver, while replacing or unregistering a built-in provider factory revokes
the private pool constructor rather than bypassing the extension. Isolated
workers forward only the helper env allowlist and explicit static pool
variables, not ambient default provider keys or unrelated cloud identity
material.

Secret lifetime and diagnostics are bounded. Successfully retired helper
provider objects are released after close, wrapper shutdown clears the helper
cache, `ash config explain` masks helper definitions, and runtime/OpenTelemetry
surfaces expose only secret-free profile IDs such as `helper:openai`, pool size
and failure category.

The complete affected provider/runtime/config/setup/CLI/observability/subagent/
automation gate passes **1,078 tests**. Focused helper, credential-pool,
security and telemetry gates are green with Ruff and targeted Mypy.

P1 remains open after P1B closure. The explicit remaining work is P1C/P1D/P1E:
provider-owned model/capability semantics where still unresolved, supported
local-runtime lifecycle plus real Ollama/LM Studio/vLLM conformance, and
adapter-level cancellation/stream-cleanup plus the minimum live service/runtime
evidence required to support final claims.

### P1 progress — OpenAI current-model transport semantics slice 10

The first P1C slice removed a transport/capability mismatch on first-party
OpenAI models. Ash previously inferred tools, vision, and reasoning from broad
OpenAI model-name substrings while the API-key route always used Chat
Completions. Current GPT-6 contracts make that unsafe: GPT-6 Astra and GPT-6.1
Sol require Responses for tool calling, and GPT-6 Sol/Luna restrict Chat
Completions function calling to `reasoning_effort=none`.

The first-party API-key route now selects the public Responses API for exact
current GPT-5.6/GPT-6 model IDs, sharing Ash's already verified stateless
Responses history/tool/reasoning replay semantics with ChatGPT-plan auth.
Current OpenAI capability declarations are exact and bounded at the model IDs
Ash has verified instead of using `gpt-*`/`o*` substring heuristics; unknown
OpenAI IDs fail conservative. An operator-overridden `OPENAI_API_BASE` remains
on the generic Chat Completions adapter and explicitly receives conservative
capabilities rather than inheriting first-party OpenAI semantics.

The Responses API-key path preserves `store=false`, streaming terminal
validation, canonical native calls, encrypted reasoning replay, configured
output limits, prompt-cache controls, and provider-reported cache read/write
usage. Non-default sampling temperature and unsupported 24-hour cache retention
fail clearly instead of being silently ignored. The offline picker now points
at the current GPT-6 Astra / GPT-6.1 Sol / GPT-6 Luna frontier while live model
discovery remains the provider-owned source of account-specific availability.

Evidence was rechecked against OpenAI's first-party model, GPT-6 migration,
reasoning, prompt-caching, and changelog documentation on 2026-10-02:
`https://developers.openai.com/api/docs/models`,
`https://developers.openai.com/api/docs/guides/latest-model`,
`https://developers.openai.com/api/docs/guides/reasoning`,
`https://developers.openai.com/api/docs/guides/prompt-caching`, and
`https://developers.openai.com/api/docs/changelog`.

P1C remains open for the remaining provider-owned discovery/capability
semantics; this slice deliberately does not advance P1D or P1E claims.

### P1 progress — Anthropic model-truth slice 11

The next P1C slice removed the same class of family-wide capability assumption
from Anthropic. Ash's offline Anthropic catalog now tracks the current Claude
Fable 5.1 / Opus 5.5 / Sonnet 5.5 / Haiku 4.5 lineup, fixes Sonnet 4.6's
documented 128K output ceiling, and uses exact capability declarations; unknown
Anthropic IDs no longer inherit tools or vision merely from the provider name.

For first-party Anthropic endpoints, runtime capability negotiation now uses
the Models API when available to resolve aliases plus provider-owned image,
thinking, context-window, and max-output metadata. Native client-tool support
stays conservative unless the resolved model is one of Ash's exact verified
Claude IDs because Anthropic's current Models API capability object does not
publish a generic client-tool-use flag. Operator-overridden Anthropic base URLs
start conservative instead of inheriting first-party model semantics.

Built-in pricing now covers the current four-model lineup and distinguishes
Anthropic's 5-minute cache-write price from the configured one-hour extended
retention price, while explicit user pricing remains authoritative. Evidence
was rechecked against Anthropic's first-party Models API, model overview, model
versioning, current model pages, and tool-use documentation on 2026-10-02.

P1C remains open for the remaining hosted-provider discovery/capability
semantics; this slice does not consume the separate P1D/P1E closure work.

### P1 progress — Google Gemini capability/replay slice 12

Google's OpenAI-compatible model list remains the availability/discovery
surface, but the first-party route now probes the selected model through
Google's native Models API for richer provider-owned input/output token limits
and thinking metadata. The native metadata request uses Google's
`x-goog-api-key` contract and the same bounded HTTPS/redaction protections as
other credentialed catalogs. Exact documented Gemini function-calling models
provide the offline tools/vision/reasoning floor; unknown IDs remain
conservative, and provider-owned metadata can narrow a conflicting claim.

This slice also closes a multi-step tool-loop correctness gap. Gemini 3
requires thought signatures to be returned during function calling, including
through Google's OpenAI compatibility layer. Ash now captures
`tool_calls[].extra_content.google.thought_signature`, seals it in durable
provider replay state, and restores it onto the matching assistant tool call
before the next request. Missing required Gemini 3 replay state fails locally
with a clear error rather than reaching Google as a known-invalid request.
Operator-overridden Google endpoints remain conservative and do not inherit
this first-party protocol requirement merely from the provider name.

Evidence was rechecked on 2026-10-02 against Google's first-party Models API,
Gemini model overview, function-calling guide, thought-signature guide, token
guide, image-understanding guide, and OpenAI-compatibility documentation:
`https://ai.google.dev/api/models`,
`https://ai.google.dev/gemini-api/docs/models`,
`https://ai.google.dev/gemini-api/docs/function-calling`,
`https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures`,
`https://ai.google.dev/gemini-api/docs/tokens`,
`https://ai.google.dev/gemini-api/docs/image-understanding`, and
`https://ai.google.dev/gemini-api/docs/openai`.

P1C remains open for the remaining hosted-provider capability/usage/cache and
model-switch semantics; P1D/P1E remain separate finish gates.

### P1 progress — Vertex Gemini capability/replay slice 13

The enterprise Vertex route previously treated every deployment/model ID as
capability-unknown, which was correct for arbitrary publisher IDs but too
conservative for Google's own documented Gemini Chat Completions models. Ash
now gives exact verified `google/gemini-*` IDs their documented tools, vision,
and reasoning floor while leaving unknown/non-Google Vertex model IDs
conservative. Gemini 3.8 Flash additionally carries Vertex's documented
1,048,576-token context and 65,536-token output limits.

Vertex Gemini tool turns now share the same bounded thought-signature replay
implementation as the direct Google route. The reusable helper captures the
OpenAI-compatible `extra_content.google.thought_signature`, seals it before
persistence, restores it on the matching assistant tool call, and fails locally
when a required Gemini 3 signature cannot be recovered. Direct Gemini state is
sealed as `google` and Vertex state as `vertex`, preventing cross-route replay.
Unknown Vertex models do not initialize this replay machinery.

Evidence was rechecked on 2026-10-02 against Google Cloud's Gemini 3.8 Flash
model page, Vertex OpenAI function-calling sample, OpenAI compatibility guide,
and thought-signature documentation. Gemini 3.8 Flash is documented with Chat
Completions, multimodal input, thinking, function calling, a 1,048,576-token
context window, and 65,536 maximum output tokens; Gemini 3 requires prior
function-call thought signatures to be returned or the provider rejects the
request.

At the slice-13 checkpoint, P1C still had the final hosted-provider
capability/usage/cache audit open. Azure deployment IDs and Bedrock
model/profile IDs remained conservative because provider-owned evidence could
not identify their runtime capabilities safely; the following slice closes
P1C without weakening that boundary.

### P1 progress — hosted capability closure and Azure usage slice 14

The final P1C audit rechecked every hosted route against the bounded finish
condition instead of expanding the provider list again. Direct Anthropic,
OpenAI, Google Gemini, DeepSeek, and Groq routes now have exact current
first-party semantics where the provider contract supports them. Vertex Gemini
uses exact documented Google model semantics. OpenRouter, Hugging Face, Vercel
AI Gateway, Mistral, xAI, Together, Fireworks, Cerebras, and NVIDIA remain
catalog-driven and conservative when provider metadata is absent or
contradictory. Azure deployment IDs and Bedrock model/profile IDs intentionally
remain capability-unknown because their names/inventory do not prove the
serving model's OpenAI-wire feature set.

The audit found one remaining usage defect: Azure's GA v1 Chat Completions
contract supports `stream_options.include_usage`, but Ash inherited the generic
custom-base default that omitted it. Azure now requests the authoritative
trailing usage chunk and normalizes prompt, completion, and cached-input counts
through the same terminal-tail path already used by other OpenAI-compatible
providers. Missing provider usage elsewhere remains explicitly estimated rather
than presented as authoritative.

Prompt-cache behavior is now truthful rather than artificially uniform:
Anthropic and current first-party OpenAI routes expose their supported cache
controls and provider-reported read/write usage; DeepSeek and Groq preserve
their automatic cache-hit accounting; Gemini's implicit caching remains
provider-managed rather than receiving invented OpenAI cache controls; routes
without a verified cache contract are left untouched. Core terminal semantics
already reject output after a terminal chunk while accepting a compatible
usage-only terminal tail, and runtime model switching re-runs dynamic
capability negotiation before the next turn with provider retirement and
rollback coverage.

Azure usage evidence was rechecked against Microsoft's current first-party v1
Chat Completions reference on 2026-10-02:
`https://learn.microsoft.com/en-us/rest/api/aifoundry/azureopenai/chat`.
Google caching behavior was rechecked against current Gemini API and Google
Cloud context-caching documentation on the same date.

`P1C_MODEL_CAPABILITY_TRUTH = CLOSED`. The finite P1 roadmap now advances to
P1D local-runtime parity; live credentialed/provider conformance remains P1E
and does not block this hosted-model semantic closure.

### P1 progress — local-runtime ownership and cancellation slice 15

P1D starts from an explicit lifecycle decision rather than copying a competitor
surface. Ollama, LM Studio/llmster, and vLLM already own installation, daemon,
model-download/load, and serving lifecycle through their supported tools. Ash
therefore treats those runtimes as separately managed services and owns the
integration contract: safe loopback/default endpoints, model discovery,
capability negotiation, readiness guidance, streaming, cancellation, agent
tool semantics, and cleanup. This matches the useful comparator pattern where
separately managed Ollama/LM Studio remain external while a harness-managed
server is reserved for a backend the harness itself installs.

The first implementation gap in that contract was cancellation/resource
cleanup. LM Studio and vLLM use Ash's OpenAI-wire adapter, but an outer provider
generator could be cancelled after yielding a chunk without deterministically
closing the inner OpenAI SDK stream. Ash now has one bounded stream-lease helper
that closes SDK streams on EOF, error, or cancellation while preserving the
primary exception. OpenAI Chat Completions, current Responses, DeepSeek, and
Groq use the same primitive. The core loop also owns each provider-stream lease
explicitly and closes it before retry, successful finalization, or cancellation,
so local server connections are not left to garbage-collection timing.

Setup recovery is now runtime-specific: LM Studio points to `lms server start`
plus `lms load <model>`/JIT loading, while vLLM points to `vllm serve <model>`
and its `/v1/models` readiness boundary. Ollama already had its specific
`ollama serve` / `ollama pull` path.

Evidence was rechecked on 2026-10-02 against Ollama's CLI reference, LM Studio's
`lms`/headless documentation, vLLM's OpenAI-compatible server/serve docs, and
OpenClaw's current local-model guidance. P1D remains open for realistic local
coding-turn conformance and any concrete readiness/capability defects those
journeys expose; P1E remains the separate live-service claim gate.

### P1 progress — local coding-turn closure slice 16

The remaining P1D gap was the difference between protocol-unit coverage and a
real Ash user journey. Fresh-process deterministic E2E now exercises every
supported local route through discovery/capability negotiation, streamed
inference, tool execution, continuation, and an actual workspace mutation:

- **Ollama** probes `/api/show`, negotiates declared native tools/context, emits
  a native `/api/chat` `write_file` call, receives the tool result on the next
  request, and completes the turn.
- **LM Studio** uses native `/api/v1/models` capability metadata to prove tool
  training, sends the OpenAI-compatible native tool schema, performs the same
  file edit, and completes after canonical tool-result replay.
- **vLLM** exposes its served context through `/v1/models` but does not prove
  auto-tool/parser configuration. The fresh-process request therefore contains
  no native `tools` field; Ash's text/XML fallback performs the same file edit
  and completes safely.

These journeys complement the existing bounded catalog/readiness tests,
Ollama streaming/usage/thinking tests, LM Studio loaded-context/capability
parsing, vLLM conservative tool negotiation, and the cancellation/resource
cleanup slice immediately above. Runtime installation, daemon/model lifecycle,
and hardware fit remain intentionally runtime-owned.

`P1D_LOCAL_RUNTIME_PARITY = CLOSED`. This is not a claim that real Ollama, LM
Studio, and vLLM binaries were exercised on the current host: none are installed
or listening here at this checkpoint. P1E closes below with that limitation
preserved explicitly rather than promoting deterministic local-runtime evidence
to a live-runtime claim.

### P1 progress — live-conformance and provider/model closure slice 17

P1E closes on scoped external evidence, not on the impossible standard that
every supported vendor/model/runtime combination must be live-tested on one
host. Claims that materially depend on live external behavior remain labeled by
their actual evidence level.

On 2026-10-03, Ash's existing user-owned ChatGPT-plan registration remained
refreshable and plan-enabled after the P1 stream/cancellation refactors. Live
model discovery returned the current selectable account catalog including
`gpt-6-astra`. A bounded `ash providers test openai/gpt-6-astra` then verified
authoritative model discovery, selected-model availability, and a real
Responses completion. A separate isolated temporary-workspace turn exercised
the current native-tool path end to end: GPT-6 Astra called `write_file`, Ash
executed the edit, replayed the canonical tool result, received the exact final
response, and recorded provider-authoritative token usage. The temporary trust
entry/workspace was removed immediately after the run.

That current OpenAI evidence complements the earlier bounded real OpenRouter
service journey, which independently proved live catalog discovery,
provider-reported usage, and a coding workflow that edited and externally
tested a project using an ephemeral credential that Ash did not persist. No
ambient API keys for the other cloud providers and no Ollama/LM Studio/vLLM
binaries were available at this checkpoint, so Ash does **not** relabel those
routes as live-verified. Their deterministic protocol/fresh-process evidence
and the table's qualified `Partial` status remain the truthful boundary.

The current comparator pass rechecked OpenClaw and Hermes provider/local-model
surfaces. Both now offer managed llama.cpp-style local runtimes while also
supporting externally managed local servers; OpenClaw explicitly keeps LM
Studio and Ollama as separately managed options, and Hermes accepts existing
llama-server/custom OpenAI-compatible endpoints. Ash's decision to leave
Ollama, LM Studio, and vLLM lifecycle runtime-owned is therefore a deliberate
product boundary, not an unnoticed provider/model parity defect. No current
comparator evidence exposed a material provider/model user need that should
keep this finite gate open.

Current comparator references rechecked on 2026-10-03:
`https://docs.openclaw.ai/gateway/local-models`,
`https://docs.openclaw.ai/plugins/llama-cpp`,
`https://hermes-agent.nousresearch.com/docs/user-guide/local-models`, and
`https://hermes-agent.nousresearch.com/docs/integrations/providers/`.

`P1E_LIVE_CONFORMANCE = CLOSED` and `P1_PROVIDER_MODEL_PARITY = CLOSED`.
Qualified provider/runtime rows remain evidence boundaries, not hidden claims
that every external service/version has been exercised. The active finite
roadmap advances to P2 agent/workspace parity.

### P2 finish flags

These are the bounded stop conditions for agent/workspace parity. Do not turn
P2 into an open-ended inventory of every multi-agent pattern another harness
can express.

- `P2A_DELEGATION_DELIVERY = CLOSED`
- `P2B_DURABLE_BACKGROUND_RECOVERY = CLOSED`
- `P2C_WORKTREE_WORKSPACE_ISOLATION = CLOSED`
- `P2D_ADAPTIVE_ORCHESTRATION = CLOSED`
- `P2E_REMOTE_MULTIWORKSPACE_CONFORMANCE = CLOSED`
- `P2_AGENT_WORKSPACE_PARITY = CLOSED`

P2 closes when focused child work can be delegated with bounded context/tools,
foreground and background results return to the owning workflow safely, durable
work has honest recovery/steering/stop semantics, parallel edits are isolated
and reviewable, adaptive decomposition is either supported or deliberately
replaced by a stronger bounded orchestration contract, and remote/multi-workspace
claims are backed by current evidence. Presentation features alone do not keep
the gate open when the underlying workflow is already accessible through CLI,
SDK, ACP/A2A, or persisted status/event surfaces.

### P2 progress — durable background completion delivery slice 1

Ash already persisted subagent reports to a workspace-scoped SQLite lead inbox,
but background completion stopped one layer too early: the active parent session
did not consume those reports automatically. A user or host had to inspect
`/agents`, the top-level agent CLI, or SDK state manually before the parent model
could act on completed background work. Current OpenClaw and Hermes background
delegation both return completions to the requester, so this was a material
workflow gap rather than cosmetic monitoring.

Background reports now carry explicit durable delivery metadata, including the
originating task and graph identity. Foreground reports are acknowledged as soon
as their ordinary `spawn_agent`/delegation tool result returns, preventing
already-consumed reports from accumulating in the lead inbox. Background
reports remain pending until the parent loop reaches a safe model boundary. The
loop then persists a bounded completion notice into session history **before**
acknowledging the IPC row, giving the handoff at-least-once failure semantics
instead of losing a completion on a crash between coordination and history
stores.

Worker-controlled task/summary text is redacted, size-bounded, JSON-quoted, and
wrapped as explicitly untrusted evidence in a user-role history message. It is
not promoted to system instructions. No extra model turn is started merely
because a worker finishes: completions join the next already-running model
iteration or the next user turn, avoiding surprise cost and runaway background
chaining. The in-memory session recognizes a persisted IPC message ID so an
acknowledgement failure cannot duplicate the report repeatedly within the same
turn.

Current comparator evidence rechecked on 2026-10-03: OpenClaw subagents run as
tracked background tasks and announce results back to their requester; Hermes
background delegation similarly returns handles immediately and routes final
results back to the owning session. OpenAI's current managed multi-agent harness
also exposes explicit spawn/message/wait/interrupt/list semantics for a live
agent tree. The parent-context delivery path, ordinary delegation DAGs, and
persisted agent CLI/API surfaces pass together, so
`P2A_DELEGATION_DELIVERY = CLOSED`.

### P2 progress — recovery, isolation, and adaptive orchestration closure slice 2

P2B and P2C were revalidated as production behaviors rather than inferred from
their implementations. Focused recovery tests prove renewable leases,
side-effect replay fencing, restart dispatch, attempt-scoped durable approvals,
live steering, persisted stop, active graph cancellation, and single-winner
approval resolution. Focused worktree tests prove clean-lead admission, managed
branch/commit identity, dependency-artifact handoff, conflict-safe application,
deterministic cleanup, stop cleanup, explicit discard, and the requirement to
apply isolated work before report-based continuation. These gates close
`P2B_DURABLE_BACKGROUND_RECOVERY` and
`P2C_WORKTREE_WORKSPACE_ISOLATION`.

The remaining comparator difference was adaptive hierarchy. Ash's root-owned
atomic DAG was safer than unconstrained recursive spawning but could not expand
work when a long-running child discovered a new bounded subproblem. Current
OpenClaw exposes depth-capped nested subagents; Hermes keeps delegation flat by
default but supports opt-in orchestrator children; OpenAI's current multi-agent
runtime likewise models live agent trees. Ash now adopts the useful capability
without adopting unrestricted recursion:

- direct children remain leaves by default (`agent_max_spawn_depth = 1`);
- nesting is an explicit user-owned opt-in with a maximum depth of five and a
  separate direct-child limit;
- only the dedicated `orchestrator` role receives `delegate_agents`; leaf
  workers never receive recursive `spawn_agent`;
- orchestrators are shared-workspace coordinators, not isolated editors, and
  nested graphs must complete synchronously before the orchestrator returns;
- descendant tasks carry durable `parent_task_id` lineage plus explicit spawn
  depth, use the same global concurrency/token/time/cost and permission
  boundaries, and cannot override their owning lineage;
- cancellation traverses both dependency edges and parent-child lineage, so a
  stopped owner cannot leave descendant work running;
- the same contract passes through Ash's real subprocess worker boundary,
  including explicit approval brokerage and provider reconstruction.

The new depth/child limits stay user-owned: repository project configuration
cannot raise them. A custom OpenAI-compatible subprocess regression enables
native tools only through Ash's existing exact per-model capability declaration;
an earlier loopback attempt using an overridden OpenAI endpoint correctly stayed
conservative and was not weakened for the test. With in-process and subprocess
nested journeys, depth/child admission, and lineage cancellation green,
`P2D_ADAPTIVE_ORCHESTRATION = CLOSED`.

### P2 progress — remote and multi-workspace closure slice 3

P2E keeps Ash's coding-workspace boundary explicit instead of copying a gateway
persona model. One Ash runtime/client owns one immutable workspace root;
concurrent independent roots use independent clients/processes, same-repository
parallel edits use managed Git worktrees, and independent remote agents use the
official A2A path. Current OpenClaw can multiplex long-lived personas with
separate workspaces and channel bindings inside one gateway, while Hermes
primarily scopes a session to its launch directory or remote terminal backend.
For Ash's terminal-first coding mission, channel/persona routing is an
interfaces/anywhere-access question for P5, not an agent/workspace blocker.

Focused P2E conformance proves the official A2A client stream/resume journey,
durable remote-context restart recovery and endpoint-rebinding refusal, remote
cancellation of an active Ash turn, workspace-scoped A2A task state, SDK agent
state isolation, cross-workspace session-resume refusal, workspace-namespaced
agent identity/IPC/sprints, and cross-workspace approval refusal. Therefore
`P2E_REMOTE_MULTIWORKSPACE_CONFORMANCE = CLOSED` and
`P2_AGENT_WORKSPACE_PARITY = CLOSED`.

Comparator references rechecked on 2026-10-03:
`https://docs.openclaw.ai/tools/subagents/nesting`,
`https://docs.openclaw.ai/tools/subagents/operations`,
`https://docs.openclaw.ai/concepts/multi-agent/index.html`,
`https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation`,
`https://hermes-agent.nousresearch.com/docs/user-guide/configuration`,
`https://hermes-agent.nousresearch.com/docs/user-guide/git-worktrees`, and
`https://developers.openai.com/api/docs/guides/responses-multi-agent`.

All five bounded P2 finish flags are closed. The active roadmap advances to P3
web/computer interaction parity.

### P3 finish flags

P3 is intentionally bounded around interaction workflows a coding harness can
justify. It is not a requirement to absorb every remote-desktop or general
assistant surface exposed by gateway products.

- `P3A_WEB_RETRIEVAL = CLOSED`
- `P3B_BROWSER_INTERACTION = CLOSED`
- `P3C_SIGNED_IN_BROWSER = CLOSED`
- `P3D_REMOTE_BROWSER_OPERATION = OPEN`
- `P3E_DESKTOP_COMPUTER_USE = OPEN`
- `P3F_INTERACTION_CONFORMANCE = OPEN`
- `P3_WEB_COMPUTER_PARITY = OPEN` until P3A-P3F are all closed.

P3 closes when public-web research/fetch is dependable and appropriately
bounded; browser interaction can complete realistic modern web-app/test flows;
authenticated browser state has an explicit safe user-consent story; local vs
remote browser ownership is a deliberate product decision; general desktop
computer use is either implemented for a concrete coding workflow or rejected
for a defensible scope/security reason; and external-behavior claims have
realistic conformance evidence plus a current comparator pass.

### P3A closure — guarded public web retrieval

P3A is CLOSED. Ash keeps the public-web surface intentionally small:
`web_search` provides normalized live search and `web_fetch` retrieves one
public HTTP(S) resource. Search supports explicit/auto Brave and Tavily
selection, bounded results and fields, freshness filters, allowed-domain
filtering, provider provenance/citations, credential redaction, provider
fallback on backend failures, and user-owned setup/doctor/config controls.
Fetch remains keyless and enforces scheme/credential checks, public-host DNS
validation, DNS-result pinning against rebinding, redirect revalidation,
allowed domains, response-size/content-type bounds, bounded model output, and
sanitized citation URLs.

The closure audit fixed two material production gaps. Search-provider API calls
now use the same public-address DNS validation/pinning boundary as fetch and
ignore ambient HTTP proxy configuration by default, so provider credentials are
not sent through a weaker network path. Direct fetch likewise ignores ambient
proxy configuration and now uses a browser-like User-Agent plus
`Accept-Language`. HTML extraction no longer behaves as a raw tag stripper:
it suppresses document head/script/style/template/SVG and common site chrome,
prefers visible `main`/`article` content when present, and falls back to
bounded body text for pages without semantic main content. Invalid or negative
`Content-Length` values fail closed. Search also normalizes nullable provider
fields and disables Brave display-text decorations.

The current Brave Web Search contract still uses
`/res/v1/web/search`, `X-Subscription-Token`, `pd/pw/pm/py` freshness,
and the optional `text_decorations` flag. Tavily still uses
`POST https://api.tavily.com/search`, Bearer authentication,
`max_results`, and `time_range`. Ash retains its own conservative
500-character query limit even though Brave currently accepts a larger web
query; Tavily's current reference does not require Ash to lower that bound.

OpenClaw and Hermes expose many additional search backends, including
unofficial/key-free DuckDuckGo paths and self-hosted SearXNG. That provider
breadth is useful but is not a P3A closure requirement for Ash core: adding an
unofficial scraper would create a brittle production dependency, while
self-hosting/search-plugin breadth can be added later through a deliberate
provider/plugin contract. Ash's core contract is configured live search plus a
keyless guarded fetch path, with the browser available for JS-heavy or
authenticated pages.

P3A closure evidence: 38/38 focused `web_fetch`/`web_search` tests, 6/6
setup/doctor/config/runtime/permission wiring tests, Ruff clean, targeted Mypy
clean, and `git diff --check` clean.

Comparator/provider references rechecked on 2026-10-03:
`https://docs.openclaw.ai/tools/web`,
`https://docs.openclaw.ai/tools/web-fetch`,
`https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search/`,
`https://api-dashboard.search.brave.com/api-reference/web/search/get`, and
`https://docs.tavily.com/documentation/api-reference/endpoint/search`.

### P3 progress — browser interaction audit slice 1

Ash enters P3 with a substantial browser baseline rather than a blank computer
tool: guarded public `web_search`/`web_fetch`; an isolated Playwright Chromium
session; accessibility snapshots with stable per-tab refs; explicit tab
open/focus/close; navigate/click/type/scroll/back; bounded screenshots;
workspace-scoped upload/download; opt-in private persistent profiles; and
loopback-only CDP attachment that creates an Ash-owned policy context instead
of taking ownership of the source browser. Real Chromium E2E already covers
snapshot/fill/click, popup tabs, uploads, network-policy enforcement, CDP
attachment, storage-state reuse, runtime attach/disconnect, and source-browser
survival.

The current comparator pass nevertheless confirms a material action-surface
gap. OpenClaw's current browser control includes keyboard press, hover, drag,
select, richer waits/dialog handling, batching, and optional evaluation; Hermes'
default Browser Use path lets the agent compose browser operations in code;
OpenAI's current computer-use guidance likewise recommends persistent
Playwright/code execution for complex browser interactions while retaining a
structured action alternative. Ash does not need unrestricted page JavaScript
to close the basic workflow gap, but modern forms, drag/drop UIs, keyboard-only
controls, hover menus, select elements, and asynchronous app states require more
than click/type/scroll.

The first P3B slice therefore adds bounded structured operations: keyboard
press, hover, select, drag, and explicit waits. The second slice adds explicit
modal-state handling for action-triggered alert/confirm/prompt dialogs. A
triggering browser action can now return the pending dialog instead of hanging;
other browser actions fail closed while the modal is open; `browser_dialog`
can accept, dismiss, or answer a prompt; chained dialogs remain explicit; and
the blocked Playwright action resumes before Ash returns a fresh snapshot.
`browser_dialog` remains side-effecting and approval-gated, and prompt text is
treated as a sensitive tool argument.

The final bounded frame-actionability slice closes the remaining material
interaction gap without changing Ash's public ref grammar. Snapshots now
traverse up to 32 visible frames, share the existing 150-element interaction
budget across the page and frames, include bounded/redacted frame ARIA context,
redact password values inside frames, and map each opaque snapshot-scoped ref
internally to the exact Playwright page/frame that produced it. Hidden-frame
controls are excluded and stale/detached frame refs fail closed. Nested-frame
ancestry is handled internally instead of exposing brittle frame-index paths in
the model-facing tool contract.

P3B is CLOSED. The final closure gate passes 59/59 browser unit tests and
10/10 complete real-Chromium browser E2E tests, with Ruff, targeted Mypy, and
`git diff --check` clean. The E2E gate covers top-level form actions, bounded
keyboard/hover/select/drag/wait primitives, explicit chained dialog handling,
visible iframe discovery/action with hidden-frame exclusion and password
redaction, popup/tab control, upload/download/network-policy behavior, CDP
attachment/storage-state reuse, runtime attach/disconnect, and source-browser
survival.

Arbitrary page evaluation and batch execution are not P3B closure requirements.
Current OpenClaw and Hermes expose those escape hatches, and OpenAI recommends
code execution for some computer-use integrations, but Ash already has a strong
structured interaction surface. Unrestricted evaluation would widen access to
authenticated DOM/storage/network state, while batching would compress the
existing per-action approval and fresh-ref boundaries. Keep both out of Ash core
unless a concrete coding workflow later justifies a separately designed safety
contract.

Comparator references rechecked on 2026-10-03:
`https://docs.openclaw.ai/tools/browser-control`,
`https://hermes-agent.nousresearch.com/docs/user-guide/features/browser/`, and
`https://developers.openai.com/api/docs/guides/tools-computer-use`.

### P3C closure — explicit signed-in browser consent

P3C is CLOSED. Ash now has two deliberate authenticated-browser paths, both
off by default and both user-owned rather than project-configurable:

- **Ash-owned persistent profile.** `browser_persistent_profile=true` uses a
  private Chromium user-data directory under Ash's local state. The default
  remains ephemeral. Users who need to sign in manually can combine the
  persistent profile with headed mode and complete login themselves in the
  visible browser; model-facing `browser_type` still refuses password fields.
  Real Chromium E2E proves a persistent auth cookie survives an Ash browser
  restart.
- **Existing-browser state import.** Loopback-only CDP attachment still never
  takes direct ownership of the source browser's tabs. Read-only
  `/browser inspect` inventories bounded/redacted tab metadata without reading
  storage. Auth-state reuse requires the explicit `--reuse-storage-state`
  action or equivalent user-owned config **and** a non-empty user-owned
  `allowed_web_domains` allowlist. Ash filters the source state before creating
  its isolated context: only cookies whose cookie domain matches that allowlist
  and localStorage for matching HTTP(S) origins cross the boundary. Unrelated
  cookies/origins are dropped, and extra storage categories such as IndexedDB
  or passkey/private-key credential state are not copied. The same domain
  allowlist also governs Ash browser network access, keeping credential scope
  aligned with navigation scope.

Consent is reversible. `/browser reset-profile` safely closes an active
Ash-owned persistent browser if necessary and deletes only Ash's anchored
`browser-profile` directory; subsequent browser use starts fresh. It does not
modify the source browser used for CDP state import. Real Chromium E2E proves
profile persistence and reset, scoped source-cookie import, source-browser
survival, and runtime attach/disconnect. The CLI reports whether storage-state
reuse is enabled and the exact configured domain scope.

This is intentionally narrower than OpenClaw's direct existing-session/extension
control and Hermes' whole-profile snapshot option. Current OpenClaw guidance
explicitly treats signed-in profiles as sensitive state and prefers a dedicated
agent profile by default; its manual-login guidance recommends that the user,
not the model, perform login in that profile. Hermes likewise labels real-profile
browsing a consent-gated convenience rather than an isolation boundary.
Playwright warns that stored authentication state can contain impersonation-
capable cookies/tokens. Ash therefore keeps direct personal-tab takeover and
whole-profile copying out of the P3C contract: existing login reuse crosses into
an Ash-owned policy context only through an explicit, domain-scoped state-copy
boundary.

P3C closure evidence: 70/70 focused signed-in browser/config/runtime/CLI unit
tests, 11/11 complete real-Chromium browser E2E tests, Ruff clean, targeted
Mypy clean, and `git diff --check` clean.

Comparator references rechecked on 2026-10-03:
`https://docs.openclaw.ai/tools/browser/profiles`,
`https://docs.openclaw.ai/tools/browser-login`,
`https://docs.openclaw.ai/gateway/security/browser-control`,
`https://hermes-agent.nousresearch.com/docs/user-guide/features/browser/`, and
`https://playwright.dev/docs/auth`.

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
  native function calls, and bounded opaque encrypted reasoning replay. At this
  M4 checkpoint the API-key route remained unchanged; P1C slice 10 later moves
  current first-party GPT-5.6/GPT-6 API-key models onto the same public
  Responses semantics. Deterministic local tests cover the
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
| API-key providers | Partial | Every built-in cloud route now assembles from a fresh non-interactive process with its provider credential contract (including Google `GEMINI_API_KEY` fallback), while custom endpoints retain fresh-process coverage; deterministic fresh-process CLI loopback E2E proves real streamed completion across the previously covered built-in cloud catalog: Anthropic through the native Messages/SSE protocol with `x-api-key`, protocol-version, and provider-usage assertions, plus the OpenAI-wire routes for OpenAI, Google, OpenRouter, Vercel AI Gateway, DeepSeek, Groq, Mistral, xAI, Together, Fireworks, Cerebras, and NVIDIA with provider-specific bearer credentials, dynamic model-catalog probes where applicable, Together's list-shaped catalog, and Google's client-identification header. Current first-party OpenAI GPT-5.6/GPT-6 API-key models use the public Responses API so native-tool/reasoning semantics match the model contract; older verified OpenAI models retain Chat Completions, unknown IDs fail conservative, and an operator-overridden OpenAI base URL does not inherit first-party capabilities. Hugging Face Inference Providers is additionally wired as a first-class gateway with `HF_TOKEN`, official `/v1/models` discovery, model/provider routing aliases, and conservative capability-floor parsing across its live upstream providers; Vercel AI Gateway is wired as a first-class `AI_GATEWAY_API_KEY` route using its official OpenAI-compatible `https://ai-gateway.vercel.sh/v1` endpoint and `/v1/models` catalog, giving Ash another high-leverage gateway to hundreds of models without widening the transport surface. Both still need real service conformance before joining the live-verified subset. A bounded real OpenRouter service run additionally proved live catalog discovery, a no-tools completion with provider-reported usage, and a coding journey that edited and externally tested a project using an ephemeral credential that was not persisted by Ash; interactive generic onboarding consumes the same readiness-owned catalog shape used at runtime; custom OpenAI-compatible routes fail closed unless exact per-model metadata is explicitly declared; OpenRouter, Hugging Face, Vercel AI Gateway, Mistral, xAI, Together, Fireworks, and Cerebras start conservatively and recover only capabilities proven by bounded provider-owned metadata (including multi-source xAI metadata that fails closed on direct or follow-up alias disagreement about canonical model identity, and selected-model Fireworks management metadata); runtime provider/model switches own retired-provider closure through loop shutdown, surface cleanup failures, and preserve close-once semantics for successful resources across shutdown retries; real vendor cross-version/service interoperability remains broader than the exercised OpenRouter service plus deterministic protocol coverage |
| OpenAI ChatGPT-plan auth | Verified live | Optional first-party `openai/*` auth mode uses OpenAI's open-source Sign in with ChatGPT flow with exact loopback state/nonce/PKCE and ID-token validation, private profile-scoped multi-account registration storage, stable host identity, rotating refresh serialization, revocation-aware logout, live model discovery, user-owned setup/CLI controls, project-config exclusion, and a dedicated stateless Responses adapter enforcing `store=false`, `stream=true`, full canonical history/tool replay, bounded encrypted reasoning replay, and success only on `response.completed`; deterministic auth/provider/runtime regressions pass. The original 2026-10-01 account journey verified sign-in, catalog discovery, plan-backed completion, native function call, canonical tool-result continuation, and provider-reported usage. A second bounded live journey on 2026-10-03, after the P1 stream/cancellation refactors, reconfirmed a refreshable plan registration, the current account model catalog, `ash providers test openai/gpt-6-astra`, and an isolated native `write_file` continuation with provider-authoritative usage. |
| Provider route verification | Verified locally | `ash providers test` separates catalog/model discovery from a bounded no-tools model completion, requires a safe terminal response before declaring the route ready, redacts completion failures, and closes the probe provider; `ash doctor --connect` remains a non-billable catalog/model check |
| Local models | Partial | Ollama URL validation, discovery, health detail, safe bounded pulls, and dynamic tool/context probing are wired; LM Studio and vLLM no longer inherit OpenAI capabilities from wire compatibility, LM Studio consumes its native per-model tool/vision/reasoning/loaded-context metadata, and vLLM preserves served context while generic `supported_parameters=tools` does not enable native auto-tool calling without explicit server capability evidence. Deterministic fresh-process E2E now proves an actual workspace-writing coding turn for every supported local route: Ollama and LM Studio negotiate native tools, while vLLM deliberately uses Ash's text/XML fallback when the server does not prove auto-tool support; all three perform a real `write_file`, replay the tool result, and finish the turn without bearer auth. `/capabilities --refresh` re-probes dynamic manifests, provider streams are explicitly closed on success/retry/cancellation, and runtime lifecycle remains owned by `ollama`, `lms`/llmster, or `vllm serve`. Real cross-version runtime-binary conformance remains unclaimed because those binaries were not installed on the P1 closure host. |
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
| Browser automation | Strong partial | Optional Playwright pack owns an isolated Chromium context with public-host/domain routing for requests and WebSockets, blocked service workers/password fills, bounded ARIA snapshots and visible-frame refs, structured modern interactions, explicit dialog state, bounded vision screenshots, safe workspace uploads and atomic bounded workspace downloads, setup/doctor support, deterministic cleanup, opt-in private Ash-owned persistent profiles, and loopback-only CDP attachment through a separate Ash-owned policy context. Existing-browser auth reuse is explicit and requires user-owned `allowed_web_domains`; only matching cookies/localStorage are copied, unrelated/future credential categories are dropped, and the source browser remains untouched. `/browser inspect` reads only bounded/redacted tab inventory; `/browser status`, `/browser connect`, `/browser disconnect`, and `/browser reset-profile` provide inspectable attach, detach, scope, and revocation controls. Real Chromium E2E proves persistent auth/reset, scoped state reuse, hot-swap, interaction breadth, and source-browser survival. Remote browser operation and general desktop computer use remain P3 follow-up decisions |
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
| Spawn subagent tool | Verified locally | Real bounded provider-backed Ash loop, persisted reports, background execution, cancellation, and start-time inheritance of the parent permission mode plus managed/session/persistent rules. Direct children remain leaves by default; an explicit user-owned depth increase enables the dedicated orchestrator role, which receives bounded `delegate_agents` but never unrestricted recursive `spawn_agent`. The default execution boundary remains in-process, while optional config-backed subprocess execution serializes durable task/config/policy plus only selected provider environment into a scrubbed child that rebuilds the provider there; both ordinary and nested subprocess regressions prove the parent provider factory is not reused. Direct foreground child `ASK` decisions broker through the active TUI or explicit runtime approval callback, including the subprocess approval channel, while autonomous workers persist attempt-scoped approval requests for later operator resolution and direct foreground headless workers still fail closed when no broker exists. |
| Parallel agents | Verified locally | Atomic DAG submission, dependency-ready dispatch, bounded retries, foreground/background operation, restart recovery, independent child sessions/workspaces, durable graph-wide token and USD ceilings, safe foreground approval brokerage, and a durable asynchronous approval inbox for background/queued DAG workers. Opt-in orchestrators can synchronously expand one bounded child DAG with durable parent lineage; depth, direct-child count, global concurrency, budgets, and cascade cancellation remain enforced. Approval responses are single-resolution, task/attempt/owner/digest correlated, cancellation-aware, and stale requests are retired before retried attempts can run. |
| Agent status/output | Verified locally | Live basic and full slash status with durable task identity, token budgets, and USD cost usage; top-level persisted status/report/message inspection; completed background reports are also persisted into the owning parent session at the next safe model boundary without starting a surprise model turn. |
| Agent messaging | Verified locally | Typed SQLite IPC is persisted and inspectable; running workers consume steer/stop messages, acknowledge delivery, enforce pending-message backpressure, and use correlated `approval_request`/`approval_response` messages for asynchronous child permissions. Background completion handoff persists bounded, JSON-quoted, explicitly untrusted worker evidence into parent history before acknowledging IPC, preserving at-least-once delivery across coordination/history-store failures. |
| Role tool policies | Verified locally | Read/search baseline, coder-only scoped edits, sandbox-required tester commands, leaf workers without delegation, and a dedicated orchestrator role whose only agent-creation surface is bounded synchronous `delegate_agents`; nesting remains flat by default and repository config cannot raise the user-owned depth/child limits. |
| Worktree isolation | Verified locally | Clean-lead precondition, locked `ash-agent/*` branches, exact branch/commit verification, conflict-safe dependent merges, bounded commits, deterministic cleanup, explicit full-branch squash/discard, and Ash-owned storage-root/worktree inode pinning carried into the worker `SafetyGuard` and sandbox. If a concurrently hostile same-account host process redirects the destination pathname during external `git worktree add`, Ash revalidates after Git returns and refuses the lease before any worker execution; Git may already have written to the redirected host path because the Git CLI exposes only a pathname destination, so that host-side race is not claimed as a strict same-principal namespace guarantee. |
| Result consolidation | Verified locally | Foreground DAG calls return typed terminal results and errors plus evidence-linked synthesis, bounded summaries, workspace-relative path evidence, cross-agent claim-conflict detection, and durable graph-consolidation artifacts |
| Agent steering/stop | Verified locally | In-process stop, persisted stop, atomic graph cancellation with active-turn revocation, parent-lineage cascade cancellation for nested work, live steering at safe iteration boundaries, delivery state, and report-based resume; isolated changes must be applied before continuation. |

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
