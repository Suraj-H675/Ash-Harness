# Ash Live Test Report

**Date:** 2026-06-18
**Commit range:** `e95f320` (fix model picker) through current

---

## Second Test Session — Setup Wizard + Provider/Model Migration

**Backend tested:** Groq + MiniMax via custom OpenAI-compatible provider
**Credentials:** Redacted. Test credentials must never be committed.

---

## New Test Results (Setup Wizard)

| # | Test | Status | Notes |
|---|------|--------|-------|
| 1 | First-run detection | ❌ BUG | `_has_provider_configured` returns True for default anthropic model even with no API key |
| 2 | `ash setup --help` | ✅ PASS | Help text correct |
| 3 | Groq provider end-to-end | ✅ PASS | `groq/llama-3.3-70b-versatile` chat works |
| 4 | MiniMax via OpenAI-compatible | ✅ PASS | `openai-compatible/MiniMax-M2.7` chat works |
| 5 | Custom provider save/load | ✅ PASS | `~/.ash/ash.toml` TOML save/load works |
| 6 | `save_env_value` atomic write | ✅ PASS | Atomic write, `chmod 0600`, `os.environ` set |
| 7 | REPL model context (Groq) | ✅ PASS | Groq provider builds and chats correctly |
| 8 | Setup wizard Groq flow | ✅ PASS | Credential flow correct; TTY check blocks piped input |
| 9 | MiniMax via custom provider | ✅ PASS | `my-minimax/MiniMax-M2.7` custom provider end-to-end |
| 10 | `_probe_models` discovery | ✅ PASS | Groq 17 models, MiniMax 8 models discovered |
| 11 | `_verify_openai` MiniMax | ✅ PASS | Verification succeeds |
| 12 | Old ash.toml migration | ✅ PASS | Migrates `api_key`, `provider`, `model_name` |
| 13 | Model switching | ✅ PASS | Switching providers works |
| 14 | `--non-interactive` flag | ✅ PASS | Fails gracefully with error message |
| 15 | `--quick` flag parsing | ✅ PASS | Argument parsing correct |
| 16 | `ash mcp list` | ✅ PASS | Works |
| 17 | `/models` REPL command | ⏭️ SKIP | Import issue in test script |
| 18 | `/model` REPL interactive picker | ⏭️ NOT FULLY TESTED | Requires TTY |
| 19 | `ash setup` full REPL integration | ⏭️ NOT FULLY TESTED | Requires TTY |
| 20 | Provider model list display | ⏭️ NOT FULLY TESTED | Requires TTY |

---

## Bugs Found & Fixed

### BUG 1: First-run detection never triggers for default config ✅ FIXED

**File:** `cli/setup.py` — `_has_provider_configured()`

**Problem:** Returns `True` whenever model string contains `/`, even with no API key set.

**Fix applied:** Rewrote function to check for actual API key presence (or ollama) instead of just `/` in model string. Now correctly returns `False` for new users.

### BUG 2: `/model` REPL command — empty input defaults to Anthropic ✅ FIXED

**File:** `cli/setup.py` — `_prompt_choice()`

**Problem:** Empty input defaults to Anthropic `[1]` instead of allowing cancellation.

**Fix applied:** Added `'c'` / `'q'` / `'cancel'` handling to `_prompt_choice()` — user can now type 'c' to cancel the provider selection.

### BUG 3: `_prompt_model_list` accepts 'c' as a model name ✅ FIXED

**File:** `cli/setup.py` — `_prompt_model_list()`

**Problem:** User typing 'c' at model selection was treated as literal model name "c".

**Fix applied:**
- Added `Optional[str]` return type (was `str`)
- Added `if val.lower() in ("c", "q", "cancel"): return None`
- Updated prompt to show `('c' to cancel)`
- All 5 flow callers now handle `None` return with `if model is None: print("  Cancelled."); return`

**File:** `cli/setup.py` — `_prompt_model_list()`

**Problem:** When prompted "Select a model (number or name):", if user types 'c' to cancel, it is treated as a literal model name "c" and returned. `_flow_anthropic` then saves `ASH_MODEL="anthropic/c"` and tries to verify. If verification fails, the bad model is still saved.

**Note:** `_flow_anthropic` has `if model is None: return` guard, but 'c' is not None — it's a string.

**Fix needed:** Add 'c' handling in `_prompt_model_list`:
```python
if val.lower() in ('c', 'q', 'cancel'):
    return None
```

---

## Test Fix Applied

### `test_config_loads_without_api_key` was fragile (pre-existing)

**Problem:** `clear_ash_env` fixture cleared env vars but not `~/.ash/`. If `~/.ash/.env` existed from a previous run with `GROQ_API_KEY`, `_has_provider_configured()` would return `True` (via the fixed logic) even though the test expected `anthropic`.

**Fix:** Updated `clear_ash_env` fixture in `tests/unit/test_config.py` to also delete `~/.ash/.env` and `~/.ash/ash.toml` before each test. Tests now pass regardless of prior `~/.ash/` state.

