# Interactive Setup Wizard Plan — Ash

## Goal

Add an interactive `ash setup` command (model/provider selection wizard) modeled exactly after Hermès' `hermes setup model` flow. Users should be able to:
- Run `ash setup` (or `ash setup model`) to configure provider + API key
- Be prompted on first run if no provider is configured
- Add custom OpenAI-compatible endpoints with base URL + API key
- Have credentials stored in `~/.ash/.env` (API keys + `ASH_MODEL` as env vars), custom OpenAI-compatible providers in `~/.ash/ash.toml`

---

## Reference: Hermès Setup Flow

Hermès (`hermes setup model`):
1. Checks if configured via `_has_any_provider_configured()`
2. Shows numbered provider list (from `CANONICAL_PROVIDERS`)
3. User picks → routes to `_model_flow_*()` for that provider
4. Each flow: prompts for key via `getpass.getpass()`, verifies, saves to `~/.hermes/.env`
5. Saves model/provider config to `~/.hermes/config.yaml`
6. `_model_flow_custom()` for OpenAI-compatible: asks base URL + key, probes `/models`, saves as named custom provider

---

## What Exists Today in Ash

| Component | Current State |
|-----------|--------------|
| `ash setup` command | **Does not exist** |
| First-run detection | **Does not exist** |
| `~/.ash/` directory | Only `db/` created by SessionStore |
| `~/.ash/.env` | Does not exist |
| `~/.ash/ash.toml` | Does not exist — will be created for custom_providers |
| `ash.toml` (project root) | Old flat format, effectively dead |
| `providers/*` key reading | Direct `os.environ.get(...)` per provider |
| `config.py` AshConfig | Has `model` in `provider/model` string format |

---

## Implementation Plan

### Phase 1: New Files

#### 1. `cli/setup.py` — The Setup Wizard

New file `cli/setup.py`. This is the core of the wizard.

**Entry point:** `cmd_setup(args)` → `run_setup_wizard(args)`

**`run_setup_wizard(args)` flow:**
1. Detect non-interactive environment (Docker, CI) — if so, print error and exit
2. Check `args.section` — if `"model"`, run only `setup_model_provider(config)`
3. Check `is_interactive_stdin()` — if not interactive, error out
4. Show banner
5. Check if existing install: `_has_any_provider_configured()`
   - If existing: offer "Configure model/provider" vs "Full setup"
   - If new: offer "Quick setup (recommended)" vs "Full setup"
6. Quick setup → `setup_model_provider(config, quick=True)`
7. Full setup → `setup_model_provider(config, quick=False)` + other sections

**`setup_model_provider(config, *, quick: bool = False)` flow:**
1. Delegate to `select_provider_and_model()` (same shared code path used by `/model` REPL command in Hermès)
2. `quick=True` skips: credential rotation, vision, TTS

**Provider list (Ash — reduced from Hermès):**
```python
PROVIDERS = [
    ("anthropic",       "Anthropic",          "Claude models — ANTHROPIC_API_KEY"),
    ("openai",          "OpenAI",              "GPT-4o, GPT-4o-mini, o3 — OPENAI_API_KEY"),
    ("deepseek",        "DeepSeek",            "DeepSeek-V3, DeepSeek-Coder — DEEPSEEK_API_KEY"),
    ("groq",            "Groq",                "Llama, Qwen — GROQ_API_KEY"),
    ("ollama",          "Ollama",              "Local models (no API key needed)"),
    ("openai-compatible","OpenAI-Compatible",  "Custom endpoint with any OpenAI-compatible API"),
]
```
(Hermès has 35+ providers, Ash starts with 6 core ones.)

**`select_provider_and_model()` flow:**
1. Show header: "Select your inference provider"
2. Numbered list of providers
3. User picks number (or 'c' to cancel)
4. Route to provider-specific flow

**Provider flows:**

- **`_flow_anthropic()`**: Check `ANTHROPIC_API_KEY` in env. If missing, prompt via `getpass.getpass()`. Save via `save_env_value()`. Probe API with `models` call. Pick model from list.

- **`_flow_openai()`**: Check `OPENAI_API_KEY`. If missing, prompt. Save. Optional `OPENAI_API_BASE` override. Probe `/models`. Pick model.

- **`_flow_deepseek()`**: Check `DEEPSEEK_API_KEY`. Prompt if missing. Optional `DEEPSEEK_API_BASE` override. Probe `/models`. Pick model.

