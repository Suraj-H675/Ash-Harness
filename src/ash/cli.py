"""Entry point: ``python -m ash``.

Loads configuration, wires every module together, and runs an interactive
REPL on stdin. The REPL accepts a single user prompt per line and prints
the assistant's final response after tool calls complete. ``exit`` or
``quit`` (or EOF) terminates the session.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import re
import sys
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from ash.config import AshConfig
    from ash.core.loop import AshLoop
    from ash.providers.base import ProviderABC
    from ash.safety.guard import SafetyGuard


KNOWN_PROVIDERS = frozenset(
    {
        "anthropic",
        "openai",
        "deepseek",
        "groq",
        "ollama",
        "openai-compatible",
    }
)

AVAILABLE_MODELS: list[str] = [
    "anthropic/claude-opus-4-7",
    "anthropic/claude-sonnet-4-6",
    "anthropic/claude-haiku-4-5",
    "openai/gpt-5.2",
    "openai/gpt-5.2-codex",
    "openai/gpt-5-mini",
    "openai/gpt-4.1",
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


def _emit_config_diagnostics(config: AshConfig) -> None:
    for diagnostic in config.config_diagnostics:
        print(f"Warning: {diagnostic}", file=sys.stderr)


def _load_config_or_report(**overrides: Any) -> tuple[AshConfig | None, int]:
    from ash.config import AshConfig
    from ash.exceptions import classify_exception, format_error

    try:
        return (
            AshConfig.load(
                _override_source="cli",
                _override_detail="command-line option",
                **overrides,
            ),
            0,
        )
    except Exception as exc:  # noqa: BLE001 - stable CLI error boundary
        error = classify_exception(exc)
        print(format_error(error), file=sys.stderr)
    return None, error.exit_code


def _print_classified_error(exc: BaseException) -> None:
    """Render interactive failures through Ash's shared error taxonomy."""

    from ash.exceptions import classify_exception, format_error

    print(format_error(classify_exception(exc)), file=sys.stderr, flush=True)


def _config_overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if (
        getattr(args, "mode", None) is not None
        and getattr(args, "command", "") != "diff-mode"
    ):
        overrides["safety_tier"] = args.mode
    if getattr(args, "ci", False):
        overrides.update({"no_color": True, "reduced_motion": True})
    if getattr(args, "db_directory", None) is not None:
        overrides["db_directory"] = args.db_directory
    if getattr(args, "command", "") == "diff-mode" and getattr(args, "mode", None):
        overrides["approval_diff_mode"] = args.mode
    return overrides


def _parse_model_string(model: str) -> tuple[str, str]:
    from ash.providers.registry import parse_model_string

    return parse_model_string(model)


def _build_provider(config: AshConfig) -> ProviderABC:
    from ash.providers.registry import get_provider_registry

    return get_provider_registry().build(config)


def _prompt_cache_key(config: AshConfig) -> str:
    from ash.providers.registry import prompt_cache_key

    return prompt_cache_key(config)


def _build_tools(
    safety_guard: SafetyGuard,
    project_root: Path | None = None,
    *,
    sandbox_manager: Any | None = None,
    allow_project_extensions: bool = False,
    provider_factory: Any | None = None,
    agent_db_path: Path | None = None,
    allowed_web_domains: list[str] | tuple[str, ...] | None = None,
    repo_map: Any | None = None,
    runtime_config: AshConfig | None = None,
    lsp_manager: Any | None = None,
) -> dict[str, Any]:
    from ash.runtime import build_tools

    return build_tools(
        safety_guard,
        project_root,
        sandbox_manager=sandbox_manager,
        allow_project_extensions=allow_project_extensions,
        provider_factory=provider_factory,
        agent_db_path=agent_db_path,
        allowed_web_domains=allowed_web_domains,
        repo_map=repo_map,
        runtime_config=runtime_config,
        lsp_manager=lsp_manager,
    )


def _build_repo_map(config: AshConfig):
    from ash.runtime import build_repo_map

    return build_repo_map(config)


def _print_model_list(config: AshConfig) -> None:
    """Show models grouped by provider."""
    print(_render_model_list(config))


def _configured_model_catalog(config: AshConfig) -> list[str]:
    """Return configured custom and local models after built-in defaults."""

    catalog = [model for model in AVAILABLE_MODELS if "<your-model>" not in model]
    custom_providers = config.custom_providers
    if isinstance(custom_providers, dict):
        for provider_name in sorted(custom_providers, key=str.casefold):
            provider_config = custom_providers.get(provider_name)
            if not isinstance(provider_config, dict):
                continue
            models = provider_config.get("models", [])
            if not isinstance(models, list):
                continue
            for raw_model in models:
                if not isinstance(raw_model, str) or not raw_model.strip():
                    continue
                catalog.append(f"{provider_name}/{raw_model.strip()}")
    return list(dict.fromkeys(catalog))


def _render_model_list(
    config: AshConfig,
    *,
    numbered: bool = False,
) -> str:
    """Render known models for interactive or machine-independent display."""
    from ash.providers.capabilities import infer_capabilities

    # Determine current provider/model
    try:
        current_provider, current_model = _parse_model_string(config.model)
    except ValueError:
        current_provider, current_model = "anthropic", config.model

    # Group by provider
    grouped: dict[str, list[str]] = {}
    for m in _configured_model_catalog(config):
        prov, mod = _parse_model_string(m)
        grouped.setdefault(prov, []).append(mod)

    lines = ["Available models:"]
    number = 0
    for prov, models in grouped.items():
        lines.append(f"\n{prov.capitalize()}:")
        for model in models:
            number += 1
            marker = (
                " (current)"
                if prov == current_provider and model == current_model
                else ""
            )
            capabilities = infer_capabilities(prov, model)
            labels = [
                label
                for label, enabled in (
                    ("tools", capabilities.native_tools),
                    ("vision", capabilities.vision),
                    ("reasoning", capabilities.reasoning),
                    ("local", capabilities.local),
                )
                if enabled
            ]
            prefix = f"[{number}] " if numbered else ""
            lines.append(f"  {prefix}{model} [{', '.join(labels)}]{marker}")
    return "\n".join(lines)


def _render_context_budget(report: Any | None) -> str:
    if report is None:
        return ""
    lines = ["Budget:"]
    for name, item in report.slices.items():
        suffix = " truncated" if item.truncated else ""
        lines.append(f"  {name}: ~{item.used}/{item.limit}{suffix}")
    if report.fragments:
        lines.append("Sources:")
        for fragment in report.fragments:
            lines.append(
                f"  {fragment.kind}: {fragment.source} [{fragment.trust}] "
                f"sha256={fragment.content_sha256[:12]}"
            )
    return "\n".join(lines)


def _render_context_provenance(report: Any | None) -> str:
    """Render bounded per-fragment provider-input provenance."""

    if report is None:
        return "Provenance unavailable; run a turn first."
    lines = ["Provider-input provenance:"]
    for fragment in report.fragments:
        metadata = ", ".join(f"{key}={value}" for key, value in fragment.metadata)
        suffix = f"; {metadata}" if metadata else ""
        lines.append(
            f"  {fragment.kind} source={fragment.source} trust={fragment.trust.value} "
            f"tokens={fragment.tokens}/{fragment.limit} "
            f"truncated={str(fragment.truncated).lower()} "
            f"sha256={fragment.content_sha256[:12]}{suffix}"
        )
    return "\n".join(lines)


def _render_model_capabilities(model_string: str) -> str:
    """Render one model's capability and budget metadata without network I/O."""

    from ash.providers.capabilities import infer_capabilities

    provider, model_name = _parse_model_string(model_string)
    capabilities = infer_capabilities(provider, model_name)
    labels = [
        label
        for label, enabled in (
            ("tools", capabilities.native_tools),
            ("vision", capabilities.vision),
            ("reasoning", capabilities.reasoning),
            ("local", capabilities.local),
        )
        if enabled
    ]
    budgets: list[str] = []
    if capabilities.context_window is not None:
        budgets.append(f"context {capabilities.context_window:,}")
    if capabilities.max_output_tokens is not None:
        budgets.append(f"output {capabilities.max_output_tokens:,}")
    suffix = f"; {'; '.join(budgets)}" if budgets else ""
    return f"{model_string}: [{', '.join(labels) or 'unknown'}]{suffix}"


def _render_runtime_capabilities(loop: AshLoop, config: AshConfig) -> str:
    """Render the active provider/model's negotiated capability manifest."""

    provider = loop.provider
    capabilities = provider.capabilities
    model = (
        f"{getattr(provider, 'provider_family', config.provider)}/{provider.model_name}"
    )
    lines = [
        f"Runtime capabilities for {model}:",
        "  source: dynamic manifest"
        if getattr(
            provider,
            "_dynamic_capabilities",
            None,
        )
        is not None
        else "  source: static/default registry",
        f"  tools={str(capabilities.native_tools).lower()}",
        f"  vision={str(capabilities.vision).lower()}",
        f"  reasoning={str(capabilities.reasoning).lower()}",
        f"  local={str(capabilities.local).lower()}",
    ]
    if capabilities.context_window is not None:
        lines.append(f"  context_window={capabilities.context_window:,}")
    if capabilities.max_output_tokens is not None:
        lines.append(f"  max_output_tokens={capabilities.max_output_tokens:,}")
    return "\n".join(lines)


async def _discover_live_model_catalog(config: AshConfig) -> list[str]:
    """Probe the selected provider's live catalog with a short timeout."""

    from ash.commands.setup import (
        _probe_anthropic_models_detailed,
        _probe_models_detailed,
        _probe_ollama_models_detailed,
    )
    from ash.providers.readiness import resolve_provider_connection

    provider, model_name = _parse_model_string(config.model)
    connection = resolve_provider_connection(
        config.model_copy(update={"model": f"{provider}/{model_name}"})
    )
    if connection.provider == "anthropic":
        models = await asyncio.to_thread(
            lambda: _probe_anthropic_models_detailed(
                api_key=connection.api_key,
                base_url=connection.base_url,
            )
        )
    elif connection.provider == "ollama":
        models = await asyncio.to_thread(
            lambda: _probe_ollama_models_detailed(connection.base_url)
        )
    else:
        models = await asyncio.to_thread(
            lambda: _probe_models_detailed(
                connection.base_url,
                connection.api_key or None,
            )
        )
    prefix = connection.provider
    return [
        f"{prefix}/{model}"
        for model in models.models
        if isinstance(model, str) and model
    ]


def render_model_catalog_refresh(
    config: AshConfig,
    discovered: list[str],
    *,
    error: str | None = None,
) -> str:
    """Render static plus newly discovered catalogs with explicit provenance."""

    lines = ["Available models:"]
    if error:
        lines.append(f"Live discovery unavailable: {error}")
    if discovered:
        lines.append("\nLive:")
        current = config.model
        for model in sorted(set(discovered)):
            marker = " (current)" if model == current else ""
            lines.append(f"  {model}{marker}")
    lines.extend(["", _render_model_list(config)])
    return "\n".join(lines)


async def _interactive_model_picker(
    config: AshConfig,
    loop: AshLoop,
    prompt_input: Any,
    write_output: Any,
) -> None:
    """Show models grouped by provider, let user pick by provider number."""
    write_output(_render_model_list(config, numbered=True))
    choice = (await prompt_input.read("Pick a number (or 'c' to cancel)> ")).strip()
    if choice.lower() == "c":
        return
    try:
        idx = int(choice) - 1
        catalog = _configured_model_catalog(config)
        model_str = catalog[idx]
    except (ValueError, IndexError):
        write_output("Invalid selection.", file=sys.stderr)
        return

    try:
        loop.switch_model(model_str)
        config.model = model_str
        write_output(f"Switched to {model_str}")
    except Exception as exc:
        write_output(f"Error: {exc}", file=sys.stderr)