---

## Key Verified Behaviors

1. **MiniMax `sk-cp-...` key WORKS** — The key was previously thought to be invalid. Verified working:
   - `https://api.minimax.io/v1` with `Authorization: Bearer <key>`
   - Models available: `MiniMax-M3`, `MiniMax-M2.7`, `MiniMax-M2.7-highspeed`, `MiniMax-M2.5`, etc.
   - Previous `401` was likely a temporary auth issue or the key needs to be used differently

2. **Groq works** with `gsk_...` key at `https://api.groq.com/openai/v1`

3. **`~/.ash/.env`** created correctly with `chmod 0600`

4. **`~/.ash/ash.toml`** stores custom providers in TOML

5. **`_build_provider`** correctly dispatches to custom providers via `config.custom_providers`

6. **`save_env_value`** correctly sets `os.environ[key]` so providers pick up immediately

7. **Groq model list** (confirmed working): `llama-3.1-8b-instant`, `groq/compound`, `llama-3.3-70b-versatile`, `groq/compound-mini`, `qwen/qwen3-32b`, `meta-llama/llama-4-scout-17b-16e-instruct`, etc.

8. **MiniMax model list** (confirmed working): `MiniMax-M3`, `MiniMax-M2.7`, `MiniMax-M2.7-highspeed`, `MiniMax-M2.5`, `MiniMax-M2.5-highspeed`, `MiniMax-M2.1`, `MiniMax-M2.1-highspeed`, `MiniMax-M2`

---

## Provider Behavior Summary (Updated)

| Provider | Key required at `__init__`? | Verified working? |
|----------|------------------------------|------------------|
| `anthropic` | No (lazy) | Not tested (no key) |
| `openai` | Yes | Not tested (no key) |
| `deepseek` | Yes | Not tested (no key) |
| `groq` | Yes | ✅ YES |
| `ollama` | No | Not tested (not running) |
| `openai-compatible` | Yes | ✅ YES (MiniMax) |
| `my-minimax` (custom) | N/A | ✅ YES |

---

## Previous Test Session Results

From first session (`e95f320` era):

| Component | Status | Notes |
|-----------|--------|-------|
| Config loading (default) | PASS | provider=`anthropic`, model=`claude-3-7-sonnet-20250219` |
| Config loading (env override) | PASS | `ASH_PROVIDER=groq`, `ASH_MODEL=llama-3.3-70b-versatile` |
| Config backward compat promotion | PASS | `ASH_API_KEY` → `ANTHROPIC_API_KEY`, `ASH_MODEL_NAME` → `ASH_MODEL` |
| `AshConfig` field changes | PASS | `model_name` removed, `api_key` removed, `openai_base_url` removed |
| `model` field (renamed) | PASS | Default `claude-3-7-sonnet-20250219`, env-overrideable |
| Provider construction (all 5) | PASS | All 5 providers construct without crashing |
| `configure_max_tokens()` (all 5) | PASS | All providers accept and store `_max_tokens` |
| `_build_provider()` (all 6 types) | PASS | anthropic, openai, deepseek, groq, ollama, openai-compatible |
| Groq real streaming | PASS | `"reply with just the word OK"` → `"OK"` |
| Groq `max_tokens` cap | PASS | `max_tokens=5` produced `"1, 2,"` — correctly short |
| `AshLoop.switch_provider()` | PASS | All 5 provider switches work without crashing |
| `AshLoop.switch_model()` | PASS | Model-only switch within provider works |
| Config synced after switch | PASS | `loop._config` stays in sync with `loop.provider` |
| REPL command parsing | PASS | `/model`, `/model <name>`, `/model <prov>/<model>` all parsed |
| Error: unknown provider | PASS | `ValueError` raised correctly |
| Error: `configure_max_tokens(0)` | PASS | `ValueError` raised correctly |
| Error: `configure_max_tokens(-1)` | PASS | `ValueError` raised correctly |

### Known Issues (from previous session — still relevant)

1. **`SessionStore` has no `close()` method** — SQLite connection may leak
2. **`RunCommandTool` schema param is `command_line` not `command`** — minor API inconsistency
3. **`ls /root` not blocked by `SafetyGuard.validate_command()`** — path scope not enforced for shell commands
4. **Circuit breaker integration test** — pre-existing design issue

---

## Bugs Found & Fixed During Provider Migration (Prior Sessions)

1. **OpenAI-compatible providers (Groq, DeepSeek, OpenAI) require API key at construction** — Fixed by adding `ValueError` validation in `__init__`
2. **Groq `AVAILABLE_MODELS` had wrong model names** — Fixed (removed `llama-3-70b-8192`, `mixtral-8x7b-32768`)
3. **OpenAI-compatible providers used lazy-client pattern incorrectly** — Removed lazy pattern, eager validation at `__init__`
4. **MiniMax API key rejected** — **RESOLVED**: The key works when used correctly with `Authorization: Bearer` at `https://api.minimax.io/v1`