- **`_flow_groq()`**: Check `GROQ_API_KEY`. Prompt if missing. Optional `GROQ_API_BASE` override. Probe `/models`. Pick model.

- **`_flow_ollama()`**: No key needed. Ask for base URL (default `http://localhost:11434`). Probe `/api/tags` to list local models. Pick model.

- **`_flow_openai_compatible()`** (the "custom" flow): Ask for base URL, API key (optional). Probe `/models`. Pick model. Save to `~/.ash/ash.toml` under `custom_providers`. This makes it available in future `/model` commands.

**Model selection after provider/key:**
- For providers with `/models` endpoint (OpenAI-compatible, Groq, DeepSeek, OpenAI): call the API, show numbered list of models, user picks
- For Ollama: call `/api/tags`, show local models
- For Anthropic: hardcoded list of known Claude models
- User can also type a model name directly

**Verification step (after picking model):**
- Call the API with a trivial request (e.g., single-user message, max_tokens=1)
- If success → print "✓ Configured successfully"
- If failure → show error, offer to retry or skip verification

**Credential storage:**
- `save_env_value("ANTHROPIC_API_KEY", key)` → writes to `~/.ash/.env`
- `save_config(config)` → writes to `~/.ash/ash.toml` (TOML)

---

#### 2. `cli/config.py` — Credential Storage

New file `cli/config.py` with these functions:

**`~/.ash/.env` management:**
```python
ASH_DIR = Path.home() / ".ash"
ENV_FILE = ASH_DIR / ".env"
CONFIG_FILE = ASH_DIR / "ash.toml"   # TOML, not YAML

def save_env_value(key: str, value: str) -> None:
    """Atomically write key=value to ~/.ash/.env."""
    # Create ~/.ash/ if it doesn't exist
    # Read existing lines
    # Find and replace or append
    # Write atomically via temp file + os.replace()

def get_env_value(key: str) -> str | None:
    """Read from env vars first, then from ~/.ash/.env."""
    # Check os.environ first
    # Then load ~/.ash/.env and look up key

def load_env() -> dict[str, str]:
    """Load all vars from ~/.ash/.env."""

def ensure_ash_dir() -> None:
    """Create ~/.ash/ directory if missing."""

def save_config(config: dict) -> None:
    """Save custom_providers to ~/.ash/ash.toml (TOML)."""

def load_config() -> dict:
    """Load from ~/.ash/ash.toml."""
```

**`~/.ash/ash.toml` structure:**
```toml
[custom_providers.my-minimax]
base_url = "https://api.minimax.io/v1"
api_key = "sk-cp-..."
models = ["MiniMax-M2.7"]
```

**Atomic write pattern (from Hermès):**
```python
fd, tmp = tempfile.mkstemp(dir=str(ENV_FILE.parent), suffix='.tmp')
with os.fdopen(fd, 'w') as f:
    f.writelines(lines)
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, ENV_FILE)  # atomic on POSIX
os.chmod(ENV_FILE, 0o600)
```

---

#### 3. `cli/__init__.py`

Empty init file for `cli/` package.

---

### Phase 2: Modify `__main__.py`

#### Add `ash setup` subcommand

```python
# In main() argparse section:
setup_parser = subparsers.add_parser("setup", help="Configure Ash (provider, API key, model)")
setup_parser.add_argument("section", nargs="?", choices=["model", "providers", "all"], help="Which section to configure")
setup_parser.add_argument("--quick", action="store_true", help="Skip optional configuration")
setup_parser.add_argument("--non-interactive", action="store_true", help="Run in non-interactive mode (fail if input needed)")

# In main() after args parsed:
if args.command == "setup":
    from cli.setup import cmd_setup
    return cmd_setup(args)
```

#### Add first-run detection (before starting REPL)

In `main()`, after `config = AshConfig.load()`:

```python
# Check if unconfigured — if so, prompt to run setup
if not _has_provider_configured(config):
    print("Ash is not configured yet.")
    reply = input("Run 'ash setup' to configure your provider and API key. Press Enter to continue to REPL anyway: ").strip()
    if reply.lower() in ("", "y", "yes"):
        from cli.setup import cmd_setup
        result = cmd_setup(argparse.Namespace(section="model", quick=True, non_interactive=False))
        # Reload config after setup
        config = AshConfig.load()
    else:
        print("Continuing without configuration...")
```

Or simpler: just show a warning and let them into the REPL.

#### Add `select_provider_and_model()` for REPL use

