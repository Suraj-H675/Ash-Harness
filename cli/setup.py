"""Interactive setup wizard for Ash.

Handles provider + model selection, API key collection, and credential
storage to ~/.ash/.env and ~/.ash/ash.toml.
"""

from __future__ import annotations

import getpass
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import httpx

from cli.config import (
    backup_config_file,
    get_env_value,
    get_config_path,
    is_config_migration_recorded,
    is_interactive_stdin,
    load_config,
    mask_key,
    record_config_migration,
    save_config,
    save_env_values,
)


class SetupOutcome(IntEnum):
    SUCCESS = 0
    CANCELLED = 1
    ERROR = 2


class SetupBack(Exception):
    """Return from a provider flow to provider selection."""


class SetupCancelled(Exception):
    """Cancel setup without treating it as an internal error."""


# ---------------------------------------------------------------------------
# Provider catalogue
# ---------------------------------------------------------------------------

PROVIDERS = [
    ("anthropic", "Anthropic", "Anthropic API models - ANTHROPIC_API_KEY"),
    ("openai", "OpenAI", "OpenAI API models - OPENAI_API_KEY"),
    ("deepseek", "DeepSeek", "DeepSeek API models - DEEPSEEK_API_KEY"),
    ("groq", "Groq", "Groq-hosted models - GROQ_API_KEY"),
    ("ollama", "Ollama", "Local models (no API key needed)"),
    (
        "openai-compatible",
        "OpenAI-Compatible",
        "Custom endpoint with any OpenAI-compatible API",
    ),
]
WEB_SEARCH_PROVIDERS = (
    (
        "brave",
        "Brave Search",
        "BRAVE_SEARCH_API_KEY",
        "https://api-dashboard.search.brave.com/",
    ),
    (
        "tavily",
        "Tavily",
        "TAVILY_API_KEY",
        "https://app.tavily.com/",
    ),
)
_PROVIDER_NAME = re.compile(r"^[A-Za-z0-9_.:-]+$")


