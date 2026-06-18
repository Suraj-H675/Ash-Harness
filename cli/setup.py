"""Interactive setup wizard for Ash.

Handles provider + model selection, API key collection, and credential
storage to ~/.ash/.env and ~/.ash/ash.toml.
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from typing import Optional

import httpx

from cli.config import (
    get_env_value,
    get_config_path,
    is_interactive_stdin,
    load_config,
    mask_key,
    save_config,
    save_env_value,
)


# ---------------------------------------------------------------------------
# Provider catalogue
# ---------------------------------------------------------------------------

PROVIDERS = [
    ("anthropic", "Anthropic", "Claude models — ANTHROPIC_API_KEY"),
    ("openai", "OpenAI", "GPT-4o, GPT-4o-mini, o3 — OPENAI_API_KEY"),
    ("deepseek", "DeepSeek", "DeepSeek-V3, DeepSeek-Coder — DEEPSEEK_API_KEY"),
    ("groq", "Groq", "Llama, Qwen — GROQ_API_KEY"),
    ("ollama", "Ollama", "Local models (no API key needed)"),
    (
        "openai-compatible",
        "OpenAI-Compatible",
        "Custom endpoint with any OpenAI-compatible API",
    ),
]


# ---------------------------------------------------------------------------
# Entry points (called from __main__.py)
# ---------------------------------------------------------------------------


def cmd_setup(args) -> int:
    """Entry point for the ``ash setup`` command. Returns exit code."""
    run_setup_wizard(args)
    return 0


def run_setup_wizard(args) -> None:
    """Main setup wizard orchestrator."""
    section = getattr(args, "section", None) or "all"
    quick = getattr(args, "quick", False)
    non_interactive = getattr(args, "non_interactive", False)

    # Non-interactive: fail fast
    if non_interactive or not is_interactive_stdin():
        print("Error: ash setup requires an interactive terminal.", file=sys.stderr)
        print("Use --non-interactive to suppress this error.", file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Import AshConfig here to avoid circular imports at top level
    # ------------------------------------------------------------------
    from config import AshConfig

    config = AshConfig.load()

    # Banner
    _print_header("Ash Setup Wizard")

    # Check for old ash.toml and offer migration
    _migrate_old_ash_toml()

    if section in ("model", "all"):
        setup_model_provider(config, quick=quick)

    _print_info("Setup complete!")


def setup_model_provider(config, *, quick: bool = False) -> None:
    """Provider + model selection — shared entry point from wizard and REPL."""
    select_provider_and_model(config)


# ---------------------------------------------------------------------------
# Provider + model selection (shared with REPL /model command)
# ---------------------------------------------------------------------------


def select_provider_and_model(config) -> None:
    """Show provider list, route to provider flow, verify model."""
    _print_header("Select your inference provider")

    # Show numbered provider list
    print("Choose a provider:\n")
    for i, (pid, name, desc) in enumerate(PROVIDERS, 1):
        print(f"  [{i}] {name}")
        print(f"      {desc}\n")

    default_idx = 0
    choice = _prompt_choice(
        "Enter a number",
        [str(i) for i in range(1, len(PROVIDERS) + 1)],
        default=default_idx,
    )

    if choice is None:
        print("Cancelled.")
        return

    provider_id = PROVIDERS[choice][0]

    # Route to provider-specific flow
    current = _get_current_model(config)
    if provider_id == "anthropic":
        _flow_anthropic(current)
    elif provider_id == "openai":
        _flow_openai(current)
    elif provider_id == "deepseek":
        _flow_deepseek(current)
    elif provider_id == "groq":
        _flow_groq(current)
    elif provider_id == "ollama":
        _flow_ollama(current)
    elif provider_id == "openai-compatible":
        _flow_openai_compatible()


# ---------------------------------------------------------------------------
# Provider flows
# ---------------------------------------------------------------------------


def _flow_anthropic(current: str) -> None:
    """Anthropic setup: API key, model selection, verification."""
    _print_header("Anthropic Configuration")

    api_key = _prompt_api_key("ANTHROPIC_API_KEY", "Anthropic API key")
    if api_key is None:
        return

    # Known Claude models
    models = [
        "claude-opus-4-20250514",
        "claude-3-7-sonnet-20250219",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-haiku-20241022",
    ]
    model = _prompt_model_list(models, current)
    if model is None:
        print("  Cancelled.")
        return

    # Save ASH_MODEL env var
    save_env_value("ASH_MODEL", f"anthropic/{model}")
    # Verify
    _verify_anthropic(api_key, model)


def _flow_openai(current: str) -> None:
    """OpenAI setup: API key, optional base URL override, model selection."""
    _print_header("OpenAI Configuration")

    api_key = _prompt_api_key("OPENAI_API_KEY", "OpenAI API key")
    if api_key is None:
        return

    base_url_override = _prompt_optional_url(
        "OPENAI_API_BASE",
        "https://api.openai.com/v1",
    )
    if base_url_override:
        save_env_value("OPENAI_API_BASE", base_url_override)

    models = _probe_models("https://api.openai.com/v1", api_key)
    if not models:
        print("Could not fetch models. Using default list.")
        models = ["gpt-4o", "gpt-4o-mini", "o3", "o4-mini"]
    model = _prompt_model_list(models, current)
    if model is None:
        print("  Cancelled.")
        return

    save_env_value("ASH_MODEL", f"openai/{model}")
    _verify_openai(api_key, base_url_override, model)


def _flow_deepseek(current: str) -> None:
    """DeepSeek setup: API key, optional base URL override, model selection."""
    _print_header("DeepSeek Configuration")

    api_key = _prompt_api_key("DEEPSEEK_API_KEY", "DeepSeek API key")
    if api_key is None:
        return

    base_url_override = _prompt_optional_url(
        "DEEPSEEK_API_BASE",
        "https://api.deepseek.com/v1",
    )
    if base_url_override:
        save_env_value("DEEPSEEK_API_BASE", base_url_override)

    models = _probe_models("https://api.deepseek.com/v1", api_key)
    if not models:
        print("Could not fetch models. Using default list.")
        models = ["deepseek-chat", "deepseek-reasoner"]
    model = _prompt_model_list(models, current)
    if model is None:
        print("  Cancelled.")
        return

    save_env_value("ASH_MODEL", f"deepseek/{model}")
    _verify_openai(api_key, base_url_override, model)


def _flow_groq(current: str) -> None:
    """Groq setup: API key, model selection."""
    _print_header("Groq Configuration")

    api_key = _prompt_api_key("GROQ_API_KEY", "Groq API key")
    if api_key is None:
        return

    models = _probe_models("https://api.groq.com/openai/v1", api_key)
    if not models:
        print("Could not fetch models. Using default list.")
        models = [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "qwen3.3-32b",
            "compound-mini",
        ]
    model = _prompt_model_list(models, current)
    if model is None:
        print("  Cancelled.")
        return

    save_env_value("ASH_MODEL", f"groq/{model}")
    _verify_openai(api_key, "https://api.groq.com/openai/v1", model)


def _flow_ollama(current: str) -> None:
    """Ollama setup: base URL, local model selection via /api/tags."""
    _print_header("Ollama Configuration")

    base_url = input(f"  Ollama base URL [{'http://localhost:11434'}]: ").strip()
    if not base_url:
        base_url = "http://localhost:11434"
    save_env_value("OLLAMA_API_BASE", base_url)

    models = _probe_ollama_models(base_url)
    if not models:
        print("Warning: could not reach Ollama. Using default list.")
        models = ["llama3", "qwen2.5-coder:7b"]

    model = _prompt_model_list(models, current)
    if model is None:
        print("  Cancelled.")
        return

    save_env_value("ASH_MODEL", f"ollama/{model}")
    print(f"  Configured Ollama with model: {model}")


def _flow_openai_compatible() -> None:
    """OpenAI-compatible custom endpoint: base URL, optional API key, model selection.

    Saves to ~/.ash/ash.toml under [custom_providers.<name>].
    """
    _print_header("OpenAI-Compatible Endpoint")

    name = input("  Provider name (e.g. my-minimax): ").strip()
    if not name:
        print("Cancelled.")
        return

    base_url = input("  Base URL (e.g. https://api.minimax.io/v1): ").strip()
    if not base_url:
        print("Cancelled.")
        return

    api_key = input("  API key (optional, press Enter to skip): ").strip()
    if api_key:
        save_env_value("OPENAI_API_KEY", api_key)

    # Probe models
    models = _probe_models(base_url, api_key or None)
    if not models:
        print("  Warning: could not probe /models endpoint. Enter model name manually.")
        models = []

    print("\n  Available models from endpoint:")
    for i, m in enumerate(models, 1):
        print(f"    [{i}] {m}")

    model = input("\n  Select or type a model name: ").strip()
    if not model:
        print("Cancelled.")
        return

    # If user picked a number, resolve to model name
    if model.isdigit():
        idx = int(model) - 1
        if 0 <= idx < len(models):
            model = models[idx]
        else:
            print("Invalid selection.")
            return

    # Save to ash.toml as custom_providers
    custom = load_config().get("custom_providers", {})
    custom[name] = {
        "base_url": base_url,
        "api_key": api_key,
        "models": models,
    }
    save_config({"custom_providers": custom})

    # Set ASH_MODEL to this custom provider's model
    save_env_value("ASH_MODEL", f"{name}/{model}")
    print(f"\n  Saved custom provider '{name}' to {get_config_path()}")
    print(f"  Model: {model}")


# ---------------------------------------------------------------------------
# API probing
# ---------------------------------------------------------------------------


def _probe_models(base_url: str, api_key: Optional[str]) -> list[str]:
    """Call the /models endpoint of an OpenAI-compatible API. Returns model IDs."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        resp = httpx.get(
            f"{base_url.rstrip('/')}/models",
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            # OpenAI-compatible response shape: {"object": "list", "data": [...]}
            models = []
            for item in data.get("data", []):
                mid = item.get("id", "")
                if mid:
                    models.append(mid)
            return models
    except Exception:
        pass
    return []


