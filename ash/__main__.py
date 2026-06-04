"""Entry point: ``python -m ash``.

Loads configuration, wires every module together, and runs an interactive
REPL on stdin. The REPL accepts a single user prompt per line and prints
the assistant's final response after tool calls complete. ``exit`` or
``quit`` (or EOF) terminates the session.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from ash.config import AshConfig
from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.providers.anthropic import AnthropicProvider
from ash.providers.base import ProviderABC
from ash.safety.guard import SafetyGuard
from ash.tools.command import RunCommandTool
from ash.tools.filesystem import ReadFileTool, ReplaceFileContentTool, WriteFileTool
from ash.ui.terminal import TerminalUI


def _build_provider(config: AshConfig) -> ProviderABC:
    if config.provider == "anthropic":
        return AnthropicProvider(model_name=config.model_name, api_key=config.api_key)
    raise ValueError(f"Unsupported provider: {config.provider!r}")


def _build_tools(safety_guard: SafetyGuard) -> dict[str, Any]:
    return {
        ReadFileTool(safety_guard).name: ReadFileTool(safety_guard),
        WriteFileTool(safety_guard).name: WriteFileTool(safety_guard),
        ReplaceFileContentTool(safety_guard).name: ReplaceFileContentTool(safety_guard),
        RunCommandTool(safety_guard).name: RunCommandTool(safety_guard),
    }


async def _repl(loop: AshLoop) -> int:
    print("ash — type 'exit' to quit", flush=True)
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
        try:
            response = await loop.run_turn(user_input)
        except Exception as exc:  # noqa: BLE001
            print(f"Error: {exc}", file=sys.stderr, flush=True)
            continue
        print(response, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ash", description="Ash coding harness REPL")
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
    tools = _build_tools(safety_guard)

    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=ui,
        project_root=config.workspace_root,
        tools=tools,
    )

    return asyncio.run(_bootstrap_and_repl(loop, session_id=args.session))


async def _bootstrap_and_repl(loop: AshLoop, *, session_id: str | None) -> int:
    await loop.start_session(session_id)
    return await _repl(loop)


if __name__ == "__main__":
    raise SystemExit(main())