@dataclass(frozen=True)
class ModelProbe:
    models: tuple[str, ...] = ()
    error: str | None = None

    @property
    def verified(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------
# Entry points (called from __main__.py)
# ---------------------------------------------------------------------------


def cmd_setup(args) -> int:
    """Entry point for the ``ash setup`` command. Returns exit code."""
    try:
        return int(run_setup_wizard(args))
    except (EOFError, KeyboardInterrupt, SetupCancelled):
        print("\nSetup cancelled.", file=sys.stderr)
        return int(SetupOutcome.CANCELLED)
    except Exception as exc:  # noqa: BLE001 - CLI boundary returns a stable status
        print(f"Setup failed: {exc}", file=sys.stderr)
        return int(SetupOutcome.ERROR)


def run_setup_wizard(args) -> SetupOutcome:
    """Main setup wizard orchestrator."""
    section = getattr(args, "section", None) or "all"
    quick = getattr(args, "quick", False)
    non_interactive = getattr(args, "non_interactive", False)

    from config import AshConfig

    config = AshConfig.load()

    if non_interactive or not is_interactive_stdin():
        if section == "web":
            if _has_web_search_configured(config):
                print(
                    f"Web search is configured for {config.web_search_provider}."
                )
                return SetupOutcome.SUCCESS
            print(
                "Error: web search needs BRAVE_SEARCH_API_KEY or "
                "TAVILY_API_KEY.",
                file=sys.stderr,
            )
            return SetupOutcome.ERROR
        if _has_provider_configured(config):
            print(f"Ash is configured for {config.model}.")
            print("Run 'ash doctor --connect' to verify endpoint connectivity.")
            return SetupOutcome.SUCCESS
        print("Error: ash setup requires an interactive terminal.", file=sys.stderr)
        print(
            "Set ASH_MODEL and its provider API key, configure Ollama, or rerun in a TTY.",
            file=sys.stderr,
        )
        return SetupOutcome.ERROR

    # Banner
    _print_header("Ash Setup Wizard")

    # Check for old ash.toml and offer migration
    _migrate_old_ash_toml()

    if section in ("model", "all"):
        result = setup_model_provider(config, quick=quick)
        if result != SetupOutcome.SUCCESS:
            print("Setup cancelled.", file=sys.stderr)
            return result

    if section == "web" or (section == "all" and not quick):
        result = setup_web_search()
        if result != SetupOutcome.SUCCESS:
            print("Setup cancelled.", file=sys.stderr)
            return result

    _print_info("Setup complete!")
    return SetupOutcome.SUCCESS


def setup_model_provider(config, *, quick: bool = False) -> SetupOutcome:
    """Provider + model selection — shared entry point from wizard and REPL."""
    return select_provider_and_model(config)


def setup_web_search() -> SetupOutcome:
    """Configure an optional fixed-endpoint live search provider."""

    while True:
        _print_header("Web Search Configuration")
        print("Choose a search provider, or skip this optional capability:\n")
        for index, (_provider, name, env_var, url) in enumerate(
            WEB_SEARCH_PROVIDERS, 1
        ):
            configured = " (configured)" if get_env_value(env_var) else ""
            print(f"  [{index}] {name}{configured}")
            print(f"      Create a key: {url}\n")
        skip_index = len(WEB_SEARCH_PROVIDERS) + 1
        print(f"  [{skip_index}] Skip\n")
        try:
            choice = _prompt_choice(
                "Enter a number",
                [str(index) for index in range(1, skip_index + 1)],
                default=skip_index - 1,
            )
            if choice == skip_index - 1:
                return SetupOutcome.SUCCESS
            provider, name, env_var, _url = WEB_SEARCH_PROVIDERS[choice]
            api_key = _prompt_api_key(env_var, f"{name} API key")
            save_env_values(
                {
                    env_var: api_key,
                    "ASH_WEB_SEARCH_PROVIDER": provider,
                }
            )
            _print_info(f"Configured {name} for live web search.")
            return SetupOutcome.SUCCESS
        except SetupBack:
            print("  Returning to web search provider selection.")
        except SetupCancelled:
            return SetupOutcome.CANCELLED


# ---------------------------------------------------------------------------
# Provider + model selection (shared with REPL /model command)
# ---------------------------------------------------------------------------


def select_provider_and_model(config) -> SetupOutcome:
    """Show provider list, route to provider flow, verify model."""
    while True:
        _print_header("Select your inference provider")
        print("Choose a provider:\n")
        for i, (_provider_id, name, desc) in enumerate(PROVIDERS, 1):
            print(f"  [{i}] {name}")
            print(f"      {desc}\n")

        try:
            choice = _prompt_choice(
                "Enter a number",
                [str(i) for i in range(1, len(PROVIDERS) + 1)],
                default=0,
            )
            provider_id = PROVIDERS[choice][0]
            current = _get_current_model_for_provider(config, provider_id)
            if provider_id == "anthropic":
                return _flow_anthropic(current)
            if provider_id == "openai":
                return _flow_openai(current)
            if provider_id == "deepseek":
                return _flow_deepseek(current)
            if provider_id == "groq":
                return _flow_groq(current)
            if provider_id == "ollama":
                return _flow_ollama(current)
            return _flow_openai_compatible()
        except SetupBack:
            print("  Returning to provider selection.")
        except SetupCancelled:
            return SetupOutcome.CANCELLED


# ---------------------------------------------------------------------------
# Provider flows
# ---------------------------------------------------------------------------


def _flow_anthropic(current: str) -> SetupOutcome:
    """Anthropic setup: API key, model selection, verification."""
    _print_header("Anthropic Configuration")

    api_key = _prompt_api_key("ANTHROPIC_API_KEY", "Anthropic API key")
    base_url_override = _prompt_optional_url(
        "ANTHROPIC_API_BASE",
        "https://api.anthropic.com",
    )
    base_url = base_url_override or "https://api.anthropic.com"

    models, verified = _discover_models(
        "Anthropic",
        lambda: _probe_anthropic_models_detailed(api_key, base_url),
        fallback=[current] if current else [],
    )
    model = _prompt_model_list(models, current)
    _confirm_undiscovered_model(model, models, verified)

    settings = {
        "ANTHROPIC_API_KEY": api_key,
        "ASH_MODEL": f"anthropic/{model}",
    }
    if base_url_override:
        settings["ANTHROPIC_API_BASE"] = base_url_override
    save_env_values(settings)
    _print_verification_status(verified)
    return SetupOutcome.SUCCESS


def _flow_openai(current: str) -> SetupOutcome:
    """OpenAI setup: API key, optional base URL override, model selection."""
    _print_header("OpenAI Configuration")

    api_key = _prompt_api_key("OPENAI_API_KEY", "OpenAI API key")

    base_url_override = _prompt_optional_url(
        "OPENAI_API_BASE",
        "https://api.openai.com/v1",
    )
    base_url = base_url_override or "https://api.openai.com/v1"
    models, verified = _discover_models(
        "OpenAI",
        lambda: _probe_models_detailed(base_url, api_key),
        fallback=[current] if current else [],
    )
    model = _prompt_model_list(models, current)
    _confirm_undiscovered_model(model, models, verified)

    settings = {
        "OPENAI_API_KEY": api_key,
        "ASH_MODEL": f"openai/{model}",
    }
    if base_url_override:
        settings["OPENAI_API_BASE"] = base_url_override
    save_env_values(settings)
    _print_verification_status(verified)
    return SetupOutcome.SUCCESS


def _flow_deepseek(current: str) -> SetupOutcome:
    """DeepSeek setup: API key, optional base URL override, model selection."""
    _print_header("DeepSeek Configuration")

    api_key = _prompt_api_key("DEEPSEEK_API_KEY", "DeepSeek API key")

    base_url_override = _prompt_optional_url(
        "DEEPSEEK_API_BASE",
        "https://api.deepseek.com/v1",
    )
    base_url = base_url_override or "https://api.deepseek.com/v1"
    models, verified = _discover_models(
        "DeepSeek",
        lambda: _probe_models_detailed(base_url, api_key),
        fallback=[current] if current else [],
    )
    model = _prompt_model_list(models, current)
    _confirm_undiscovered_model(model, models, verified)

    settings = {
        "DEEPSEEK_API_KEY": api_key,
        "ASH_MODEL": f"deepseek/{model}",
    }
    if base_url_override:
        settings["DEEPSEEK_API_BASE"] = base_url_override
    save_env_values(settings)
    _print_verification_status(verified)
    return SetupOutcome.SUCCESS


def _flow_groq(current: str) -> SetupOutcome:
    """Groq setup: API key, model selection."""
    _print_header("Groq Configuration")

    api_key = _prompt_api_key("GROQ_API_KEY", "Groq API key")

    models, verified = _discover_models(
        "Groq",
        lambda: _probe_models_detailed(
            "https://api.groq.com/openai/v1",
            api_key,
        ),
        fallback=[current] if current else [],
    )
    model = _prompt_model_list(models, current)
    _confirm_undiscovered_model(model, models, verified)

    save_env_values(
        {
            "GROQ_API_KEY": api_key,
            "ASH_MODEL": f"groq/{model}",
        }
    )
    _print_verification_status(verified)
    return SetupOutcome.SUCCESS


def _flow_ollama(current: str) -> SetupOutcome:
    """Ollama setup: base URL, local model selection via /api/tags."""
    _print_header("Ollama Configuration")

    base_url = _prompt_setup_text(
        f"  Ollama base URL [{'http://localhost:11434'}]: ",
        allow_empty=True,
    )
    if not base_url:
        base_url = "http://localhost:11434"
    base_url = _validate_base_url(base_url)

    models, verified = _discover_models(
        "Ollama",
        lambda: _probe_ollama_models_detailed(base_url),
        fallback=[current] if current else [],
        guidance=(
            "Start Ollama with 'ollama serve'. If it has no models, run "
            "'ollama pull <model>', then retry."
        ),
    )

    model = _prompt_model_list(models, current)
    _confirm_undiscovered_model(model, models, verified)

    save_env_values(
        {
            "OLLAMA_API_BASE": base_url,
            "ASH_MODEL": f"ollama/{model}",
        }
    )
    print(f"  Configured Ollama with model: {model}")
    _print_verification_status(verified)
    return SetupOutcome.SUCCESS


def _flow_openai_compatible() -> SetupOutcome:
    """OpenAI-compatible custom endpoint: base URL, optional API key, model selection.

    Saves to ~/.ash/ash.toml under [custom_providers.<name>].
    """
    _print_header("OpenAI-Compatible Endpoint")

    name = _prompt_setup_text("  Provider name (e.g. my-minimax): ")
    if not _PROVIDER_NAME.fullmatch(name) or name in {item[0] for item in PROVIDERS}:
        print("  Provider name must be a unique identifier without spaces or '/'.")
        raise SetupBack

    base_url = _prompt_setup_text("  Base URL (e.g. https://api.minimax.io/v1): ")
    base_url = _validate_base_url(base_url)

    api_key = getpass.getpass("  API key (optional, press Enter to skip): ").strip()
    if api_key.casefold() in {"c", "cancel", "q", "quit"}:
        raise SetupCancelled
    if api_key.casefold() in {"b", "back"}:
        raise SetupBack
    key_env = (
        "ASH_PROVIDER_"
        + "".join(
            character if character.isalnum() else "_" for character in name.upper()
        )
        + "_API_KEY"
    )
    models, verified = _discover_models(
        name,
        lambda: _probe_models_detailed(base_url, api_key or None),
    )

    print("\n  Available models from endpoint:")
    for i, m in enumerate(models, 1):
        print(f"    [{i}] {m}")

    model = _prompt_setup_text("\n  Select or type a model name: ")

    # If user picked a number, resolve to model name
    if model.isdigit():
        idx = int(model) - 1
        if 0 <= idx < len(models):
            model = models[idx]
        else:
            print("Invalid selection.")
            raise SetupBack
    _confirm_undiscovered_model(model, models, verified)

    # Save to ash.toml as custom_providers
    custom = load_config().get("custom_providers", {})
    custom[name] = {
        "base_url": base_url,
        "key_env": key_env,
        "models": models,
    }
    save_config({"custom_providers": custom})

    settings = {"ASH_MODEL": f"{name}/{model}"}
    if api_key:
        settings[key_env] = api_key
    save_env_values(settings)
    print(f"\n  Saved custom provider '{name}' to {get_config_path()}")
    print(f"  Model: {model}")
    _print_verification_status(verified)
    return SetupOutcome.SUCCESS


# ---------------------------------------------------------------------------
# API probing
# ---------------------------------------------------------------------------


def _response_error(response: httpx.Response, *, secret: str | None = None) -> str:
    detail = " ".join(response.text.split())[:200]
    if secret:
        detail = detail.replace(secret, "[REDACTED]")
    return f"HTTP {response.status_code}" + (f": {detail}" if detail else "")


def _models_from_payload(payload: object, *, collection: str) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    values = payload.get(collection, [])
    if not isinstance(values, list):
        return ()
    models: list[str] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id" if collection == "data" else "name")
        if isinstance(model_id, str) and model_id:
            models.append(model_id)
    return tuple(dict.fromkeys(models))


def _probe_anthropic_models_detailed(
    api_key: str,
    base_url: str = "https://api.anthropic.com",
) -> ModelProbe:
    normalized_base = base_url.rstrip("/")
    endpoint = (
        f"{normalized_base}/models"
        if normalized_base.endswith("/v1")
        else f"{normalized_base}/v1/models"
    )
    try:
        response = httpx.get(
            endpoint,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout=10,
        )
        if response.status_code != 200:
            return ModelProbe(error=_response_error(response, secret=api_key))
        models = _models_from_payload(response.json(), collection="data")
        return ModelProbe(
            models=models,
            error=None if models else "the endpoint returned no model IDs",
        )
    except Exception as exc:  # noqa: BLE001 - setup must surface probe failures
        return ModelProbe(error=f"{type(exc).__name__}: {exc}")


def _probe_anthropic_models(api_key: str) -> list[str]:
    """Call Anthropic's model-list endpoint and return newest-first IDs."""
    return list(_probe_anthropic_models_detailed(api_key).models)


def _probe_models_detailed(base_url: str, api_key: Optional[str]) -> ModelProbe:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/models",
            headers=headers,
            timeout=10,
        )
        if response.status_code != 200:
            return ModelProbe(error=_response_error(response, secret=api_key))
        models = _models_from_payload(response.json(), collection="data")
        return ModelProbe(
            models=models,
            error=None if models else "the endpoint returned no model IDs",
        )
    except Exception as exc:  # noqa: BLE001 - setup must surface probe failures
        return ModelProbe(error=f"{type(exc).__name__}: {exc}")


