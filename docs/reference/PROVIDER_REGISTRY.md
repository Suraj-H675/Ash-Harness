# Provider And Capability Registry

Normal CLI users configure built-in or OpenAI-compatible providers with
`ash setup`. Embedders can add a provider implementation without modifying
Ash's CLI or runtime branches.

The first-party `openai/*` route has two **user-owned** authentication modes:
`api_key` and `chatgpt`. API-key auth uses the public Responses API for the
current GPT-5.6/GPT-6 model families where reasoning/tool semantics require or
prefer it, while older supported OpenAI models retain the Chat Completions
adapter. Operator-overridden `OPENAI_API_BASE` endpoints stay on the generic
Chat Completions path and do not inherit first-party model capabilities from an
OpenAI model name. ChatGPT auth uses OpenAI Sign in with ChatGPT plus the public
Responses API. `openai_auth_mode` is excluded from project configuration so
repository-controlled input cannot change the user's selected OpenAI
authentication mode. The ChatGPT path keeps registrations profile-scoped, uses
descriptor-anchored private storage, serializes rotating refresh-token updates
across processes, and replays bounded opaque reasoning items required by
`store=false` Responses conversations.

The first-party `anthropic/*` route starts from exact, verified model
declarations rather than family-name heuristics. Current offline defaults are
Claude Fable 5.1, Opus 5.5, Sonnet 5.5, and Haiku 4.5; unknown model IDs fail
conservative. At runtime Ash asks Anthropic's Models API for the selected
model's provider-owned image/thinking and token-limit metadata when the SDK
supports that endpoint, while retaining exact static tool-use evidence for
verified model IDs because the Models API does not expose generic client-tool
support. An operator-overridden Anthropic base URL does not inherit
first-party capabilities before its own endpoint proves metadata.

The first-party `google/*` route keeps Google's OpenAI-compatible inference
surface, while capability negotiation probes the selected model through
Google's native Models API using `x-goog-api-key`. Provider-owned model
metadata supplies input/output token limits and thinking support; exact
first-party declarations supply function calling and image-input support only
for models Google documents for those capabilities. Unknown IDs remain
conservative, and an operator-overridden Google base URL does not inherit
first-party assumptions.

Gemini 3 OpenAI-compatible function calls carry a Google thought signature
that must be replayed during multi-step tool use. Ash captures
`tool_calls[].extra_content.google.thought_signature`, seals the opaque value in
the same durable provider replay store used for other provider-owned reasoning
state, and restores it on the matching assistant tool call before the next
request. Required Gemini 3 signature history that cannot be recovered fails
locally instead of being sent as a known-invalid provider request.

```python
from ash.providers import (
    ProviderABC,
    ProviderCapabilities,
    get_provider_registry,
)


class ExampleProvider(ProviderABC):
    # Implement model_name, count_tokens, and stream_chat.
    ...


registry = get_provider_registry()
registry.register(
    "example",
    lambda config, model: ExampleProvider(model=model),
    capabilities=lambda model: ProviderCapabilities(
        native_tools=True,
        vision=True,
        context_window=128_000,
        max_output_tokens=16_000,
    ),
)
```

`AshClient.create(config=AshConfig(model="example/model"))` and all CLI/SDK
subagent factories then resolve the same registration. If the returned provider
keeps the default `provider_family="custom"`, Ash binds it to the registered
family. Explicit provider-owned families are preserved.

Registrations are process-local and thread-safe. Duplicate names fail unless
`replace=True` is explicit. `unregister()` removes capability declarations
owned by that provider registration. A resolver must return an immutable
`ProviderCapabilities`; undeclared families receive stable conservative
defaults.

## Readiness Boundary

For built-in and configured custom providers, Ash resolves a single connection
description before runtime construction or ash doctor --connect. It includes
the canonical provider/model identifier, exact base URL, authentication mode,
and provider-specific model-catalog endpoint when that route has one. This prevents diagnostics from
probing a vendor default while a turn sends credentials to an operator-selected
gateway.

First-class enterprise routes deliberately do not reuse generic bearer custom
providers. The vertex route requires an explicit user-owned Google Cloud
project and location, uses the optional gcp extra, and obtains short-lived
bearer credentials from Google Application Default Credentials. The token
callable refreshes ADC without persisting tokens in Ash config and retains only
a bounded in-memory history for provider-error redaction. Vertex does not expose
a trustworthy live OpenAI model catalog for Ash, so the model ID is explicit
and ash providers test verifies it with a bounded completion. For exact Google
Gemini IDs whose Vertex Chat Completions contract is documented, Ash can still
declare the verified tools, vision, reasoning, and model-limit floor without
guessing from arbitrary publisher/model names. Current Gemini 3 tool calls use
the same opaque Google thought-signature protocol as the direct Gemini
OpenAI-compatible route, but Vertex replay state is sealed under a distinct
`vertex` route identity so state cannot cross those provider boundaries.