def _probe_ollama_models(base_url: str) -> list[str]:
    """Call Ollama /api/tags. Returns model names."""
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------


def _verify_anthropic(api_key: str, model: str) -> None:
    """Verify Anthropic API key with a minimal completion request."""
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        print("  Warning: 'anthropic' package not installed — skipping verification.")
        return

    client = AsyncAnthropic(api_key=api_key)

    async def verify() -> bool:
        try:
            async with client.messages.stream(
                model=model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            ) as stream:
                async for _ in stream.text_stream:
                    pass
            return True
        except Exception as exc:
            print(f"\n  Verification failed: {exc}")
            return False

    success = asyncio.run(verify())
    if success:
        print("  ✓ API key verified successfully.")


def _verify_openai(
    api_key: str,
    base_url: Optional[str],
    model: str,
) -> None:
    """Verify an OpenAI-compatible API key with a trivial request."""
    url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            print("  ✓ API key verified successfully.")
        else:
            print(
                f"\n  Verification failed: HTTP {resp.status_code} — {resp.text[:200]}"
            )
    except Exception as exc:
        print(f"\n  Verification failed: {exc}")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _has_provider_configured(config) -> bool:
    """Return True if Ash has a working provider + API key combination.

    A model string with "/" is not enough — we must also have a way to
    actually call the API (an API key, or ollama which needs no key).
    """
    model_str = getattr(config, "model", "") or ""
    if not model_str or "/" not in model_str:
        return False

    provider = model_str.split("/", 1)[0]

    # Ollama needs no API key
    if provider == "ollama":
        return True

    # Known providers: check for their specific API key
    key_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "groq": "GROQ_API_KEY",
    }
    if provider in key_map:
        if get_env_value(key_map[provider]):
            return True

    # Custom provider: must have entry in custom_providers
    if provider in config.custom_providers:
        return True

    return False