def _probe_models(base_url: str, api_key: Optional[str]) -> list[str]:
    """Call the /models endpoint of an OpenAI-compatible API. Returns model IDs."""
    return list(_probe_models_detailed(base_url, api_key).models)


def _probe_ollama_models_detailed(base_url: str) -> ModelProbe:
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=10)
        if response.status_code != 200:
            return ModelProbe(error=_response_error(response))
        models = _models_from_payload(response.json(), collection="models")
        return ModelProbe(
            models=models,
            error=None if models else "Ollama is running but has no installed models",
        )
    except Exception as exc:  # noqa: BLE001 - setup must surface probe failures
        return ModelProbe(error=f"{type(exc).__name__}: {exc}")


def _probe_ollama_models(base_url: str) -> list[str]:
    """Call Ollama /api/tags. Returns model names."""
    return list(_probe_ollama_models_detailed(base_url).models)


# ---------------------------------------------------------------------------
# Verification and recovery helpers
# ---------------------------------------------------------------------------


def _discover_models(
    provider_name: str,
    probe: Callable[[], ModelProbe],
    *,
    fallback: list[str] | None = None,
    guidance: str = "",
) -> tuple[list[str], bool]:
    """Probe with explicit retry/back/cancel/save-unverified decisions."""

    while True:
        result = probe()
        if result.models:
            print(
                f"  Verified {provider_name}; discovered {len(result.models)} model(s)."
            )
            return list(result.models), True
        print(f"  Could not verify {provider_name}: {result.error or 'unknown error'}")
        if guidance:
            print(f"  {guidance}")
        action = (
            input(
                "  Retry [r], continue unverified [s], go back [b], or cancel [c]? [r] "
            )
            .strip()
            .casefold()
        )
        if action in {"", "r", "retry"}:
            continue
        if action in {"s", "save", "continue"}:
            return list(fallback or ()), False
        if action in {"b", "back"}:
            raise SetupBack
        if action in {"c", "cancel", "q", "quit"}:
            raise SetupCancelled
        print("  Invalid choice.")


