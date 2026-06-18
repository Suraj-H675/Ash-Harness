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


AVAILABLE_MODELS: dict[str, list[str]] = {
    "anthropic": [
        "claude-3-7-sonnet-20250219",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-haiku-20241022",
        "claude-opus-4-20250514",
    ],
    "openai": [
        "gpt-4o",
        "gpt-4o-mini",
        "o3",
        "o4-mini",
    ],
    "ollama": [
        "llama3",
        "qwen2.5-coder:7b",
    ],
    "deepseek": [
        "deepseek-chat",
        "deepseek-reasoner",
    ],
    "groq": [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "qwen/qwen3.3-32b",
        "groq/compound-mini",
    ],
    "openai-compatible": [
        "<your-model>",
    ],
}


def _build_provider(config: AshConfig) -> ProviderABC:
    if config.provider == "anthropic":
        from providers.anthropic import AnthropicProvider

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        base_url = os.environ.get("ANTHROPIC_API_BASE") or None
        prov = AnthropicProvider(
            model_name=config.model,
            api_key=api_key,
            base_url=base_url,
        )
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov

    elif config.provider == "openai":
        from providers.openai import OpenAIProvider

        api_key = os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("OPENAI_API_BASE") or None
        prov = OpenAIProvider(
            model_name=config.model,
            api_key=api_key,
            base_url=base_url,
        )
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov

    elif config.provider == "ollama":
        from providers.ollama import OllamaProvider

        base_url = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
        prov = OllamaProvider(
            model_name=config.model,
            base_url=base_url,
        )
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov

    elif config.provider == "deepseek":
        from providers.deepseek import DeepSeekProvider

        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        base_url = os.environ.get("DEEPSEEK_API_BASE") or None
        prov = DeepSeekProvider(
            model_name=config.model,
            api_key=api_key,
            base_url=base_url,
        )
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov

    elif config.provider == "groq":
        from providers.groq import GroqProvider

        api_key = os.environ.get("GROQ_API_KEY", "")
        base_url = os.environ.get("GROQ_API_BASE") or None
        prov = GroqProvider(
            model_name=config.model,
            api_key=api_key,
            base_url=base_url,
        )
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov

    elif config.provider == "openai-compatible":
        from providers.openai import OpenAIProvider

        api_key = os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("OPENAI_API_BASE", "")
        prov = OpenAIProvider(
            model_name=config.model,
            api_key=api_key,
            base_url=base_url if base_url else None,
        )
        prov.configure_max_tokens(config.max_completion_tokens)
        return prov

    raise ValueError(f"Unsupported provider: {config.provider!r}")


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


def _print_providers(config: AshConfig) -> None:
    print("\nAvailable providers:")
    for prov in AVAILABLE_MODELS:
        marker = " (current)" if prov == config.provider else ""
        print(f"  {prov}{marker}")
    print()


def _print_model_list(config: AshConfig) -> None:
    print("\nAvailable models:")
    for prov, models in AVAILABLE_MODELS.items():
        marker = " (current)" if prov == config.provider else ""
        print(f"  {prov}{marker}: {', '.join(models)}")
    print()


def _interactive_model_picker(config: AshConfig, loop: AshLoop) -> None:
    """Show numbered list of all provider-model pairs, let user pick one."""
    print("\nAvailable models:")
    numbered: list[tuple[str, str]] = []
    for i, (prov, models) in enumerate(AVAILABLE_MODELS.items(), 1):
        for j, model in enumerate(models, 1):
            marker = " ← current" if prov == config.provider and model == config.model else ""
            print(f"  [{i}-{j}] {prov}/{model}{marker}")
            numbered.append((prov, model))

    choice = input("Pick [provider-model, or 'c' to cancel]: ").strip()
    if choice.lower() == "c":
        return
    try:
        parts = choice.split("-")
        prov_idx = int(parts[0]) - 1
        model_idx = int(parts[1]) - 1
        prov_keys = list(AVAILABLE_MODELS.keys())
        prov = prov_keys[prov_idx]
        model = AVAILABLE_MODELS[prov][model_idx]
    except (ValueError, IndexError, KeyError):
        print("Invalid selection.")
        return

    loop.switch_provider(prov, model)
    config.provider = prov
    config.model = model
    print(f"Switched to {prov}/{model}\n")


async def _repl(loop: AshLoop, config: AshConfig) -> int:
    print(
        "ash — '/model' to switch, '/providers' to list, '/models' to list models, 'exit' to quit",
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

        # /model with no args → interactive picker
        if user_input == "/model":
            _interactive_model_picker(config, loop)
            continue

        # /model <modelname> → switch model within current provider
        if user_input.startswith("/model "):
            arg = user_input[7:].strip()
            # If it contains a slash, treat as provider/model
            if "/" in arg:
                prov, model = arg.split("/", 1)
            else:
                prov = config.provider
                model = arg
            try:
                loop.switch_provider(prov, model)
                config.provider = prov
                config.model = model
                print(f"Switched to {prov}/{model}", flush=True)
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr, flush=True)
            continue

        # /providers → list
        if user_input == "/providers":
            _print_providers(config)
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
