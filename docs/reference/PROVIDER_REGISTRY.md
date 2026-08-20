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

Connectivity diagnostics must receive a successful model catalog containing
the selected model. A reachable endpoint with an empty catalog or a different
model is reported as not ready; `ash setup` remains the remediation path.

Provider registration executes trusted Python code in the Ash host. It is an
embedding API, not the future untrusted plugin ABI. Out-of-process plugins must
cross a policy-enforced protocol boundary before they can contribute providers.