def _confirm_undiscovered_model(model: str, models: list[str], verified: bool) -> None:
    if not verified or model in models:
        return
    answer = (
        input(f"  {model!r} was not returned by the endpoint. Use it anyway? [y/N] ")
        .strip()
        .casefold()
    )
    if answer not in {"y", "yes"}:
        raise SetupBack


def _print_verification_status(verified: bool) -> None:
    if verified:
        print("  Provider credentials and model discovery verified.")
    else:
        print("  Saved without verification. Run 'ash doctor --connect' before use.")


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


def _has_web_search_configured(config) -> bool:
    selected = str(getattr(config, "web_search_provider", "auto")).casefold()
    keys = {
        "brave": "BRAVE_SEARCH_API_KEY",
        "tavily": "TAVILY_API_KEY",
    }
    if selected == "auto":
        return any(get_env_value(key) for key in keys.values())
    key = keys.get(selected)
    return bool(key and get_env_value(key))


def _get_current_model(config) -> str:
    """Return just the model name (no provider prefix) from config."""
    model_str = getattr(config, "model", "") or ""
    if "/" in model_str:
        return model_str.split("/", 1)[1]
    return model_str


def _get_current_model_for_provider(config, provider: str) -> str:
    model_str = getattr(config, "model", "") or ""
    configured_provider, separator, model_name = model_str.partition("/")
    if separator and configured_provider == provider:
        return model_name
    return ""