def _get_current_model(config) -> str:
    """Return just the model name (no provider prefix) from config."""
    model_str = getattr(config, "model", "") or ""
    if "/" in model_str:
        return model_str.split("/", 1)[1]
    return model_str


def _migrate_old_ash_toml() -> None:
    """Detect old project-root ash.toml and offer to migrate to ~/.ash/."""
    from pathlib import Path

    old_path = Path.cwd() / "ash.toml"
    if not old_path.exists():
        return

    try:
        import toml  # type: ignore[import-untyped]

        old_config = toml.load(old_path)
    except Exception:
        return

    if not old_config:
        return

    _print_header("Old Configuration Found")
    print(f"  Found ash.toml in {old_path.parent}")
    print("  This old format has been replaced by ~/.ash/.env")
    resp = input("\n  Migrate settings now? [Y/n] ").strip().lower()
    if resp in ("n", "no"):
        return

    # Migrate api_key
    if old_config.get("api_key"):
        save_env_value("ANTHROPIC_API_KEY", str(old_config["api_key"]))

    # Migrate provider + model
    provider = old_config.get("provider", "anthropic")
    model_name = old_config.get("model_name", "")
    if model_name:
        save_env_value("ASH_MODEL", f"{provider}/{model_name}")

    print("\n  Migration complete. Old ash.toml left in place.")
    print("  You may remove it manually.")


