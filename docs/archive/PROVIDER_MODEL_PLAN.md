# Provider/Model String Migration Plan

## Goal

Migrate Ash from separate `provider` + `model` fields to a single `provider/model` string format, matching the approach used by OpenClaw, Aider, and Hermès.

---

## Decision Log

- **Config format**: Single `model = "anthropic/claude-3-7-sonnet"` field. Remove `provider` field entirely.
- **REPL `/model` command**: Accepts `"provider/model"` string (e.g., `/model anthropic/claude-3-7-sonnet`)
- **AshLoop API**: Keep both `switch_provider(provider, model)` AND `switch_model(model)`. The former takes two args, the latter takes one. Both are useful.
- **Interactive picker**: Simplify to just model names grouped by provider headers (not numbered `1-1` format)
- **Ollama models**: Always prefixed with `ollama/` (e.g., `"ollama/qwen2.5-coder:7b"`) so provider is always derivable from the string

---

## Changes by File

### 1. `config.py`

**Before:**
```python
provider: str = Field("anthropic", description="Primary model provider...")
model: str = Field("claude-3-7-sonnet-20250219", description="Model name within the selected provider.")
```

**After:**
```python
model: str = Field(
    "anthropic/claude-3-7-sonnet-20250219",
    description="Model in provider/model string format (e.g. anthropic/claude-3-7-sonnet-20250219, ollama/qwen2.5-coder:7b)",
)
```

**Remove:**
- The `provider` field entirely
- `model_post_init` backward compat for `ASH_MODEL_NAME` (no longer needed)

**Add:**
- A validator or helper property that parses `model` into `provider` and `model_name`:
```python
@property
def provider(self) -> str:
    """Parse provider from the model string."""
    return self.model.split("/", 1)[0]

@property
def model_name(self) -> str:
    """Parse model name from the model string."""
    return self.model.split("/", 1)[1]
```

**Backward compat:**
- On startup, if `ASH_PROVIDER` is set and `ASH_MODEL` is NOT a `provider/model` string, prepend the provider: `ASH_MODEL = f"{ASH_PROVIDER}/{ASH_MODEL}"`
- If `ASH_MODEL` is already a `provider/model` string, use it as-is

**TOML example:**
```toml
model = "anthropic/claude-3-7-sonnet-20250219"
```

---

### 2. `__main__.py`

#### `AVAILABLE_MODELS` constant

**Before:**
```python
AVAILABLE_MODELS: dict[str, list[str]] = {
    "anthropic": ["claude-3-7-sonnet-20250219", ...],
    "openai": ["gpt-4o", ...],
    ...
}
```

**After:**
```python
AVAILABLE_MODELS: list[str] = [
    "anthropic/claude-3-7-sonnet-20250219",
    "anthropic/claude-3-5-sonnet-20241022",
    "anthropic/claude-3-5-haiku-20241022",
    "anthropic/claude-opus-4-20250514",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "openai/o3",
    "openai/o4-mini",
    "ollama/llama3",
    "ollama/qwen2.5-coder:7b",
    "deepseek/deepseek-chat",
    "deepseek/deepseek-reasoner",
    "groq/llama-3.3-70b-versatile",
    "groq/llama-3.1-8b-instant",
    "groq/qwen3.3-32b",
    "groq/compound-mini",
    "openai-compatible/<your-model>",
]
```

#### `_build_provider(config)` changes

**Before:**
```python
def _build_provider(config: AshConfig) -> ProviderABC:
    if config.provider == "anthropic":
        ...
    elif config.provider == "openai":
        ...
```