_LEGACY_PROVIDER_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "groq": "GROQ_API_KEY",
}
_LEGACY_MARKERS = frozenset({"api_key", "model_name"})
_LEGACY_RESERVED = frozenset({"api_key", "provider", "model_name"})
_LEGACY_PATH_FIELDS = frozenset(
    {"workspace_root", "db_directory", "chroma_persist_dir", "onnx_model_path"}
)
_PLACEHOLDER_KEYS = frozenset({"replace-with-your-api-key", "your-api-key", "changeme"})


def _migrate_old_ash_toml() -> None:
    """Safely migrate the original project-root Ash config format."""
    old_path = Path.cwd() / "ash.toml"
    if not old_path.exists():
        return
    if old_path.is_symlink():
        print(f"  Warning: refusing to migrate symlinked config: {old_path}")
        return

    try:
        import tomllib

        with old_path.open("rb") as handle:
            old_config = tomllib.load(handle)
    except Exception:
        return

    if not isinstance(old_config, dict) or not (_LEGACY_MARKERS & old_config.keys()):
        return
    if is_config_migration_recorded(old_path):
        return

    _print_header("Old Configuration Found")
    print(f"  Found ash.toml in {old_path.parent}")
    print("  This old format has been replaced by ~/.ash/.env")
    resp = input("\n  Migrate settings now? [Y/n] ").strip().lower()
    if resp in ("n", "no"):
        return

    user_config = load_config(strict=True)
    config_updates, env_updates, preserved = _plan_legacy_config_migration(
        old_config,
        old_path=old_path,
        user_config=user_config,
    )
    source_backup = backup_config_file(old_path, label="legacy-project-ash.toml")
    destination_path = get_config_path()
    destination_backup: Path | None = None
    if config_updates != user_config and destination_path.is_file():
        destination_backup = backup_config_file(
            destination_path,
            label="user-ash.toml-pre-migration",
        )
    if config_updates != user_config:
        save_config(config_updates)
    if env_updates:
        save_env_values(env_updates)
    record_config_migration(old_path, source_backup)

    print("\n  Migration complete.")
    print(f"  Legacy backup: {source_backup}")
    if destination_backup is not None:
        print(f"  Previous user config backup: {destination_backup}")
    if preserved:
        print("  Preserved existing destination values: " + ", ".join(preserved))
    print(
        "  Old ash.toml was left in place and will not be prompted again unless changed."
    )