# ---------------------------------------------------------------------------
# Interactive prompt helpers
# ---------------------------------------------------------------------------


def _prompt_api_key(
    env_var: str,
    desc: str,
    env_var_legacy: str | None = None,
) -> Optional[str]:
    """Prompt for an API key if not already set. Returns key or None."""
    # Check existing env
    existing = get_env_value(env_var)
    if env_var_legacy:
        existing = existing or get_env_value(env_var_legacy)
    if existing:
        print(f"  Found existing {desc}: {mask_key(env_var)}")
        resp = input("    Rotate? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            return existing

    key = getpass.getpass(f"  Enter {desc}: ").strip()
    if not key:
        print("  No key entered.")
        return None

    save_env_value(env_var, key)
    return key


def _prompt_optional_url(env_var: str, default: str) -> Optional[str]:
    """Prompt for an optional base URL override. Returns URL or None."""
    existing = get_env_value(env_var)
    prompt = f"  {env_var}"
    if existing:
        prompt += f" [{existing}]"
    else:
        prompt += f" [{default}]"
    val = input(prompt + ": ").strip()
    if val:
        return val
    return existing or None


def _prompt_model_list(models: list[str], current: str) -> Optional[str]:
    """Show a numbered list of models, let user pick or type. Returns model name or None to cancel."""
    print("\n  Available models:")
    for i, m in enumerate(models, 1):
        marker = " (current)" if m == current else ""
        print(f"    [{i}] {m}{marker}")

    while True:
        val = input("\n  Select a model (number or name, 'c' to cancel): ").strip()
        if not val:
            continue
        if val.lower() in ("c", "q", "cancel"):
            return None
        if val.isdigit():
            idx = int(val) - 1
            if 0 <= idx < len(models):
                return models[idx]
            print("  Invalid number.")
        else:
            return val


def _prompt_choice(prompt: str, options: list[str], default: int) -> Optional[int]:
    """Ask user to pick from numbered options. Returns 0-based index or None to cancel."""
    options_str = "/".join(f"'{o}'" for o in options)
    while True:
        val = input(f"\n  {prompt} ({options_str}) [{default + 1}], 'c' to cancel: ").strip()
        if not val:
            return default
        if val.lower() in ("c", "q", "cancel"):
            return None
        if val.isdigit():
            idx = int(val) - 1
            if 0 <= idx < len(options):
                return idx
        print("  Invalid choice.")


def _print_header(title: str) -> None:
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  {title}")
    print(sep)


def _print_info(msg: str) -> None:
    print(f"  {msg}")
