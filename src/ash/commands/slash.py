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
    SlashCommand("help", "Show available commands", "/help [query]"),
    SlashCommand("status", "Show session and runtime status", "/status"),
    SlashCommand("cancel", "Cancel the running turn", "/cancel"),
    SlashCommand(
        "model", "Choose or switch the active model", "/model [provider/model]"
    ),
    SlashCommand(
        "models",
        "List known models; --refresh probes the live endpoint",
        "/models [--refresh]",
    ),
    SlashCommand("new", "Start a new session", "/new", aliases=("clear",)),
    SlashCommand("sessions", "List or search recent sessions", "/sessions [query]"),
    SlashCommand("resume", "Resume a session by ID or name", "/resume [session]"),
    SlashCommand("rename", "Rename the current session", "/rename <title>"),
    SlashCommand(
        "fork",
        "Fork the session at a message boundary",
        "/fork [message-count] [branch-name]",
    ),
    SlashCommand("tree", "Show the current session branch tree", "/tree"),
    SlashCommand(
        "rewind",
        "Rewind transcript, optionally restoring direct file edits",
        "/rewind <message-count> [--files]",
    ),
    SlashCommand("undo", "Undo Ash's latest direct file edits", "/undo"),
    SlashCommand(
        "export", "Export a redacted transcript", "/export [jsonl|markdown] [path]"
    ),
    SlashCommand("import", "Import an Ash JSONL transcript", "/import <path>"),
    SlashCommand(
        "context",
        "Show context usage or --provenance details",
        "/context [--provenance]",
    ),
    SlashCommand("compact", "Compact older conversation history", "/compact"),
    SlashCommand(
        "capabilities",
        "Show the active model's negotiated capabilities",
        "/capabilities",
    ),
    SlashCommand("plan", "Toggle editable sprint planning", "/plan [on|off]"),
    SlashCommand("skills", "List available instruction skills", "/skills [query]"),
    SlashCommand(
        "plugins",
        "List or manage local and HTTPS Git plugins",
        "/plugins [install PATH|URL --ref REF|enable NAME|disable NAME|uninstall NAME --yes]",
    ),
    SlashCommand(
        "reload-plugins", "Reload active plugin components", "/reload-plugins"
    ),
    SlashCommand("hooks", "List trusted command hook configs", "/hooks"),
    SlashCommand("commands", "List custom Markdown commands", "/commands"),
    SlashCommand(
        "agents",
        "Show basic or full subagent status; stop or resume",
        "/agents [--full] [stop|resume AGENT_ID]",
    ),
    SlashCommand(
        "diff",
        "Show the current Git diff or latest Ash turn checkpoint diff",
        "/diff [--staged|--turn] [path]",
    ),
    SlashCommand(
        "review",
        "Review Git changes with the active model",
        "/review [worktree|staged|commit REF|branch BASE]",
    ),
    SlashCommand(
        "permissions",
        "Inspect/change mode or grants",
        "/permissions [mode|allow TOOL|ask TOOL|deny TOOL|revoke TOOL|remove RULE_ID]",
    ),
    SlashCommand("sandbox", "Show active sandbox capabilities", "/sandbox"),
    SlashCommand("doctor", "Run local diagnostics", "/doctor"),
    SlashCommand(
        "mcp",
        "Inspect or reload live MCP servers and capabilities",
        "/mcp [status|refresh|tools|resources|prompts|tasks|cancel SERVER TASK_ID]",
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


def matching_commands(query: str | None = None) -> tuple[SlashCommand, ...]:
    """Return slash commands matching a free-text query."""

    normalized_query = " ".join((query or "").split()).casefold()
    return tuple(
        command
        for command in COMMANDS
        if not normalized_query or _command_matches(command, normalized_query)
    )


def render_help(query: str | None = None) -> str:
    """Render a stable, compact command reference."""

    commands = matching_commands(query)
    if not commands:
        return f"No slash commands match {query!r}."
    width = max(len(command.usage) for command in commands)
    return "\n".join(
        f"{command.usage:<{width}}  {command.description}"
        f"{_render_aliases(command.aliases)}"
        for command in commands
    )


def _command_matches(command: SlashCommand, query: str) -> bool:
    fields = (
        command.name,
        command.description,
        command.usage,
        *(command.aliases),
        *(f"/{alias}" for alias in command.aliases),
    )
    return any(query in field.casefold() for field in fields)


def _render_aliases(aliases: tuple[str, ...]) -> str:
    if not aliases:
        return ""
    rendered = ", ".join(f"/{alias}" for alias in aliases)
    return f" (aliases: {rendered})"
