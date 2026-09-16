# Provider And Capability Registry

Normal CLI users configure built-in or OpenAI-compatible providers with
`ash setup`. Embedders can add a provider implementation without modifying
Ash's CLI or runtime branches.

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
description before runtime construction or `ash doctor --connect`. It includes
the canonical provider/model identifier, exact base URL, authentication mode,
and provider-specific model-catalog endpoint. This prevents diagnostics from
probing a vendor default while a turn sends credentials to an operator-selected
gateway.

Custom OpenAI-compatible provider records use `auth_mode = "bearer"` or
`auth_mode = "none"`. Bearer mode requires its declared key source to be
present before a REPL can start. Anonymous mode intentionally sends no bearer
header and does not inherit `OPENAI_API_KEY`. Older records without an
`auth_mode` preserve bearer behavior when they declare a key and are otherwise
treated as anonymous.

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

Connectivity diagnostics must receive a successful model catalog containing
the selected model. A reachable endpoint with an empty catalog or a different
model is reported as not ready; `ash setup` remains the remediation path.

Provider registration executes trusted Python code in the Ash host. It is an
embedding API, not the future untrusted plugin ABI. Out-of-process plugins must
cross a policy-enforced protocol boundary before they can contribute providers.