The bedrock route requires an explicit AWS Region and optionally a profile,
uses the optional aws extra, and delegates AWS credential-chain refresh and
SigV4 request signing to the official OpenAI Bedrock provider. Ash targets the
AWS-recommended bedrock-runtime endpoint, not Mantle. Native
ListFoundationModels and ListInferenceProfiles results are candidate IDs because
AWS API compatibility is model-specific; only the completion probe establishes
that the selected ID supports Runtime Chat Completions.

The azure route uses Microsoft's GA OpenAI-compatible v1 surface. The user owns
the exact public Azure resource/project endpoint and chooses either api_key or
entra; project configuration cannot change either setting. API-key mode uses
the ordinary OpenAI SDK path and needs no Azure SDK dependency. Entra mode uses
the optional azure capability pack, constructs async DefaultAzureCredential,
and passes Microsoft's async bearer-token provider into the same OpenAI adapter.
Tokens are retained only in a bounded in-memory redaction window and are never
written to Ash config or session storage. Ash owns and closes credentials it
creates. Azure deployment/model IDs are explicit because Ash does not treat
Azure's management-plane inventory as an authoritative OpenAI model catalog;
ash providers test verifies the selected deployment with a bounded completion.
Azure v1 explicitly supports streamed usage chunks, so Ash requests
`stream_options.include_usage=true` and normalizes provider-reported prompt,
completion, and cached-input counts when the stream completes. Deployment
capabilities remain conservative because an arbitrary Azure deployment name
does not prove which underlying model/version is serving it.

Project configuration cannot set Vertex project/location, Bedrock
region/profile, or Azure endpoint/auth mode. These are user-owned controls, and
isolated provider workers receive only the cloud credential-chain environment
required by an active or fallback enterprise route.

Custom OpenAI-compatible provider records use `auth_mode = "bearer"` or
`auth_mode = "none"`. Bearer mode requires its declared key source to be
present before a REPL can start. Anonymous mode intentionally sends no bearer
header and does not inherit `OPENAI_API_KEY`. Older records without an
`auth_mode` preserve bearer behavior when they declare a key and are otherwise
treated as anonymous.

These custom-provider auth modes are separate from the first-party OpenAI
`openai_auth_mode`; ChatGPT-plan tokens are never reused for custom endpoints.

## Same-provider API-key credential pools

Ash supports ordered API-key credential pools for Ash-owned built-in API-key
routes and configured custom bearer routes. The user config stores only
environment-variable references under provider_api_key_envs; raw keys remain
in the process environment or the active Ash profile's private dotenv file.
Project config cannot define or reorder these references.

    [provider_api_key_envs]
    openai = ["OPENAI_PRIMARY", "OPENAI_BACKUP"]

All referenced variables are validated before any child provider is built.
When a pool is active, isolated workers receive the referenced credential
variables instead of the provider's default/stale API-key variable. Azure
API-key pools likewise do not inherit Entra identity material; cloud-identity
routes such as Vertex ADC, Bedrock AWS credentials, Azure Entra, Ollama, and
OpenAI ChatGPT-plan auth are intentionally outside this API-key pool.

Within one provider/model route, Ash prefers the most recently successful
credential. Before retained model output/state, HTTP 401/403 failures, explicit
billing/quota exhaustion, and rate limits can place that credential on a
bounded session-local cooldown and advance to the next ordered credential.
Rate-limit cooldowns honor bounded Retry-After values. Bad requests, model/
context/format errors, connection failures, and ordinary 5xx responses do not
rotate credentials; those remain subject to the normal request retry and
cross-model failover layers. Once text, tool output, reasoning, reasoning
blocks, or provider replay state has been exposed, neither credential rotation
nor outer provider retry/failover may replay the request.

The same rule applies during dynamic capability detection so an unusable
primary credential does not prevent session startup when another configured
credential can verify the same model. If every credential is cooling down,
the pool exposes the earliest bounded retry interval to the existing retry/
failover layer instead of busy-looping. Pool snapshots and OpenTelemetry use
only the validated environment-reference name, pool size, cooldown reason, and
failure category; key values never cross those diagnostic boundaries.

Credential changes can invalidate provider-side cache/account/routing affinity.
Ash therefore does not promise identical prompt-cache hit rates, latency, or
billing behavior after rotation even though the selected provider/model route
is unchanged.

### Dynamic credential helpers

One user-owned helper can precede the static env profiles for an API-key route:

    [provider_api_key_helpers.openai]
    command = ["op", "read", "op://Engineering/OpenAI/api-key"]
    env = ["OP_SERVICE_ACCOUNT_TOKEN"]
    timeout_seconds = 10
    ttl_seconds = 300

The helper table is excluded from project-controlled configuration. `command`
is a bounded argv list, not a shell string. Ash accepts only a bare executable
resolved outside the active workspace or an explicit absolute executable path
outside the workspace. The process runs from user-owned Ash state rather than
the repository, under Ash's managed process-tree cleanup, with a scrubbed
environment plus only the explicitly declared helper env names. Helper argv
must not contain credentials; operating systems may expose process arguments,
so vault/SSO authentication belongs in the env allowlist instead.

