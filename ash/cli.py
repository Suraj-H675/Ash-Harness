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
import os
import sys
from pathlib import Path
from typing import Any

from config import AshConfig
from core.loop import AshLoop
from core.session import SessionStore
from providers.base import ProviderABC
from safety.guard import SafetyGuard
from tools.command import RunCommandTool
from tools.base import BaseTool
from tools.filesystem import (
    ReadFileTool,
    ReplaceFileContentTool,
    WholeEditTool,
    WriteFileTool,
)
from tools.git import AutoCommitTool, GitDiffTool, GitLogTool, GitStatusTool
from ui.terminal import TerminalUI


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


def _build_tools(
    safety_guard: SafetyGuard,
    project_root: Path | None = None,
    *,
    sandbox_manager: Any | None = None,
    allow_project_extensions: bool = False,
    provider_factory: Any | None = None,
    agent_db_path: Path | None = None,
) -> dict[str, Any]:
    from plugins.skills import ActivateSkillTool, ListSkillsTool, SkillCatalog
    from tools.ask_user import AskUserTool
    from tools.patch import ApplyPatchTool
    from tools.process import BackgroundProcessTool
    from tools.search import GlobFilesTool, ListDirectoryTool, SearchTextTool

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
    return {tool.name: tool for tool in tools}


def _print_model_list(config: AshConfig) -> None:
    """Show models grouped by provider."""
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

    print("\nAvailable models:")
    for prov, models in grouped.items():
        print(f"\n{prov.capitalize()}:")
        for model in models:
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
            print(f"  {model} [{', '.join(labels)}]{marker}")
    print()


def _interactive_model_picker(config: AshConfig, loop: AshLoop) -> None:
    """Show models grouped by provider, let user pick by provider number."""
    # Determine current
    try:
        current_provider, current_model = _parse_model_string(config.model)
    except ValueError:
        current_provider, current_model = "anthropic", config.model

    # Group by provider
    grouped: dict[str, list[tuple[int, str, str]]] = {}
    for i, model_str in enumerate(AVAILABLE_MODELS, 1):
        prov, mod = _parse_model_string(model_str)
        grouped.setdefault(prov, []).append((i, mod, model_str))

    print("\nAvailable models:")
    for prov, entries in grouped.items():
        print(f"\n{prov.capitalize()}:")
        for num, mod, _ in entries:
            marker = (
                " ← current"
                if prov == current_provider and mod == current_model
                else ""
            )
            print(f"  [{num}] {mod}{marker}")

    choice = input("\nPick a number (or 'c' to cancel): ").strip()
    if choice.lower() == "c":
        return
    try:
        idx = int(choice) - 1
        model_str = AVAILABLE_MODELS[idx]
    except (ValueError, IndexError):
        print("Invalid selection.")
        return

    try:
        loop.switch_model(model_str)
        config.model = model_str
        print(f"Switched to {model_str}\n")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)


