"""Interactive setup wizard for Ash.

Handles provider + model selection, API key collection, and credential
storage to ~/.ash/.env and ~/.ash/ash.toml.
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum, IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlsplit

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ash.config import (
    CURRENT_CONFIG_SCHEMA_VERSION,
    PROJECT_CONFIG_FIELDS,
    PROJECT_MODEL_PROVIDERS,
)
from ash.commands.config import (
    backup_config_file,
    get_env_value,
    get_config_path,
    is_config_migration_recorded,
    is_interactive_stdin,
    load_config,
    mask_key,
    MAX_CONFIG_FILE_BYTES,
    mutate_config,
    record_config_migration,
    replace_config_if_current,
    save_env_values,
)
from ash.provider_catalog import (
    BUILTIN_PROVIDERS,
    ProviderDescriptor,
    get_provider_descriptor,
)
from ash.safe_io import read_bounded_bytes
from ash.safety.browser_process import run_browser_subprocess
from ash.safety.trust import is_workspace_trusted
from ash.ui.safe_text import terminal_safe_text

if TYPE_CHECKING:
    from ash.providers.readiness import CatalogFormat


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
_PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
BROWSER_INSTALL_TIMEOUT_SECONDS = 300
SETUP_MODEL_PREVIEW_LIMIT = 18


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


def _setup_status_text(label: str) -> Text:
    normalized = label.casefold()
    if any(token in normalized for token in ("ready", "detected", "signed in", "configured")):
        return Text(label, style="green")
    if any(token in normalized for token in ("needs", "required", "disabled")):
        return Text(label, style="yellow")
    if "local" in normalized or "available" in normalized:
        return Text(label, style="cyan")
    if "manual" in normalized:
        return Text(label, style="cyan")
    return Text(label, style="dim")


def _print_setup_banner() -> None:
    console = _setup_console()
    title = Text()
    title.append("ASH", style="bold cyan")
    title.append("  /  SETUP", style="bold")
    body = Text(
        "Configure the model route and the capabilities Ash can use in this profile."
    )
    body.append("\n")
    body.append(
        "Provider → model → optional capabilities → verification",
        style="dim",
    )
    console.print(
        Panel(
            body,
            title=title,
            border_style="bright_black",
            padding=(1, 2),
        )
    )


def _provider_status(config, descriptor: ProviderDescriptor) -> str:
    """Describe whether a catalog provider has enough local setup to try."""

    if descriptor.id == "openai-compatible":
        return "manual setup"
    if descriptor.local:
        return "available to test"
    if descriptor.id == "vertex":
        project = (
            str(getattr(config, "vertex_project", "") or "").strip()
            or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        )
        location = (
            str(getattr(config, "vertex_location", "") or "").strip()
            or os.environ.get("GOOGLE_CLOUD_LOCATION", "").strip()
        )
        return (
            "ADC scope configured"
            if project and location
            else "needs project/location"
        )
    if descriptor.id == "bedrock":
        region = (
            str(getattr(config, "bedrock_region", "") or "").strip()
            or os.environ.get("AWS_REGION", "").strip()
            or os.environ.get("AWS_DEFAULT_REGION", "").strip()
        )
        return "AWS scope configured" if region else "needs AWS region"
    if descriptor.id == "openai" and getattr(config, "openai_auth_mode", "") == "chatgpt":
        from ash.providers.openai_chatgpt_auth import ChatGPTCredentialStore

        state = ChatGPTCredentialStore().credential_state()
        if state in {"usable", "refreshable"}:
            return "ChatGPT signed in"
        if state == "plan_disabled":
            return "ChatGPT plan permission needed"
        return "ChatGPT sign-in needed"
    return (
        "key detected"
        if any(get_env_value(name) for name in descriptor.key_envs)
        else "needs key"
    )


def _setup_status_payload(config) -> dict[str, Any]:
    """Return the secret-free setup inventory used by human and JSON output."""

    from ash.observability import observability_status
    from ash.profiles import active_profile_name

    model = str(getattr(config, "model", "") or "")
    provider_id = model.split("/", 1)[0] if "/" in model else ""
    descriptor = get_provider_descriptor(provider_id) if provider_id else None
    workspace_root = getattr(config, "workspace_root", Path.cwd())
    if not isinstance(workspace_root, Path):
        workspace_root = Path.cwd()
    telemetry = observability_status(config)
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
            "observability": {
                "enabled": telemetry.enabled,
                "available": telemetry.available,
                "ready": bool(
                    telemetry.enabled
                    and telemetry.available
                    and telemetry.traces_endpoint
                    and telemetry.metrics_endpoint
                ),
                "sample_rate": telemetry.sample_rate,
                "content_capture": telemetry.content_capture,
            },
            "memory": {"backend": str(getattr(config, "memory_backend", "sqlite"))},
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
    observability = capabilities["observability"]
    if not observability["enabled"]:
        observability_status_text = "off"
        observability_next = "set ASH_OBSERVABILITY_ENABLED=true"
    elif not observability["available"]:
        observability_status_text = "needs extra"
        observability_next = "install observability extra"
    elif not observability["ready"]:
        observability_status_text = "needs endpoint"
        observability_next = "set ASH_OBSERVABILITY_OTLP_ENDPOINT"
    else:
        observability_status_text = "ready"
        observability_next = ""
    optional = [
        (
            "Web search",
            "configured" if capabilities["web_search"]["configured"] else "not configured",
            "" if capabilities["web_search"]["configured"] else "ash setup web",
        ),
        (
            "Browser",
            "installed" if capabilities["browser"]["installed"] else "optional",
            "" if capabilities["browser"]["installed"] else "ash setup browser",
        ),
        (
            "MCP",
            "configured" if capabilities["mcp"]["configured"] else "none",
            "" if capabilities["mcp"]["configured"] else "ash mcp add …",
        ),
        (
            "Observability",
            observability_status_text,
            observability_next,
        ),
        ("Memory", str(capabilities["memory"]["backend"]), ""),
        ("Sandbox", str(capabilities["sandbox"]["backend"]), ""),
    ]
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="dim", no_wrap=True)
    summary.add_column()
    summary.add_row("Profile", terminal_safe_text(profile, single_line=True))
    summary.add_row(
        "Provider",
        Text(terminal_safe_text(provider_name, single_line=True), style="bold"),
    )
    summary.add_row("Model", terminal_safe_text(model, single_line=True))
    summary.add_row("Route", _setup_status_text(provider_state))
    summary.add_row("Fallbacks", str(fallback_count))
    console.print(
        Panel(
            summary,
            title=terminal_safe_text(title, single_line=True),
            border_style="bright_black",
            padding=(1, 2),
        )
    )
    table = Table(
        show_header=True,
        header_style="bold",
        box=box.SIMPLE,
        pad_edge=False,
    )
    table.add_column("Capability", style="bold")
    table.add_column("Status")
    show_next = console.width >= 68
    if show_next:
        table.add_column("Next", style="dim")
    for capability, status, next_step in optional:
        cells: list[Any] = [
            Text(terminal_safe_text(capability, single_line=True), style="bold"),
            _setup_status_text(terminal_safe_text(status, single_line=True)),
        ]
        if show_next:
            cells.append(terminal_safe_text(next_step or "—", single_line=True))
        table.add_row(*cells)
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
                print(
                    "Web search is configured for "
                    f"{terminal_safe_text(str(config.web_search_provider), single_line=True)}."
                )
                return SetupOutcome.SUCCESS
            print(
                "Error: web search needs BRAVE_SEARCH_API_KEY or TAVILY_API_KEY.",
                file=sys.stderr,
            )
            return SetupOutcome.ERROR
        if _has_provider_configured(config):
            print(
                "Ash is configured for "
                f"{terminal_safe_text(str(config.model), single_line=True)}."
            )
            print("Run 'ash doctor --connect' to verify endpoint connectivity.")
            return SetupOutcome.SUCCESS
        print("Error: ash setup requires an interactive terminal.", file=sys.stderr)
        print(
            "Set ASH_MODEL and its provider API key, configure Ollama, or rerun in a TTY.",
            file=sys.stderr,
        )
        return SetupOutcome.ERROR

    # Banner
    _print_setup_banner()
    _render_setup_status(config, title="Before you begin")

    # Check for old ash.toml and offer migration
    _migrate_old_ash_toml()
    config = AshConfig.load()

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
        print(
            "Primary: "
            f"{terminal_safe_text(str(config.model), single_line=True)}"
        )
        if fallbacks:
            print("Fallback chain (tried in order):")
            for index, model in enumerate(fallbacks, 1):
                print(f"  {index}. {terminal_safe_text(model, single_line=True)}")
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
        print(
            "  Custom: "
            + ", ".join(
                terminal_safe_text(str(provider), single_line=True)
                for provider in sorted(custom, key=str.casefold)
            )
        )
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
    if not raw.isdigit():
        print(f"  Position must be a number from 1 to {length}.")
        return None
    try:
        position = int(raw)
    except ValueError:
        print(f"  Position must be a number from 1 to {length}.")
        return None
    if not 1 <= position <= length:
        print(f"  Position must be a number from 1 to {length}.")
        return None
    return position - 1


def _save_fallback_models(config, fallbacks: list[str]) -> None:
    """Persist a fallback chain without dropping other user configuration."""

    mutate_config(
        lambda user_config: user_config.__setitem__(
            "fallback_models", list(fallbacks)
        )
    )
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
        completed = run_browser_subprocess(
            [sys.executable, "-I", "-m", "playwright", "install", "chromium"],
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
        completed = run_browser_subprocess(
            [sys.executable, "-I", "-m", "playwright", "install", "--list"],
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
        print(
            "Ash supports the routes below directly. "
            "Choose by number, provider name, or provider ID.\n"
        )

        try:
            descriptor = _prompt_provider(config)
            provider_id = descriptor.id
            current = _get_current_model_for_provider(config, provider_id)
            if provider_id == "anthropic":
                return _flow_anthropic(current)
            if provider_id == "openai":
                return _flow_openai(current, config)
            if provider_id == "deepseek":
                return _flow_deepseek(current)
            if provider_id == "groq":
                return _flow_groq(current)
            if provider_id == "vertex":
                return _flow_vertex(current, config)
            if provider_id == "bedrock":
                return _flow_bedrock(current, config)
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
        api_key = _prompt_api_key(
            descriptor.key_env,
            f"{descriptor.name} API key",
            descriptor.key_env_aliases,
        )

    base_env = f"{descriptor.id.upper().replace('-', '_')}_API_BASE"
    base_url_override = _prompt_optional_url(base_env, descriptor.base_url)
    base_url = base_url_override or descriptor.base_url
    _require_secure_provider_transport(base_url, descriptor.id, api_key)
    from ash.providers.readiness import (
        GOOGLE_API_CLIENT_HEADER,
        builtin_provider_catalog_format,
    )

    catalog_format = builtin_provider_catalog_format(descriptor.id)
    extra_headers: Mapping[str, str] | None = None
    if descriptor.id == "google":
        extra_headers = {"x-goog-api-client": GOOGLE_API_CLIENT_HEADER}
    models, verified = _discover_models(
        descriptor.name,
        lambda: _probe_models_detailed(
            base_url,
            api_key or None,
            catalog_format=catalog_format,
            extra_headers=extra_headers,
        ),
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
    _require_secure_provider_transport(base_url, "anthropic", api_key)

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


def _flow_openai(current: str, config) -> SetupOutcome:
    """OpenAI setup: ChatGPT plan sign-in or API key, then model selection."""
    _print_header("OpenAI Configuration")

    current_mode = str(getattr(config, "openai_auth_mode", "api_key"))
    default = 0 if current_mode == "chatgpt" else 1
    auth_choice = _prompt_choice(
        "Choose OpenAI authentication",
        [
            "Continue with ChatGPT (use eligible ChatGPT plan)",
            "OpenAI API key",
        ],
        default=default,
    )
    if auth_choice == 0:
        from ash.providers.openai_chatgpt_auth import (
            ChatGPTAuthError,
            ChatGPTAuthManager,
            ChatGPTCredentialStore,
        )

        manager = ChatGPTAuthManager()
        store = ChatGPTCredentialStore()
        account = store.active()
        if account is None or not account.signed_in or not account.plan_enabled:
            print(
                "  Continue with ChatGPT will open your browser. "
                "Eligible ChatGPT plans can authorize Ash for inference."
            )
            try:
                account = asyncio.run(
                    manager.login(
                        client_id=(
                            account.client_id
                            if account is not None
                            else None
                        )
                    )
                )
            except ChatGPTAuthError as exc:
                print(
                    "  ChatGPT sign-in failed: "
                    + terminal_safe_text(str(exc), single_line=True)
                )
                return SetupOutcome.ERROR
        if not account.plan_enabled:
            print(
                "  ChatGPT sign-in completed, but plan usage was not authorized. "
                "The registration was retained without switching Ash inference."
            )
            return SetupOutcome.ERROR
        try:
            catalog = asyncio.run(manager.list_models())
        except ChatGPTAuthError as exc:
            print(
                "  Could not load ChatGPT-plan models: "
                + terminal_safe_text(str(exc), single_line=True)
            )
            return SetupOutcome.ERROR
        models = [slug for slug, _display_name in catalog]
        selected = _prompt_model_list(models, current)
        save_env_values(
            {
                "ASH_OPENAI_AUTH_MODE": "chatgpt",
                "ASH_MODEL": f"openai/{selected}",
            }
        )
        _print_info(
            "Configured OpenAI to use the selected ChatGPT account and plan."
        )
        return SetupOutcome.SUCCESS

    api_key = _prompt_api_key("OPENAI_API_KEY", "OpenAI API key")

    base_url_override = _prompt_optional_url(
        "OPENAI_API_BASE",
        "https://api.openai.com/v1",
    )
    base_url = base_url_override or "https://api.openai.com/v1"
    _require_secure_provider_transport(base_url, "openai", api_key)
    models, verified = _discover_models(
        "OpenAI",
        lambda: _probe_models_detailed(base_url, api_key),
        fallback=[current] if current else [],
    )
    model = _prompt_model_list(models, current)
    _confirm_undiscovered_model(model, models, verified)

    settings = {
        "OPENAI_API_KEY": api_key,
        "ASH_OPENAI_AUTH_MODE": "api_key",
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
    _require_secure_provider_transport(base_url, "deepseek", api_key)
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
    _require_secure_provider_transport(base_url, "groq", api_key)

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


def _flow_vertex(current: str, config) -> SetupOutcome:
    """Vertex setup: explicit project/location, ADC verification, manual model ID."""

    _print_header("Google Vertex AI Configuration")
    if importlib.util.find_spec("google.auth") is None:
        from ash.install import pipx_install_command

        print(
            "  Vertex AI support is not installed. Install the GCP extra:\n"
            f"\n    {pipx_install_command('gcp')}\n",
            file=sys.stderr,
        )
        return SetupOutcome.ERROR

    current_project = (
        str(getattr(config, "vertex_project", "") or "").strip()
        or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    )
    current_location = (
        str(getattr(config, "vertex_location", "") or "").strip()
        or os.environ.get("GOOGLE_CLOUD_LOCATION", "").strip()
    )
    project = _prompt_setup_text(
        f"  Google Cloud project{f' [{current_project}]' if current_project else ''}: ",
        allow_empty=bool(current_project),
    )
    project = project or current_project
    location = _prompt_setup_text(
        f"  Vertex location{f' [{current_location}]' if current_location else ''}: ",
        allow_empty=bool(current_location),
    )
    location = location or current_location

    from ash.providers.vertex import (
        GoogleAdcTokenProvider,
        VertexBackendUnavailable,
        VertexCredentialError,
        vertex_openai_base_url,
    )

    try:
        vertex_openai_base_url(project, location)
        asyncio.run(GoogleAdcTokenProvider()())
    except (ValueError, VertexBackendUnavailable, VertexCredentialError) as exc:
        print(
            "  Vertex ADC verification failed: "
            + terminal_safe_text(str(exc), single_line=True)
        )
        return SetupOutcome.ERROR

    prompt = "  Vertex model ID"
    if current:
        prompt += f" [{terminal_safe_text(current, single_line=True)}]"
    model = _prompt_setup_text(prompt + ": ", allow_empty=bool(current)) or current
    if not model:
        print("  Vertex model ID is required.")
        return SetupOutcome.ERROR

    save_env_values(
        {
            "ASH_VERTEX_PROJECT": project,
            "ASH_VERTEX_LOCATION": location,
            "ASH_MODEL": f"vertex/{model}",
        }
    )
    _print_info(
        "Verified Google Application Default Credentials. "
        "Run ash providers test to verify the selected model deployment."
    )
    return SetupOutcome.SUCCESS


def _flow_bedrock(current: str, config) -> SetupOutcome:
    """Bedrock setup: explicit region, AWS credential chain, native discovery."""

    _print_header("Amazon Bedrock Configuration")
    current_region = (
        str(getattr(config, "bedrock_region", "") or "").strip()
        or os.environ.get("AWS_REGION", "").strip()
        or os.environ.get("AWS_DEFAULT_REGION", "").strip()
    )
    current_profile = (
        str(getattr(config, "bedrock_profile", "") or "").strip()
        or os.environ.get("AWS_PROFILE", "").strip()
    )
    region = _prompt_setup_text(
        f"  AWS Region{f' [{current_region}]' if current_region else ''}: ",
        allow_empty=bool(current_region),
    )
    region = region or current_region
    profile = _prompt_setup_text(
        f"  AWS profile (optional){f' [{current_profile}]' if current_profile else ''}: ",
        allow_empty=True,
    )
    profile = profile or current_profile

    models, verified = _discover_models(
        "Amazon Bedrock candidate discovery",
        lambda: _probe_bedrock_models_detailed(region, profile),
        fallback=[current] if current else [],
        guidance=(
            "Install the Ash aws extra and configure an AWS IAM identity with "
            "Bedrock discovery/inference permissions."
        ),
    )
    model = _prompt_model_list(models, current)
    _confirm_undiscovered_model(model, models, verified)

    try:
        from ash.providers.bedrock import BedrockProvider

        provider = BedrockProvider(
            model,
            region=region,
            profile=profile,
        )
        asyncio.run(provider.aclose())
    except (ImportError, ValueError, RuntimeError) as exc:
        print(
            "  Bedrock Runtime provider is unavailable: "
            + terminal_safe_text(str(exc), single_line=True)
        )
        return SetupOutcome.ERROR

    settings = {
        "ASH_BEDROCK_REGION": region,
        "ASH_MODEL": f"bedrock/{model}",
    }
    if profile:
        settings["ASH_BEDROCK_PROFILE"] = profile
    save_env_values(settings)
    _print_verification_status(verified)
    _print_info(
        "Bedrock discovery returns candidate IDs; run ash providers test to "
        "verify that the selected ID supports Runtime Chat Completions."
    )
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
    print(
        "  Configured Ollama with model: "
        f"{terminal_safe_text(model, single_line=True)}"
    )
    _print_verification_status(verified)
    return SetupOutcome.SUCCESS


def _flow_openai_compatible() -> SetupOutcome:
    """OpenAI-compatible custom endpoint: base URL, optional API key, model selection.

    Saves to ~/.ash/ash.toml under [custom_providers.<name>].
    """
    _print_header("OpenAI-Compatible Endpoint")

    name = _prompt_setup_text("  Provider name (e.g. my-minimax): ").casefold()
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
    _require_secure_provider_transport(base_url, name, api_key)
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
        print(f"    [{i}] {terminal_safe_text(m, single_line=True)}")

    model = _prompt_setup_text("\n  Select or type a model name: ")

    # If user picked a number, resolve to model name
    if model.isdigit():
        try:
            idx = int(model) - 1
        except ValueError:
            print("Invalid selection.")
            raise SetupBack from None
        if 0 <= idx < len(models):
            model = models[idx]
        else:
            print("Invalid selection.")
            raise SetupBack
    _confirm_undiscovered_model(model, models, verified)

    # Update custom_providers without replacing unrelated user configuration.
    custom_provider = {
        "base_url": base_url,
        "models": models,
        "auth_mode": "bearer" if api_key else "none",
    }
    if api_key:
        custom_provider["key_env"] = key_env

    def save_custom_provider(user_config: dict[str, Any]) -> None:
        custom = user_config.get("custom_providers", {})
        if not isinstance(custom, dict):
            raise ValueError("custom_providers must be a TOML table")
        custom[name] = custom_provider
        user_config["custom_providers"] = custom

    mutate_config(save_custom_provider)

    settings = {"ASH_MODEL": f"{name}/{model}"}
    if api_key:
        settings[key_env] = api_key
    save_env_values(settings)
    print(
        "\n  Saved custom provider "
        f"'{terminal_safe_text(name, single_line=True)}' to "
        f"{terminal_safe_text(str(get_config_path()), single_line=True)}"
    )
    print(f"  Model: {terminal_safe_text(model, single_line=True)}")
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
        provider_catalog_endpoint,
    )

    endpoint = provider_catalog_endpoint(base_url.rstrip("/"), "anthropic")
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


def _probe_models_detailed(
    base_url: str,
    api_key: Optional[str],
    *,
    catalog_format: CatalogFormat = "openai",
    extra_headers: Mapping[str, str] | None = None,
) -> ModelProbe:
    from ash.providers.readiness import (
        ProviderVerificationError,
        probe_model_catalog,
        provider_catalog_endpoint,
    )

    endpoint = provider_catalog_endpoint(base_url.rstrip("/"), catalog_format)
    headers = dict(extra_headers or {})
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        models = probe_model_catalog(
            endpoint,
            headers=headers,
            catalog_format=catalog_format,
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


def _probe_bedrock_models_detailed(region: str, profile: str = "") -> ModelProbe:
    from ash.providers.bedrock import (
        BedrockBackendUnavailable,
        BedrockDiscoveryError,
        discover_bedrock_models,
    )

    try:
        models = discover_bedrock_models(region=region, profile=profile)
    except (ValueError, BedrockBackendUnavailable, BedrockDiscoveryError) as exc:
        return ModelProbe(error=str(exc))
    return ModelProbe(models=models)


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
                "  Verified "
                f"{terminal_safe_text(provider_name, single_line=True)}; discovered "
                f"{len(result.models)} model(s)."
            )
            return list(result.models), True
        print(
            "  Could not verify "
            f"{terminal_safe_text(provider_name, single_line=True)}: "
            f"{terminal_safe_text(result.error or 'unknown error')}"
        )
        if guidance:
            print(f"  {terminal_safe_text(guidance)}")
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

    if (
        getattr(config, "provider", "") == "openai"
        and getattr(config, "openai_auth_mode", "") == "chatgpt"
    ):
        from ash.providers.openai_chatgpt_auth import ChatGPTCredentialStore

        return ChatGPTCredentialStore().credential_state() in {
            "usable",
            "refreshable",
        }

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

        legacy_snapshot = read_bounded_bytes(
            old_path,
            MAX_CONFIG_FILE_BYTES,
            label="legacy TOML config",
        )
        old_config = tomllib.loads(legacy_snapshot.decode("utf-8"))
    except Exception:
        return

    if not isinstance(old_config, dict) or not (_LEGACY_MARKERS & old_config.keys()):
        return
    if is_config_migration_recorded(old_path):
        return
    if not is_workspace_trusted(old_path.parent):
        print(
            "  Legacy ash.toml migration skipped because the workspace is not trusted. "
            "Review it and run `ash trust add` before migrating safe settings."
        )
        return

    _print_header("Old Configuration Found")
    print(f"  Found ash.toml in {old_path.parent}")
    print("  This old format has been replaced by ~/.ash/.env")
    resp = input("\n  Migrate settings now? [Y/n] ").strip().lower()
    if resp in ("n", "no"):
        return
    current_snapshot = read_bounded_bytes(
        old_path,
        MAX_CONFIG_FILE_BYTES,
        label="legacy TOML config",
    )
    if current_snapshot != legacy_snapshot:
        raise ValueError(
            f"legacy configuration changed while migration was pending: {old_path}"
        )
    legacy_snapshot_digest = hashlib.sha256(legacy_snapshot).hexdigest()

    user_config = load_config(strict=True)
    config_updates, env_updates, preserved, skipped = _plan_legacy_config_migration(
        old_config,
        old_path=old_path,
        user_config=user_config,
    )
    from ash.config import AshConfig

    validation_values = dict(config_updates)
    if "model" not in validation_values and "ASH_MODEL" in env_updates:
        validation_values["model"] = env_updates["ASH_MODEL"]
    try:
        AshConfig.validate_persisted_values(validation_values)
    except ValueError as exc:
        raise ValueError(
            f"legacy configuration migration would create invalid settings: {exc}"
        ) from exc

    source_backup = backup_config_file(
        old_path,
        label="legacy-project-ash.toml",
        expected_sha256=legacy_snapshot_digest,
    )
    destination_path = get_config_path()
    destination_backup: Path | None = None
    if config_updates != user_config and destination_path.is_file():
        destination_backup = backup_config_file(
            destination_path,
            label="user-ash.toml-pre-migration",
        )
    if config_updates != user_config:
        replace_config_if_current(user_config, config_updates)
    if env_updates:
        save_env_values(env_updates)
    record_config_migration(
        old_path,
        source_backup,
        source_sha256=legacy_snapshot_digest,
    )

    print("\n  Migration complete.")
    print(f"  Legacy backup: {source_backup}")
    if destination_backup is not None:
        print(f"  Previous user config backup: {destination_backup}")
    if preserved:
        print("  Preserved existing destination values: " + ", ".join(preserved))
    if skipped:
        shown = ", ".join(skipped[:20])
        suffix = f", and {len(skipped) - 20} more" if len(skipped) > 20 else ""
        print(
            "  Not migrated (user-owned or unsupported legacy fields): "
            + shown
            + suffix
        )
    print(
        "  Old ash.toml was left in place and will not be prompted again unless changed."
    )


def _plan_legacy_config_migration(
    old_config: dict[str, object],
    *,
    old_path: Path,
    user_config: dict[str, object],
) -> tuple[dict[str, object], dict[str, str], list[str], list[str]]:
    """Map legacy values without replacing newer destination settings."""

    merged = dict(user_config)
    env_updates: dict[str, str] = {}
    preserved: list[str] = []
    skipped: list[str] = []
    for field, value in old_config.items():
        if field in _LEGACY_RESERVED:
            if field == "api_key":
                skipped.append(field)
            continue
        if field not in PROJECT_CONFIG_FIELDS:
            skipped.append(field)
            continue
        normalized = value
        if field in merged:
            preserved.append(field)
        else:
            merged[field] = normalized
    merged.setdefault("config_schema_version", CURRENT_CONFIG_SCHEMA_VERSION)

    provider = str(old_config.get("provider") or "anthropic").strip().casefold()
    model_name = str(old_config.get("model_name") or "").strip()
    if model_name and provider in PROJECT_MODEL_PROVIDERS:
        _preserve_or_stage_env(
            "ASH_MODEL",
            f"{provider}/{model_name}",
            env_updates,
            preserved,
        )
    elif model_name:
        skipped.append("model_name")
        if "provider" in old_config:
            skipped.append("provider")
    api_key = str(old_config.get("api_key") or "").strip()
    key_name = _LEGACY_PROVIDER_KEYS.get(provider)
    if api_key and key_name and get_env_value(key_name) is not None:
        preserved.append(key_name)
    return (
        merged,
        env_updates,
        sorted(set(preserved)),
        sorted(set(skipped)),
    )


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


# ---------------------------------------------------------------------------
# Interactive prompt helpers
# ---------------------------------------------------------------------------


def _prompt_api_key(
    env_var: str,
    desc: str,
    env_var_fallbacks: tuple[str, ...] = (),
) -> str:
    """Prompt for an API key, with blank input returning to provider selection."""
    # Check existing env
    existing = None
    existing_env = env_var
    for candidate in (env_var, *env_var_fallbacks):
        existing = get_env_value(candidate)
        if existing:
            existing_env = candidate
            break
    if existing:
        print(f"  Found existing {desc}: {mask_key(existing_env)}")
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
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("port must be an integer between 1 and 65535") from exc
    if port is not None and not 0 < port <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    if parsed.username or parsed.password:
        raise ValueError("embedded credentials are not allowed")
    if parsed.query or parsed.fragment:
        raise ValueError("query strings and fragments are not allowed")
    return value.strip().rstrip("/")


def _require_secure_provider_transport(
    base_url: str,
    provider: str,
    api_key: str,
) -> None:
    if not api_key:
        return
    from ash.providers.readiness import require_secure_provider_transport

    require_secure_provider_transport(base_url, provider=provider)


def _prompt_model_list(models: list[str], current: str) -> str:
    """Show models and return a selection, or raise a navigation signal."""
    visible = list(models)

    while True:
        _render_model_list(models, visible, current)
        val = input(
            "\n  Model › "
        ).strip()
        if not val:
            if current and current in models:
                return current
            continue
        if val.casefold() in ("c", "q", "cancel", "quit"):
            raise SetupCancelled
        if val.casefold() in ("b", "back"):
            raise SetupBack
        if val.casefold() in ("all", "*"):
            visible = list(models)
            continue
        if val.startswith("/"):
            query = val[1:].strip().casefold()
            if not query:
                visible = list(models)
                continue
            visible = [model for model in models if query in model.casefold()]
            if not visible:
                print(f"  No models match {val[1:].strip()!r}.")
                visible = list(models)
            continue
        if val.isdigit():
            try:
                idx = int(val) - 1
            except ValueError:
                print("  Invalid number.")
                continue
            if 0 <= idx < len(models):
                return models[idx]
            print("  Invalid number.")
        else:
            return val


def _render_model_list(
    models: list[str],
    visible: list[str],
    current: str,
) -> None:
    console = _setup_console()
    table = Table(
        title=f"Models  ·  {len(models)} discovered",
        box=box.ROUNDED,
        border_style="bright_black",
        header_style="bold",
        pad_edge=True,
    )
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Model", style="bold")
    table.add_column("State", no_wrap=True)
    shown = visible[:SETUP_MODEL_PREVIEW_LIMIT]
    if current and current in visible and current not in shown and shown:
        shown[-1] = current
    positions = {model: index for index, model in enumerate(models, 1)}
    if current:
        console.print(
            Text.assemble(
                ("  Current  ", "dim"),
                (terminal_safe_text(current, single_line=True), "bold"),
                ("  ·  Enter keeps it", "dim"),
            )
        )
    for model in shown:
        state = Text("current", style="green") if model == current else Text("")
        table.add_row(
            str(positions[model]),
            terminal_safe_text(model, single_line=True),
            state,
        )
    console.print()
    console.print(table)
    if len(visible) > len(shown):
        console.print(
            Text(
                f"  Showing {len(shown)} of {len(visible)} matches. "
                "Type /text to filter the catalog; Enter keeps the current model.",
                style="dim",
            )
        )
    else:
        console.print(
            Text(
                "  Enter a number or exact model name. "
                "Use /text to search, all to reset, b to go back"
                + (", or Enter to keep the current model." if current else "."),
                style="dim",
            )
        )


def _render_provider_catalog(
    config,
    descriptors: list[ProviderDescriptor] | tuple[ProviderDescriptor, ...],
) -> None:
    console = _setup_console()
    width = console.width
    table = Table(
        title=f"Providers  ·  {len(PROVIDERS)} routes",
        box=box.ROUNDED if width >= 52 else box.SIMPLE,
        border_style="bright_black",
        header_style="bold",
        pad_edge=width >= 52,
    )
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Provider", style="bold", no_wrap=width >= 48)
    show_type = width >= 68
    if show_type:
        table.add_column("Type", style="dim", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    show_about = width >= 112
    if show_about:
        table.add_column("About")
    positions = {descriptor.id: index for index, descriptor in enumerate(PROVIDERS, 1)}
    for descriptor in descriptors:
        status = _provider_status(config, descriptor)
        display_status = _compact_provider_status(status) if width < 60 else status
        cells: list[Any] = [
            str(positions[descriptor.id]),
            terminal_safe_text(descriptor.name, single_line=True),
        ]
        if show_type:
            cells.append(terminal_safe_text(descriptor.category, single_line=True))
        cells.append(_setup_status_text(display_status))
        if show_about:
            cells.append(terminal_safe_text(descriptor.description, single_line=True))
        table.add_row(*cells)
    console.print(table)
    hint = (
        "  /text search  ·  all reset  ·  c cancel"
        if width < 52
        else (
            "  Search with /text (for example /gateway or /local). "
            "Type all to reset; c cancels."
        )
    )
    console.print(Text(hint, style="dim"))


def _compact_provider_status(status: str) -> str:
    normalized = status.casefold()
    if "signed in" in normalized:
        return "signed in"
    if "permission" in normalized:
        return "consent"
    if "sign-in" in normalized or "sign in" in normalized:
        return "sign in"
    if "detected" in normalized or "ready" in normalized:
        return "ready"
    if "needs key" in normalized:
        return "key"
    if "scope configured" in normalized:
        return "scope"
    if "project/location" in normalized:
        return "scope"
    if "aws region" in normalized:
        return "region"
    if "available" in normalized or "local" in normalized:
        return "local"
    if "manual" in normalized:
        return "manual"
    return status


def _prompt_provider(config) -> ProviderDescriptor:
    visible = list(PROVIDERS)
    current_provider = str(getattr(config, "model", "") or "").partition("/")[0]
    default = next(
        (
            descriptor
            for descriptor in PROVIDERS
            if descriptor.id == current_provider
        ),
        None,
    )
    while True:
        _render_provider_catalog(config, visible)
        suffix = f" [{default.name}]" if default is not None else ""
        value = input(f"\n  Provider{suffix} › ").strip()
        if not value and default is not None:
            return default
        if value.casefold() in {"c", "cancel", "q", "quit"}:
            raise SetupCancelled
        if value.casefold() in {"all", "*"}:
            visible = list(PROVIDERS)
            continue
        if value.startswith("/"):
            query = value[1:].strip().casefold()
            if not query:
                visible = list(PROVIDERS)
                continue
            visible = [
                descriptor
                for descriptor in PROVIDERS
                if query
                in " ".join(
                    (
                        descriptor.id,
                        descriptor.name,
                        descriptor.category,
                        descriptor.description,
                    )
                ).casefold()
            ]
            if not visible:
                print(f"  No providers match {value[1:].strip()!r}.")
                visible = list(PROVIDERS)
            continue
        if value.isdigit():
            try:
                index = int(value) - 1
            except ValueError:
                print("  Invalid choice.")
                continue
            if 0 <= index < len(PROVIDERS):
                return PROVIDERS[index]
            print("  Invalid choice.")
            continue
        query = value.casefold()
        exact = [
            descriptor
            for descriptor in PROVIDERS
            if query in {descriptor.id.casefold(), descriptor.name.casefold()}
        ]
        if len(exact) == 1:
            return exact[0]
        matches = [
            descriptor
            for descriptor in PROVIDERS
            if query in descriptor.id.casefold()
            or query in descriptor.name.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            visible = matches
            print(f"  {len(matches)} providers match {value!r}; narrow the search.")
            continue
        print("  Invalid choice.")


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
            try:
                idx = int(val) - 1
            except ValueError:
                print("  Invalid choice.")
                continue
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
    console = _setup_console()
    line = Text()
    line.append("━━ ", style="bright_black")
    line.append(terminal_safe_text(title, single_line=True), style="bold")
    line.append(" ", style="bold")
    line.append("━" * 8, style="bright_black")
    console.print()
    console.print(line)


def _print_info(msg: str) -> None:
    line = Text("✓ ", style="green")
    line.append(terminal_safe_text(msg))
    _setup_console().print(line)