async def _repl(loop: AshLoop, config: AshConfig, sandbox_manager: Any) -> int:
    from ash.ui.terminal import TerminalUI
    from ash.commands.custom_commands import CommandSource, CustomCommandCatalog
    from ash.commands.slash import parse_slash_command, render_help
    from ash.safety.trust import is_workspace_trusted
    from ash.ui.prompt import PromptInput
    from ash.ui.status import StatusLine
    from ash.ui.turn_input import InteractiveTurnController
    from ash.ui.output import ReplPrinter
    from ash.ui.notifications import TerminalNotifier
    from ash.ui.help_overlay import show_help_overlay

    command_roots = [(Path.home() / ".ash" / "commands", "user")]
    plugin_roots = [(Path.home() / ".ash" / "plugins", "user")]
    if is_workspace_trusted(loop.project_root):
        command_roots.append((loop.project_root / ".ash" / "commands", "project"))
        plugin_roots.append((loop.project_root / ".ash" / "plugins", "project"))
    from ash.plugins.lifecycle import load_extension_state
    from ash.plugins.registry import PluginCatalog

    active_plugins = PluginCatalog(
        tuple(plugin_roots),
        disabled_plugins=load_extension_state().disabled_plugins,
    ).discover()
    command_sources: list[tuple[Path, str] | CommandSource] = list(command_roots)
    command_sources.extend(
        CommandSource(
            paths=plugin.command_paths(),
            source=f"plugin:{plugin.manifest.name}",
            namespace=plugin.manifest.name,
        )
        for plugin in active_plugins
    )
    custom_commands = CustomCommandCatalog(tuple(command_sources))
    discovered_commands = custom_commands.discover()

    status_line = StatusLine(loop, config, sandbox_manager)

    prompt_input = PromptInput(
        status_provider=status_line,
        extra_commands=[command.name for command in discovered_commands],
        input_mode=config.input_mode,
        keybindings=config.keybindings,
        workspace_root=loop.project_root,
        transcript=loop.ui.transcript if isinstance(loop.ui, TerminalUI) else None,
        tui_mode=config.tui_mode,
        theme=config.theme,
        repo_map=getattr(loop, "repo_map", None),
        mcp_runtime=getattr(loop, "_mcp_runtime", None),
        screen_reader_mode=config.screen_reader_mode,
    )
    if not isinstance(loop.ui, TerminalUI):
        raise TypeError("interactive REPL requires TerminalUI")
    loop.ui.viewport_mode = prompt_input.uses_viewport
    if prompt_input.uses_viewport:
        loop.ui.load_session_transcript(loop.current_session)
    print = ReplPrinter(loop.ui, viewport=prompt_input.uses_viewport)  # noqa: A001
    turn_controller = InteractiveTurnController(
        loop,
        prompt_input,
        loop.ui,
        write_status=loop.ui.write_status,
        notifier=TerminalNotifier(
            config.notification_method,
            events=config.notification_events,
        ),
        notification_include_preview=config.notification_include_preview,
    )

    async def reload_plugin_components() -> str:
        nonlocal custom_commands, discovered_commands

        from ash.hooks.config import HookConfigSource, load_command_hooks
        from ash.mcp.server import MCPConfigSource, load_mcp_server_sources
        from ash.plugins.agents import AgentCatalog, AgentSource
        from ash.plugins.skills import (
            ActivateSkillTool,
            ListSkillsTool,
            ReadSkillResourceTool,
            SkillCatalog,
            SkillSource,
        )
        from ash.plugins.runtime import build_plugin_runtime_tools
        from ash.tools.agent import SpawnAgentTool

        trusted = is_workspace_trusted(loop.project_root)
        refreshed_plugin_roots = [(Path.home() / ".ash" / "plugins", "user")]
        refreshed_command_roots = [(Path.home() / ".ash" / "commands", "user")]
        if trusted:
            refreshed_plugin_roots.append(
                (loop.project_root / ".ash" / "plugins", "project")
            )
            refreshed_command_roots.append(
                (loop.project_root / ".ash" / "commands", "project")
            )
        plugin_catalog = PluginCatalog(
            tuple(refreshed_plugin_roots),
            disabled_plugins=load_extension_state().disabled_plugins,
        )
        plugins = plugin_catalog.discover()
        if plugin_catalog.errors:
            raise ValueError(next(iter(plugin_catalog.errors.values())))

        refreshed_command_sources: list[tuple[Path, str] | CommandSource] = list(
            refreshed_command_roots
        )
        refreshed_command_sources.extend(
            CommandSource(
                paths=plugin.command_paths(),
                source=f"plugin:{plugin.manifest.name}",
                namespace=plugin.manifest.name,
            )
            for plugin in plugins
        )
        next_commands = CustomCommandCatalog(tuple(refreshed_command_sources))
        next_discovered_commands = next_commands.discover()
        if next_commands.errors:
            raise ValueError(next(iter(next_commands.errors.values())))

        skill_sources: list[Path | SkillSource] = [Path.home() / ".ash" / "skills"]
        agent_sources: list[Path | AgentSource] = [Path.home() / ".ash" / "agents"]
        if trusted:
            skill_sources.append(loop.project_root / ".ash" / "skills")
            agent_sources.append(loop.project_root / ".ash" / "agents")
        skill_sources.extend(
            SkillSource(plugin.skill_paths(), plugin.manifest.name)
            for plugin in plugins
        )
        agent_sources.extend(
            AgentSource(plugin.agent_paths(), plugin.manifest.name)
            for plugin in plugins
        )
        next_skills = SkillCatalog(tuple(skill_sources))
        discovered_skills = next_skills.discover()
        if next_skills.errors:
            raise ValueError(next(iter(next_skills.errors.values())))
        next_agents = AgentCatalog(tuple(agent_sources))
        discovered_agents = next_agents.discover()
        if next_agents.errors:
            raise ValueError(next(iter(next_agents.errors.values())))

        project_hook_environment = (("ASH_PROJECT_ROOT", str(loop.project_root)),)
        hook_sources: list[Path | HookConfigSource] = [
            HookConfigSource(
                Path.home() / ".ash" / "hooks.json",
                cwd=loop.project_root,
                environment=project_hook_environment,
            )
        ]
        if trusted:
            hook_sources.append(
                HookConfigSource(
                    loop.project_root / ".ash" / "hooks.json",
                    cwd=loop.project_root,
                    environment=project_hook_environment,
                )
            )
        hook_sources.extend(
            HookConfigSource(
                path,
                cwd=plugin.root,
                environment=(("ASH_PLUGIN_ROOT", str(plugin.root)),),
            )
            for plugin in plugins
            for path in plugin.hook_paths()
        )
        next_hooks = load_command_hooks(hook_sources)

        mcp_sources: list[MCPConfigSource] = []
        if trusted:
            mcp_sources.append(MCPConfigSource(loop.project_root / ".mcp.json"))
        mcp_sources.extend(
            MCPConfigSource(
                path,
                namespace=plugin.manifest.name,
                cwd=plugin.root,
                environment=(("ASH_PLUGIN_ROOT", str(plugin.root)),),
            )
            for plugin in plugins
            for path in plugin.mcp_paths()
        )
        next_mcp = load_mcp_server_sources(mcp_sources)
        next_plugin_tools = build_plugin_runtime_tools(
            plugins,
            loop.safety_guard,
            backend_preference=config.sandbox_backend,
            docker_image=config.sandbox_docker_image,
            allow_unisolated=config.allow_unsafe_plugin_runtime,
        )

        list_skills_tool = loop.tools.get("list_skills")
        activate_skill_tool = loop.tools.get("activate_skill")
        read_skill_resource_tool = loop.tools.get("read_skill_resource")
        if (
            not isinstance(list_skills_tool, ListSkillsTool)
            or not isinstance(activate_skill_tool, ActivateSkillTool)
            or not isinstance(read_skill_resource_tool, ReadSkillResourceTool)
        ):
            raise RuntimeError("skill tools are unavailable")
        list_skills_tool.catalog = next_skills
        activate_skill_tool.catalog = next_skills
        read_skill_resource_tool.catalog = next_skills
        spawn_tool = loop.tools.get("spawn_agent")
        if isinstance(spawn_tool, SpawnAgentTool):
            spawn_tool.set_custom_agents(
                {agent.name: agent for agent in discovered_agents}
            )
        next_hooks.set_event_sink(loop._emit_event)
        loop.hooks = next_hooks
        await loop.reload_plugin_runtime_tools(next_plugin_tools)
        mcp_errors = await loop.reload_mcp_servers(next_mcp)
        custom_commands = next_commands
        discovered_commands = next_discovered_commands
        prompt_input.set_extra_commands(
            [command.name for command in discovered_commands]
        )
        summary = (
            f"Reloaded {len(plugins)} plugin(s): {len(discovered_skills)} skills, "
            f"{len(discovered_agents)} agents, {len(discovered_commands)} commands, "
            f"{len(next_plugin_tools)} executable tools, "
            f"{len(hook_sources)} hook config(s), {len(next_mcp)} MCP server(s)."
        )
        if mcp_errors:
            summary += " MCP errors: " + "; ".join(
                f"{name}: {error}" for name, error in sorted(mcp_errors.items())
            )
        return summary

    print(
        "ash - type /help for commands",
        flush=True,
    )
    while True:
        try:
            user_input = (await prompt_input.read("> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            return 0
        if user_input.startswith("!"):
            command_line = user_input[1:].strip()
            if not command_line:
                continue
            if loop.current_session is None:
                await loop.start_session()
            assert loop.current_session is not None
            from uuid import uuid4

            await loop._execute_tool_calls(
                [
                    {
                        "call_id": str(uuid4()),
                        "name": "run_command",
                        "arguments": {"command_line": command_line},
                    }
                ],
                loop.current_session,
            )
            continue

        expand_mentions = False
        try:
            parsed_command = parse_slash_command(user_input)
            expand_mentions = parsed_command is None
        except ValueError as exc:
            try:
                custom = custom_commands.parse(user_input)
            except ValueError as custom_exc:
                print(f"Error: {custom_exc}", file=sys.stderr, flush=True)
                continue
            if custom is None:
                _print_classified_error(exc)
                continue
            custom_command, custom_arguments = custom
            user_input = custom_command.expand(custom_arguments)
            parsed_command = None
            expand_mentions = True

        if parsed_command is not None:
            command, arguments = parsed_command
            if command.name == "exit":
                return 0
            if command.name == "help":
                help_query = " ".join(arguments)
                if prompt_input.interactive and not config.screen_reader_mode:
                    await show_help_overlay(initial_query=help_query)
                else:
                    print(render_help(help_query or None), flush=True)
                continue
            if command.name == "status":
                session = loop.current_session
                capabilities = loop.provider.capabilities
                provider_circuit = loop.provider_circuit_breaker.snapshot(
                    loop._provider_circuit_key
                )
                session_usage = (
                    loop.session_store.get_session_usage(session.session_id)
                    if session is not None
                    else None
                )
                print(
                    "\n".join(
                        (
                            f"Model: {config.model}",
                            f"Workspace: {config.workspace_root}",
                            f"Mode: {loop.safety_tier}",
                            f"Session: {session.session_id if session else '(none)'}",
                            f"Title: {(session.title or '(untitled)') if session else '(none)'}",
                            f"Recovered interrupted turns: {loop.recovered_turns}",
                            "Recovery attention: "
                            + (
                                f"{len(loop.recovery_summary.unknown_calls)} unknown tool(s), "
                                f"{len(loop.recovery_summary.unresolved_files)} unresolved file(s)"
                                if loop.recovery_summary is not None
                                and loop.recovery_summary.needs_attention
                                else "none"
                            ),
                            "Capabilities: "
                            + ", ".join(
                                label
                                for label, enabled in (
                                    ("tools", capabilities.native_tools),
                                    ("vision", capabilities.vision),
                                    ("reasoning", capabilities.reasoning),
                                    ("local", capabilities.local),
                                )
                                if enabled
                            ),
                            "Fallbacks: "
                            + (", ".join(config.fallback_models) or "(none)"),
                            "Provider circuit: "
                            + (
                                f"open ({provider_circuit['retry_after']:.1f}s cooldown)"
                                if provider_circuit["open"]
                                else f"closed ({provider_circuit['failures']} failures)"
                            ),
                            "Tokens: "
                            + (
                                f"{session_usage.prompt_tokens} prompt, "
                                f"{session_usage.completion_tokens} completion"
                                + (
                                    " "
                                    f"({session_usage.estimated_prompt_tokens} prompt, "
                                    f"{session_usage.estimated_completion_tokens} completion estimated)"
                                    if session_usage.has_estimates
                                    else " (provider reported)"
                                )
                                if session_usage is not None
                                else "0 prompt, 0 completion"
                            ),
                            "Prompt cache: "
                            + (
                                f"{session_usage.cache_read_tokens} read, "
                                f"{session_usage.cache_write_tokens} written"
                                if session_usage is not None
                                else "0 read, 0 written"
                            ),
                            "Cost: "
                            + (
                                f"${session_usage.cost_usd:.6f}"
                                + (
                                    f" (${session_usage.estimated_cost_usd:.6f} estimated)"
                                    if session_usage.estimated_cost_usd > 0
                                    else ""
                                )
                                if session_usage is not None
                                else "$0.000000"
                            ),
                        )
                    ),
                    flush=True,
                )
                continue
            if command.name == "cancel":
                print("No turn is currently running.", flush=True)
                continue
            if command.name == "new":
                session = await loop.start_session()
                loop.ui.load_session_transcript(session)
                print(f"Started session {session.session_id}", flush=True)
                continue
            if command.name == "sessions":
                if arguments[:1] == ["prune"]:
                    if len(arguments) != 2 or not arguments[1].isdigit():
                        print("Usage: /sessions prune <days>", file=sys.stderr)
                        continue
                    deleted = loop.session_store.cleanup_sessions(
                        int(arguments[1]), project_path=str(loop.project_root)
                    )
                    print(f"Deleted {deleted} expired session(s).")
                    continue
                query = " ".join(arguments)
                sessions = loop.session_store.list_sessions(
                    project_path=str(loop.project_root),
                    query=query,
                )
                if not sessions:
                    print("No matching sessions.", flush=True)
                for item in sessions:
                    title = item.title or "(untitled)"
                    print(
                        f"{item.session_id}  {title}  "
                        f"{item.message_count} messages  {item.updated_at.isoformat()}",
                        flush=True,
                    )
                continue
            if command.name == "resume":
                if len(arguments) > 1:
                    print(f"Usage: {command.usage}", file=sys.stderr, flush=True)
                    continue
                try:
                    selected_session_id: str | None
                    if arguments:
                        summary = loop.session_store.resolve_session(
                            arguments[0], str(loop.project_root)
                        )
                        selected_session_id = summary.session_id
                    else:
                        from ash.commands.sessions import pick_session

                        selected_session_id = await pick_session(
                            loop.session_store,
                            project_path=str(loop.project_root),
                        )
                        if selected_session_id is None:
                            print("Resume cancelled.", flush=True)
                            continue
                    session = await loop.start_session(selected_session_id)
                except (KeyError, ValueError) as exc:
                    _print_classified_error(exc)
                    continue
                loop.ui.load_session_transcript(session)
                print(f"Resumed session {session.session_id}", flush=True)
                continue
            if command.name == "rename":
                if not arguments or loop.current_session is None:
                    print(f"Usage: {command.usage}", file=sys.stderr, flush=True)
                    continue
                title = " ".join(arguments)
                loop.session_store.rename_session(
                    loop.current_session.session_id,
                    title,
                )
                loop.current_session.title = " ".join(title.split())
                print(f"Renamed session to {loop.current_session.title}", flush=True)
                continue
            if command.name == "fork":
                if loop.current_session is None:
                    print(f"Usage: {command.usage}", file=sys.stderr, flush=True)
                    continue
                try:
                    count: int | None = None
                    name_parts = arguments
                    if arguments:
                        try:
                            count = int(arguments[0])
                        except ValueError:
                            pass
                        else:
                            name_parts = arguments[1:]
                    session = loop.session_store.fork_session(
                        loop.current_session.session_id,
                        message_count=count,
                        branch_name=" ".join(name_parts),
                    )
                except ValueError as exc:
                    _print_classified_error(exc)
                    continue
                loop.current_session = session
                loop.ui.load_session_transcript(session)
                print(f"Forked session {session.session_id}", flush=True)
                continue
            if command.name == "tree":
                if loop.current_session is None or arguments:
                    print(f"Usage: {command.usage}", file=sys.stderr, flush=True)
                    continue
                for node in loop.session_store.session_tree(
                    loop.current_session.session_id
                ):
                    marker = (
                        "*"
                        if node.session_id == loop.current_session.session_id
                        else " "
                    )
                    label = node.branch_name or (
                        "root" if node.parent_session_id is None else "branch"
                    )
                    print(
                        f"{marker} {'  ' * node.depth}{node.session_id}  {label}",
                        flush=True,
                    )
                continue
            if command.name == "rewind":
                with_files = arguments[1:] == ["--files"]
                if (
                    loop.current_session is None
                    or len(arguments) not in {1, 2}
                    or (len(arguments) == 2 and not with_files)
                ):
                    print(f"Usage: {command.usage}", file=sys.stderr, flush=True)
                    continue
                try:
                    count = int(arguments[0])
                    if with_files:
                        from ash.core.checkpoints import rewind_session_with_files

                        session, restored = rewind_session_with_files(
                            loop.session_store,
                            loop.safety_guard,
                            loop.current_session.session_id,
                            count,
                        )
                    else:
                        restored = []
                        session = loop.session_store.rewind_session(
                            loop.current_session.session_id, count
                        )
                except (RuntimeError, ValueError) as exc:
                    _print_classified_error(exc)
                    continue
                loop.current_session = session
                loop.ui.load_session_transcript(session)
                suffix = f" and restored {len(restored)} file(s)" if with_files else ""
                print(
                    f"Rewound transcript to {len(session.messages)} messages{suffix}."
                )
                continue
            if command.name == "undo":
                from ash.core.checkpoints import undo_latest_checkpoint

                if loop.current_session is None:
                    print("Error: no active session", file=sys.stderr)
                    continue
                try:
                    restored = undo_latest_checkpoint(
                        loop.session_store,
                        loop.safety_guard,
                        loop.current_session.session_id,
                    )
                except RuntimeError as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    continue
                print(
                    "Restored: " + ", ".join(str(path) for path in restored)
                    if restored
                    else "No file checkpoint is available."
                )
                continue
            if command.name == "export":
                if loop.current_session is None or len(arguments) > 2:
                    print(f"Usage: {command.usage}", file=sys.stderr, flush=True)
                    continue
                export_format = arguments[0] if arguments else "jsonl"
                suffix = ".md" if export_format == "markdown" else ".jsonl"
                raw_path = (
                    arguments[1]
                    if len(arguments) == 2
                    else f"ash-session-{loop.current_session.session_id[:8]}{suffix}"
                )
                try:
                    output_path = loop.safety_guard.validate_path(raw_path)
                    content = loop.session_store.export_session(
                        loop.current_session.session_id, format=export_format
                    )
                    output_path.write_text(content, encoding="utf-8")
                except (OSError, ValueError) as exc:
                    _print_classified_error(exc)
                    continue
                print(f"Exported to {output_path}", flush=True)
                continue
            if command.name == "import":
                if len(arguments) != 1:
                    print(f"Usage: {command.usage}", file=sys.stderr, flush=True)
                    continue
                try:
                    input_path = loop.safety_guard.validate_path(arguments[0])
                    if input_path.stat().st_size > 50 * 1024 * 1024:
                        raise ValueError("session import exceeds 50 MiB")
                    session = loop.session_store.import_session_jsonl(
                        input_path.read_text(encoding="utf-8"),
                        project_path=str(loop.project_root),
                    )
                except (OSError, ValueError) as exc:
                    _print_classified_error(exc)
                    continue
                loop.current_session = session
                loop.ui.load_session_transcript(session)
                print(f"Imported and resumed session {session.session_id}")
                continue
            if command.name == "context":
                maximum = config.max_context_tokens - config.max_completion_tokens
                has_summary = bool(
                    loop.current_session and loop.current_session.context_summary
                )
                if arguments and arguments[0] == "--provenance":
                    print(
                        _render_context_provenance(loop._last_context_budget),
                        flush=True,
                    )
                    continue
                budget = _render_context_budget(loop._last_context_budget)
                budget_suffix = f"\n{budget}" if budget else ""
                last_usage = loop.last_turn_usage
                print(
                    f"Context: ~{loop._last_context_tokens}/{maximum} input tokens; "
                    f"summary={'yes' if has_summary else 'no'}"
                    f"; last cache={last_usage['cache_read_tokens']} read/"
                    f"{last_usage['cache_write_tokens']} written "
                    f"({float(last_usage['cache_hit_rate']):.1%} hit)"
                    f"; last usage={last_usage['usage_source']}"
                    f"{budget_suffix}",
                    flush=True,
                )
                continue
            if command.name == "compact":
                tokens, changed = loop.compact_current_context()
                state = "updated" if changed else "already compact"
                print(
                    f"Context summary {state}; estimated input {tokens} tokens.",
                    flush=True,
                )
                continue
            if command.name == "capabilities":
                print(_render_runtime_capabilities(loop, config), flush=True)
                continue
            if command.name == "plan":
                if len(arguments) > 1 or arguments[:1] not in ([], ["on"], ["off"]):
                    print(f"Usage: {command.usage}", file=sys.stderr)
                    continue
                if arguments:
                    enabled = arguments[0] == "on"
                    loop.enable_sprint_planning = enabled
                    if enabled and loop.planner is None:
                        from ash.core.planner import Planner

                        loop.planner = Planner(loop.provider)
                print(
                    "Sprint planning: "
                    + ("enabled" if loop.enable_sprint_planning else "disabled")
                )
                continue
            if command.name == "skills":
                result = await loop.tools["list_skills"].run(query=" ".join(arguments))
                print(result.output or "No matching skills.", flush=True)
                continue
            if command.name == "plugins":
                from ash.commands.extensions import (
                    PluginAction,
                    manage_local_plugin,
                    render_plugin_action,
                )
                from ash.plugins.lifecycle import PluginLifecycleError
                from ash.plugins.lifecycle import load_extension_state
                from ash.plugins.registry import PluginCatalog
                from ash.safety.trust import is_workspace_trusted
                from ash.plugins.catalog import default_catalog_path

                if arguments:
                    action = arguments[0]
                    if action not in {"install", "enable", "disable", "uninstall"}:
                        print(f"Usage: {command.usage}", file=sys.stderr)
                        continue
                    positional = [
                        item for item in arguments[1:] if not item.startswith("--")
                    ]
                    flags = {item for item in arguments[1:] if item.startswith("--")}
                    ref = (
                        arguments[index + 1]
                        if "--ref" in arguments
                        and (index := arguments.index("--ref")) + 1 < len(arguments)
                        else None
                    )
                    catalog_path = default_catalog_path()
                    allowed_flags = (
                        {"--replace", "--ref"}
                        if action == "install"
                        else {"--yes"}
                        if action == "uninstall"
                        else set()
                    )
                    if len(positional) != 1 or not flags <= allowed_flags:
                        print(f"Usage: {command.usage}", file=sys.stderr)
                        continue
                    try:
                        plugin_result = manage_local_plugin(
                            cast(PluginAction, action),
                            positional[0],
                            replace="--replace" in flags,
                            confirmed="--yes" in flags,
                            git_ref=ref,
                            catalog_path=catalog_path,
                        )
                        reload_summary = await reload_plugin_components()
                    except (OSError, PluginLifecycleError, ValueError) as exc:
                        print(f"Error: {exc}", file=sys.stderr)
                        continue
                    print(render_plugin_action(plugin_result, json_output=False))
                    print(reload_summary)
                    continue

                roots = [(Path.home() / ".ash" / "plugins", "user")]
                if is_workspace_trusted(loop.project_root):
                    roots.append((loop.project_root / ".ash" / "plugins", "project"))
                catalog = PluginCatalog(
                    tuple(roots),
                    disabled_plugins=load_extension_state().disabled_plugins,
                )
                discovered = catalog.discover(include_disabled=True)
                if not discovered:
                    print("No plugins discovered.")
                for plugin in discovered:
                    print(
                        f"{plugin.manifest.name} {plugin.manifest.version} "
                        f"[{plugin.source}; "
                        f"{'enabled' if plugin.enabled else 'disabled'}] - "
                        f"{plugin.manifest.description}"
                    )
                for path, error in catalog.errors.items():
                    print(f"Invalid plugin {path}: {error}", file=sys.stderr)
                continue
            if command.name == "reload-plugins":
                if arguments:
                    print(f"Usage: {command.usage}", file=sys.stderr)
                continue
                try:
                    print(await reload_plugin_components(), flush=True)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    print(f"Error reloading plugins: {exc}", file=sys.stderr)
                continue
            if command.name == "hooks":
                from ash.commands.extensions import (
                    discover_extensions,
                    render_extension_inventory,
                )

                inventory = discover_extensions(loop.project_root)
                print(
                    render_extension_inventory(inventory, kind="hooks"),
                    flush=True,
                )
                continue
            if command.name == "commands":
                if not discovered_commands:
                    print("No custom commands discovered.")
                for custom_item in discovered_commands:
                    print(
                        f"/{custom_item.name} [{custom_item.source}] - "
                        f"{custom_item.description}"
                    )
                for path, error in custom_commands.errors.items():
                    print(f"Invalid command {path}: {error}", file=sys.stderr)
                continue
            if command.name == "agents":
                from ash.tools.agent import SpawnAgentTool

                agent_tool = loop.tools.get("spawn_agent")
                if arguments[:1] == ["stop"]:
                    if len(arguments) != 2 or not isinstance(
                        agent_tool, SpawnAgentTool
                    ):
                        print(f"Usage: {command.usage}", file=sys.stderr)
                        continue
                    stopped = await agent_tool.stop(arguments[1])
                    print(
                        f"Stopped {arguments[1]}."
                        if stopped
                        else f"Agent {arguments[1]} is not running."
                    )
                    continue
                if arguments[:1] == ["resume"]:
                    if len(arguments) != 2 or not isinstance(
                        agent_tool, SpawnAgentTool
                    ):
                        print(f"Usage: {command.usage}", file=sys.stderr)
                        continue
                    resumed = await agent_tool.resume(arguments[1])
                    print(resumed.output or resumed.error or "Resume failed.")
                    continue
                if arguments:
                    if arguments != ["--full"]:
                        print(f"Usage: {command.usage}", file=sys.stderr)
                        continue
                statuses = (
                    agent_tool.detailed_statuses()
                    if arguments == ["--full"]
                    and isinstance(agent_tool, SpawnAgentTool)
                    else agent_tool.statuses()
                    if isinstance(agent_tool, SpawnAgentTool)
                    else []
                )
                if not statuses:
                    print("No subagents have run in this process.")
                for status in statuses:
                    if arguments == ["--full"]:
                        task_id = (
                            status.get("active_task_id")
                            or status.get("latest_task_id")
                            or "-"
                        )
                        print(
                            f"{status['agent_id']} [{status['role']}] "
                            f"{status['status']} task={task_id} "
                            f"tokens={status['used_tokens']}/"
                            f"{status['token_budget']} "
                            f"cost=${status['used_cost_usd']:.6f}: "
                            f"{status['task']}"
                        )
                    else:
                        print(
                            f"{status['agent_id']} [{status['role']}] "
                            f"{status['status']}: {status['task']}"
                        )
                continue
            if command.name == "diff":
                staged = "--staged" in arguments
                turn_diff = "--turn" in arguments
                if staged and turn_diff:
                    print(f"Usage: {command.usage}", file=sys.stderr, flush=True)
                    continue
                paths = [
                    argument
                    for argument in arguments
                    if argument not in {"--staged", "--turn"}
                ]
                if len(paths) > 1:
                    print(f"Usage: {command.usage}", file=sys.stderr, flush=True)
                    continue
                if turn_diff:
                    if paths:
                        print(f"Usage: {command.usage}", file=sys.stderr, flush=True)
                        continue
                    if loop.current_session is None:
                        print("No active session.", flush=True)
                        continue
                    from ash.core.checkpoints import diff_latest_checkpoint

                    try:
                        print(
                            diff_latest_checkpoint(
                                loop.session_store,
                                loop.safety_guard,
                                loop.current_session.session_id,
                            ),
                            flush=True,
                        )
                    except RuntimeError as exc:
                        _print_classified_error(exc)
                    continue
                result = await loop.tools["git_diff"].run(
                    staged=staged, path=paths[0] if paths else ""
                )
                print(result.output or result.error or "No changes.", flush=True)
                continue
            if command.name == "review":
                from ash.commands.review import (
                    build_review_prompt,
                    collect_review_changes,
                )

                try:
                    label, changes = await collect_review_changes(
                        loop.project_root, arguments
                    )
                except ValueError as exc:
                    _print_classified_error(exc)
                    continue
                if not changes.strip():
                    print(f"No changes found for {label}.", flush=True)
                    continue
                user_input = build_review_prompt(label, changes)
                parsed_command = None
            if command.name == "permissions":
                from ash.commands.permissions import render_permission_rules
                from ash.safety.grants import (
                    PermissionRule,
                    RuleEffect,
                    add_permission_rule,
                    load_permission_rules,
                    remove_permission_rule,
                    remove_permission_rules_for_tool,
                )
                from ash.safety.policy import PermissionMode, PermissionPolicy

                if not arguments:
                    print(f"Permission mode: {loop.permission_policy.mode.value}")
                    print(
                        render_permission_rules(
                            loop.project_root,
                            loop.permission_policy.persistent_rules,
                        )
                    )
                    continue
                if len(arguments) == 2 and arguments[0] in {
                    "allow",
                    "ask",
                    "deny",
                    "revoke",
                    "remove",
                }:
                    action, target = arguments
                    try:
                        if action == "remove":
                            remove_permission_rule(loop.project_root, target)
                        else:
                            if target not in loop.tools:
                                print(
                                    f"Error: unknown tool {target!r}",
                                    file=sys.stderr,
                                )
                                continue
                            if action == "revoke":
                                remove_permission_rules_for_tool(
                                    loop.project_root,
                                    target,
                                    effect=RuleEffect.ALLOW,
                                )
                            else:
                                add_permission_rule(
                                    loop.project_root,
                                    PermissionRule.create(action, target),
                                )
                        loop.permission_policy.set_persistent_rules(
                            load_permission_rules(loop.project_root)
                        )
                        loop.notify_permission_rules_changed(
                            source="slash_permissions",
                            rule_count=len(loop.permission_policy.persistent_rules),
                        )
                    except ValueError as exc:
                        print(f"Error: {exc}", file=sys.stderr)
                        continue
                    print(f"Permission rule {action}: {target}")
                    continue
                if len(arguments) != 1:
                    print(f"Usage: {command.usage}", file=sys.stderr, flush=True)
                    continue
                try:
                    mode = PermissionMode(arguments[0])
                except ValueError:
                    allowed = ", ".join(item.value for item in PermissionMode)
                    print(f"Error: mode must be one of: {allowed}", file=sys.stderr)
                    continue
                if mode == PermissionMode.AUTO_APPROVE:
                    from ash.sandbox import auto_approve_safety_error

                    safety_error = auto_approve_safety_error(
                        sandbox_manager,
                        allow_unsafe=config.allow_unsafe_auto_approve,
                    )
                    if safety_error:
                        print(f"Error: {safety_error}", file=sys.stderr)
                        continue
                loop.permission_policy = PermissionPolicy(
                    mode,
                    managed_rules=loop.permission_policy.managed_rules,
                    persistent_rules=loop.permission_policy.persistent_rules,
                    session_rules=loop.permission_policy.session_rules,
                )
                loop.notify_permission_rules_changed(
                    source="permission_mode",
                    rule_count=len(loop.permission_policy.persistent_rules),
                )
                loop.safety_tier = mode.value
                config.safety_tier = mode.value
                if hasattr(loop.ui, "safety_tier"):
                    loop.ui.safety_tier = mode.value
                if loop.current_session is not None:
                    loop.session_store.append_audit_log(
                        loop.current_session.session_id,
                        action_type="permission_mode",
                        target_resource=mode.value,
                        details={
                            "previous_mode": (
                                config.safety_tier
                                if config.safety_tier != mode.value
                                else mode.value
                            ),
                            "mode": mode.value,
                        },
                        result="SUCCESS",
                    )
                print(f"Permission mode: {mode.value}")
                continue
            if command.name == "sandbox":
                manager = sandbox_manager
                sandbox_status = manager.status()
                sandbox_capabilities = ", ".join(
                    f"{name}={'yes' if available else 'no'}"
                    for name, available in sandbox_status["available"].items()
                )
                print(
                    f"Sandbox: {sandbox_status['backend']} "
                    f"(tier {sandbox_status['tier']}); "
                    f"isolated={'yes' if sandbox_status['isolated'] else 'no'}; "
                    f"filesystem={sandbox_status['filesystem']}; "
                    f"network={sandbox_status['network']}; "
                    f"fail_closed={'yes' if sandbox_status['fail_closed'] else 'no'}; "
                    f"{sandbox_capabilities}"
                )
                if sandbox_status["remediation"]:
                    print(f"Action: {sandbox_status['remediation']}")
                continue
            if command.name == "doctor":
                from ash.commands.doctor import render_doctor, run_doctor

                print(render_doctor(await run_doctor()), flush=True)
                continue
            if command.name == "mcp":
                action = arguments[0] if arguments else "status"
                if (
                    (action == "cancel" and len(arguments) != 3)
                    or (action != "cancel" and len(arguments) > 1)
                    or action
                    not in {
                        "status",
                        "refresh",
                        "tools",
                        "resources",
                        "prompts",
                        "tasks",
                        "cancel",
                    }
                ):
                    print(f"Usage: {command.usage}", file=sys.stderr)
                    continue
                runtime = loop._mcp_runtime
                if runtime is None:
                    print("No live MCP servers.")
                    continue
                if action == "status":
                    for name in loop._mcp_configs:
                        state = "connected" if name in runtime.clients else "failed"
                        print(f"{name}: {state}")
                    for name, error in runtime.errors.items():
                        print(f"{name}: {error}", file=sys.stderr)
                    continue
                if action == "refresh":
                    await reload_plugin_components()
                    refreshed_runtime = loop._mcp_runtime
                    print("MCP configuration reloaded.")
                    if refreshed_runtime is None:
                        continue
                    for name in loop._mcp_configs:
                        state = (
                            "connected"
                            if name in refreshed_runtime.clients
                            else "failed"
                        )
                        print(f"{name}: {state}")
                    for name, error in sorted(refreshed_runtime.errors.items()):
                        print(f"{name}: {error}", file=sys.stderr)
                    continue
                elif action == "tools":
                    for name in sorted(
                        key for key in loop.tools if key.startswith("mcp__")
                    ):
                        print(name)
                elif action == "tasks":
                    tasks = await runtime.list_tasks()
                    if not tasks:
                        print("No MCP tasks.")
                    for task in tasks:
                        message = task.get("statusMessage")
                        suffix = f": {message}" if message else ""
                        print(
                            f"{task['server']}: {task['taskId']} "
                            f"{task['status']}{suffix}"
                        )
                elif action == "cancel":
                    task = await runtime.cancel_task(arguments[1], arguments[2])
                    message = task.get("statusMessage")
                    suffix = f": {message}" if message else ""
                    print(
                        f"{task['server']}: {task['taskId']} {task['status']}{suffix}"
                    )
                else:
                    items = (
                        await runtime.list_resources()
                        if action == "resources"
                        else await runtime.list_prompts()
                    )
                    for capability in items:
                        identifier = capability.get("uri") or capability.get("name")
                        print(f"{capability['server']}: {identifier}")
                continue
            if command.name == "memory":
                action = arguments[0] if arguments else "status"
                if action == "status" and len(arguments) == 1 or not arguments:
                    state = (
                        "enabled" if loop._vector_pipeline is not None else "disabled"
                    )
                    print(f"Memory: {state}; backend={config.memory_backend}")
                    continue
                if action == "index" and len(arguments) == 2:
                    memory_path = loop.safety_guard.validate_path(arguments[1])
                    if not memory_path.is_file():
                        print(f"Error: not a file: {memory_path}", file=sys.stderr)
                        continue
                    await loop.index_file_for_memory(memory_path)
                    print(f"Indexed {memory_path}")
                    continue
                if action == "search" and len(arguments) >= 2:
                    hits = await loop.semantic_search(" ".join(arguments[1:]))
                    if not hits:
                        print("No memory matches.")
                    for hit in hits:
                        print(f"{hit.score:.3f} {hit.file_path}: {hit.content[:300]}")
                    continue
                if action == "export" and len(arguments) == 1:
                    if loop._vector_pipeline is None:
                        print("Memory is disabled.")
                    else:
                        print(
                            json.dumps(loop._vector_pipeline.export(), sort_keys=True)
                        )
                    continue
                if action == "clear" and len(arguments) == 1:
                    if loop._vector_pipeline is None:
                        print("Memory is disabled.")
                    else:
                        loop._vector_pipeline.clear()
                        print("Project memory index cleared.")
                    continue
                print(f"Usage: {command.usage}", file=sys.stderr)
                continue

        # /model with no args → interactive picker (from setup wizard)
        if (
            parsed_command is not None
            and parsed_command[0].name == "model"
            and not parsed_command[1]
        ):
            await _interactive_model_picker(config, loop, prompt_input, print)
            continue

        # /model provider/model → switch to full string
        if parsed_command is not None and parsed_command[0].name == "model":
            model_str = " ".join(parsed_command[1]).strip()
            if "/" not in model_str:
                print(
                    "Error: model must be in provider/model format (e.g. anthropic/claude-sonnet-4-6)",
                    file=sys.stderr,
                )
                continue
            print(_render_model_capabilities(model_str))
            try:
                loop.switch_model(model_str)
                config.model = model_str
                print(f"Switched to {model_str}", flush=True)
            except Exception as exc:
                _print_classified_error(exc)
            continue

        # /models → list
        if parsed_command is not None and parsed_command[0].name == "models":
            refresh = bool(arguments) and arguments[0] == "--refresh"
            lines = ["Available models:", _render_model_list(config)]
            custom_models = [
                model
                for model in _configured_model_catalog(config)
                if model.split("/", 1)[0]
                not in {"anthropic", "openai", "deepseek", "groq", "ollama"}
            ]
            for model in custom_models:
                lines.append(_render_model_capabilities(model))
            if refresh:
                try:
                    live_models = await _discover_live_model_catalog(config)
                    print(render_model_catalog_refresh(config, live_models))
                except Exception as exc:
                    print(
                        render_model_catalog_refresh(
                            config,
                            [],
                            error=str(exc),
                        ),
                        flush=True,
                    )
            else:
                print("\n".join(lines))
            continue

        # Normal turn
        try:
            user_metadata: dict[str, Any] | None = None
            if expand_mentions:
                from ash.commands.attachments import prepare_extended_mentions

                prepared = await prepare_extended_mentions(
                    user_input,
                    loop.safety_guard,
                    allow_images=loop.provider.capabilities.vision,
                    repo_map=loop.repo_map,
                    mcp_runtime=loop._mcp_runtime,
                    token_budget=config.attachment_token_budget,
                    count_tokens=loop.provider.count_tokens,
                )
                user_input = prepared.prompt
                user_metadata = prepared.message_metadata()
            response = await turn_controller.run(
                user_input, user_metadata=user_metadata
            )
            if response is None:
                continue
        except EOFError:
            print()
            return 0
        except Exception as exc:  # noqa: BLE001
            _print_classified_error(exc)
            continue
        if not prompt_input.uses_viewport:
            print(response, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ash", description="Ash coding harness REPL")
    try:
        version = importlib.metadata.version("ash-ai")
    except importlib.metadata.PackageNotFoundError:
        version = "0.1.0"
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser.add_argument("--version", action="version", version=f"ash {version}")
    subparsers = parser.add_subparsers(dest="command")
    setup_parser = subparsers.add_parser(
        "setup",
        help="Configure Ash (model, credentials, web search, browser)",
    )
    setup_parser.add_argument(
        "section",
        nargs="?",
        choices=["model", "providers", "web", "browser", "all"],
        help="Which section to configure",
    )
    setup_parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip optional configuration",
    )
    setup_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Run in non-interactive mode (fail if input needed)",
    )
    doctor_parser = subparsers.add_parser(
        "doctor", help="Diagnose local setup and runtime dependencies"
    )
    doctor_parser.add_argument("--json", action="store_true", dest="json_output")
    doctor_parser.add_argument(
        "--connect",
        action="store_true",
        help="Also probe the configured local or API endpoint",
    )
    sandbox_parser = subparsers.add_parser(
        "sandbox", help="Inspect sandbox enforcement or build the baseline image"
    )
    sandbox_subparsers = sandbox_parser.add_subparsers(
        dest="sandbox_action", required=True
    )
    sandbox_status_parser = sandbox_subparsers.add_parser("status")
    sandbox_status_parser.add_argument("--json", action="store_true")
    sandbox_build_parser = sandbox_subparsers.add_parser("build")
    sandbox_build_parser.add_argument("--image", default=None)
    config_parser = subparsers.add_parser(
        "config", help="Inspect Ash configuration sources"
    )
    config_subparsers = config_parser.add_subparsers(
        dest="config_action", required=True
    )
    config_explain = config_subparsers.add_parser(
        "explain", help="Show effective config values and their sources"
    )
    config_explain.add_argument("--json", action="store_true")
    diff_mode_parser = subparsers.add_parser(
        "diff-mode",
        help="Select approval diff preview layout",
    )
    diff_mode_parser.add_argument("mode", choices=["unified", "side-by-side"])
    ollama_parser = subparsers.add_parser(
        "ollama",
        help="Manage local Ollama models",
    )
    ollama_subparsers = ollama_parser.add_subparsers(
        dest="ollama_action", required=True
    )
    ollama_pull = ollama_subparsers.add_parser("pull")
    ollama_pull.add_argument("model")
    ollama_pull.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Pull timeout in seconds",
    )
    trust_parser = subparsers.add_parser(
        "trust", help="Inspect or change project extension trust"
    )
    trust_parser.add_argument("action", choices=["status", "add", "remove"])
    trust_parser.add_argument("path", nargs="?", type=Path, default=Path.cwd())
    reset_parser = subparsers.add_parser(
        "reset", help="Selectively remove Ash local configuration or data"
    )
    reset_parser.add_argument("--config", action="store_true")
    reset_parser.add_argument("--sessions", action="store_true")
    reset_parser.add_argument("--cache", action="store_true")
    reset_parser.add_argument("--all", action="store_true")
    reset_parser.add_argument("--yes", action="store_true")
    update_parser = subparsers.add_parser(
        "update", help="Check GitHub for a newer Ash release"
    )
    update_parser.add_argument("--json", action="store_true")
    storage_parser = subparsers.add_parser(
        "storage", help="Check, back up, or restore the session database"
    )
    storage_subparsers = storage_parser.add_subparsers(
        dest="storage_action", required=True
    )
    storage_check = storage_subparsers.add_parser("check")
    storage_check.add_argument("--json", action="store_true")
    storage_backup = storage_subparsers.add_parser("backup")
    storage_backup.add_argument("destination", nargs="?", type=Path)
    storage_restore = storage_subparsers.add_parser("restore")
    storage_restore.add_argument("backup", type=Path)
    storage_restore.add_argument("--yes", action="store_true")
    storage_debug = storage_subparsers.add_parser(
        "debug-bundle", help="Create a bounded, redacted diagnostics bundle"
    )
    storage_debug.add_argument("destination", nargs="?", type=Path)
    metrics_parser = subparsers.add_parser(
        "metrics", help="Show aggregate local-only model usage metrics"
    )
    metrics_parser.add_argument("--json", action="store_true")
    audit_parser = subparsers.add_parser(
        "audit", help="Inspect or export tamper-evident session audit logs"
    )
    audit_subparsers = audit_parser.add_subparsers(dest="audit_action", required=True)
    audit_list = audit_subparsers.add_parser("list")
    audit_list.add_argument("--session", required=True, dest="audit_session")
    audit_list.add_argument("--json", action="store_true")
    audit_verify = audit_subparsers.add_parser("verify")
    audit_verify.add_argument("--session", required=True, dest="audit_session")
    audit_verify.add_argument("--json", action="store_true")
    audit_export = audit_subparsers.add_parser("export")
    audit_export.add_argument("--session", required=True, dest="audit_session")
    audit_export.add_argument("--output", required=True, type=Path)
    sessions_parser = subparsers.add_parser("sessions", help="List saved Ash sessions")
    sessions_parser.add_argument(
        "sessions_action",
        nargs="?",
        choices=["list", "tree"],
        default="list",
    )
    sessions_parser.add_argument(
        "--session", help="Session ID or exact title for tree inspection"
    )
    sessions_parser.add_argument("--query", default="")
    sessions_parser.add_argument("--limit", type=int, default=20)
    sessions_parser.add_argument("--all-projects", action="store_true")
    sessions_parser.add_argument("--json", action="store_true")
    plans_parser = subparsers.add_parser(
        "plans", help="Inspect or update persisted sprint plans"
    )
    plans_subparsers = plans_parser.add_subparsers(dest="plans_action", required=True)
    plans_list = plans_subparsers.add_parser("list")
    plans_list.add_argument("--limit", type=int, default=20)
    plans_list.add_argument("--all-projects", action="store_true")
    plans_list.add_argument("--json", action="store_true")
    plans_show = plans_subparsers.add_parser("show")
    plans_show.add_argument("sprint_id")
    plans_show.add_argument("--json", action="store_true")
    plans_update = plans_subparsers.add_parser("update")
    plans_update.add_argument("sprint_id")
    plans_update.add_argument("item_idx", type=int)
    plans_update.add_argument(
        "status",
        choices=["pending", "in_progress", "done", "skipped", "failed"],
    )
    plans_update.add_argument("--notes", default="")
    plans_update.add_argument("--json", action="store_true")
    cron_parser = subparsers.add_parser(
        "cron", help="Create and operate durable unattended Ash schedules"
    )
    cron_subparsers = cron_parser.add_subparsers(dest="cron_action", required=True)
    cron_status = cron_subparsers.add_parser(
        "status", help="Show scheduler, run, and worker liveness for this workspace"
    )
    cron_status.add_argument("--json", action="store_true")
    cron_list = cron_subparsers.add_parser(
        "list", help="List enabled schedules for this workspace"
    )
    cron_list.add_argument("--all", action="store_true", dest="include_disabled")
    cron_list.add_argument("--json", action="store_true")
    cron_add = cron_subparsers.add_parser(
        "add", help="Create one validated one-shot, interval, or cron schedule"
    )
    cron_add.add_argument("name")
    cron_add.add_argument("--prompt", required=True, help="Prompt text, or - for stdin")
    cron_schedule = cron_add.add_mutually_exclusive_group(required=True)
    cron_schedule.add_argument("--at", help="Future ISO 8601 timestamp with UTC offset")
    cron_schedule.add_argument("--every", help="Interval such as 30m, 2h, or 1d")
    cron_schedule.add_argument(
        "--cron", dest="cron_expression", help="Five-field cron expression"
    )
    cron_add.add_argument(
        "--timezone", default="UTC", help="IANA timezone for --cron (default: UTC)"
    )
    cron_add.add_argument(
        "--misfire-grace",
        type=int,
        default=86_400,
        help="Maximum lateness in seconds, 0..2592000 (default: 86400)",
    )
    cron_add.add_argument(
        "--timeout",
        type=float,
        default=1800.0,
        help="Whole-turn wall timeout in seconds, 1..86400 (default: 1800)",
    )
    cron_add.add_argument(
        "--token-budget",
        type=int,
        default=100_000,
        help="Aggregate prompt and completion budget, 1..10000000",
    )
    cron_add.add_argument("--json", action="store_true")
    cron_show = cron_subparsers.add_parser(
        "show", help="Show a schedule and its stored prompt"
    )
    cron_show.add_argument("job")
    cron_show.add_argument("--json", action="store_true")
    for cron_action, cron_help in (
        ("pause", "Prevent future claims without cancelling an active run"),
        ("resume", "Resume future claims for a paused schedule"),
    ):
        cron_change = cron_subparsers.add_parser(cron_action, help=cron_help)
        cron_change.add_argument("job")
        cron_change.add_argument("--json", action="store_true")
    cron_remove = cron_subparsers.add_parser(
        "remove", help="Soft-delete a schedule while retaining run history"
    )
    cron_remove.add_argument("job")
    cron_remove.add_argument("--yes", action="store_true")
    cron_remove.add_argument("--json", action="store_true")
    cron_run = cron_subparsers.add_parser(
        "run", help="Run one schedule immediately through the isolated worker"
    )
    cron_run.add_argument("job")
    cron_run.add_argument("--json", action="store_true")
    cron_history = cron_subparsers.add_parser(
        "history", help="Show newest-first terminal and active run history"
    )
    cron_history.add_argument("job", nargs="?")
    cron_history.add_argument("--limit", type=int, default=100)
    cron_history.add_argument("--json", action="store_true")
    cron_cancel = cron_subparsers.add_parser(
        "cancel", help="Request cancellation of one active run"
    )
    cron_cancel.add_argument("run_id")
    cron_cancel.add_argument("--json", action="store_true")
    cron_worker = cron_subparsers.add_parser(
        "worker", help="Claim and execute due schedules for this workspace"
    )
    cron_worker.add_argument(
        "--once",
        action="store_true",
        help="Drain one due batch; exit 0 on success, 1 on failure, or 130 on stop",
    )
    cron_worker.add_argument("--json", action="store_true")
    permissions_parser = subparsers.add_parser(
        "permissions", help="Inspect or change persistent project tool grants"
    )
    permissions_parser.add_argument("--json", action="store_true")
    permissions_subparsers = permissions_parser.add_subparsers(
        dest="permissions_action"
    )
    permissions_status = permissions_subparsers.add_parser("status")
    permissions_status.add_argument("--json", action="store_true")
    for effect in ("allow", "ask", "deny"):
        permissions_rule = permissions_subparsers.add_parser(effect)
        permissions_rule.add_argument("tool_name")
        permissions_rule.add_argument(
            "--exact",
            action="append",
            default=[],
            metavar="ARGUMENT=JSON",
        )
        permissions_rule.add_argument(
            "--prefix",
            action="append",
            default=[],
            metavar="ARGUMENT=TEXT",
        )
        permissions_rule.add_argument(
            "--path-prefix",
            action="append",
            default=[],
            metavar="PATH_ARGUMENT=RELATIVE_PATH",
        )
        permissions_rule.add_argument(
            "--domain",
            action="append",
            default=[],
            metavar="URL_OR_DOMAIN_ARGUMENT=HOSTNAME",
        )
        permissions_rule.add_argument(
            "--command-prefix",
            nargs="+",
            default=[],
            metavar="TOKEN",
        )
        permissions_rule.add_argument("--json", action="store_true")
    permissions_revoke = permissions_subparsers.add_parser("revoke")
    permissions_revoke.add_argument("tool_name")
    permissions_revoke.add_argument("--json", action="store_true")
    permissions_remove = permissions_subparsers.add_parser("remove")
    permissions_remove.add_argument("rule_id")
    permissions_remove.add_argument("--json", action="store_true")
    permissions_clear = permissions_subparsers.add_parser("clear")
    permissions_clear.add_argument("--yes", action="store_true")
    permissions_clear.add_argument("--json", action="store_true")
    extensions_parser = subparsers.add_parser(
        "extensions", help="Inspect and manage skills, plugins, and hooks"
    )
    extensions_parser.add_argument(
        "extensions_action",
        nargs="?",
        choices=[
            "all",
            "skills",
            "agents",
            "plugins",
            "hooks",
            "install",
            "enable",
            "disable",
            "uninstall",
        ],
        default="all",
    )
    extensions_parser.add_argument("extensions_target", nargs="?")
    extensions_parser.add_argument("--ref")
    extensions_parser.add_argument("--replace", action="store_true")
    extensions_parser.add_argument("--yes", action="store_true")
    extensions_parser.add_argument("--json", action="store_true")
    agents_parser = subparsers.add_parser(
        "agents", help="Inspect persisted subagent status and reports"
    )
    agents_subparsers = agents_parser.add_subparsers(
        dest="agents_action", required=True
    )
    agents_list = agents_subparsers.add_parser("list")
    agents_list.add_argument("--json", action="store_true")
    agents_reports = agents_subparsers.add_parser("reports")
    agents_reports.add_argument("--limit", type=int, default=20)
    agents_reports.add_argument("--json", action="store_true")
    agents_tasks = agents_subparsers.add_parser("tasks")
    agents_tasks.add_argument(
        "--state",
        choices=("queued", "leased", "running", "succeeded", "failed", "cancelled"),
    )
    agents_tasks.add_argument("--owner")
    agents_tasks.add_argument("--graph", dest="graph_id")
    agents_tasks.add_argument("--limit", type=int, default=100)
    agents_tasks.add_argument("--json", action="store_true")
    agents_events = agents_subparsers.add_parser("events")
    agents_events.add_argument("--task", dest="task_id")
    agents_events.add_argument("--type", dest="event_type")
    agents_events.add_argument("--after", type=int, default=0, dest="after_sequence")
    agents_events.add_argument("--limit", type=int, default=100)
    agents_events.add_argument("--json", action="store_true")
    agents_cancel = agents_subparsers.add_parser("cancel")
    agents_cancel.add_argument("graph_id")
    agents_cancel.add_argument("--reason", default="cancelled by operator")
    agents_cancel.add_argument("--yes", action="store_true")
    agents_cancel.add_argument("--json", action="store_true")
    agents_messages = agents_subparsers.add_parser("messages")
    agents_messages.add_argument("--recipient", default="lead")
    agents_messages.add_argument("--all", action="store_true", dest="all_messages")
    agents_messages.add_argument("--limit", type=int, default=50)
    agents_messages.add_argument("--json", action="store_true")
    agents_send = agents_subparsers.add_parser("send")
    agents_send.add_argument("recipient")
    agents_send.add_argument("content")
    agents_send.add_argument("--sender", default="lead")
    agents_send.add_argument("--type", default="steer", dest="message_type")
    agents_send.add_argument("--json-content", action="store_true")
    agents_send.add_argument("--force", action="store_true")
    agents_send.add_argument("--json", action="store_true")
    agents_branches = agents_subparsers.add_parser("branches")
    agents_branches.add_argument("--json", action="store_true")
    agents_apply = agents_subparsers.add_parser("apply")
    agents_apply.add_argument("branch")
    agents_discard = agents_subparsers.add_parser("discard")
    agents_discard.add_argument("branch")
    agents_discard.add_argument("--yes", action="store_true")
    serve_parser = subparsers.add_parser(
        "serve", help="Run the authenticated local Ash HTTP API"
    )
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--token-env", default="ASH_SERVER_TOKEN")
    serve_parser.add_argument("--rate-limit", type=int, default=60)
    serve_parser.add_argument("--allow-remote", action="store_true")
    serve_parser.add_argument(
        "--log-level",
        choices=["critical", "error", "warning", "info", "debug"],
        default="info",
    )
    acp_parser = subparsers.add_parser(
        "acp", help="Run Ash as an Agent Client Protocol v1 stdio agent"
    )
    acp_parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the optional ACP runtime without starting stdio transport",
    )
    a2a_parser = subparsers.add_parser(
        "a2a", help="Expose or call agents through Agent2Agent Protocol 1.0"
    )
    a2a_subparsers = a2a_parser.add_subparsers(dest="a2a_action", required=True)
    a2a_subparsers.add_parser("check", help="Validate the optional A2A runtime")
    a2a_serve = a2a_subparsers.add_parser(
        "serve", help="Run the authenticated A2A 1.0 HTTP server"
    )
    a2a_serve.add_argument("--host", default="127.0.0.1")
    a2a_serve.add_argument("--port", type=int, default=8770)
    a2a_serve.add_argument("--public-url")
    a2a_serve.add_argument("--token-env", default="ASH_A2A_TOKEN")
    a2a_serve.add_argument("--rate-limit", type=int, default=60)
    a2a_serve.add_argument("--allow-remote", action="store_true")
    a2a_serve.add_argument(
        "--log-level",
        choices=["critical", "error", "warning", "info", "debug"],
        default="info",
    )
    a2a_inspect = a2a_subparsers.add_parser(
        "inspect", help="Resolve and print a remote Agent Card"
    )
    a2a_inspect.add_argument("url")
    a2a_inspect.add_argument("--token-env", default="ASH_A2A_TOKEN")
    a2a_inspect.add_argument("--timeout", type=float, default=30.0)
    a2a_send = a2a_subparsers.add_parser(
        "send", help="Send a text task to a remote A2A agent"
    )
    a2a_send.add_argument("url")
    a2a_send.add_argument("prompt", help="Prompt text, or - to read standard input")
    a2a_send.add_argument("--context-id")
    a2a_send.add_argument("--token-env", default="ASH_A2A_TOKEN")
    a2a_send.add_argument("--timeout", type=float, default=300.0)
    a2a_send.add_argument("--json", action="store_true")
    lsp_parser = subparsers.add_parser(
        "lsp", help="Inspect and query managed language servers"
    )
    lsp_subparsers = lsp_parser.add_subparsers(dest="lsp_action", required=True)
    lsp_status = lsp_subparsers.add_parser(
        "status", help="List detected and configured language servers"
    )
    lsp_status.add_argument("--json", action="store_true")
    lsp_diagnostics = lsp_subparsers.add_parser(
        "diagnostics", help="Get diagnostics for a workspace file"
    )
    lsp_diagnostics.add_argument("file_path")
    lsp_diagnostics.add_argument("--json", action="store_true")
    lsp_query = lsp_subparsers.add_parser(
        "query", help="Run a semantic language-server query"
    )
    lsp_query.add_argument(
        "operation",
        choices=[
            "hover",
            "definition",
            "references",
            "implementation",
            "documentSymbol",
            "workspaceSymbol",
            "prepareCallHierarchy",
            "incomingCalls",
            "outgoingCalls",
        ],
    )
    lsp_query.add_argument("file_path", nargs="?", default="")
    lsp_query.add_argument("--line", type=int, default=1)
    lsp_query.add_argument("--character", type=int, default=1)
    lsp_query.add_argument("--query", default="")
    lsp_query.add_argument("--json", action="store_true")
    mcp_subparser = subparsers.add_parser("mcp")
    mcp_action_subparsers = mcp_subparser.add_subparsers(dest="action", required=True)
    mcp_list = mcp_action_subparsers.add_parser("list")
    mcp_list.add_argument("--json", action="store_true")
    mcp_status = mcp_action_subparsers.add_parser("status")
    mcp_status.add_argument("--json", action="store_true")
    mcp_add = mcp_action_subparsers.add_parser("add")
    mcp_add.add_argument("server_name")
    mcp_add.add_argument(
        "--transport", choices=["stdio", "http", "sse"], default="stdio"
    )
    mcp_add.add_argument("--url", default="")
    mcp_add.add_argument("--env", action="append", default=[])
    mcp_add.add_argument("--header", action="append", default=[])
    mcp_add.add_argument("--auth", choices=["none", "oauth"], default="none")
    mcp_add.add_argument("--oauth-client-id", default="")
    mcp_add.add_argument("--oauth-client-secret-env", default="")
    mcp_add.add_argument("--oauth-scope", default="")
    mcp_add.add_argument("--oauth-redirect-port", type=int, default=0)
    mcp_add.add_argument("--json", action="store_true")
    mcp_remove = mcp_action_subparsers.add_parser("remove")
    mcp_remove.add_argument("server_name")
    mcp_login = mcp_action_subparsers.add_parser("login")
    mcp_login.add_argument("server_name")
    mcp_login.add_argument("--no-browser", action="store_true")
    mcp_login.add_argument("--timeout", type=float, default=300.0)
    mcp_login.add_argument("--scope", default="")
    mcp_logout = mcp_action_subparsers.add_parser("logout")
    mcp_logout.add_argument("server_name")
    parser.add_argument(
        "--db-directory",
        type=Path,
        default=None,
        help="Override ASH_DB_DIRECTORY for this run.",
    )
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument(
        "--session",
        default=None,
        help="Restore an existing session by id instead of creating a new one.",
    )
    session_group.add_argument(
        "-c",
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Continue the most recently updated session in this project.",
    )
    session_group.add_argument(
        "-r",
        "--resume",
        nargs="?",
        const="",
        default=None,
        metavar="SESSION",
        help="Resume by id/name, or open the interactive session picker.",
    )
    parser.add_argument(
        "--fork-session",
        action="store_true",
        help="Fork the resumed session instead of appending to it.",
    )
    parser.add_argument(
        "-p",
        "--prompt",
        default=None,
        help="Run one non-interactive prompt and exit.",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json", "stream-json"],
        default="text",
        help="Output format for --prompt mode.",
    )
    parser.add_argument(
        "--mode",
        choices=["interactive", "auto_edit", "plan", "auto_approve", "dry_run"],
        default=None,
        help="Override the configured permission mode for this run.",
    )
    parser.add_argument(
        "--json-schema",
        type=Path,
        default=None,
        help="Require one-shot output to validate against a JSON Schema file.",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Run without interactive prompts or ANSI; defaults one-shot output to stream-json.",
    )
    args, unknown_args = parser.parse_known_args(argv)
    if unknown_args:
        if args.command == "mcp" and getattr(args, "action", None) == "add":
            args.server_command = unknown_args
        else:
            parser.error(f"unrecognized arguments: {' '.join(unknown_args)}")
    elif args.command == "mcp" and getattr(args, "action", None) == "add":
        args.server_command = []
    if args.json_schema is not None and args.prompt is None:
        parser.error("--json-schema requires --prompt")
    if args.prompt == "-":
        args.prompt = sys.stdin.read()
    elif args.prompt is None and not sys.stdin.isatty() and args.command is None:
        try:
            piped_prompt = sys.stdin.read()
        except OSError:
            piped_prompt = ""
        if piped_prompt.strip():
            args.prompt = piped_prompt
    if args.ci:
        if args.command is None and args.prompt is None:
            print(
                "Error: --ci requires --prompt, piped stdin, or a subcommand.",
                file=sys.stderr,
            )
            return 2
        if args.prompt is not None and "--output-format" not in raw_argv:
            args.output_format = "stream-json"

    from ash.config import AshConfig
    from ash.core.session import SessionStore
    from ash.exceptions import classify_exception, format_error

    if args.command == "setup":
        from ash.commands.setup import cmd_setup

        return cmd_setup(args)

    if args.command == "doctor":
        from ash.commands.doctor import render_doctor, run_doctor

        checks = asyncio.run(run_doctor(connect=args.connect))
        print(render_doctor(checks, json_output=args.json_output))
        return 1 if any(check.status == "fail" for check in checks) else 0

    if args.command == "ollama":
        from ash.commands.ollama import pull_model, validate_ollama_model

        if args.ollama_action != "pull":
            parser.error("unsupported ollama action")
        if args.timeout <= 0:
            print("Error: timeout must be positive", file=sys.stderr)
            return 2
        try:
            validate_ollama_model(args.model)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        return asyncio.run(pull_model(args.model, timeout_seconds=args.timeout))

    if args.command == "lsp":
        from ash.commands.lsp import inspect_lsp, render_lsp
        from ash.core.redaction import redact_text

        config, exit_code = _load_config_or_report(**_config_overrides_from_args(args))
        if config is None:
            return exit_code
        if args.lsp_action == "query":
            if args.operation != "workspaceSymbol" and not args.file_path:
                parser.error("ash lsp query requires FILE except for workspaceSymbol")
            if args.line < 1 or args.character < 1:
                parser.error("--line and --character must be positive")
        try:
            payload = asyncio.run(
                inspect_lsp(
                    config,
                    action=args.lsp_action,
                    file_path=getattr(args, "file_path", ""),
                    operation=getattr(args, "operation", ""),
                    line=getattr(args, "line", 1),
                    character=getattr(args, "character", 1),
                    query=getattr(args, "query", ""),
                )
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Error: {redact_text(str(exc))}", file=sys.stderr)
            return 2
        print(render_lsp(payload, json_output=args.json))
        return 0

    if args.command == "sandbox":
        from ash.commands.sandbox import (
            build_sandbox_image,
            render_sandbox_status,
            sandbox_status,
        )

        try:
            sandbox_config = AshConfig.load()
            if args.sandbox_action == "status":
                print(
                    render_sandbox_status(
                        sandbox_status(sandbox_config),
                        json_output=args.json,
                    )
                )
                return 0
            image = args.image or sandbox_config.sandbox_docker_image
            print(f"Building local sandbox image {image}...")
            return build_sandbox_image(image)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.command == "config":
        from ash.commands.config import explain_config, render_config_explain

        try:
            config = AshConfig.load(
                _override_source="cli",
                _override_detail="command-line option",
                **_config_overrides_from_args(args),
            )
        except Exception as exc:  # noqa: BLE001
            error = classify_exception(exc)
            if args.json:
                print(json.dumps({"error": error.to_dict()}, sort_keys=True))
            else:
                print(format_error(error), file=sys.stderr)
            return error.exit_code
        _emit_config_diagnostics(config)
        print(
            render_config_explain(
                explain_config(config),
                json_output=args.json,
            )
        )
        return 0

    if args.command == "diff-mode":
        from ash.commands.config import load_config, save_config

        try:
            config = AshConfig.load(
                _override_source="cli",
                _override_detail="command-line option",
                **_config_overrides_from_args(args),
            )
            user_config = load_config(strict=True)
            if not isinstance(user_config, dict):
                raise ValueError("user configuration must contain a TOML table")
            user_config["approval_diff_mode"] = config.approval_diff_mode
            save_config(user_config)
            print(f"Approval diff mode saved: {config.approval_diff_mode}")
            return 0
        except Exception as exc:  # noqa: BLE001 - stable CLI error boundary
            error = classify_exception(exc)
            print(format_error(error), file=sys.stderr)
            return error.exit_code

    if args.command == "trust":
        from ash.safety.trust import (
            canonical_workspace,
            is_workspace_trusted,
            set_workspace_trusted,
        )

        if args.action == "status":
            trusted = is_workspace_trusted(args.path)
            print(
                f"{canonical_workspace(args.path)}: {'trusted' if trusted else 'untrusted'}"
            )
            return 0 if trusted else 1
        trusted = args.action == "add"
        set_workspace_trusted(args.path, trusted)
        print(
            f"{canonical_workspace(args.path)}: {'trusted' if trusted else 'untrusted'}"
        )
        return 0

    if args.command == "reset":
        from ash.commands.reset import reset_local_state

        selected = args.config or args.sessions or args.cache or args.all
        if not selected:
            print(
                "Error: choose --config, --sessions, --cache, or --all", file=sys.stderr
            )
            return 2
        confirmed = args.yes
        if not confirmed and sys.stdin.isatty():
            confirmed = input(
                "Remove selected Ash local state? [y/N] "
            ).strip().casefold() in {"y", "yes"}
        if not confirmed:
            print("Reset cancelled.", file=sys.stderr)
            return 2
        removed = reset_local_state(
            config=args.config or args.all,
            sessions=args.sessions or args.all,
            cache=args.cache or args.all,
            confirmed=True,
        )
        print(f"Removed {len(removed)} path(s).")
        return 0

    if args.command == "update":
        from ash.commands.update import check_for_update, render_update_status

        try:
            update_status = check_for_update(current_version=version)
        except ValueError as exc:
            if args.json:
                print(json.dumps({"error": str(exc)}, sort_keys=True))
            else:
                print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(render_update_status(update_status, json_output=args.json))
        return 0

    if args.command == "metrics":
        from ash.commands.storage import render_local_metrics
        from ash.core.session import SessionStore

        metrics_config = AshConfig.load(
            **({"db_directory": args.db_directory} if args.db_directory else {})
        )
        store = SessionStore(metrics_config.db_directory / "sessions.db")
        summary = store.local_metrics_summary()
        print(render_local_metrics(summary, json_output=args.json))
        return 0

    if args.command == "storage":
        from ash.commands.storage import (
            backup_database,
            check_database,
            create_debug_bundle,
            render_storage_check,
            restore_database,
        )

        storage_config = AshConfig.load(
            **({"db_directory": args.db_directory} if args.db_directory else {})
        )
        database = storage_config.db_directory / "sessions.db"
        if args.storage_action == "check":
            check = check_database(database)
            print(render_storage_check(check, json_output=args.json))
            return 0 if check.ok else 1
        if args.storage_action == "debug-bundle":
            try:
                bundle_path = create_debug_bundle(storage_config, args.destination)
            except (OSError, RuntimeError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            print(f"Debug bundle created: {bundle_path}")
            return 0
        if args.storage_action == "backup":
            try:
                backup_path = backup_database(database, args.destination)
            except (OSError, RuntimeError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            print(f"Backup created: {backup_path}")
            return 0
        confirmed = args.yes
        if not confirmed and sys.stdin.isatty():
            confirmed = input(
                "Stop other Ash processes and restore this session backup? [y/N] "
            ).strip().casefold() in {"y", "yes"}
        try:
            restored, preserved = restore_database(
                database, args.backup, confirmed=confirmed
            )
        except (OSError, RuntimeError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Restored: {restored}")
        for path in preserved:
            print(f"Preserved previous data: {path}")
        return 0

    if args.command == "audit":
        from ash.commands.audit import (
            export_audit_log,
            render_audit_records,
            render_audit_verification,
        )

        audit_config = AshConfig.load(
            **({"db_directory": args.db_directory} if args.db_directory else {})
        )
        store = SessionStore(audit_config.db_directory / "sessions.db")
        try:
            store.load_session(args.audit_session)
        except KeyError:
            print(f"Error: session not found: {args.audit_session}", file=sys.stderr)
            return 1
        if args.audit_action == "list":
            print(
                render_audit_records(
                    args.audit_session,
                    store.list_audit_logs(args.audit_session),
                    json_output=args.json,
                )
            )
            return 0
        if args.audit_action == "verify":
            errors = store.verify_audit_log(args.audit_session)
            print(
                render_audit_verification(
                    args.audit_session,
                    errors,
                    json_output=args.json,
                )
            )
            return 0 if not errors else 1
        try:
            exported = export_audit_log(store, args.audit_session, args.output)
        except OSError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Audit log exported: {exported}")
        return 0

    if args.command == "cron":
        from ash.automation.store import AutomationError
        from ash.commands.automation import (
            automation_store,
            create_job_from_cli,
            job_payload,
            render_job,
            render_jobs,
            render_runs,
            render_status,
            run_manual,
            run_payload,
            run_worker,
        )
        from ash.safety.trust import is_workspace_trusted

        config, exit_code = _load_config_or_report(**_config_overrides_from_args(args))
        if config is None:
            return exit_code
        json_output = bool(getattr(args, "json", False))
        try:
            if args.cron_action == "status":
                print(render_status(config, json_output=json_output))
                return 0
            if args.cron_action == "worker":
                if not args.once and not json_output:
                    print(
                        "Automation worker active for "
                        f"{config.workspace_root.resolve()}; press Ctrl+C to stop."
                    )

                def report_finished(run: Any) -> None:
                    if args.once:
                        return
                    if json_output:
                        print(
                            json.dumps(
                                {
                                    "event": "automation.run.finished",
                                    "run": run_payload(run),
                                },
                                sort_keys=True,
                            ),
                            flush=True,
                        )
                    else:
                        print(render_runs([run]), flush=True)

                automation_summary = asyncio.run(
                    run_worker(
                        config,
                        once=args.once,
                        on_run_finished=report_finished,
                    )
                )
                if args.once:
                    if json_output:
                        print(json.dumps(automation_summary.to_dict(), sort_keys=True))
                    else:
                        print(
                            f"Completed {automation_summary.completed} due automation "
                            f"run(s): {automation_summary.succeeded} succeeded, "
                            f"{automation_summary.failed} failed, "
                            f"{automation_summary.cancelled} cancelled, "
                            f"{automation_summary.skipped} skipped."
                        )
                if automation_summary.stopped:
                    return 130
                return 0 if automation_summary.ok else 1
            with automation_store(config) as cron_store:
                if args.cron_action == "list":
                    jobs = cron_store.list_jobs(
                        config.workspace_root,
                        include_disabled=args.include_disabled,
                    )
                    print(render_jobs(jobs, json_output=json_output))
                    return 0
                if args.cron_action == "add":
                    if not config.automation_enabled:
                        raise AutomationError(
                            "automation is disabled by user configuration"
                        )
                    if not is_workspace_trusted(config.workspace_root):
                        raise AutomationError(
                            "workspace must be trusted before creating unattended "
                            "automation; run `ash trust add`"
                        )
                    prompt = (
                        sys.stdin.read() if args.prompt == "-" else str(args.prompt)
                    )
                    job = create_job_from_cli(
                        config,
                        name=args.name,
                        prompt=prompt,
                        at=args.at,
                        every=args.every,
                        cron=args.cron_expression,
                        timezone_name=args.timezone,
                        misfire_grace_seconds=args.misfire_grace,
                        timeout_seconds=args.timeout,
                        token_budget=args.token_budget,
                    )
                    print(
                        json.dumps(
                            job_payload(job, include_prompt=True), sort_keys=True
                        )
                        if json_output
                        else f"Created automation {job.name} ({job.job_id})."
                    )
                    return 0
                if args.cron_action == "show":
                    shown_job = cron_store.get_job(
                        args.job, workspace=config.workspace_root
                    )
                    if shown_job is None:
                        raise AutomationError(f"automation not found: {args.job}")
                    print(render_job(shown_job, json_output=json_output))
                    return 0
                if args.cron_action in {"pause", "resume"}:
                    if args.cron_action == "resume" and not config.automation_enabled:
                        raise AutomationError(
                            "automation is disabled by user configuration"
                        )
                    if args.cron_action == "resume" and not is_workspace_trusted(
                        config.workspace_root
                    ):
                        raise AutomationError(
                            "workspace must be trusted before resuming unattended "
                            "automation; run `ash trust add`"
                        )
                    changed_job = cron_store.set_enabled(
                        args.job,
                        workspace=config.workspace_root,
                        enabled=args.cron_action == "resume",
                    )
                    print(
                        json.dumps(job_payload(changed_job), sort_keys=True)
                        if json_output
                        else (
                            f"{args.cron_action.title()}d automation "
                            f"{changed_job.name}."
                        )
                    )
                    return 0
                if args.cron_action == "remove":
                    confirmed = args.yes
                    if not confirmed and sys.stdin.isatty():
                        confirmed = input(
                            f"Remove automation {args.job}? [y/N] "
                        ).strip().casefold() in {"y", "yes"}
                    if not confirmed:
                        raise AutomationError(
                            "automation removal cancelled; pass --yes"
                        )
                    removed_job = cron_store.remove_job(
                        args.job, workspace=config.workspace_root
                    )
                    print(
                        json.dumps(
                            {
                                "removed": True,
                                "job_id": removed_job.job_id,
                                "name": removed_job.name,
                            },
                            sort_keys=True,
                        )
                        if json_output
                        else (
                            f"Removed automation {removed_job.name} "
                            f"({removed_job.job_id})."
                        )
                    )
                    return 0
                if args.cron_action == "history":
                    job_id = None
                    if args.job:
                        history_job = cron_store.get_job(
                            args.job,
                            workspace=config.workspace_root,
                            include_deleted=True,
                        )
                        if history_job is None:
                            raise AutomationError(f"automation not found: {args.job}")
                        job_id = history_job.job_id
                    runs = cron_store.list_runs(
                        workspace=config.workspace_root,
                        job_id=job_id,
                        limit=args.limit,
                    )
                    print(render_runs(runs, json_output=json_output))
                    return 0
                if args.cron_action == "cancel":
                    run = cron_store.get_run(args.run_id)
                    if (
                        run is None
                        or cron_store.get_job(
                            run.job_id, workspace=config.workspace_root
                        )
                        is None
                    ):
                        raise AutomationError(
                            f"automation run not found: {args.run_id}"
                        )
                    if run.status != "running":
                        raise AutomationError(
                            f"automation run is not running: {args.run_id} "
                            f"(status={run.status})"
                        )
                    run = cron_store.request_cancel(
                        args.run_id, workspace=config.workspace_root
                    )
                    print(
                        json.dumps(run_payload(run), sort_keys=True)
                        if json_output
                        else f"Cancellation requested for {run.run_id}."
                    )
                    return 0
            if args.cron_action == "run":
                if not is_workspace_trusted(config.workspace_root):
                    raise AutomationError(
                        "workspace must be trusted before running unattended "
                        "automation; run `ash trust add`"
                    )
                run = asyncio.run(run_manual(config, args.job))
                print(
                    json.dumps(run_payload(run), sort_keys=True)
                    if json_output
                    else render_runs([run])
                )
                return 0 if run.status == "succeeded" else 1
        except KeyboardInterrupt:
            if not json_output:
                print("Interrupted.", file=sys.stderr)
            return 130
        except (AutomationError, OSError, ValueError) as exc:
            if json_output:
                print(json.dumps({"error": str(exc)}, sort_keys=True))
            else:
                print(f"Error: {exc}", file=sys.stderr)
            return 2

    if args.command == "sessions":
        from ash.commands.sessions import (
            list_session_summaries,
            render_session_summaries,
            render_session_tree,
        )

        sessions_config = AshConfig.load(
            **({"db_directory": args.db_directory} if args.db_directory else {})
        )
        store = SessionStore(sessions_config.db_directory / "sessions.db")
        if args.sessions_action == "tree":
            try:
                selected_summary = (
                    store.resolve_session(
                        args.session, str(sessions_config.workspace_root)
                    )
                    if args.session
                    else store.latest_session(str(sessions_config.workspace_root))
                )
                if selected_summary is None:
                    raise ValueError("no sessions found in this project")
                tree = store.session_tree(selected_summary.session_id)
            except (KeyError, ValueError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
            print(render_session_tree(tree, json_output=args.json))
            return 0
        if args.session:
            print("Error: --session requires 'sessions tree'", file=sys.stderr)
            return 2
        try:
            sessions = list_session_summaries(
                store,
                project_path=str(sessions_config.workspace_root),
                all_projects=args.all_projects,
                limit=args.limit,
                query=args.query,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        print(render_session_summaries(sessions, json_output=args.json))
        return 0

    if args.command == "plans":
        from ash.commands.plans import (
            list_plans,
            render_plan_detail,
            render_plan_summaries,
            render_updated_plan_item,
            show_plan,
            update_plan_item,
        )

        plans_config = AshConfig.load(
            **({"db_directory": args.db_directory} if args.db_directory else {})
        )
        store = SessionStore(plans_config.db_directory / "sessions.db")
        try:
            if args.plans_action == "list":
                print(
                    render_plan_summaries(
                        list_plans(
                            store,
                            project_path=str(plans_config.workspace_root),
                            all_projects=args.all_projects,
                            limit=args.limit,
                        ),
                        json_output=args.json,
                    )
                )
            elif args.plans_action == "show":
                print(
                    render_plan_detail(
                        show_plan(store, args.sprint_id),
                        json_output=args.json,
                    )
                )
            else:
                print(
                    render_updated_plan_item(
                        update_plan_item(
                            store,
                            args.sprint_id,
                            args.item_idx,
                            args.status,
                            notes=args.notes,
                        ),
                        json_output=args.json,
                    )
                )
        except (KeyError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.command == "permissions":
        from ash.commands.permissions import (
            add_cli_permission_rule,
            clear_permission_grants,
            remove_cli_permission_rule,
            render_permission_rules,
            revoke_permission_grant,
        )
        from ash.safety.grants import load_permission_rules

        permissions_config = AshConfig.load()
        workspace = permissions_config.workspace_root
        action = args.permissions_action or "status"
        try:
            if action == "status":
                from ash.safety.grants import load_managed_permission_rules

                load_managed_permission_rules(workspace)
            if action in {"allow", "ask", "deny"}:
                _, rules = add_cli_permission_rule(
                    workspace,
                    action,
                    args.tool_name,
                    exact=args.exact,
                    prefix=args.prefix,
                    path_prefix=args.path_prefix,
                    domain=args.domain,
                    command_prefix=args.command_prefix,
                )
            elif action == "revoke":
                revoke_permission_grant(workspace, args.tool_name)
                rules = load_permission_rules(workspace)
            elif action == "remove":
                rules = remove_cli_permission_rule(workspace, args.rule_id)
            elif action == "clear":
                confirmed = args.yes
                if not confirmed and sys.stdin.isatty():
                    confirmed = input(
                        f"Clear persistent grants for {workspace.resolve()}? [y/N] "
                    ).strip().casefold() in {"y", "yes"}
                if not confirmed:
                    print("Permission grant clear cancelled.", file=sys.stderr)
                    return 2
                clear_permission_grants(workspace)
                rules = []
            else:
                rules = load_permission_rules(workspace)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        print(
            render_permission_rules(
                workspace,
                rules,
                json_output=getattr(args, "json", False),
            )
        )
        return 0

    if args.command == "agents":
        from ash.agents.tasks import AgentTaskError
        from ash.agents.worktree import WorktreeError
        from ash.commands.agents import (
            apply_agent_branch,
            cancel_agent_graph,
            discard_agent_branch,
            list_agent_messages,
            list_agent_branches,
            list_agent_reports,
            list_agent_statuses,
            list_agent_task_events,
            list_agent_tasks,
            render_agent_messages,
            render_agent_branches,
            render_agent_reports,
            render_agent_statuses,
            render_agent_task_events,
            render_agent_tasks,
            render_cancelled_agent_graph,
            render_sent_agent_message,
            send_agent_message,
        )

        agents_config = AshConfig.load()
        database = agents_config.db_directory / "agents.db"
        worktree_storage = agents_config.db_directory / "worktrees"
        try:
            if args.agents_action == "list":
                print(
                    render_agent_statuses(
                        list_agent_statuses(database),
                        json_output=args.json,
                    )
                )
            elif args.agents_action == "reports":
                print(
                    render_agent_reports(
                        list_agent_reports(database, limit=args.limit),
                        json_output=args.json,
                    )
                )
            elif args.agents_action == "tasks":
                print(
                    render_agent_tasks(
                        list_agent_tasks(
                            database,
                            task_state=args.state,
                            owner_agent_id=args.owner,
                            graph_id=args.graph_id,
                            limit=args.limit,
                        ),
                        json_output=args.json,
                    )
                )
            elif args.agents_action == "cancel":
                if not args.yes:
                    print(
                        "Error: cancelling an agent graph requires --yes",
                        file=sys.stderr,
                    )
                    return 2
                print(
                    render_cancelled_agent_graph(
                        cancel_agent_graph(
                            database,
                            graph_id=args.graph_id,
                            reason=args.reason,
                        ),
                        json_output=args.json,
                    )
                )
            elif args.agents_action == "events":
                print(
                    render_agent_task_events(
                        list_agent_task_events(
                            database,
                            task_id=args.task_id,
                            event_type=args.event_type,
                            after_sequence=args.after_sequence,
                            limit=args.limit,
                        ),
                        json_output=args.json,
                    )
                )
            elif args.agents_action == "branches":
                branches = asyncio.run(
                    list_agent_branches(
                        agents_config.workspace_root,
                        worktree_storage,
                    )
                )
                if args.json:
                    print(
                        json.dumps(
                            {
                                "branches": [
                                    {"branch": branch, "commit": commit}
                                    for branch, commit in branches
                                ]
                            },
                            sort_keys=True,
                        )
                    )
                else:
                    print(render_agent_branches(branches))
            elif args.agents_action == "apply":
                commit = asyncio.run(
                    apply_agent_branch(
                        agents_config.workspace_root,
                        worktree_storage,
                        args.branch,
                    )
                )
                print(f"Applied {args.branch} ({commit[:12]}).")
            elif args.agents_action == "discard":
                if not args.yes:
                    print(
                        "Error: discarding an agent branch requires --yes",
                        file=sys.stderr,
                    )
                    return 2
                asyncio.run(
                    discard_agent_branch(
                        agents_config.workspace_root,
                        worktree_storage,
                        args.branch,
                    )
                )
                print(f"Discarded {args.branch}.")
            else:
                if args.agents_action == "messages":
                    print(
                        render_agent_messages(
                            list_agent_messages(
                                database,
                                recipient_id=args.recipient,
                                undelivered_only=not args.all_messages,
                                limit=args.limit,
                            ),
                            json_output=args.json,
                        )
                    )
                else:
                    print(
                        render_sent_agent_message(
                            send_agent_message(
                                database,
                                recipient_id=args.recipient,
                                sender_id=args.sender,
                                message_type=args.message_type,
                                content=args.content,
                                json_content=args.json_content,
                                require_registered=not args.force,
                            ),
                            json_output=args.json,
                        )
                    )
        except (AgentTaskError, ValueError, WorktreeError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.command == "extensions":
        from ash.commands.extensions import (
            discover_extensions,
            manage_local_plugin,
            render_extension_inventory,
            render_plugin_action,
        )
        from ash.plugins.lifecycle import PluginLifecycleError

        action = args.extensions_action
        if action in {"install", "enable", "disable", "uninstall"}:
            if not args.extensions_target:
                print(
                    f"Error: `ash extensions {action}` requires a target",
                    file=sys.stderr,
                )
                return 2
            if args.replace and action != "install":
                print("Error: --replace is only valid with install", file=sys.stderr)
                return 2
            if args.ref and action != "install":
                print("Error: --ref is only valid with install", file=sys.stderr)
                return 2
            if args.yes and action != "uninstall":
                print("Error: --yes is only valid with uninstall", file=sys.stderr)
                return 2
            try:
                result = manage_local_plugin(
                    action,
                    args.extensions_target,
                    replace=args.replace,
                    confirmed=args.yes,
                    git_ref=args.ref,
                )
            except (OSError, PluginLifecycleError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
            print(render_plugin_action(result, json_output=args.json))
        else:
            if args.extensions_target or args.replace or args.yes:
                print(
                    "Error: inventory actions do not accept a target, --replace, or --yes",
                    file=sys.stderr,
                )
                return 2
            extension_config = AshConfig.load()
            inventory = discover_extensions(extension_config.workspace_root)
            print(
                render_extension_inventory(
                    inventory,
                    kind=action,
                    json_output=args.json,
                )
            )
        return 0

    if args.command == "acp":
        try:
            from acp import PROTOCOL_VERSION
            from ash.server.acp import run_acp_agent
        except ModuleNotFoundError as exc:
            if exc.name == "acp" or (exc.name or "").startswith("acp."):
                from ash.install import pipx_install_command

                print(
                    f"Error: ACP support requires `{pipx_install_command('acp')}`.",
                    file=sys.stderr,
                )
                return 2
            raise
        if args.check:
            print(f"Ash ACP protocol v{PROTOCOL_VERSION} is ready.")
            return 0
        try:
            asyncio.run(run_acp_agent())
        except KeyboardInterrupt:
            return 130
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Error: ACP server failed: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.command == "a2a":
        try:
            from a2a.client.errors import A2AClientError
            from a2a.utils.constants import PROTOCOL_VERSION_1_0
            from ash.commands.a2a import inspect_a2a, send_a2a, serve_a2a
            from httpx import HTTPError
        except ModuleNotFoundError as exc:
            if exc.name == "a2a" or (exc.name or "").startswith("a2a."):
                from ash.install import pipx_install_command

                print(
                    f"Error: A2A support requires `{pipx_install_command('a2a')}`.",
                    file=sys.stderr,
                )
                return 2
            raise
        if args.a2a_action == "check":
            print(f"Ash A2A protocol v{PROTOCOL_VERSION_1_0} is ready.")
            return 0
        try:
            operation = {
                "serve": serve_a2a,
                "inspect": inspect_a2a,
                "send": send_a2a,
            }[args.a2a_action]
            return asyncio.run(operation(args))
        except KeyboardInterrupt:
            return 130
        except (A2AClientError, HTTPError, OSError, RuntimeError, ValueError) as exc:
            print(f"Error: A2A operation failed: {exc}", file=sys.stderr)
            return 2

    if args.command == "serve":
        from ash.commands.serve import serve_http

        try:
            return asyncio.run(serve_http(args))
        except KeyboardInterrupt:
            return 130
        except Exception as exc:  # noqa: BLE001 - stable CLI error boundary
            error = classify_exception(exc)
            print(format_error(error), file=sys.stderr)
            return error.exit_code

    if args.command == "mcp":
        from ash.commands.mcp import (
            parse_key_value_options,
            render_mcp_servers,
        )
        from ash.mcp.server import MCPServerConfig, load_mcp_servers, save_mcp_servers

        path = Path.cwd() / ".mcp.json"
        servers = load_mcp_servers(path)
        if args.action in {"list", "status"}:
            print(render_mcp_servers(servers, json_output=args.json))
            return 0
        if not args.server_name:
            print("Error: server name is required.", file=sys.stderr)
            return 2
        if args.action == "remove":
            if args.server_name not in servers:
                print(
                    f"Error: MCP server {args.server_name!r} is not configured.",
                    file=sys.stderr,
                )
                return 2
            del servers[args.server_name]
            save_mcp_servers(servers, path)
            print(f"Removed MCP server {args.server_name}.")
            return 0
        if args.action in {"login", "logout"}:
            mcp_config = servers.get(args.server_name)
            if mcp_config is None:
                print(
                    f"Error: MCP server {args.server_name!r} is not configured.",
                    file=sys.stderr,
                )
                return 2
            if mcp_config.auth != "oauth":
                print(
                    f"Error: MCP server {args.server_name!r} does not use OAuth.",
                    file=sys.stderr,
                )
                return 2
            from ash.mcp.oauth import (
                MCPOAuthError,
                MCPOAuthTokenStore,
                authorize_mcp_server,
            )

            oauth_store = MCPOAuthTokenStore(args.server_name)
            if args.action == "logout":
                credentials_removed = oauth_store.remove()
                print(
                    f"Removed OAuth credentials for MCP server {args.server_name}."
                    if credentials_removed
                    else f"No OAuth credentials stored for {args.server_name}."
                )
                return 0
            if args.timeout <= 0 or args.timeout > 1800:
                print(
                    "Error: --timeout must be greater than 0 and at most 1800 seconds.",
                    file=sys.stderr,
                )
                return 2
            try:
                opener = (lambda url: False) if args.no_browser else webbrowser.open
                asyncio.run(
                    authorize_mcp_server(
                        args.server_name,
                        mcp_config.resolved_url,
                        oauth_config=mcp_config.resolved_oauth,
                        store=oauth_store,
                        open_browser=opener,
                        timeout_seconds=args.timeout,
                        manual_paste=True,
                        requested_scope=args.scope,
                    )
                )
            except (MCPOAuthError, OSError, ValueError) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
            print(f"Authorized MCP server {args.server_name}.")
            return 0
        command_parts = list(args.server_command)
        if command_parts[:1] == ["--"]:
            command_parts = command_parts[1:]
        if args.transport == "stdio" and not command_parts:
            print(
                "Error: stdio MCP add requires '-- command [args...]'.", file=sys.stderr
            )
            return 2
        if args.transport != "stdio" and not args.url:
            print("Error: HTTP/SSE MCP add requires --url.", file=sys.stderr)
            return 2
        try:
            env = parse_key_value_options(args.env, label="--env")
            headers = parse_key_value_options(args.header, label="--header")
            secret_env = args.oauth_client_secret_env.strip()
            if secret_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", secret_env):
                raise ValueError(
                    "--oauth-client-secret-env must be an environment variable name"
                )
            oauth = {
                key: value
                for key, value in {
                    "client_id": args.oauth_client_id,
                    "client_secret": f"${{{secret_env}}}" if secret_env else "",
                    "scope": args.oauth_scope,
                    "redirect_port": args.oauth_redirect_port,
                }.items()
                if value not in {"", 0}
            }
            servers[args.server_name] = MCPServerConfig(
                name=args.server_name,
                command=command_parts[0] if command_parts else "",
                args=command_parts[1:],
                env=env,
                transport=args.transport,
                url=args.url,
                headers=headers,
                auth=args.auth,
                oauth=oauth,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        save_mcp_servers(servers, path)
        if args.json:
            print(
                render_mcp_servers(
                    {args.server_name: servers[args.server_name]},
                    json_output=True,
                )
            )
        else:
            print(f"Added MCP server {args.server_name}.")
        return 0

    runtime_overrides = _config_overrides_from_args(args)

    loaded_config, config_exit_code = _load_config_or_report(**runtime_overrides)
    if loaded_config is None:
        return config_exit_code
    config = loaded_config

    from ash.safety.trust import is_workspace_trusted, set_workspace_trusted

    workspace_trusted = is_workspace_trusted(config.workspace_root)
    if (
        not workspace_trusted
        and args.prompt is None
        and not args.ci
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    ):
        answer = (
            input(
                f"Trust project extensions in {config.workspace_root.resolve()}? [y/N] "
            )
            .strip()
            .casefold()
        )
        if answer in {"y", "yes"}:
            set_workspace_trusted(config.workspace_root, True)
            workspace_trusted = True
            loaded_config, config_exit_code = _load_config_or_report(
                **runtime_overrides
            )
            if loaded_config is None:
                return config_exit_code
            config = loaded_config

    _emit_config_diagnostics(config)

    # First-run detection runs after trust so a trusted project model layer is
    # active immediately. Credentials and provider endpoints remain user-owned.
    from ash.commands.setup import _has_provider_configured

    if not _has_provider_configured(config):
        if args.prompt is not None or not sys.stdin.isatty():
            print(
                "Error: Ash is not configured. Run 'ash setup' in an interactive terminal.",
                file=sys.stderr,
            )
            return 2
        print(
            "Ash is not configured yet. "
            "Run 'ash setup' to configure your provider and API key.",
            flush=True,
        )
        reply = input(
            "Press Enter to run setup, or type 'repl' to continue without a provider: "
        ).strip()
        if reply.lower() not in ("repl", "continue"):
            from ash.commands.setup import cmd_setup

            setup_code = cmd_setup(
                argparse.Namespace(section="model", quick=True, non_interactive=False)
            )
            if setup_code != 0:
                return setup_code
            loaded_config, config_exit_code = _load_config_or_report(
                **runtime_overrides
            )
            if loaded_config is None:
                return config_exit_code
            config = loaded_config
            if not _has_provider_configured(config):
                print(
                    "Ash is still not configured. Run 'ash setup' to complete provider setup.",
                    file=sys.stderr,
                )
                return 2

    from ash.safety.grants import (
        PermissionGrantError,
        load_managed_permission_rules,
        load_permission_rules,
    )
    from ash.ui.terminal import TerminalUI

    try:
        permission_rules = load_permission_rules(config.workspace_root)
        managed_permission_rules = load_managed_permission_rules(config.workspace_root)
    except PermissionGrantError as exc:
        print(f"Error: invalid permission policy: {exc}", file=sys.stderr)
        return 2

    db_path = config.db_directory / "sessions.db"
    session_store = SessionStore(db_path)
    if config.session_retention_days > 0:
        session_store.cleanup_sessions(
            config.session_retention_days,
            project_path=str(config.workspace_root.resolve()),
        )
    startup_session_id = args.session
    if args.continue_session or args.resume is not None or args.fork_session:
        from ash.commands.sessions import select_startup_session

        try:
            startup_selection = asyncio.run(
                select_startup_session(
                    session_store,
                    project_path=str(config.workspace_root),
                    continue_session=args.continue_session,
                    resume=args.resume,
                    legacy_session_id=args.session,
                    fork_session=args.fork_session,
                    interactive=(
                        args.prompt is None
                        and sys.stdin.isatty()
                        and sys.stdout.isatty()
                    ),
                )
            )
        except (KeyError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        if startup_selection.cancelled:
            return 0
        startup_session_id = startup_selection.session_id
    if args.prompt is not None:
        from ash.ui.headless import HeadlessUI

        ui: Any = HeadlessUI(output_format=args.output_format)
    else:
        ui = TerminalUI(
            safety_tier=config.safety_tier,
            workspace_root=config.workspace_root,
            show_token_meter=config.show_token_meter,
            no_color=config.no_color,
            reduced_motion=config.reduced_motion,
            theme=config.theme,
            screen_reader_mode=config.screen_reader_mode,
        )
    from ash.runtime import build_runtime
    from ash.sandbox import SandboxBackendUnavailable

    try:
        runtime = build_runtime(
            config,
            ui,
            session_store=session_store,
            permission_rules=permission_rules,
            managed_rules=managed_permission_rules,
            workspace_trusted=workspace_trusted,
            run_maintenance=False,
        )
    except (OSError, ValueError, SandboxBackendUnavailable) as exc:
        print(f"Error initializing runtime: {exc}", file=sys.stderr)
        return 2
    loop = runtime.loop
    sandbox_manager = runtime.sandbox_manager

    if args.prompt is not None:
        try:
            return asyncio.run(
                _bootstrap_and_headless(
                    loop,
                    config,
                    prompt=args.prompt,
                    session_id=startup_session_id,
                    ui=ui,
                    json_schema_path=args.json_schema,
                )
            )
        except KeyboardInterrupt:
            print("Interrupted.", file=sys.stderr)
            return 130
    try:
        return asyncio.run(
            _bootstrap_and_repl(
                loop,
                config,
                sandbox_manager,
                session_id=startup_session_id,
            )
        )
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


async def _bootstrap_and_repl(
    loop: AshLoop,
    config: AshConfig,
    sandbox_manager: Any,
    *,
    session_id: str | None,
) -> int:
    from ash.exceptions import classify_exception, format_error

    try:
        await loop.start_session(session_id)
        if loop.recovery_summary is not None and loop.recovered_turns:
            summary = loop.recovery_summary
            print(
                "Recovered "
                f"{summary.interrupted_turns} interrupted turn(s): "
                f"{summary.compensated_calls} tool call(s) compensated; "
                f"{len(summary.unknown_calls)} unknown tool outcome(s); "
                f"{len(summary.unresolved_files)} unresolved file(s).",
                file=sys.stderr if summary.needs_attention else sys.stdout,
                flush=True,
            )
        return await _repl(loop, config, sandbox_manager)
    except Exception as exc:  # noqa: BLE001
        error = classify_exception(exc)
        print(format_error(error), file=sys.stderr)
        return error.exit_code
    finally:
        await loop.aclose()


async def _bootstrap_and_headless(
    loop: AshLoop,
    config: AshConfig,
    *,
    prompt: str,
    session_id: str | None,
    ui: Any,
    json_schema_path: Path | None = None,
) -> int:
    from ash.exceptions import classify_exception, format_error
    from ash.commands.attachments import PreparedAttachments, prepare_file_mentions

    try:
        session = await loop.start_session(session_id)
        safety_guard = getattr(loop, "safety_guard", None)
        provider = getattr(loop, "provider", None)
        prepared = (
            prepare_file_mentions(
                prompt,
                safety_guard,
                allow_images=provider.capabilities.vision,
                token_budget=config.attachment_token_budget,
                count_tokens=provider.count_tokens,
            )
            if safety_guard is not None and provider is not None
            else PreparedAttachments(prompt)
        )
        prompt = prepared.prompt
        schema = None
        if json_schema_path is not None:
            schema = _load_json_schema(json_schema_path)
            prompt = (
                f"{prompt}\n\nReturn only JSON matching this schema:\n"
                f"{json.dumps(schema, ensure_ascii=False)}"
            )
        user_metadata = prepared.message_metadata()
        response = (
            await loop.run_turn(prompt)
            if user_metadata is None
            else await loop.run_turn(prompt, user_metadata=user_metadata)
        )
        payload = {
            "response": response,
            "session_id": session.session_id,
            "model": config.model,
            "context_tokens": loop._last_context_tokens,
            "usage": loop.last_turn_usage,
        }
        if loop.recovery_summary is not None and loop.recovered_turns:
            payload["recovery"] = loop.recovery_summary.to_dict()
        if schema is not None:
            payload["structured_output"] = validate_structured_output(response, schema)
        ui.emit_result(payload)
        return 0
    except Exception as exc:  # noqa: BLE001
        error = classify_exception(exc)
        if hasattr(ui, "emit_error"):
            ui.emit_error(error.to_dict())
        elif ui.output_format in {"json", "stream-json"}:
            print(json.dumps({"type": "error", "error": error.to_dict()}), flush=True)
        else:
            print(format_error(error), file=sys.stderr)
        return error.exit_code
    finally:
        await loop.aclose()


def _load_json_schema(path: Path) -> dict[str, Any]:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON Schema file: {exc}") from exc
    if not isinstance(schema, dict):
        raise ValueError("JSON Schema root must be an object")
    return schema


def validate_structured_output(response: str, schema: dict[str, Any]) -> Any:
    import jsonschema  # type: ignore[import-untyped]

    try:
        value = json.loads(response)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model output is not valid JSON: {exc}") from exc
    try:
        jsonschema.validate(value, schema)
    except jsonschema.ValidationError as exc:
        raise ValueError(
            f"Model output failed schema validation: {exc.message}"
        ) from exc
    return value