**After:**
```python
def _parse_model_string(model: str) -> tuple[str, str]:
    """Parse 'provider/model' string into (provider, model)."""
    if "/" not in model:
        raise ValueError(f"Model string must be in 'provider/model' format, got: {model!r}")
    provider, model_name = model.split("/", 1)
    return provider, model_name

def _build_provider(config: AshConfig) -> ProviderABC:
    provider, model_name = _parse_model_string(config.model)
    if provider == "anthropic":
        from providers.anthropic import AnthropicProvider
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        base_url = os.environ.get("ANTHROPIC_API_BASE") or None
        prov = AnthropicProvider(model_name=model_name, api_key=api_key, base_url=base_url)
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov
    elif provider == "openai":
        from providers.openai import OpenAIProvider
        api_key = os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("OPENAI_API_BASE") or None
        prov = OpenAIProvider(model_name=model_name, api_key=api_key, base_url=base_url)
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov
    elif provider == "ollama":
        from providers.ollama import OllamaProvider
        base_url = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
        prov = OllamaProvider(model_name=model_name, base_url=base_url)
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov
    elif provider == "deepseek":
        from providers.deepseek import DeepSeekProvider
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        base_url = os.environ.get("DEEPSEEK_API_BASE") or None
        prov = DeepSeekProvider(model_name=model_name, api_key=api_key, base_url=base_url)
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov
    elif provider == "groq":
        from providers.groq import GroqProvider
        api_key = os.environ.get("GROQ_API_KEY", "")
        base_url = os.environ.get("GROQ_API_BASE") or None
        prov = GroqProvider(model_name=model_name, api_key=api_key, base_url=base_url)
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov
    elif provider == "openai-compatible":
        from providers.openai import OpenAIProvider
        api_key = os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("OPENAI_API_BASE", "")
        prov = OpenAIProvider(model_name=model_name, api_key=api_key, base_url=base_url if base_url else None)
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov
    raise ValueError(f"Unknown provider in model string: {provider!r}")
```

#### REPL `/model` command changes

**Before:**
```python
# /model <modelname> → switch model within current provider
if user_input.startswith("/model "):
    arg = user_input[7:].strip()
    if "/" in arg:
        prov, model = arg.split("/", 1)
    else:
        prov = config.provider
        model = arg
```

**After:**
```python
# /model anthropic/claude-3-7-sonnet → switch to provider/model string
if user_input.startswith("/model "):
    model_str = user_input[7:].strip()
    if "/" not in model_str:
        print("Error: model must be in provider/model format (e.g. anthropic/claude-3-7-sonnet)", file=sys.stderr)
        continue
    try:
        loop.switch_model(model_str)
        config.model = model_str
        # Do NOT set config.provider = ... — provider is a read-only property
        # derived from config.model. Setting it would raise AttributeError.
        print(f"Switched to {model_str}", flush=True)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr, flush=True)
    continue
```

**IMPORTANT:** `config.provider` is a `@property` after migration (derived from `config.model`). Any `config.provider = X` assignments anywhere in `__main__._repl` must be **removed**. The same applies to `switch_provider` calls — only `config.model = model_str` should be set.

#### `_interactive_model_picker(config, loop)` changes

**Before:**
```
[1-1] anthropic/claude-3-7-sonnet-20250219
[1-2] anthropic/claude-3-5-sonnet-20241022
[2-1] openai/gpt-4o
...
```

**After — group by provider, list just model names:**
```
Anthropic:
  claude-3-7-sonnet-20250219 ← current
  claude-3-5-sonnet-20241022
  claude-3-5-haiku-20241022
  claude-opus-4-20250514
OpenAI:
  gpt-4o
  gpt-4o-mini
  ...
Ollama:
  llama3
  qwen2.5-coder:7b
```

When user picks a model (e.g. `claude-3-7-sonnet-20250219`), the provider is inferred by finding which provider owns that model. The current provider is shown with a marker.

Implementation:
```python
def _interactive_model_picker(config: AshConfig, loop: AshLoop) -> None:
    """Show models grouped by provider, let user pick one."""
    # Determine current provider
    try:
        current_provider, current_model = _parse_model_string(config.model)
    except ValueError:
        current_provider, current_model = "anthropic", config.model

    print("\nAvailable models:")
    # Group by provider
    grouped: dict[str, list[str]] = {}
    for m in AVAILABLE_MODELS:
        prov, mod = _parse_model_string(m)
        grouped.setdefault(prov, []).append(mod)

    # Build lookup: model_name -> full provider/model string
    model_to_full: dict[str, str] = {}
    for m in AVAILABLE_MODELS:
        prov, mod = _parse_model_string(m)
        model_to_full[mod] = m

    # Display grouped
    for prov, models in grouped.items():
        print(f"\n{prov.capitalize()}:")
        for model in models:
            marker = " ← current" if prov == current_provider and model == current_model else ""
            print(f"  {model}{marker}")

    # User picks just model name (provider determined by context or menu position)
    # If user picks from a non-current provider, we need to know which provider.
    # Strategy: ask user to pick with provider prefix, or show numbered per-provider.
```