def _plan_legacy_config_migration(
    old_config: dict[str, object],
    *,
    old_path: Path,
    user_config: dict[str, object],
) -> tuple[dict[str, object], dict[str, str], list[str]]:
    """Map legacy values without replacing newer destination settings."""

    from config import AshConfig, CURRENT_CONFIG_SCHEMA_VERSION

    merged = dict(user_config)
    env_updates: dict[str, str] = {}
    preserved: list[str] = []
    known_fields = set(AshConfig.model_fields)
    for field, value in old_config.items():
        if field in _LEGACY_RESERVED or field not in known_fields:
            continue
        normalized = _normalize_legacy_config_value(field, value, old_path.parent)
        if field in merged:
            preserved.append(field)
        else:
            merged[field] = normalized
    merged.setdefault("config_schema_version", CURRENT_CONFIG_SCHEMA_VERSION)

    provider = str(old_config.get("provider") or "anthropic").strip().casefold()
    model_name = str(old_config.get("model_name") or "").strip()
    if model_name:
        _preserve_or_stage_env(
            "ASH_MODEL",
            f"{provider}/{model_name}",
            env_updates,
            preserved,
        )
    api_key = str(old_config.get("api_key") or "").strip()
    key_name = _LEGACY_PROVIDER_KEYS.get(provider)
    if api_key and api_key.casefold() not in _PLACEHOLDER_KEYS and key_name:
        _preserve_or_stage_env(
            key_name,
            api_key,
            env_updates,
            preserved,
        )
    return merged, env_updates, sorted(set(preserved))


def _preserve_or_stage_env(
    key: str,
    value: str,
    updates: dict[str, str],
    preserved: list[str],
) -> None:
    if get_env_value(key) is not None:
        preserved.append(key)
    else:
        updates[key] = value


def _normalize_legacy_config_value(field: str, value: object, base: Path) -> object:
    if field not in _LEGACY_PATH_FIELDS or not isinstance(value, str):
        return value
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return str(path.resolve())