```python
def select_provider_and_model(config: AshConfig) -> None:
    """Interactive provider + model selection. Used by /model REPL command and setup."""
    # Same flow as cli/setup.py:select_provider_and_model()
    # DRY: import from cli.setup if available, else duplicate
```

Actually, to avoid duplication, `select_provider_and_model()` lives in `cli/setup.py` and is imported by `__main__.py` for use in the REPL.

---

### Phase 3: Modify `config.py`

**Keep `model_post_init` as-is** — it handles backward compat for old env vars (`ASH_MODEL_NAME`, `ASH_PROVIDER`). The setup wizard writes new-style values but the backward compat is still needed for existing users.

**Add `custom_providers` field:**
```python
custom_providers: dict[str, dict] = Field(
    default_factory=dict,
    description="Custom OpenAI-compatible providers: name -> {base_url, api_key, models[]}"
)
```

**Update `model_config` to use `~/.ash/` paths:**
```python
model_config = SettingsConfigDict(
    env_prefix="ASH_",
    toml_file=str(Path.home() / ".ash" / "ash.toml"),
    dotenv_file=str(Path.home() / ".ash" / ".env"),
    extra="ignore",
)
```

This means:
- `~/.ash/.env` — API keys + `ASH_MODEL` as env vars (read via `DotEnvSettingsSource`, which calls `Path(env_file).expanduser()` so `~` works)
- `~/.ash/ash.toml` — `custom_providers` as TOML (read via `TomlConfigSettingsSource`)
- Project root `ash.toml` — still loaded but only for users who haven't migrated to `~/.ash/` yet

**How the two dotenv sources differ (pydantic-settings v2):** `EnvSettingsSource` reads from `os.environ` (actual shell env). `DotEnvSettingsSource` reads from the `.env` file. They are separate sources in the priority chain — `os.environ` values always win over the `.env` file, which is exactly what we want.

The wizard's `save_config()` writes `custom_providers` to `~/.ash/ash.toml`. The wizard's `save_env_value()` writes keys and `ASH_MODEL` to `~/.ash/.env` and also sets `os.environ[key] = value` so providers pick them up immediately.

---

### Phase 4: Update `_build_provider` in `__main__.py`

**No changes needed** to how providers read keys — they read from `os.environ` directly, which `save_env_value()` sets.

**Add `KNOWN_PROVIDERS` constant:**
```python
KNOWN_PROVIDERS = frozenset({
    "anthropic", "openai", "deepseek", "groq", "ollama", "openai-compatible"
})
```

**Update `_build_provider` to handle custom providers:**

```python
def _build_provider(config: AshConfig) -> ProviderABC:
    provider, model_name = _parse_model_string(config.model)

    if provider in KNOWN_PROVIDERS:
        # existing dispatch (anthropic, openai, deepseek, groq, ollama, openai-compatible)
    elif provider in config.custom_providers:
        # custom OpenAI-compatible provider from setup wizard
        from providers.openai import OpenAIProvider
        cp = config.custom_providers[provider]
        prov = OpenAIProvider(
            model_name=model_name,
            api_key=cp.get("api_key", ""),
            base_url=cp.get("base_url"),
        )
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov
    else:
        raise ValueError(f"Unknown provider: {provider!r}")
```

The providers (`providers/*.py`) need no changes — they read from `os.environ` which `save_env_value()` sets.

### Phase 5: Backward Compat

**`ash.toml` in project root (old config):**
- Setup wizard should detect if `ash.toml` exists in cwd and offer to migrate:
  - Read old fields (`provider`, `model`, `api_key`, etc.)
  - Convert to new format
  - Write to `~/.ash/.env`
  - Delete or rename old `ash.toml`

**Existing env vars (`ANTHROPIC_API_KEY`, etc.):**
- These are read directly by providers from `os.environ`
- `get_env_value()` checks `os.environ` first, so existing env vars still work
- Setup wizard's `save_env_value()` sets both `os.environ[key]` and writes to `~/.ash/.env`