Execution defaults to a 10-second timeout and a 300-second in-memory credential
TTL; both are bounded by config validation. Combined helper output is bounded,
stderr is never copied into errors, and stdout must contain exactly one
non-whitespace UTF-8 credential line. The credential and its cache are never
persisted. Closing the helper-backed provider clears the cached credential and
successfully retired provider objects are released so old key strings are not
retained for the wrapper lifetime.

The helper is represented as the first secret-free profile
`helper:PROVIDER`. Its credential is resolved lazily, so registry construction
never executes external helper code. Expired TTL causes a normal refresh; an
explicit model-capability refresh does not rotate a healthy credential. A
pre-output 401/403 forces exactly one helper refresh, after which the normal
credential pool can advance to ordered static env backups. Helper execution
failures use the provider-neutral `credential_source` failure category and can
also fall through to static backups without pretending the error was an HTTP
authentication failure.

Helper-backed children use the same provider factory and capability resolver as
static pool entries. Replacing or unregistering an Ash built-in provider factory
revokes Ash's private credential-pool constructor, so extensions are never
silently bypassed. Isolated workers forward only helper allowlisted env names
and explicit static pool variables; ambient default API keys and unrelated
cloud identity material remain excluded.

Wire compatibility does not imply model capability. Custom routes therefore
fail closed for native tools, vision, and reasoning unless the user declares
capabilities for the exact model. Optional limits are positive integers:

```toml
[custom_providers.example.model_capabilities."agent-model"]
native_tools = true
vision = true
reasoning = false
context_window = 131072
max_output_tokens = 8192
```

An undeclared model still works through the conservative text/XML-tool path;
Ash does not send native tool schemas or image inputs merely because the server
implements an OpenAI-compatible HTTP API.

## Runtime Request Boundary

Every provider request attempt is finite even for custom provider
implementations. `provider_request_timeout_seconds` defaults to 1800 seconds
and accepts values from 1 second through 24 hours. A timeout is eligible for
Ash's normal provider retry path only when the attempt produced no retained
model output/state; once text, tool output, reasoning, reasoning blocks, or
provider replay state has started, the request is never replayed automatically.

The core loop also bounds retained provider output independently of adapter
behavior: one completion may retain at most 16 MiB across text, XML tool-call
deltas, native tool calls, and reasoning payloads, at most 4096 reasoning
blocks, and at most 100000 stream chunks. Built-in adapters may enforce tighter
transport-specific limits before data reaches this boundary. These ceilings are
resource/liveness guards, not substitutes for `max_completion_tokens` or model
capability limits.

The same wire/capability separation applies to built-in routes where Ash can
inspect provider-owned metadata. Mistral starts conservative and reads the
selected `/v1/models` entry for explicit function-calling, vision, and context
support. xAI merges only safely matched evidence from `/v1/models` and
`/v1/language-models`, so context and image-input support can be recovered even
when an alias is present in only one source; conflicting evidence fails closed.
Together uses its native `/v1/models` catalog shape for verified context limits
without assuming tools or vision. Cerebras uses its public model catalog for
explicit tools, vision, reasoning, context, and output limits. Fireworks uses
selected-model management metadata only for a canonical
`accounts/ACCOUNT/models/MODEL` resource and otherwise remains conservative.
LM Studio uses its native `/api/v1/models` metadata for tool training, vision,
reasoning, and the conservative loaded-instance context. vLLM reads served
model limits from `/v1/models`, but its catalog does not prove that
`--enable-auto-tool-choice` plus a compatible tool parser were enabled, so Ash
does not activate native auto-tool calling from wire compatibility or model
name alone. Generic `openai-compatible` routes likewise require catalog
evidence. Exact model IDs take precedence; provider aliases are accepted only
when they map to exactly one catalog entry. Missing, malformed, conflicting,
ambiguous-alias, or different-model metadata keeps the conservative path.

Local runtime lifecycle remains runtime-owned. Ash connects to and verifies
Ollama, LM Studio/llmster, and vLLM rather than installing or supervising a
second copy of their daemon/model managers. Setup therefore points users to
the runtime's own lifecycle commands (`ollama serve` / `ollama pull`, `lms
server start` / `lms load`, and `vllm serve`) while Ash owns endpoint safety,
catalog/capability negotiation, streamed inference, cancellation, provider
cleanup, and agent-loop semantics.

For routes with an authoritative model catalog, connectivity diagnostics must
receive a successful catalog containing the selected model. A reachable catalog
endpoint with an empty catalog or a different model is reported as not ready.
Enterprise routes may have no authoritative OpenAI catalog: Vertex and Azure
require an explicit model/deployment ID, while Bedrock native discovery is
candidate-only. For those routes, ash providers test treats a successful
bounded completion as the authoritative readiness signal. ash setup remains the
remediation path.

Provider registration executes trusted Python code in the Ash host. It is an
embedding API, not the future untrusted plugin ABI. Out-of-process plugins must
cross a policy-enforced protocol boundary before they can contribute providers.