# ---------------------------------------------------------------------------
# Interactive prompt helpers
# ---------------------------------------------------------------------------


def _prompt_api_key(
    env_var: str,
    desc: str,
    env_var_legacy: str | None = None,
) -> str:
    """Prompt for an API key, with blank input returning to provider selection."""
    # Check existing env
    existing = get_env_value(env_var)
    if env_var_legacy:
        existing = existing or get_env_value(env_var_legacy)
    if existing:
        print(f"  Found existing {desc}: {mask_key(env_var)}")
        resp = input("    Rotate? [y/N, b back, c cancel] ").strip().casefold()
        if resp in {"c", "cancel", "q", "quit"}:
            raise SetupCancelled
        if resp in {"b", "back"}:
            raise SetupBack
        if resp not in ("y", "yes"):
            return existing

    key = getpass.getpass(f"  Enter {desc}: ").strip()
    if not key:
        raise SetupBack
    if key.casefold() in {"c", "cancel", "q", "quit"}:
        raise SetupCancelled
    if key.casefold() in {"b", "back"}:
        raise SetupBack

    return key


def _prompt_optional_url(env_var: str, default: str) -> Optional[str]:
    """Prompt for an optional base URL override. Returns URL or None."""
    existing = get_env_value(env_var)
    prompt = f"  {env_var}"
    if existing:
        prompt += f" [{existing}]"
    else:
        prompt += f" [{default}]"
    while True:
        val = _prompt_setup_text(prompt + ": ", allow_empty=True)
        candidate = val or existing
        if not candidate:
            return None
        try:
            return _validate_base_url(candidate)
        except ValueError as exc:
            print(f"  Invalid URL: {exc}")


def _validate_base_url(value: str) -> str:
    """Validate an HTTP(S) API base URL without embedded credentials."""

    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("use an absolute http:// or https:// URL")
    if parsed.username or parsed.password:
        raise ValueError("embedded credentials are not allowed")
    if parsed.query or parsed.fragment:
        raise ValueError("query strings and fragments are not allowed")
    return value.strip().rstrip("/")


def _prompt_model_list(models: list[str], current: str) -> str:
    """Show models and return a selection, or raise a navigation signal."""
    print("\n  Available models:")
    for i, m in enumerate(models, 1):
        marker = " (current)" if m == current else ""
        print(f"    [{i}] {m}{marker}")

    while True:
        val = input(
            "\n  Select a model (number or name, 'b' back, 'c' cancel): "
        ).strip()
        if not val:
            continue
        if val.casefold() in ("c", "q", "cancel", "quit"):
            raise SetupCancelled
        if val.casefold() in ("b", "back"):
            raise SetupBack
        if val.isdigit():
            idx = int(val) - 1
            if 0 <= idx < len(models):
                return models[idx]
            print("  Invalid number.")
        else:
            return val


def _prompt_choice(prompt: str, options: list[str], default: int) -> int:
    """Ask for a numbered option, raising when the user cancels."""
    options_str = "/".join(f"'{o}'" for o in options)
    while True:
        val = input(
            f"\n  {prompt} ({options_str}) [{default + 1}], 'c' to cancel: "
        ).strip()
        if not val:
            return default
        if val.casefold() in ("c", "q", "cancel", "quit"):
            raise SetupCancelled
        if val.isdigit():
            idx = int(val) - 1
            if 0 <= idx < len(options):
                return idx
        print("  Invalid choice.")


def _prompt_setup_text(prompt: str, *, allow_empty: bool = False) -> str:
    """Read wizard text with consistent back/cancel controls."""

    while True:
        value = input(prompt).strip()
        if value.casefold() in {"c", "cancel", "q", "quit"}:
            raise SetupCancelled
        if value.casefold() in {"b", "back"}:
            raise SetupBack
        if value or allow_empty:
            return value
        print("  A value is required. Enter 'b' to go back or 'c' to cancel.")


def _print_header(title: str) -> None:
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  {title}")
    print(sep)


def _print_info(msg: str) -> None:
    print(f"  {msg}")