**Simplest UX approach — numbered per provider:**
```
Anthropic:
  [1] claude-3-7-sonnet-20250219
  [2] claude-3-5-sonnet-20241022
OpenAI:
  [3] gpt-4o
  [4] gpt-4o-mini
...
Pick a number (or 'c' to cancel): 1
Switched to anthropic/claude-3-7-sonnet-20250219
```

User picks a number, the number maps back to the full `AVAILABLE_MODELS[i]` entry.

#### `_print_providers()` changes

**Remove entirely from `__main__.py`** — the `/providers` REPL command is also removed. Delete the entire function.

#### `_print_model_list()` changes

Show models grouped by provider (same format as interactive picker but without numbers). Replace the old `_print_model_list()` entirely.

---

### 3. `core/loop.py`

#### `AshLoop.__init__` — remove `config` parameter's relevance

No changes needed — `switch_provider` and `switch_model` still work, but `config.model` is now the full string.

#### `AshLoop.switch_provider(provider, model)` changes

**Before:**
```python
def switch_provider(self, provider: str, model: str) -> None:
    new_config = self._config.model_copy(update={"provider": provider, "model": model})
```

**After:**
```python
def switch_provider(self, provider: str, model: str) -> None:
    """Switch provider and model. provider is kept for ergonomics but
    the model string is the authoritative value."""
    model_str = f"{provider}/{model}"
    new_config = self._config.model_copy(update={"model": model_str})
    self.provider = _build_provider(new_config)
    self._config = new_config
    ...
```

The two-arg form is still useful (sometimes you know the provider and model separately).

#### `AshLoop.switch_model(model)` changes

**Before:**
```python
def switch_model(self, model: str) -> None:
    if self._config is None:
        raise RuntimeError(...)
    self.switch_provider(self._config.provider, model)
```

**After:**
```python
def switch_model(self, model: str) -> None:
    """Switch to a model string. If model contains '/', treat as provider/model.
    Otherwise, prepend the current provider."""
    from __main__ import _build_provider  # lazy import to avoid circular
    if "/" in model:
        # Full provider/model string
        new_config = self._config.model_copy(update={"model": model})
    else:
        # Model-only — prepend current provider
        current_provider = self._config.model.split("/", 1)[0]
        new_config = self._config.model_copy(update={"model": f"{current_provider}/{model}"})
    self.provider = _build_provider(new_config)
    self._config = new_config
    ...
```

**Note:** `self._config.provider` is set by `__main__._repl` after calling `switch_provider()`/`switch_model()` but is not otherwise used internally. It can be left for debugging purposes but is effectively dead code — `_config.model` is the authoritative source of truth.

---

### 5. `providers/__init__.py`

No changes — exports stay the same.

---

### 6. `providers/anthropic.py`, `providers/openai.py`, etc.

No changes — these receive `model_name` as before, nothing changes here.

---

### 7. `.env.example`

**Before:**
```bash
ASH_PROVIDER=anthropic
ASH_MODEL=claude-3-7-sonnet-20250219
```

**After:**
```bash
# Single model string — provider is embedded
ASH_MODEL=anthropic/claude-3-7-sonnet-20250219
# Or for Ollama (no key needed):
# ASH_MODEL=ollama/qwen2.5-coder:7b
```

**Remove** `ASH_PROVIDER` — no longer a separate env var.

**Backward compat**: If `ASH_PROVIDER` is set but `ASH_MODEL` doesn't contain `/`, prepend: `ASH_MODEL=f"{ASH_PROVIDER}/{ASH_MODEL}"`.

---

### 8. `docs/PROVIDER_CONFIG_PLAN.md`

**Remove** — superseded by this plan.

---

### 9. `docs/TEST_REPORT.md`

No changes needed — test it normally after migration.

---

## Implementation Order

1. Update `config.py` — change `model` field format, remove `provider` field, add property accessors, update backward compat
2. Update `__main__.py` — `AVAILABLE_MODELS` to flat list, `_parse_model_string()`, update `_build_provider()`, update REPL commands, update picker
3. Update `core/loop.py` — update `switch_provider()` and `switch_model()` signatures and implementations
4. Update `.env.example`
5. Update `tests/unit/test_config.py` — update tests to use `"provider/model"` string format
6. Run unit tests — fix any failures
7. Live test with Groq
8. Update `docs/TEST_REPORT.md`
9. Commit

