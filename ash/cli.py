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
from typing import Any

from config import AshConfig
from core.loop import AshLoop
from core.session import SessionStore
from exceptions import classify_exception, format_error
from providers.base import ProviderABC
from safety.guard import SafetyGuard
from tools.command import RunCommandTool
from tools.base import BaseTool
from tools.filesystem import (
    ReadFileTool,
    ReplaceFileContentTool,
    ReplaceFileEditsTool,
    WholeEditTool,
    WriteFileTool,
)
from tools.git import AutoCommitTool, GitDiffTool, GitLogTool, GitStatusTool
from ui.terminal import TerminalUI
from ash_logging import get_logger


_log = get_logger(__name__)


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
) -> dict[str, Any]:
    from plugins.skills import ActivateSkillTool, ListSkillsTool, SkillCatalog
    from tools.ask_user import AskUserTool
    from tools.patch import ApplyPatchTool
    from tools.process import BackgroundProcessTool
    from tools.search import GlobFilesTool, ListDirectoryTool, SearchTextTool
    from tools.web import WebFetchTool
    from tools.symbols import FindReferencesTool, FindSymbolTool

    root = project_root if project_root is not None else safety_guard.project_root
    skill_roots = [Path.home() / ".ash" / "skills"]
    from plugins.registry import PluginCatalog

    plugin_roots = [(Path.home() / ".ash" / "plugins", "user")]
    if allow_project_extensions:
        skill_roots.append(root / ".ash" / "skills")
        plugin_roots.append((root / ".ash" / "plugins", "project"))
    skill_roots.extend(
        plugin.root for plugin in PluginCatalog(tuple(plugin_roots)).discover()
    )
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
        ),
        AutoCommitTool(safety_guard),
        GitStatusTool(safety_guard),
        GitDiffTool(safety_guard),
        GitLogTool(safety_guard),
        ApplyPatchTool(safety_guard),
        BackgroundProcessTool(safety_guard),
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
        _log.warning("repository map unavailable: {}", exc)
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
    from cli.custom_commands import CustomCommandCatalog
    from cli.slash import parse_slash_command, render_help
    from safety.trust import is_workspace_trusted
    from ui.prompt import PromptInput
    from ui.status import StatusLine
    from ui.turn_input import InteractiveTurnController
    from ui.output import ReplPrinter
    from ui.notifications import TerminalNotifier

    command_roots = [(Path.home() / ".ash" / "commands", "user")]
    if is_workspace_trusted(loop.project_root):
        command_roots.append((loop.project_root / ".ash" / "commands", "project"))
    custom_commands = CustomCommandCatalog(tuple(command_roots))
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

            shell_result = (
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
            )[0]
            print(
                shell_result.get("output") or shell_result.get("error") or "",
                flush=True,
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
                print(render_help(" ".join(arguments) or None), flush=True)
                continue
            if command.name == "status":
                session = loop.current_session
                capabilities = loop.provider.capabilities
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
                            "Tokens: "
                            + (
                                f"{session_usage.prompt_tokens} prompt, "
                                f"{session_usage.completion_tokens} completion"
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
                            f"Cost: ${(session_usage.cost_usd if session_usage is not None else 0.0):.6f}",
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
                if len(arguments) != 1:
                    print(f"Usage: {command.usage}", file=sys.stderr, flush=True)
                    continue
                try:
                    session = await loop.start_session(arguments[0])
                except KeyError as exc:
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
                if loop.current_session is None or len(arguments) != 1:
                    print(f"Usage: {command.usage}", file=sys.stderr, flush=True)
                    continue
                try:
                    count = int(arguments[0])
                    session = loop.session_store.rewind_session(
                        loop.current_session.session_id, count
                    )
                except ValueError as exc:
                    print(f"Error: {exc}", file=sys.stderr, flush=True)
                    continue
                loop.current_session = session
                loop.ui.load_session_transcript(session)
                print(f"Rewound transcript to {len(session.messages)} messages.")
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
                from plugins.registry import PluginCatalog
                from safety.trust import is_workspace_trusted

                roots = [(Path.home() / ".ash" / "plugins", "user")]
                if is_workspace_trusted(loop.project_root):
                    roots.append((loop.project_root / ".ash" / "plugins", "project"))
                catalog = PluginCatalog(tuple(roots))
                discovered = catalog.discover()
                if not discovered:
                    print("No plugins discovered.")
                for plugin in discovered:
                    print(
                        f"{plugin.manifest.name} {plugin.manifest.version} "
                        f"[{plugin.source}] - {plugin.manifest.description}"
                    )
                for path, error in catalog.errors.items():
                    print(f"Invalid plugin {path}: {error}", file=sys.stderr)
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
                paths = [argument for argument in arguments if argument != "--staged"]
                if len(paths) > 1:
                    print(f"Usage: {command.usage}", file=sys.stderr, flush=True)
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
                from sandbox import SandboxManager

                manager = SandboxManager(workspace_root=loop.project_root)
                sandbox_capabilities = ", ".join(
                    f"{name}={'yes' if available else 'no'}"
                    for name, available in manager.capabilities().items()
                )
                print(
                    f"Sandbox: {manager.backend_name} (tier {manager.tier}); "
                    f"network={'enabled' if manager.network else 'disabled'}; "
                    f"{sandbox_capabilities}"
                )
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
            from cli.attachments import expand_file_mentions

            if expand_mentions:
                user_input = expand_file_mentions(user_input, loop.safety_guard)
            response = await turn_controller.run(user_input)
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
        "extensions", help="Inspect trusted skills, plugins, and hooks"
    )
    extensions_parser.add_argument(
        "kind",
        nargs="?",
        choices=["all", "skills", "plugins", "hooks"],
        default="all",
    )
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
    parser.add_argument(
        "--session",
        default=None,
        help="Restore an existing session by id instead of creating a new one.",
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

    if args.command == "setup":
        from cli.setup import cmd_setup

        return cmd_setup(args)

    if args.command == "doctor":
        from cli.doctor import render_doctor, run_doctor

        checks = asyncio.run(run_doctor(connect=args.connect))
        print(render_doctor(checks, json_output=args.json_output))
        return 1 if any(check.status == "fail" for check in checks) else 0

    if args.command == "config":
        from cli.config import explain_config, render_config_explain

        try:
            config = AshConfig.load()
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
        from cli.agents import (
            list_agent_messages,
            list_agent_reports,
            list_agent_statuses,
            render_agent_messages,
            render_agent_reports,
            render_agent_statuses,
            render_sent_agent_message,
            send_agent_message,
        )

        agents_config = AshConfig.load()
        database = agents_config.db_directory / "agents.db"
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
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.command == "extensions":
        from cli.extensions import discover_extensions, render_extension_inventory

        extension_config = AshConfig.load()
        inventory = discover_extensions(extension_config.workspace_root)
        print(
            render_extension_inventory(
                inventory,
                kind=args.kind,
                json_output=args.json,
            )
        )
        return 0

    if args.command == "serve":
        from cli.serve import serve_http

        try:
            return asyncio.run(serve_http(args))
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

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

    runtime_overrides: dict[str, Any] = {}
    if args.mode is not None:
        runtime_overrides["safety_tier"] = args.mode
    if args.ci:
        runtime_overrides.update({"no_color": True, "reduced_motion": True})
    if args.db_directory is not None:
        runtime_overrides["db_directory"] = args.db_directory

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
    from sandbox import SandboxManager

    sandbox_manager = SandboxManager(workspace_root=config.workspace_root)
    if (
        config.safety_tier == "auto_approve"
        and not sandbox_manager.is_fully_isolated()
        and not config.allow_unsafe_auto_approve
    ):
        print(
            "Error: auto_approve requires an available OS sandbox. "
            "Use interactive/auto_edit mode or explicitly set "
            "ASH_ALLOW_UNSAFE_AUTO_APPROVE=true.",
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
    from hooks.config import load_command_hooks

    hook_paths = [Path.home() / ".ash" / "hooks.json"]
    if workspace_trusted:
        hook_paths.append(config.workspace_root / ".ash" / "hooks.json")
    try:
        hooks = load_command_hooks(hook_paths)
    except (OSError, ValueError) as exc:
        print(f"Error loading hooks: {exc}", file=sys.stderr)
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
        mcp_config_path=(
            config.workspace_root / ".mcp.json" if workspace_trusted else None
        ),
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

    def checkpoint_context() -> tuple[str, str] | None:
        if loop.current_session is None or loop.turn_context is None:
            return None
        return loop.current_session.session_id, loop.turn_context.turn_id

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
                    session_id=args.session,
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
                session_id=args.session,
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
    try:
        await loop.start_session(session_id)
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
    try:
        session = await loop.start_session(session_id)
        schema = None
        if json_schema_path is not None:
            schema = _load_json_schema(json_schema_path)
            prompt = (
                f"{prompt}\n\nReturn only JSON matching this schema:\n"
                f"{json.dumps(schema, ensure_ascii=False)}"
            )
        response = await loop.run_turn(prompt)
        payload = {
            "response": response,
            "session_id": session.session_id,
            "model": config.model,
            "context_tokens": loop._last_context_tokens,
            "usage": loop.last_turn_usage,
        }
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