**`.env.example` update:**
- Add header comment: `# Ash configuration is now stored in ~/.ash/.env — copy this file to ~/.ash/.env and fill in your keys`
- Keep all `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc. variable names (these are still what Ash reads)
- Keep non-provider settings as-is

---

## File Changes Summary

| File | Change |
|------|--------|
| `cli/__init__.py` | **NEW** — package init |
| `cli/setup.py` | **NEW** — `cmd_setup()`, `run_setup_wizard()`, `select_provider_and_model()`, provider flows |
| `cli/config.py` | **NEW** — `save_env_value()`, `get_env_value()`, `load_env()`, `save_config()`, `load_config()`, `ensure_ash_dir()` |
| `config.py` | Add `custom_providers` field; update `model_config` to use `~/.ash/` paths |
| `__main__.py` | Add `setup` subcommand; add first-run detection; update `_build_provider()` to handle custom providers; define `KNOWN_PROVIDERS` |
| `.env.example` | Update to reflect `~/.ash/.env` storage approach |

---

## Implementation Order

1. **`cli/config.py`** — credential storage (foundation)
2. **`cli/setup.py`** — setup wizard functions (the bulk of the work)
3. **`config.py`** — add `custom_providers` field, update `model_config` dotenv path
4. **`__main__.py`** — add `setup` subcommand, first-run detection, `KNOWN_PROVIDERS`, update `_build_provider()`
5. **`.env.example`** — update docs
6. **Tests** — unit tests for `save_env_value()`, `get_env_value()`, setup flow

---

## Detailed Function Inventory

### `cli/config.py`

| Function | Signature | Description |
|----------|-----------|-------------|
| `ensure_ash_dir()` | `() -> Path` | Create `~/.ash/` if missing, return path |
| `get_env_path()` | `() -> Path` | Return `~/.ash/.env` |
| `get_config_path()` | `() -> Path` | Return `~/.ash/ash.toml` |
| `save_env_value()` | `(key: str, value: str) -> None` | Atomically write to `~/.ash/.env` |
| `get_env_value()` | `(key: str) -> str \| None` | Read from `os.environ` then `~/.ash/.env` |
| `load_env()` | `() -> dict[str, str]` | Load all from `~/.ash/.env` |
| `save_config()` | `(config: dict) -> None` | Save `custom_providers` to `~/.ash/ash.toml` (TOML) |
| `load_config()` | `() -> dict` | Load from `~/.ash/ash.toml` |
| `is_interactive_stdin()` | `() -> bool` | Check if stdin is a TTY |
| `mask_key()` | `(key: str) -> str` | Return `"sk-...xxxx"` for display |

### `cli/setup.py`

| Function | Signature | Description |
|----------|-----------|-------------|
| `cmd_setup()` | `(args: argparse.Namespace) -> int` | Entry point from `main()` |
| `run_setup_wizard()` | `(args: argparse.Namespace) -> None` | Main wizard orchestrator |
| `setup_model_provider()` | `(config: dict, *, quick: bool) -> None` | Provider/model setup section |
| `select_provider_and_model()` | `() -> None` | Show provider list, get selection |
| `_prompt_provider_choice()` | `(choices: list, default: int) -> int \| None` | Numbered provider selection |
| `_flow_anthropic()` | `(current_model: str) -> None` | Anthropic setup flow |
| `_flow_openai()` | `(current_model: str) -> None` | OpenAI setup flow |
| `_flow_deepseek()` | `(current_model: str) -> None` | DeepSeek setup flow |
| `_flow_groq()` | `(current_model: str) -> None` | Groq setup flow |
| `_flow_ollama()` | `(current_model: str) -> None` | Ollama setup flow |
| `_flow_openai_compatible()` | `() -> None` | Custom endpoint setup |
| `_probe_models()` | `(base_url: str, api_key: str \| None) -> list[str]` | Call `/models` endpoint |
| `_verify_api_key()` | `(provider: str, base_url: str, api_key: str) -> bool` | Test API key with trivial request |
| `_has_provider_configured()` | `(config: AshConfig) -> bool` | Check if Ash has any provider configured |
| `_print_header()` | `(title: str) -> None` | Print section header |
| `_print_info()` | `(msg: str) -> None` | Print info message |
| `_prompt_choice()` | `(prompt: str, options: list[str], default: int) -> int` | Numbered choice prompt |

### `__main__.py` changes

| Change | Description |
|--------|-------------|
| Add `setup` subcommand | `ash setup [model] [--quick]` |
| Add first-run check | Before REPL starts, check `_has_provider_configured()` |
| Define `KNOWN_PROVIDERS` | Frozenset of built-in provider names |
| Update `_build_provider()` | Handle custom provider names from `config.custom_providers` |

---

## Known Gotchas

1. **`~/.ash/` creation timing** — Need `ensure_ash_dir()` called before any config load
2. **Hermès stores API keys in `.env` as plain text** — same approach for Ash, just in `~/.ash/.env` with `chmod 0600`
3. **The `model = "provider/model"` string** — setup wizard sets `ASH_MODEL=provider/model` env var, which `AshConfig` picks up via env source
4. **Anthropic SDK is lazy** — no verification possible without a real request; other providers can probe `/models`
5. **Non-interactive environments** — setup wizard must detect `non_interactive` and fail gracefully instead of hanging on `input()`
6. **The `sk-cp-...` MiniMax key** — it's a Group Secret, not a Bearer key. The setup wizard should NOT fail on auth error for MiniMax if the key "looks like" a MiniMax key — just warn and save

---

## What NOT to Implement (Yet)

- Credential rotation / pool (Hermès feature — skip for now)
- Vision / image analysis setup
- TTS setup
- MCP server setup (different concern)
- OAuth flows (require browser — out of scope)
- `ash setup --non-interactive` full implementation (just fail gracefully for now)

---

## Design Decision: Where to Store Config

**Decision:** Setup wizard writes ALL config to `~/.ash/.env` (as env vars), NOT to a YAML file.

Reasoning:
- Pydantic's `env_settings` already reads from env vars
- Adding `dotenv_file="~/.ash/.env"` to `model_config` makes it auto-loaded
- No need for a separate YAML config file
- Hermès uses `.env` for keys + `config.yaml` for structured config — but Ash is simpler
- Model string (`ASH_MODEL=groq/llama-3.3-70b-versatile`) is just another env var

**Final config loading order:**
1. `init_settings` (explicit overrides)
2. `env_settings` (env vars + `~/.ash/.env` via `dotenv_file`)
3. `TomlConfigSettingsSource` (`~/.ash/ash.toml` — created by wizard for custom_providers)
4. `dotenv_settings` (project root `.env` — backward compat)
5. `file_secret_settings`

Wait — if both `env_settings` and `dotenv_settings` are in the chain, and `dotenv_file` is set, does dotenv override env or vice versa?

From pydantic-settings source: `DotenvSettingsSource` reads the dotenv file and returns it as a dict. The priority in `settings_customize_sources` determines which wins. Since `env_settings` comes before `TomlConfigSettingsSource` in our chain, env vars take precedence over dotenv file. But `dotenv_settings` (from dotenv_settings source) is AFTER `TomlConfigSettingsSource` in our chain — meaning dotenv would override TOML if they conflict.

Actually our chain is: `init > env > Toml > dotenv > file_secret`. With `dotenv_file="~/.ash/.env"`, dotenv_settings reads from `~/.ash/.env`. This is AFTER env vars in priority, so if a key is in BOTH env and `~/.ash/.env`, the env var wins.

This is exactly what we want: explicitly set env vars (from current shell) override `~/.ash/.env` file values.

**What the wizard writes to `~/.ash/.env`:**
```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
DEEPSEEK_API_KEY=...
GROQ_API_KEY=...
ASH_MODEL=groq/llama-3.3-70b-versatile
OPENAI_API_BASE=https://api.minimax.io/v1
```

**What AshConfig reads from these:**
- `ANTHROPIC_API_KEY` → used by `providers/anthropic.py` via `os.environ.get("ANTHROPIC_API_KEY", "")`
- `ASH_MODEL` → the `model` field, parsed to get provider/model

**Custom providers** are trickier. The wizard needs to save them somewhere pydantic can read them. Write to `~/.ash/ash.toml`:
```toml
[custom_providers.my-minimax]
base_url = "https://api.minimax.io/v1"
models = ["MiniMax-M2.7"]
```

This goes into `AshConfig.custom_providers` field.

---

## Backward Compatibility

| Old User | New Behavior |
|----------|-------------|
| Has `ash.toml` with `provider=anthropic model=claude-3-5-haiku` | Wizard detects old config, prompts to migrate |
| Has env vars set in shell | Works as-is — env vars take priority |
| Has `ANTHROPIC_API_KEY=...` in shell | Works as-is |
| Upgrading from before setup wizard | First run shows setup prompt |

---

## Testing Plan

1. **Unit tests for `cli/config.py`** — test atomic writes, read/write round-trip
2. **Unit tests for provider flows** — mock `input()` and `getpass`, verify correct env values saved
3. **Integration test** — run setup wizard in subprocess with piped input, verify `~/.ash/.env` created correctly
4. **Full REPL test** — start ash, `/setup`, select provider, verify model works