---

## Breaking Changes (User-Facing)

| Old | New |
|-----|-----|
| `ASH_PROVIDER=anthropic` env var | Removed — provider is in `ASH_MODEL` string |
| `ASH_MODEL_NAME=...` env var | Renamed to `ASH_MODEL`, now uses `provider/model` format |
| `provider = "anthropic"` in ash.toml | Removed — merged into `model = "anthropic/..."` |
| `model = "claude-3-5-sonnet"` in ash.toml | `model = "anthropic/claude-3-5-sonnet"` |
| `/model claude-3-5-haiku` (model-only) | `/model anthropic/claude-3-5-haiku` (full string) |
| `loop.switch_provider("anthropic", "claude-3-5-haiku")` | Still works (two args) |
| `loop.switch_model("claude-3-5-haiku")` | Still works (prepends current provider) |

---

## New REPL Commands

| Command | Behavior |
|---------|----------|
| `/model` | Opens interactive picker — numbered per provider |
| `/model anthropic/claude-3-7-sonnet` | Switches directly to that model string |
| `/models` | Lists all models grouped by provider |
| `/providers` | **Removed** — no longer relevant |

---

## Error Handling

- `ASH_MODEL` value without `/` → error with helpful message listing available formats
- `loop.switch_model("unknown-model")` (no `/`) → prepends current provider, may fail at API call time (deferred to streaming)
- Unknown provider in model string → `ValueError` at `_build_provider()` time
- Model not found in `AVAILABLE_MODELS` → allowed (user might have custom model)

---

## Backward Compat Strategy

```python
# In config.py model_post_init or a model_validator
import os

model_val = os.environ.get("ASH_MODEL", "")
provider_val = os.environ.get("ASH_PROVIDER", "")
model_name_val = os.environ.get("ASH_MODEL_NAME", "")  # old var

if model_name_val and not model_val:
    # Old ASH_MODEL_NAME set but no ASH_MODEL — promote
    if provider_val:
        model_val = f"{provider_val}/{model_name_val}"
    else:
        model_val = f"anthropic/{model_name_val}"  # default to anthropic

if model_val and "/" not in model_val:
    # ASH_MODEL set but no provider — prepend provider or default
    if provider_val:
        model_val = f"{provider_val}/{model_val}"
    else:
        raise ValueError(
            f"ASH_MODEL={model_val!r} does not have a provider prefix. "
            f"Use format: provider/model (e.g. anthropic/claude-3-7-sonnet)"
        )
```

---

## Files That Change

| File | Change |
|------|--------|
| `config.py` | Remove `provider` field; change `model` to `provider/model` string; add property accessors |
| `__main__.py` | Flat `AVAILABLE_MODELS`; `_parse_model_string()`; updated `_build_provider()`; REPL command; picker redesign |
| `core/loop.py` | Update `switch_provider()` and `switch_model()` implementations |
| `.env.example` | `ASH_MODEL=provider/model`; remove `ASH_PROVIDER` |
| `tests/unit/test_config.py` | Update all test cases to use `provider/model` string format |
| `docs/PROVIDER_CONFIG_PLAN.md` | Delete (superseded) |

---

## Dead Code Removed

The following is explicitly deleted, not just unused:

| File | What is deleted |
|------|----------------|
| `config.py` | The `provider: str = Field(...)` class attribute entirely. The `model_post_init` backward compat for `ASH_MODEL_NAME` is removed (replaced with new backward compat logic). |
| `__main__.py` | `_print_providers()` function entirely. The `/providers` REPL command handler entirely. Any `config.provider = X` assignment in `_repl` (provider is now a read-only `@property`). |
| `AshLoop` (in `__main__._repl` callers) | `config.provider = prov` line after `switch_provider()` and `switch_model()` calls. |

## What NOT to Change

- `providers/` directory — all provider adapters unchanged
- `AshLoop.__init__` signature — still accepts `config` param
- `_build_tools()` — unchanged
- The provider construction logic — still uses `provider` derived from the string
