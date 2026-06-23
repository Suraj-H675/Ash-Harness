"""Slash-command metadata and parsing for interactive Ash sessions."""

from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommand:
    name: str
    description: str
    usage: str
    aliases: tuple[str, ...] = ()


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("help", "Show available commands", "/help"),
    SlashCommand("status", "Show session and runtime status", "/status"),
    SlashCommand(
        "model", "Choose or switch the active model", "/model [provider/model]"
    ),
    SlashCommand("models", "List known models", "/models"),
    SlashCommand("new", "Start a new session", "/new", aliases=("clear",)),
    SlashCommand("sessions", "List or search recent sessions", "/sessions [query]"),
    SlashCommand("resume", "Resume a session by ID", "/resume <session-id>"),
    SlashCommand("rename", "Rename the current session", "/rename <title>"),
    SlashCommand(
        "fork", "Fork the session at a message boundary", "/fork [message-count]"
    ),
    SlashCommand(
        "rewind", "Rewind transcript to a message boundary", "/rewind <message-count>"
    ),
    SlashCommand("undo", "Undo Ash's latest direct file edits", "/undo"),
    SlashCommand(
        "export", "Export a redacted transcript", "/export [jsonl|markdown] [path]"
    ),
    SlashCommand("import", "Import an Ash JSONL transcript", "/import <path>"),
    SlashCommand("context", "Show current context usage", "/context"),
    SlashCommand("compact", "Compact older conversation history", "/compact"),
    SlashCommand("plan", "Toggle editable sprint planning", "/plan [on|off]"),
    SlashCommand("skills", "List available instruction skills", "/skills [query]"),
    SlashCommand("plugins", "List discovered declarative plugins", "/plugins"),
    SlashCommand("commands", "List custom Markdown commands", "/commands"),
    SlashCommand("agents", "Show or stop subagents", "/agents [stop AGENT_ID]"),
    SlashCommand("diff", "Show the current Git diff", "/diff [--staged] [path]"),
    SlashCommand(
        "review",
        "Review Git changes with the active model",
        "/review [worktree|staged|commit REF|branch BASE]",
    ),
    SlashCommand(
        "permissions",
        "Inspect/change mode or grants",
        "/permissions [mode|allow TOOL|revoke TOOL]",
    ),
    SlashCommand("sandbox", "Show active sandbox capabilities", "/sandbox"),
    SlashCommand("doctor", "Run local diagnostics", "/doctor"),
    SlashCommand(
        "mcp",
        "Inspect live MCP servers and capabilities",
        "/mcp [status|tools|resources|prompts]",
    ),
    SlashCommand(
        "memory",
        "Inspect, index, search, or clear memory",
        "/memory [status|index PATH|search QUERY|clear]",
    ),
    SlashCommand("exit", "Exit Ash", "/exit", aliases=("quit",)),
)

_COMMAND_LOOKUP = {
    alias: command for command in COMMANDS for alias in (command.name, *command.aliases)
}


def parse_slash_command(text: str) -> tuple[SlashCommand, list[str]] | None:
    """Parse a slash command, returning ``None`` for normal prompts."""

    if not text.startswith("/"):
        return None
    try:
        parts = shlex.split(text[1:])
    except ValueError as exc:
        raise ValueError(f"Invalid command syntax: {exc}") from exc
    if not parts:
        return _COMMAND_LOOKUP["help"], []
    command = _COMMAND_LOOKUP.get(parts[0].casefold())
    if command is None:
        raise ValueError(f"Unknown command: /{parts[0]}. Use /help for commands.")
    return command, parts[1:]


def render_help() -> str:
    """Render a stable, compact command reference."""

    width = max(len(command.usage) for command in COMMANDS)
    return "\n".join(
        f"{command.usage:<{width}}  {command.description}" for command in COMMANDS
    )
