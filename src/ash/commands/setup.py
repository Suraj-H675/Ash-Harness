"""Interactive setup wizard for Ash.

Handles provider + model selection, API key collection, and credential
storage to ~/.ash/.env and ~/.ash/ash.toml.
"""

from __future__ import annotations

import getpass
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ash.commands.config import (
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
from ash.provider_catalog import (
    BUILTIN_PROVIDERS,
    ProviderDescriptor,
    get_provider_descriptor,
)


class SetupOutcome(IntEnum):
    SUCCESS = 0
    CANCELLED = 1
    ERROR = 2


class ProviderManagementAction(Enum):
    ADD = "add"
    REPLACE = "replace"
    MOVE = "move"
    REMOVE = "remove"
    CLEAR = "clear"
    DONE = "done"


class SetupBack(Exception):
    """Return from a provider flow to provider selection."""


class SetupCancelled(Exception):
    """Cancel setup without treating it as an internal error."""


# ---------------------------------------------------------------------------
# Provider catalogue
# ---------------------------------------------------------------------------

PROVIDERS = BUILTIN_PROVIDERS + (
    ProviderDescriptor(
        "openai-compatible",
        "Custom endpoint",
        "Custom route",
        "Any OpenAI-compatible endpoint with a manual provider ID",
        "",
    ),
)
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
BROWSER_INSTALL_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class ModelProbe:
    models: tuple[str, ...] = ()
    error: str | None = None

    @property
    def verified(self) -> bool:
        return self.error is None


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _setup_console() -> Console:
    """Return a setup console that is friendly to pipes and screen readers."""

    return Console(
        no_color=_env_truthy("NO_COLOR") or _env_truthy("ASH_NO_COLOR"),
        soft_wrap=True,
    )


def _provider_status(config, descriptor: ProviderDescriptor) -> str:
    """Describe whether a catalog provider has enough local setup to try."""

    if descriptor.local:
        return "available to test"
    return "key detected" if descriptor.key_env and get_env_value(descriptor.key_env) else "needs key"


def _setup_status_payload(config) -> dict[str, Any]:
    """Return the secret-free setup inventory used by human and JSON output."""

    from ash.profiles import active_profile_name

    model = str(getattr(config, "model", "") or "")
    provider_id = model.split("/", 1)[0] if "/" in model else ""
    descriptor = get_provider_descriptor(provider_id) if provider_id else None
    workspace_root = getattr(config, "workspace_root", Path.cwd())
    if not isinstance(workspace_root, Path):
        workspace_root = Path.cwd()
    return {
        "profile": active_profile_name(),
        "model": model or None,
        "provider": {
            "id": provider_id or None,
            "name": descriptor.name if descriptor else provider_id or None,
            "category": descriptor.category if descriptor else None,
            "ready": _has_provider_configured(config),
        },
        "fallback_models": list(getattr(config, "fallback_models", []) or []),
        "capabilities": {
            "web_search": {
                "configured": _has_web_search_configured(config),
                "provider": str(getattr(config, "web_search_provider", "auto")),
            },
            "browser": {"installed": _browser_is_installed()},
            "mcp": {"configured": (workspace_root / ".mcp.json").is_file()},
            "memory": {"backend": str(getattr(config, "memory_backend", "auto"))},
            "sandbox": {"backend": str(getattr(config, "sandbox_backend", "auto"))},
        },
    }


def _render_setup_status(
    config,
    *,
    title: str = "Current setup",
    json_output: bool = False,
) -> None:
    """Render a bounded, secret-free setup summary."""

    payload = _setup_status_payload(config)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    console = _setup_console()
    profile = str(payload["profile"])
    model = str(payload["model"] or "not selected")
    provider = payload["provider"]
    provider_name = str(provider["name"] or "Not configured")
    provider_state = "ready to test" if provider["ready"] else "needs setup"
    fallback_count = len(payload["fallback_models"])
    capabilities = payload["capabilities"]
    optional = [
        (
            "Web search",
            "configured" if capabilities["web_search"]["configured"] else "not configured",
        ),
        (
            "Browser",
            "installed" if capabilities["browser"]["installed"] else "optional",
        ),
        (
            "MCP",
            "configured" if capabilities["mcp"]["configured"] else "none",
        ),
        ("Memory", str(capabilities["memory"]["backend"])),
        ("Sandbox", str(capabilities["sandbox"]["backend"])),
    ]
    console.print(Panel(
        f"[bold]{provider_name}[/bold]  [dim]{model}[/dim]\n"
        f"Profile: {profile}  •  Provider route: {provider_state}  •  "
        f"Fallbacks: {fallback_count}",
        title=title,
        border_style="cyan",
        padding=(0, 1),
    ))
    table = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
    table.add_column("Capability", style="bold")
    table.add_column("Status")
    for capability, status in optional:
        table.add_row(capability, status)
    console.print(table)


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

    from ash.config import AshConfig

    config = AshConfig.load()

    if non_interactive or not is_interactive_stdin():
        if section == "status":
            _render_setup_status(config, json_output=getattr(args, "json", False) is True)
            return SetupOutcome.SUCCESS
        if section == "browser":
            if _browser_is_installed():
                print("Playwright Chromium is installed.")
                return SetupOutcome.SUCCESS
            from ash.install import pipx_install_command

            print(
                "Error: browser automation is not installed. Install "
                f"it with `{pipx_install_command('browser')}`, then run "
                "`ash setup browser`.",
                file=sys.stderr,
            )
            return SetupOutcome.ERROR
        if section == "web":
            if _has_web_search_configured(config):
                print(f"Web search is configured for {config.web_search_provider}.")
                return SetupOutcome.SUCCESS
            print(
                "Error: web search needs BRAVE_SEARCH_API_KEY or TAVILY_API_KEY.",
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
    _render_setup_status(config, title="Before you begin")

    # Check for old ash.toml and offer migration
    _migrate_old_ash_toml()

    setup_result = SetupOutcome.SUCCESS
    if section == "model":
        setup_result = setup_model_provider(config, quick=quick)
    elif section == "providers":
        setup_result = setup_providers(config, quick=quick)
    elif section == "all":
        keep_existing = False
        if not quick and _has_provider_configured(config):
            choice = _prompt_choice(
                "An inference route is already configured",
                ["Keep current route", "Change provider or model", "Cancel setup"],
                default=0,
            )
            keep_existing = choice == 0
            if choice == 2:
                setup_result = SetupOutcome.CANCELLED
        if not keep_existing and setup_result == SetupOutcome.SUCCESS:
            setup_result = setup_model_provider(config, quick=quick)

    if setup_result != SetupOutcome.SUCCESS:
        print("Setup cancelled.", file=sys.stderr)
        return setup_result

    if section == "all" and quick:
        _print_info("QuickStart skipped optional web search and browser setup.")

    if section == "web" or (section == "all" and not quick):
        result = setup_web_search()
        if result != SetupOutcome.SUCCESS:
            print("Setup cancelled.", file=sys.stderr)
            return result

    if section == "browser":
        setup_result = setup_browser()
        if setup_result != SetupOutcome.SUCCESS:
            print("Setup cancelled.", file=sys.stderr)
            return setup_result

    try:
        config = AshConfig.load()
    except Exception:
        # The individual setup flow has already persisted its changes. A
        # summary failure must not turn a successful setup into a false error.
        pass
    _render_setup_status(config, title="Setup complete")
    _print_info("Setup complete!")
    return SetupOutcome.SUCCESS


def setup_model_provider(config, *, quick: bool = False) -> SetupOutcome:
    """Provider + model selection — shared entry point from wizard and REPL."""
    if quick and _has_provider_configured(config):
        model = str(getattr(config, "model", "") or "current model")
        _print_info(f"QuickStart reused {model}.")
        print("Run 'ash doctor --connect' to verify endpoint connectivity.")
        return SetupOutcome.SUCCESS
    return select_provider_and_model(config)


def _choose_provider_management_action() -> ProviderManagementAction:
    choice = _prompt_choice(
        prompt="Manage fallback models",
        options=["Add", "Replace", "Move", "Remove", "Clear", "Done"],
        default=5,
    )
    actions = (
        ProviderManagementAction.ADD,
        ProviderManagementAction.REPLACE,
        ProviderManagementAction.MOVE,
        ProviderManagementAction.REMOVE,
        ProviderManagementAction.CLEAR,
        ProviderManagementAction.DONE,
    )
    return actions[choice]


def _handle_add_fallback(config) -> SetupOutcome:
    fallbacks = list(getattr(config, "fallback_models", []) or [])
    while True:
        try:
            model = _prompt_fallback_model(config)
        except SetupBack:
            return SetupOutcome.SUCCESS
        except ValueError as exc:
            print(f"  Invalid fallback: {exc}")
            continue
        if model == getattr(config, "model", ""):
            print("  A fallback must differ from the primary model.")
            continue
        if model in fallbacks:
            print("  That model is already in the fallback chain.")
            continue
        fallbacks.append(model)
        _save_fallback_models(config, fallbacks)
        _print_info(f"Added fallback {model}.")
        return SetupOutcome.SUCCESS


def setup_providers(config, *, quick: bool = False) -> SetupOutcome:
    """Manage configured provider fallbacks."""
    del quick
    fallbacks = list(getattr(config, "fallback_models", []) or [])
    while True:
        _print_header("Provider Fallbacks")
        print(f"Primary: {config.model}")
        if fallbacks:
            print("Fallback chain (tried in order):")
            for index, model in enumerate(fallbacks, 1):
                print(f"  {index}. {model}")
        else:
            print("No fallback models configured.")
        print("\nChanges are saved after each successful action.\n")

        try:
            action = _choose_provider_management_action()
            if action is ProviderManagementAction.ADD:
                result = _handle_add_fallback(config)
                if result != SetupOutcome.SUCCESS:
                    return result
                fallbacks = list(getattr(config, "fallback_models", []) or [])
                continue
            elif action is ProviderManagementAction.REPLACE:
                selected_index = _prompt_fallback_index(fallbacks, "replace")
                if selected_index is not None:
                    try:
                        replacement = _prompt_fallback_model(config)
                    except ValueError as exc:
                        print(f"  Invalid fallback: {exc}")
                        continue
                    if replacement == config.model or replacement in fallbacks[:selected_index] + fallbacks[selected_index + 1 :]:
                        print("  Choose a model that is not the primary or another fallback.")
                        continue
                    fallbacks[selected_index] = replacement
                    _save_fallback_models(config, fallbacks)
                    _print_info(f"Replaced fallback {selected_index + 1} with {replacement}.")
                    continue
            elif action is ProviderManagementAction.MOVE:
                selected_index = _prompt_fallback_index(fallbacks, "move")
                if selected_index is not None:
                    position = _prompt_position(len(fallbacks))
                    if position is not None:
                        model = fallbacks.pop(selected_index)
                        fallbacks.insert(position, model)
                        _save_fallback_models(config, fallbacks)
                        _print_info(f"Moved {model} to position {position + 1}.")
                        continue
            elif action is ProviderManagementAction.REMOVE:
                selected_index = _prompt_fallback_index(fallbacks, "remove")
                if selected_index is not None:
                    removed = fallbacks.pop(selected_index)
                    _save_fallback_models(config, fallbacks)
                    _print_info(f"Removed fallback {removed}.")
                    continue
            elif action is ProviderManagementAction.CLEAR:
                if not fallbacks:
                    _print_info("Fallback chain is already empty.")
                else:
                    answer = _prompt_setup_text(
                        "  Clear every fallback? [y/N]: ", allow_empty=True
                    ).casefold()
                    if answer in {"y", "yes"}:
                        fallbacks.clear()
                        _save_fallback_models(config, fallbacks)
                        _print_info("Cleared the fallback chain.")
                    continue
            else:
                return SetupOutcome.SUCCESS
        except SetupBack:
            return SetupOutcome.SUCCESS
        except SetupCancelled:
            return SetupOutcome.CANCELLED


def _known_fallback_providers(config) -> set[str]:
    custom = getattr(config, "custom_providers", {})
    custom_ids = custom.keys() if isinstance(custom, dict) else ()
    return {descriptor.id for descriptor in PROVIDERS} | {
        str(provider).casefold() for provider in custom_ids
    }


def _prompt_fallback_model(config) -> str:
    """Ask for a canonical fallback model while showing the supported routes."""

    print("\n  Supported providers:")
    print("  " + ", ".join(descriptor.id for descriptor in PROVIDERS))
    custom = getattr(config, "custom_providers", {})
    if isinstance(custom, dict) and custom:
        print("  Custom: " + ", ".join(sorted(custom, key=str.casefold)))
    raw = _prompt_setup_text(
        "  Fallback model (provider/model, 'b' back, 'c' cancel): "
    )
    from ash.providers.identifiers import parse_model_string

    provider, model = parse_model_string(raw)
    if provider not in _known_fallback_providers(config):
        raise ValueError(
            f"unknown provider {provider!r}; choose a listed provider or configure a custom endpoint first"
        )
    return f"{provider}/{model}"


def _prompt_fallback_index(fallbacks: list[str], action: str) -> int | None:
    if not fallbacks:
        _print_info(f"Nothing to {action}; the fallback chain is empty.")
        return None
    choice = _prompt_choice(
        f"Select a fallback to {action}",
        [str(index) for index in range(1, len(fallbacks) + 1)],
        default=0,
    )
    return choice


def _prompt_position(length: int) -> int | None:
    raw = _prompt_setup_text(f"  New position (1-{length}): ")
    if not raw.isdigit() or not 1 <= int(raw) <= length:
        print(f"  Position must be a number from 1 to {length}.")
        return None
    return int(raw) - 1


def _save_fallback_models(config, fallbacks: list[str]) -> None:
    """Persist a fallback chain without dropping other user configuration."""

    user_config = load_config(strict=True)
    user_config["fallback_models"] = list(fallbacks)
    save_config(user_config)
    config.fallback_models = list(fallbacks)


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


def setup_browser() -> SetupOutcome:
    """Install the Chromium build pinned to the optional Playwright package."""

    _print_header("Browser Automation Setup")
    if importlib.util.find_spec("playwright") is None:
        from ash.install import pipx_install_command

        print(
            "  Playwright is not installed. Install Ash with the browser extra:\n"
            f"\n    {pipx_install_command('browser')}\n",
            file=sys.stderr,
        )
        return SetupOutcome.ERROR
    if _browser_is_installed():
        _print_info("Playwright Chromium is already installed.")
        return SetupOutcome.SUCCESS
    answer = _prompt_setup_text(
        "  Download the pinned Chromium build (several hundred MB)? [Y/n]: ",
        allow_empty=True,
    ).casefold()
    if answer in {"n", "no"}:
        return SetupOutcome.CANCELLED
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=False,
            timeout=BROWSER_INSTALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(
            "  Chromium installation timed out after "
            f"{BROWSER_INSTALL_TIMEOUT_SECONDS} seconds. "
            "Retry setup when the network is available.",
            file=sys.stderr,
        )
        return SetupOutcome.ERROR
    if completed.returncode != 0:
        print(
            "  Chromium installation failed. On supported Linux distributions, "
            "install required system libraries with `playwright install-deps "
            "chromium`, then retry.",
            file=sys.stderr,
        )
        return SetupOutcome.ERROR
    if not _browser_is_installed():
        print(
            "  Playwright completed without reporting an installed Chromium build.",
            file=sys.stderr,
        )
        return SetupOutcome.ERROR
    _print_info("Playwright Chromium is installed.")
    return SetupOutcome.SUCCESS


def _browser_is_installed() -> bool:
    if importlib.util.find_spec("playwright") is None:
        return False
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = (completed.stdout + completed.stderr).casefold()
    return completed.returncode == 0 and "chromium" in output


# ---------------------------------------------------------------------------
# Provider + model selection (shared with REPL /model command)
# ---------------------------------------------------------------------------


def select_provider_and_model(config) -> SetupOutcome:
    """Show provider list, route to provider flow, verify model."""
    while True:
        _print_header("Select your inference provider")
        print("Choose a provider. Existing keys and local runtimes are marked:\n")
        table = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
        table.add_column("#", justify="right")
        table.add_column("Provider", style="bold")
        table.add_column("Route")
        table.add_column("Setup")
        for i, descriptor in enumerate(PROVIDERS, 1):
            table.add_row(
                str(i),
                descriptor.name,
                descriptor.category,
                _provider_status(config, descriptor),
            )
        _setup_console().print(table)

        try:
            choice = _prompt_choice(
                "Enter a number",
                [str(i) for i in range(1, len(PROVIDERS) + 1)],
                default=0,
            )
            descriptor = PROVIDERS[choice]
            provider_id = descriptor.id
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
            if provider_id == "openai-compatible":
                return _flow_openai_compatible()
            return _flow_openai_compatible_builtin(descriptor, current)
        except SetupBack:
            print("  Returning to provider selection.")
        except SetupCancelled:
            return SetupOutcome.CANCELLED


# ---------------------------------------------------------------------------
# Provider flows
# ---------------------------------------------------------------------------


def _flow_openai_compatible_builtin(
    descriptor: ProviderDescriptor,
    current: str,
) -> SetupOutcome:
    """Configure a catalog provider that speaks the OpenAI wire protocol."""

    _print_header(f"{descriptor.name} Configuration")
    api_key = ""
    if descriptor.key_env is not None:
        api_key = _prompt_api_key(descriptor.key_env, f"{descriptor.name} API key")

    base_env = f"{descriptor.id.upper().replace('-', '_')}_API_BASE"
    base_url_override = _prompt_optional_url(base_env, descriptor.base_url)
    base_url = base_url_override or descriptor.base_url
    models, verified = _discover_models(
        descriptor.name,
        lambda: _probe_models_detailed(base_url, api_key or None),
        fallback=[current] if current else [],
        guidance=(
            "Start the local runtime and load a model, then retry."
            if descriptor.local
            else "Check the API key and endpoint, then retry."
        ),
    )
    model = _prompt_model_list(models, current)
    _confirm_undiscovered_model(model, models, verified)

    settings: dict[str, str] = {"ASH_MODEL": f"{descriptor.id}/{model}"}
    if descriptor.key_env is not None:
        settings[descriptor.key_env] = api_key
    if base_url_override:
        settings[base_env] = base_url_override
    save_env_values(settings)
    _print_verification_status(verified)
    return SetupOutcome.SUCCESS


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
    """Groq setup: API key, optional base URL override, model selection."""
    _print_header("Groq Configuration")

    api_key = _prompt_api_key("GROQ_API_KEY", "Groq API key")
    base_url_override = _prompt_optional_url(
        "GROQ_API_BASE",
        "https://api.groq.com/openai/v1",
    )
    base_url = base_url_override or "https://api.groq.com/openai/v1"

    models, verified = _discover_models(
        "Groq",
        lambda: _probe_models_detailed(base_url, api_key),
        fallback=[current] if current else [],
    )
    model = _prompt_model_list(models, current)
    _confirm_undiscovered_model(model, models, verified)

    settings = {
        "GROQ_API_KEY": api_key,
        "ASH_MODEL": f"groq/{model}",
    }
    if base_url_override:
        settings["GROQ_API_BASE"] = base_url_override
    save_env_values(settings)
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
    if not _PROVIDER_NAME.fullmatch(name) or name.casefold() in {
        item.id for item in PROVIDERS
    }:
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
    custom_provider = {
        "base_url": base_url,
        "models": models,
        "auth_mode": "bearer" if api_key else "none",
    }
    if api_key:
        custom_provider["key_env"] = key_env
    custom[name] = custom_provider
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
    from ash.providers.readiness import (
        ProviderVerificationError,
        probe_model_catalog,
    )

    endpoint = f"{base_url.rstrip('/')}/models"
    try:
        models = probe_model_catalog(
            endpoint,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            catalog_format="anthropic",
            timeout=10,
        )
    except ProviderVerificationError as exc:
        return ModelProbe(error=str(exc))
    return ModelProbe(models=models)


def _probe_anthropic_models(api_key: str) -> list[str]:
    """Call Anthropic's model-list endpoint and return newest-first IDs."""
    return list(_probe_anthropic_models_detailed(api_key).models)


def _probe_models_detailed(base_url: str, api_key: Optional[str]) -> ModelProbe:
    from ash.providers.readiness import (
        ProviderVerificationError,
        probe_model_catalog,
    )

    endpoint = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        models = probe_model_catalog(
            endpoint,
            headers=headers,
            catalog_format="openai",
            timeout=10,
        )
    except ProviderVerificationError as exc:
        return ModelProbe(error=str(exc))
    return ModelProbe(models=models)


def _probe_models(base_url: str, api_key: Optional[str]) -> list[str]:
    """Call the /models endpoint of an OpenAI-compatible API. Returns model IDs."""
    return list(_probe_models_detailed(base_url, api_key).models)


def _probe_ollama_models_detailed(base_url: str) -> ModelProbe:
    from ash.providers.readiness import (
        ProviderVerificationError,
        probe_model_catalog,
    )

    endpoint = f"{base_url.rstrip('/')}/api/tags"
    try:
        models = probe_model_catalog(
            endpoint,
            headers={},
            catalog_format="ollama",
            timeout=10,
        )
    except ProviderVerificationError as exc:
        return ModelProbe(error=str(exc))
    return ModelProbe(models=models)


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
    """Return whether the selected model can be assembled by the runtime."""

    from ash.providers.readiness import (
        ProviderConfigurationError,
        resolve_provider_connection,
    )

    try:
        resolve_provider_connection(config)
    except (ProviderConfigurationError, ValueError, AttributeError, TypeError):
        return False
    return True


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

    from ash.config import AshConfig, CURRENT_CONFIG_SCHEMA_VERSION

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
    _setup_console().print(
        Panel(
            title,
            border_style="cyan",
            padding=(0, 1),
        )
    )


def _print_info(msg: str) -> None:
    _setup_console().print(f"[green]✓[/green] {msg}")
