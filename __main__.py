"""Entry point: ``python -m ash``.

Loads configuration, wires every module together, and runs an interactive
REPL on stdin. The REPL accepts a single user prompt per line and prints
the assistant's final response after tool calls complete. ``exit`` or
``quit`` (or EOF) terminates the session.
"""

from __future__ import annotations

import argparse
import asyncio
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
from tools.filesystem import (
    ReadFileTool,
    ReplaceFileContentTool,
    WholeEditTool,
    WriteFileTool,
)
from tools.git import AutoCommitTool
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
    "anthropic/claude-3-7-sonnet-20250219",
    "anthropic/claude-3-5-sonnet-20241022",
    "anthropic/claude-3-5-haiku-20241022",
    "anthropic/claude-opus-4-20250514",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "openai/o3",
    "openai/o4-mini",
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
        prov = OpenAIProvider(
            model_name=model_name,
            api_key=cp.get("api_key", ""),
            base_url=cp.get("base_url"),
        )
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov

    raise ValueError(f"Unknown provider in model string: {provider!r}")


def _build_tools(
    safety_guard: SafetyGuard, project_root: Path | None = None
) -> dict[str, Any]:
    from tools.agent import SpawnAgentTool

    root = project_root if project_root is not None else safety_guard.project_root
    return {
        ReadFileTool(safety_guard).name: ReadFileTool(safety_guard),
        WriteFileTool(safety_guard).name: WriteFileTool(safety_guard),
        ReplaceFileContentTool(safety_guard).name: ReplaceFileContentTool(safety_guard),
        WholeEditTool(safety_guard).name: WholeEditTool(safety_guard),
        RunCommandTool(safety_guard, project_root=root).name: RunCommandTool(
            safety_guard, project_root=root
        ),
        AutoCommitTool(safety_guard).name: AutoCommitTool(safety_guard),
        SpawnAgentTool(safety_guard, None).name: SpawnAgentTool(safety_guard, None),
    }


def _print_model_list(config: AshConfig) -> None:
    """Show models grouped by provider."""
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
            print(f"  {model}{marker}")
    print()


def _interactive_model_picker(config: AshConfig, loop: AshLoop) -> None:
    """Show models grouped by provider, let user pick by provider number."""
    # Determine current
    try:
        current_provider, current_model = _parse_model_string(config.model)
    except ValueError:
        current_provider, current_model = "anthropic", config.model

    # Group by provider
    grouped: dict[str, list[tuple[int, str]]] = {}
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
    print(
        "ash — '/model' to switch, '/models' to list models, 'exit' to quit",
        flush=True,
    )
    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            return 0

        # /model with no args → interactive picker (from setup wizard)
        if user_input == "/model":
            from cli.setup import setup_model_provider

            old_model = config.model
            setup_model_provider(config)
            # Reload config after wizard may have written new ASH_MODEL
            config = AshConfig.load()
            if config.model != old_model:
                loop.switch_model(config.model)
            continue

        # /model provider/model → switch to full string
        if user_input.startswith("/model "):
            model_str = user_input[7:].strip()
            if "/" not in model_str:
                print(
                    "Error: model must be in provider/model format (e.g. anthropic/claude-3-7-sonnet)",
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
        if user_input == "/models":
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
    mcp_subparser = subparsers.add_parser("mcp")
    mcp_subparser.add_argument("action", choices=["add", "list", "remove"])
    mcp_subparser.add_argument("server_name", nargs="?")
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
    args = parser.parse_args(argv)

    if args.command == "setup":
        from cli.setup import cmd_setup

        return cmd_setup(args)

    if args.command == "mcp":
        from mcp.server import load_mcp_servers

        servers = load_mcp_servers()
        if args.action == "list":
            for name, cfg in servers.items():
                print(f"{name}: {cfg.command} {' '.join(cfg.args)}")
            return 0
        print(f"mcp {args.action}: {args.server_name or '(no server specified)'}")
        return 0

    config = AshConfig.load()

    # First-run detection: prompt to run setup if no provider configured
    from cli.setup import _has_provider_configured

    if not _has_provider_configured(config):
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

    db_path = config.db_directory / "sessions.db"
    session_store = SessionStore(db_path)
    safety_guard = SafetyGuard(
        project_root=config.workspace_root,
        blocklist_commands=config.command_blocklist,
    )
    provider = _build_provider(config)
    ui = TerminalUI(safety_tier=config.safety_tier)
    tools = _build_tools(safety_guard, config.workspace_root)

    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=ui,
        project_root=config.workspace_root,
        tools=tools,
        config=config,
    )

    return asyncio.run(_bootstrap_and_repl(loop, config, session_id=args.session))


async def _bootstrap_and_repl(
    loop: AshLoop, config: AshConfig, *, session_id: str | None
) -> int:
    await loop.start_session(session_id)
    return await _repl(loop, config)


if __name__ == "__main__":
    raise SystemExit(main())