async def _repl(loop: AshLoop, config: AshConfig) -> int:
    from cli.custom_commands import CustomCommandCatalog
    from cli.slash import parse_slash_command, render_help
    from safety.trust import is_workspace_trusted
    from ui.prompt import PromptInput

    command_roots = [(Path.home() / ".ash" / "commands", "user")]
    if is_workspace_trusted(loop.project_root):
        command_roots.append((loop.project_root / ".ash" / "commands", "project"))
    custom_commands = CustomCommandCatalog(tuple(command_roots))
    discovered_commands = custom_commands.discover()

    def status_line() -> str:
        session_id = (
            loop.current_session.session_id[:8] if loop.current_session else "none"
        )
        return (
            f" {config.model} | {loop.permission_policy.mode.value} | "
            f"ctx ~{loop._last_context_tokens} | session {session_id} | "
            f"{loop.project_root} "
        )

    prompt_input = PromptInput(
        status_provider=status_line,
        extra_commands=[command.name for command in discovered_commands],
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

        try:
            parsed_command = parse_slash_command(user_input)
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

        if parsed_command is not None:
            command, arguments = parsed_command
            if command.name == "exit":
                return 0
            if command.name == "help":
                print(render_help(), flush=True)
                continue
            if command.name == "status":
                session = loop.current_session
                capabilities = loop.provider.capabilities
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
                        )
                    ),
                    flush=True,
                )
                continue
            if command.name == "new":
                session = await loop.start_session()
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
                print(f"Imported and resumed session {session.session_id}")
                continue
            if command.name == "context":
                maximum = config.max_context_tokens - config.max_completion_tokens
                has_summary = bool(
                    loop.current_session and loop.current_session.context_summary
                )
                print(
                    f"Context: ~{loop._last_context_tokens}/{maximum} input tokens; "
                    f"summary={'yes' if has_summary else 'no'}",
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
            if command.name == "permissions":
                from safety.policy import PermissionMode, PermissionPolicy
                from safety.grants import load_tool_grants, set_tool_grant

                if not arguments:
                    grants = sorted(loop.permission_policy.persistent_tool_grants)
                    print(f"Permission mode: {loop.permission_policy.mode.value}")
                    print("Persistent grants: " + (", ".join(grants) or "(none)"))
                    continue
                if len(arguments) == 2 and arguments[0] in {"allow", "revoke"}:
                    tool_name = arguments[1]
                    if tool_name not in loop.tools:
                        print(f"Error: unknown tool {tool_name!r}", file=sys.stderr)
                        continue
                    set_tool_grant(
                        loop.project_root, tool_name, arguments[0] == "allow"
                    )
                    loop.permission_policy.persistent_tool_grants = load_tool_grants(
                        loop.project_root
                    )
                    print(f"Persistent grant {arguments[0]}: {tool_name}")
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
                    persistent_tool_grants=loop.permission_policy.persistent_tool_grants,
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
            from cli.setup import setup_model_provider

            old_model = config.model
            setup_model_provider(config)
            # Reload config after wizard may have written new ASH_MODEL
            config = AshConfig.load()
            if config.model != old_model:
                loop.switch_model(config.model)
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
            _print_model_list(config)
            continue

        # Normal turn
        try:
            response = await loop.run_turn(user_input)
        except Exception as exc:  # noqa: BLE001
            print(f"Error: {exc}", file=sys.stderr, flush=True)
            continue
        print(response, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ash", description="Ash coding harness REPL")
    try:
        version = importlib.metadata.version("ash")
    except importlib.metadata.PackageNotFoundError:
        version = "0.1.0"
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
    mcp_subparser.add_argument("action", choices=["add", "list", "remove"])
    mcp_subparser.add_argument("server_name", nargs="?")
    mcp_subparser.add_argument(
        "--transport", choices=["stdio", "http", "sse"], default="stdio"
    )
    mcp_subparser.add_argument("--url", default="")
    mcp_subparser.add_argument("server_command", nargs=argparse.REMAINDER)
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
    args = parser.parse_args(argv)
    if args.json_schema is not None and args.prompt is None:
        parser.error("--json-schema requires --prompt")
    if args.prompt == "-":
        args.prompt = sys.stdin.read()
    elif args.prompt is None and not sys.stdin.isatty() and args.command is None:
        piped_prompt = sys.stdin.read()
        if piped_prompt.strip():
            args.prompt = piped_prompt

    if args.command == "setup":
        from cli.setup import cmd_setup

        return cmd_setup(args)

    if args.command == "doctor":
        from cli.doctor import render_doctor, run_doctor

        checks = asyncio.run(run_doctor(connect=args.connect))
        print(render_doctor(checks, json_output=args.json_output))
        return 1 if any(check.status == "fail" for check in checks) else 0

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

    if args.command == "serve":
        from cli.serve import serve_http

        try:
            return asyncio.run(serve_http(args))
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

    if args.command == "mcp":
        from mcp.server import MCPServerConfig, load_mcp_servers, save_mcp_servers

        path = Path.cwd() / ".mcp.json"
        servers = load_mcp_servers(path)
        if args.action == "list":
            if not servers:
                print("No MCP servers configured.")
            for name, cfg in servers.items():
                target = (
                    cfg.url
                    if cfg.transport != "stdio"
                    else f"{cfg.command} {' '.join(cfg.args)}"
                )
                print(f"{name} [{cfg.transport}]: {target}")
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
        servers[args.server_name] = MCPServerConfig(
            name=args.server_name,
            command=command_parts[0] if command_parts else "",
            args=command_parts[1:],
            env={},
            transport=args.transport,
            url=args.url,
        )
        save_mcp_servers(servers, path)
        print(f"Added MCP server {args.server_name}.")
        return 0

    config = AshConfig.load()
    if args.mode is not None:
        config = config.model_copy(update={"safety_tier": args.mode})

    # First-run detection: prompt to run setup if no provider configured
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

            cmd_setup(
                argparse.Namespace(section="model", quick=True, non_interactive=False)
            )
            config = AshConfig.load()

    if args.db_directory is not None:
        config = AshConfig.load(db_directory=args.db_directory)

    from safety.trust import is_workspace_trusted, set_workspace_trusted

    workspace_trusted = is_workspace_trusted(config.workspace_root)
    if (
        not workspace_trusted
        and args.prompt is None
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
    tools = _build_tools(
        safety_guard,
        config.workspace_root,
        sandbox_manager=sandbox_manager,
        allow_project_extensions=workspace_trusted,
        provider_factory=lambda: _build_provider(config),
        agent_db_path=config.db_directory / "agents.db",
    )

    from context.instructions import discover_instructions, render_instructions

    instruction_text = render_instructions(
        discover_instructions(
            config.workspace_root,
            include_project=workspace_trusted,
        )
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
        tools=tools,
        hooks=hooks,
        additional_instructions=instruction_text,
        config=config,
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
    from safety.grants import load_tool_grants

    loop.permission_policy.persistent_tool_grants = load_tool_grants(
        config.workspace_root
    )
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
        return asyncio.run(_bootstrap_and_repl(loop, config, session_id=args.session))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


async def _bootstrap_and_repl(
    loop: AshLoop, config: AshConfig, *, session_id: str | None
) -> int:
    try:
        await loop.start_session(session_id)
        return await _repl(loop, config)
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
        }
        if schema is not None:
            payload["structured_output"] = validate_structured_output(response, schema)
        ui.emit_result(payload)
        return 0
    except Exception as exc:  # noqa: BLE001
        if ui.output_format in {"json", "stream-json"}:
            ui._emit({"type": "error", "error": str(exc)})
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
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
