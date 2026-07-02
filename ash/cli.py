"""Entry point: ``python -m ash``.

Loads configuration, wires every module together, and runs an interactive
REPL on stdin. The REPL accepts a single user prompt per line and prints
the assistant's final response after tool calls complete. ``exit`` or
``quit`` (or EOF) terminates the session.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from config import AshConfig
    from core.loop import AshLoop
    from providers.base import ProviderABC
    from safety.guard import SafetyGuard


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
    from config import AshConfig
    from exceptions import classify_exception, format_error

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


def _config_overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if getattr(args, "mode", None) is not None:
        overrides["safety_tier"] = args.mode
    if getattr(args, "ci", False):
        overrides.update({"no_color": True, "reduced_motion": True})
    if getattr(args, "db_directory", None) is not None:
        overrides["db_directory"] = args.db_directory
    return overrides


def _parse_model_string(model: str) -> tuple[str, str]:
    """Parse 'provider/model' string into (provider, model_name)."""
    if "/" not in model:
        raise ValueError(
            f"Model string must be in 'provider/model' format, got: {model!r}"
        )
    provider, model_name = model.split("/", 1)
    return provider, model_name


def _build_provider(config: AshConfig) -> ProviderABC:
    if config.fallback_models:
        from providers.failover import FailoverProvider

        models = [config.model, *config.fallback_models]
        providers = [
            _build_provider(
                config.model_copy(update={"model": model, "fallback_models": []})
            )
            for model in models
        ]
        return FailoverProvider(providers)
    prov: ProviderABC
    provider, model_name = _parse_model_string(config.model)
    if provider == "anthropic":
        from providers.anthropic import AnthropicProvider

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        base_url = os.environ.get("ANTHROPIC_API_BASE") or None
        prov = AnthropicProvider(
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
        )
        prov.configure_max_tokens(config.max_completion_tokens)
        prov.configure_prompt_cache(
            enabled=config.prompt_cache_enabled and base_url is None,
            retention=config.prompt_cache_retention,
        )
        return prov

    elif provider == "openai":
        from providers.openai import OpenAIProvider

        api_key = os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("OPENAI_API_BASE") or None
        prov = OpenAIProvider(
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
        )
        prov.configure_max_tokens(config.max_completion_tokens)
        prov.configure_prompt_cache(
            enabled=config.prompt_cache_enabled and base_url is None,
            cache_key=_prompt_cache_key(config),
            retention=config.prompt_cache_retention,
        )
        return prov

    elif provider == "ollama":
        from providers.ollama import OllamaProvider

        base_url = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
        prov = OllamaProvider(
            model_name=model_name,
            base_url=base_url,
        )
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov

    elif provider == "deepseek":
        from providers.deepseek import DeepSeekProvider

        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        base_url = os.environ.get("DEEPSEEK_API_BASE") or None
        prov = DeepSeekProvider(
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
        )
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov

    elif provider == "groq":
        from providers.groq import GroqProvider

        api_key = os.environ.get("GROQ_API_KEY", "")
        base_url = os.environ.get("GROQ_API_BASE") or None
        prov = GroqProvider(
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
        )
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov

    elif provider == "openai-compatible":
        from providers.openai import OpenAIProvider

        api_key = os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("OPENAI_API_BASE", "")
        prov = OpenAIProvider(
            model_name=model_name,
            api_key=api_key,
            base_url=base_url if base_url else None,
        )
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov

    elif provider in config.custom_providers:
        from providers.openai import OpenAIProvider

        cp = config.custom_providers[provider]
        key_env = cp.get("key_env", "")
        prov = OpenAIProvider(
            model_name=model_name,
            api_key=os.environ.get(key_env, "") if key_env else cp.get("api_key", ""),
            base_url=cp.get("base_url"),
        )
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov

    raise ValueError(f"Unknown provider in model string: {provider!r}")


def _prompt_cache_key(config: AshConfig) -> str:
    workspace = str(config.workspace_root.expanduser().resolve()).encode("utf-8")
    digest = hashlib.sha256(workspace).hexdigest()[:24]
    return f"ash-project-{digest}"


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
) -> dict[str, Any]:
    from tools.base import BaseTool
    from tools.command import RunCommandTool
    from tools.filesystem import (
        ReadFileTool,
        ReplaceFileContentTool,
        ReplaceFileEditsTool,
        WholeEditTool,
        WriteFileTool,
    )
    from tools.git import AutoCommitTool, GitDiffTool, GitLogTool, GitStatusTool
    from plugins.agents import AgentCatalog, AgentSource
    from plugins.skills import (
        ActivateSkillTool,
        ListSkillsTool,
        SkillCatalog,
        SkillSource,
    )
    from tools.ask_user import AskUserTool
    from tools.patch import ApplyPatchTool
    from tools.process import BackgroundProcessTool
    from tools.search import GlobFilesTool, ListDirectoryTool, SearchTextTool
    from tools.web import WebFetchTool
    from tools.symbols import FindReferencesTool, FindSymbolTool

    root = project_root if project_root is not None else safety_guard.project_root
    skill_roots: list[Path | SkillSource] = [Path.home() / ".ash" / "skills"]
    from plugins.lifecycle import load_extension_state
    from plugins.registry import PluginCatalog

    plugin_roots = [(Path.home() / ".ash" / "plugins", "user")]
    if allow_project_extensions:
        skill_roots.append(root / ".ash" / "skills")
        plugin_roots.append((root / ".ash" / "plugins", "project"))
    plugin_catalog = PluginCatalog(
        tuple(plugin_roots),
        disabled_plugins=load_extension_state().disabled_plugins,
    )
    active_plugins = plugin_catalog.discover()
    skill_roots.extend(
        SkillSource(
            paths=plugin.skill_paths(),
            namespace=plugin.manifest.name,
        )
        for plugin in active_plugins
    )
    agent_sources: list[Path | AgentSource] = [Path.home() / ".ash" / "agents"]
    if allow_project_extensions:
        agent_sources.append(root / ".ash" / "agents")
    agent_sources.extend(
        AgentSource(
            paths=plugin.agent_paths(),
            namespace=plugin.manifest.name,
        )
        for plugin in active_plugins
    )
    agent_definitions = {
        definition.name: definition
        for definition in AgentCatalog(tuple(agent_sources)).discover()
    }
    catalog = SkillCatalog(tuple(skill_roots))
    tools: list[BaseTool] = [
        ReadFileTool(safety_guard),
        WriteFileTool(safety_guard),
        ReplaceFileContentTool(safety_guard),
        ReplaceFileEditsTool(safety_guard),
        WholeEditTool(safety_guard),
        RunCommandTool(
            safety_guard,
            project_root=root,
            sandbox_manager=sandbox_manager,
            environment_allowlist=(
                runtime_config.command_env_allowlist if runtime_config else ()
            ),
        ),
        AutoCommitTool(
            safety_guard,
            environment_allowlist=(
                runtime_config.command_env_allowlist if runtime_config else ()
            ),
        ),
        GitStatusTool(safety_guard),
        GitDiffTool(safety_guard),
        GitLogTool(safety_guard),
        ApplyPatchTool(safety_guard),
        BackgroundProcessTool(
            safety_guard,
            sandbox_manager=sandbox_manager,
            environment_allowlist=(
                runtime_config.command_env_allowlist if runtime_config else ()
            ),
        ),
        AskUserTool(safety_guard),
        ListDirectoryTool(safety_guard),
        GlobFilesTool(safety_guard),
        SearchTextTool(safety_guard),
        WebFetchTool(safety_guard, allowed_domains=allowed_web_domains),
        ListSkillsTool(safety_guard, catalog),
        ActivateSkillTool(safety_guard, catalog),
    ]
    if provider_factory is not None and agent_db_path is not None:
        from agents.shared_state import SharedState
        from tools.agent import SpawnAgentTool

        tools.append(
            SpawnAgentTool(
                safety_guard,
                SharedState(agent_db_path),
                provider_factory,
                config=runtime_config,
                custom_agents=agent_definitions,
            )
        )
    if repo_map is not None:
        tools.extend(
            [
                FindSymbolTool(safety_guard, repo_map),
                FindReferencesTool(safety_guard, repo_map),
            ]
        )
    return {tool.name: tool for tool in tools}


def _build_repo_map(config: AshConfig):
    """Build the optional repository map without making startup depend on indexing."""

    if not config.repo_map_enabled:
        return None
    from repo.repomap import RepoMap

    try:
        return RepoMap(
            config.workspace_root,
            max_files=config.repo_map_max_files,
            exclude_patterns=config.repo_map_exclude_patterns,
        )
    except OSError as exc:
        from ash_logging import get_logger

        get_logger(__name__).warning("repository map unavailable: {}", exc)
        return None


def _print_model_list(config: AshConfig) -> None:
    """Show models grouped by provider."""
    print(_render_model_list(config))


def _render_model_list(config: AshConfig, *, numbered: bool = False) -> str:
    """Render known models for interactive or machine-independent display."""
    from providers.capabilities import infer_capabilities

    # Determine current provider/model
    try:
        current_provider, current_model = _parse_model_string(config.model)
    except ValueError:
        current_provider, current_model = "anthropic", config.model

    # Group by provider
    grouped: dict[str, list[str]] = {}
    for m in AVAILABLE_MODELS:
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
        model_str = AVAILABLE_MODELS[idx]
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
    from ui.terminal import TerminalUI
    from cli.custom_commands import CommandSource, CustomCommandCatalog
    from cli.slash import parse_slash_command, render_help
    from safety.trust import is_workspace_trusted
    from ui.prompt import PromptInput
    from ui.status import StatusLine
    from ui.turn_input import InteractiveTurnController
    from ui.output import ReplPrinter
    from ui.notifications import TerminalNotifier
    from ui.help_overlay import show_help_overlay

    command_roots = [(Path.home() / ".ash" / "commands", "user")]
    plugin_roots = [(Path.home() / ".ash" / "plugins", "user")]
    if is_workspace_trusted(loop.project_root):
        command_roots.append((loop.project_root / ".ash" / "commands", "project"))
        plugin_roots.append((loop.project_root / ".ash" / "plugins", "project"))
    from plugins.lifecycle import load_extension_state
    from plugins.registry import PluginCatalog

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

        from hooks.config import HookConfigSource, load_command_hooks
        from mcp.server import MCPConfigSource, load_mcp_server_sources
        from plugins.agents import AgentCatalog, AgentSource
        from plugins.skills import (
            ActivateSkillTool,
            ListSkillsTool,
            SkillCatalog,
            SkillSource,
        )
        from tools.agent import SpawnAgentTool

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

        hook_sources: list[Path | HookConfigSource] = [
            Path.home() / ".ash" / "hooks.json"
        ]
        if trusted:
            hook_sources.append(loop.project_root / ".ash" / "hooks.json")
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

        list_skills_tool = loop.tools.get("list_skills")
        activate_skill_tool = loop.tools.get("activate_skill")
        if not isinstance(list_skills_tool, ListSkillsTool) or not isinstance(
            activate_skill_tool, ActivateSkillTool
        ):
            raise RuntimeError("skill tools are unavailable")
        list_skills_tool.catalog = next_skills
        activate_skill_tool.catalog = next_skills
        spawn_tool = loop.tools.get("spawn_agent")
        if isinstance(spawn_tool, SpawnAgentTool):
            spawn_tool.set_custom_agents(
                {agent.name: agent for agent in discovered_agents}
            )
        loop.hooks = next_hooks
        mcp_errors = await loop.reload_mcp_servers(next_mcp)
        custom_commands = next_commands
        discovered_commands = next_discovered_commands
        prompt_input.set_extra_commands(
            [command.name for command in discovered_commands]
        )
        summary = (
            f"Reloaded {len(plugins)} plugin(s): {len(discovered_skills)} skills, "
            f"{len(discovered_agents)} agents, {len(discovered_commands)} commands, "
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
                print(f"Error: {exc}", file=sys.stderr, flush=True)
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
                        from cli.sessions import pick_session

                        selected_session_id = await pick_session(
                            loop.session_store,
                            project_path=str(loop.project_root),
                        )
                        if selected_session_id is None:
                            print("Resume cancelled.", flush=True)
                            continue
                    session = await loop.start_session(selected_session_id)
                except (KeyError, ValueError) as exc:
                    print(f"Error: {exc}", file=sys.stderr, flush=True)
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
                if loop.current_session is None or len(arguments) > 1:
                    print(f"Usage: {command.usage}", file=sys.stderr, flush=True)
                    continue
                try:
                    count = int(arguments[0]) if arguments else None
                    session = loop.session_store.fork_session(
                        loop.current_session.session_id, message_count=count
                    )
                except ValueError as exc:
                    print(f"Error: {exc}", file=sys.stderr, flush=True)
                    continue
                loop.current_session = session
                loop.ui.load_session_transcript(session)
                print(f"Forked session {session.session_id}", flush=True)
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
                        from core.checkpoints import rewind_session_with_files

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
                    print(f"Error: {exc}", file=sys.stderr, flush=True)
                    continue
                loop.current_session = session
                loop.ui.load_session_transcript(session)
                suffix = f" and restored {len(restored)} file(s)" if with_files else ""
                print(
                    f"Rewound transcript to {len(session.messages)} messages{suffix}."
                )
                continue
            if command.name == "undo":
                from core.checkpoints import undo_latest_checkpoint

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
                    print(f"Error: {exc}", file=sys.stderr, flush=True)
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
                    print(f"Error: {exc}", file=sys.stderr, flush=True)
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
            if command.name == "plan":
                if len(arguments) > 1 or arguments[:1] not in ([], ["on"], ["off"]):
                    print(f"Usage: {command.usage}", file=sys.stderr)
                    continue
                if arguments:
                    enabled = arguments[0] == "on"
                    loop.enable_sprint_planning = enabled
                    if enabled and loop.planner is None:
                        from core.planner import Planner

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
                from cli.extensions import (
                    PluginAction,
                    manage_local_plugin,
                    render_plugin_action,
                )
                from plugins.lifecycle import PluginLifecycleError
                from plugins.lifecycle import load_extension_state
                from plugins.registry import PluginCatalog
                from safety.trust import is_workspace_trusted

                if arguments:
                    action = arguments[0]
                    if action not in {"install", "enable", "disable", "uninstall"}:
                        print(f"Usage: {command.usage}", file=sys.stderr)
                        continue
                    positional = [
                        item for item in arguments[1:] if not item.startswith("--")
                    ]
                    flags = {item for item in arguments[1:] if item.startswith("--")}
                    allowed_flags = (
                        {"--replace"}
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
                from cli.extensions import (
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
                from tools.agent import SpawnAgentTool

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
                    print(f"Usage: {command.usage}", file=sys.stderr)
                    continue
                statuses = (
                    agent_tool.statuses()
                    if isinstance(agent_tool, SpawnAgentTool)
                    else []
                )
                if not statuses:
                    print("No subagents have run in this process.")
                for status in statuses:
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
                    from core.checkpoints import diff_latest_checkpoint

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
                        print(f"Error: {exc}", file=sys.stderr, flush=True)
                    continue
                result = await loop.tools["git_diff"].run(
                    staged=staged, path=paths[0] if paths else ""
                )
                print(result.output or result.error or "No changes.", flush=True)
                continue
            if command.name == "review":
                from cli.review import build_review_prompt, collect_review_changes

                try:
                    label, changes = await collect_review_changes(
                        loop.project_root, arguments
                    )
                except ValueError as exc:
                    print(f"Error: {exc}", file=sys.stderr, flush=True)
                    continue
                if not changes.strip():
                    print(f"No changes found for {label}.", flush=True)
                    continue
                user_input = build_review_prompt(label, changes)
                parsed_command = None
            if command.name == "permissions":
                from cli.permissions import render_permission_rules
                from safety.grants import (
                    PermissionRule,
                    RuleEffect,
                    add_permission_rule,
                    load_permission_rules,
                    remove_permission_rule,
                    remove_permission_rules_for_tool,
                )
                from safety.policy import PermissionMode, PermissionPolicy

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
                    from sandbox import auto_approve_safety_error

                    safety_error = auto_approve_safety_error(
                        sandbox_manager,
                        allow_unsafe=config.allow_unsafe_auto_approve,
                    )
                    if safety_error:
                        print(f"Error: {safety_error}", file=sys.stderr)
                        continue
                loop.permission_policy = PermissionPolicy(
                    mode,
                    persistent_rules=loop.permission_policy.persistent_rules,
                    session_rules=loop.permission_policy.session_rules,
                )
                loop.safety_tier = mode.value
                config.safety_tier = mode.value
                if hasattr(loop.ui, "safety_tier"):
                    loop.ui.safety_tier = mode.value
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
                from cli.doctor import render_doctor, run_doctor

                print(render_doctor(await run_doctor()), flush=True)
                continue
            if command.name == "mcp":
                action = arguments[0] if arguments else "status"
                if len(arguments) > 1 or action not in {
                    "status",
                    "tools",
                    "resources",
                    "prompts",
                }:
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
                elif action == "tools":
                    for name in sorted(
                        key for key in loop.tools if key.startswith("mcp__")
                    ):
                        print(name)
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
            try:
                loop.switch_model(model_str)
                config.model = model_str
                print(f"Switched to {model_str}", flush=True)
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr, flush=True)
            continue

        # /models → list
        if parsed_command is not None and parsed_command[0].name == "models":
            print(_render_model_list(config))
            continue

        # Normal turn
        try:
            from cli.attachments import prepare_file_mentions

            user_metadata: dict[str, Any] | None = None
            if expand_mentions:
                prepared = prepare_file_mentions(
                    user_input,
                    loop.safety_guard,
                    allow_images=loop.provider.capabilities.vision,
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
            print(f"Error: {exc}", file=sys.stderr, flush=True)
            continue
        if not prompt_input.uses_viewport:
            print(response, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ash", description="Ash coding harness REPL")
    try:
        version = importlib.metadata.version("ash")
    except importlib.metadata.PackageNotFoundError:
        version = "0.1.0"
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser.add_argument("--version", action="version", version=f"ash {version}")
    subparsers = parser.add_subparsers(dest="command")
    setup_parser = subparsers.add_parser(
        "setup",
        help="Configure Ash (provider, API key, model)",
    )
    setup_parser.add_argument(
        "section",
        nargs="?",
        choices=["model", "providers", "all"],
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
        choices=["list"],
        default="list",
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
    mcp_add.add_argument("--json", action="store_true")
    mcp_remove = mcp_action_subparsers.add_parser("remove")
    mcp_remove.add_argument("server_name")
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

    from config import AshConfig
    from core.session import SessionStore
    from exceptions import classify_exception, format_error

    if args.command == "setup":
        from cli.setup import cmd_setup

        return cmd_setup(args)

    if args.command == "doctor":
        from cli.doctor import render_doctor, run_doctor

        checks = asyncio.run(run_doctor(connect=args.connect))
        print(render_doctor(checks, json_output=args.json_output))
        return 1 if any(check.status == "fail" for check in checks) else 0

    if args.command == "sandbox":
        from cli.sandbox import (
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
        from cli.config import explain_config, render_config_explain

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

    if args.command == "trust":
        from safety.trust import (
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
        from cli.reset import reset_local_state

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
        from cli.update import check_for_update, render_update_status

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

    if args.command == "storage":
        from cli.storage import (
            backup_database,
            check_database,
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
        from cli.audit import (
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

    if args.command == "sessions":
        from cli.sessions import list_session_summaries, render_session_summaries

        sessions_config = AshConfig.load(
            **({"db_directory": args.db_directory} if args.db_directory else {})
        )
        store = SessionStore(sessions_config.db_directory / "sessions.db")
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
        from cli.plans import (
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
        from cli.permissions import (
            add_cli_permission_rule,
            clear_permission_grants,
            remove_cli_permission_rule,
            render_permission_rules,
            revoke_permission_grant,
        )
        from safety.grants import load_permission_rules

        permissions_config = AshConfig.load()
        workspace = permissions_config.workspace_root
        action = args.permissions_action or "status"
        try:
            if action in {"allow", "ask", "deny"}:
                _, rules = add_cli_permission_rule(
                    workspace,
                    action,
                    args.tool_name,
                    exact=args.exact,
                    prefix=args.prefix,
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
        from agents.worktree import WorktreeError
        from cli.agents import (
            apply_agent_branch,
            discard_agent_branch,
            list_agent_messages,
            list_agent_branches,
            list_agent_reports,
            list_agent_statuses,
            render_agent_messages,
            render_agent_branches,
            render_agent_reports,
            render_agent_statuses,
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
        except (ValueError, WorktreeError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.command == "extensions":
        from cli.extensions import (
            discover_extensions,
            manage_local_plugin,
            render_extension_inventory,
            render_plugin_action,
        )
        from plugins.lifecycle import PluginLifecycleError

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
            if args.yes and action != "uninstall":
                print("Error: --yes is only valid with uninstall", file=sys.stderr)
                return 2
            try:
                result = manage_local_plugin(
                    action,
                    args.extensions_target,
                    replace=args.replace,
                    confirmed=args.yes,
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

    if args.command == "serve":
        from cli.serve import serve_http

        try:
            return asyncio.run(serve_http(args))
        except Exception as exc:  # noqa: BLE001 - stable CLI error boundary
            error = classify_exception(exc)
            print(format_error(error), file=sys.stderr)
            return error.exit_code

    if args.command == "mcp":
        from cli.mcp import (
            parse_key_value_options,
            render_mcp_servers,
        )
        from mcp.server import MCPServerConfig, load_mcp_servers, save_mcp_servers

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
            servers[args.server_name] = MCPServerConfig(
                name=args.server_name,
                command=command_parts[0] if command_parts else "",
                args=command_parts[1:],
                env=env,
                transport=args.transport,
                url=args.url,
                headers=headers,
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

    from safety.trust import is_workspace_trusted, set_workspace_trusted

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
    from cli.setup import _has_provider_configured

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
            "Press Enter to continue to REPL anyway, or type 'setup' to run the setup wizard: "
        ).strip()
        if reply.lower() in ("setup", "y", "yes"):
            from cli.setup import cmd_setup

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

    from safety.grants import PermissionGrantError, load_permission_rules
    from core.loop import AshLoop
    from safety.guard import SafetyGuard
    from ui.terminal import TerminalUI

    try:
        permission_rules = load_permission_rules(config.workspace_root)
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
        from cli.sessions import select_startup_session

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
    safety_guard = SafetyGuard(
        project_root=config.workspace_root,
        blocklist_commands=config.command_blocklist,
    )
    provider = _build_provider(config)
    if args.prompt is not None:
        from ui.headless import HeadlessUI

        ui: Any = HeadlessUI(output_format=args.output_format)
    else:
        ui = TerminalUI(
            safety_tier=config.safety_tier,
            workspace_root=config.workspace_root,
            show_token_meter=config.show_token_meter,
            no_color=config.no_color,
            reduced_motion=config.reduced_motion,
            screen_reader_mode=config.screen_reader_mode,
        )
    from sandbox import SandboxManager, auto_approve_safety_error

    sandbox_manager = SandboxManager(
        workspace_root=config.workspace_root,
        network=config.sandbox_network,
        backend_preference=config.sandbox_backend,
        docker_image=config.sandbox_docker_image,
    )
    safety_error = auto_approve_safety_error(
        sandbox_manager,
        allow_unsafe=config.allow_unsafe_auto_approve,
    )
    if config.safety_tier == "auto_approve" and safety_error:
        print(
            f"Error: {safety_error}",
            file=sys.stderr,
        )
        return 2
    repo_map = _build_repo_map(config)
    tools = _build_tools(
        safety_guard,
        config.workspace_root,
        sandbox_manager=sandbox_manager,
        allow_project_extensions=workspace_trusted,
        provider_factory=lambda: _build_provider(config),
        agent_db_path=config.db_directory / "agents.db",
        allowed_web_domains=config.allowed_web_domains,
        repo_map=repo_map,
        runtime_config=config,
    )

    from context.instructions import (
        InstructionDiagnostic,
        discover_instructions,
        render_instructions,
    )

    instruction_diagnostics: list[InstructionDiagnostic] = []
    discovered_instructions = discover_instructions(
        config.workspace_root,
        include_project=workspace_trusted,
        diagnostics=instruction_diagnostics,
    )
    instruction_text = render_instructions(
        discovered_instructions,
        instruction_diagnostics,
    )
    from hooks.config import HookConfigSource, load_command_hooks

    hook_paths: list[Path | HookConfigSource] = [Path.home() / ".ash" / "hooks.json"]
    plugin_roots = [(Path.home() / ".ash" / "plugins", "user")]
    if workspace_trusted:
        hook_paths.append(config.workspace_root / ".ash" / "hooks.json")
        plugin_roots.append((config.workspace_root / ".ash" / "plugins", "project"))
    from plugins.lifecycle import load_extension_state
    from plugins.registry import PluginCatalog

    active_plugins = PluginCatalog(
        tuple(plugin_roots),
        disabled_plugins=load_extension_state().disabled_plugins,
    ).discover()
    hook_paths.extend(
        HookConfigSource(
            path=path,
            cwd=plugin.root,
            environment=(("ASH_PLUGIN_ROOT", str(plugin.root)),),
        )
        for plugin in active_plugins
        for path in plugin.hook_paths()
    )
    try:
        hooks = load_command_hooks(hook_paths)
    except (OSError, ValueError) as exc:
        print(f"Error loading hooks: {exc}", file=sys.stderr)
        return 2

    from mcp.server import MCPConfigSource, load_mcp_server_sources

    mcp_sources: list[MCPConfigSource] = []
    if workspace_trusted:
        mcp_sources.append(MCPConfigSource(config.workspace_root / ".mcp.json"))
    mcp_sources.extend(
        MCPConfigSource(
            path=path,
            namespace=plugin.manifest.name,
            cwd=plugin.root,
            environment=(("ASH_PLUGIN_ROOT", str(plugin.root)),),
        )
        for plugin in active_plugins
        for path in plugin.mcp_paths()
    )
    try:
        mcp_configs = load_mcp_server_sources(mcp_sources)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error loading MCP servers: {exc}", file=sys.stderr)
        return 2

    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=ui,
        project_root=config.workspace_root,
        repo_map=repo_map,
        tools=tools,
        hooks=hooks,
        additional_instructions=instruction_text,
        config=config,
        max_steering_messages=config.steering_queue_limit,
        safety_tier=config.safety_tier,
        mcp_configs=mcp_configs,
        enable_semantic_memory=config.memory_backend != "off",
        memory_backend=config.memory_backend,
        embedding_provider=config.embedding_provider,
        openai_api_key=config.openai_api_key,
        onnx_model_path=config.onnx_model_path,
        chroma_persist_dir=config.chroma_persist_dir,
    )
    loop.permission_policy.set_persistent_rules(permission_rules)
    from core.checkpoints import FileCheckpointMiddleware
    from core.secret_middleware import SecretRedactionMiddleware

    def checkpoint_context() -> tuple[str, str, str] | None:
        if loop.current_session is None or loop.turn_context is None:
            return None
        return (
            loop.current_session.session_id,
            loop.turn_context.turn_id,
            str(loop.turn_context.get("tool_call_id", "")),
        )

    loop.tool_middlewares.append(
        FileCheckpointMiddleware(session_store, safety_guard, checkpoint_context)
    )
    loop.tool_middlewares.append(SecretRedactionMiddleware())

    if config.enable_sprint_planning:
        from core.planner import Planner

        loop.planner = Planner(provider)
        loop.enable_sprint_planning = True

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
    from exceptions import classify_exception, format_error

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
    from exceptions import classify_exception, format_error
    from cli.attachments import PreparedAttachments, prepare_file_mentions

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
