"""Slash-command metadata and parsing for interactive Ash sessions."""

from __future__ import annotations

import shlex
import textwrap
from dataclasses import dataclass
from difflib import get_close_matches


@dataclass(frozen=True)
class SlashCommand:
    name: str
    description: str
    usage: str
    aliases: tuple[str, ...] = ()


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("help", "Show available commands", "/help [query]"),
    SlashCommand("status", "Show session and runtime status", "/status"),
    SlashCommand(
        "usage",
        "Show session token, cache, and cost usage",
        "/usage",
        aliases=("cost",),
    ),
    SlashCommand("settings", "Show active runtime settings", "/settings"),
    SlashCommand(
        "mouse",
        "Toggle or set full-screen terminal mouse capture",
        "/mouse [on|off|toggle]",
    ),
    SlashCommand("cancel", "Cancel the running turn", "/cancel"),
    SlashCommand(
        "model",
        "Switch this session's model; ash setup model saves a default",
        "/model [provider/model]",
    ),
    SlashCommand(
        "models",
        "List known models; --refresh probes the live endpoint",
        "/models [--refresh]",
    ),
    SlashCommand(
        "new", "Start a new session", "/new", aliases=("clear", "reset")
    ),
    SlashCommand(
        "sessions",
        "List sessions or search prior conversation text",
        "/sessions [search QUERY|prune DAYS]",
    ),
    SlashCommand(
        "resume",
        "Resume a session by ID or name",
        "/resume [session]",
        aliases=("continue",),
    ),
    SlashCommand("retry", "Retry the last user turn", "/retry"),
    SlashCommand("rename", "Rename the current session", "/rename <title>"),
    SlashCommand(
        "recovery",
        "Inspect interrupted-turn recovery and items needing attention",
        "/recovery",
    ),
    SlashCommand(
        "fork",
        "Fork the session at a message boundary",
        "/fork [message-count] [branch-name]",
        aliases=("branch",),
    ),
    SlashCommand("tree", "Show the current session branch tree", "/tree"),
    SlashCommand(
        "rewind",
        "Rewind transcript, optionally restoring direct file edits",
        "/rewind <message-count> [--files]",
        aliases=("checkpoint",),
    ),
    SlashCommand("undo", "Undo Ash's latest direct file edits", "/undo"),
    SlashCommand(
        "export", "Export a redacted transcript", "/export [jsonl|markdown] [path]"
    ),
    SlashCommand("copy", "Copy the latest assistant response", "/copy"),
    SlashCommand("import", "Import an Ash JSONL transcript", "/import <path>"),
    SlashCommand(
        "context",
        "Show context usage or --provenance details",
        "/context [--provenance]",
    ),
    SlashCommand(
        "compact",
        "Compact older conversation history",
        "/compact",
        aliases=("compress",),
    ),
    SlashCommand(
        "capabilities",
        "Show the active model's negotiated capabilities",
        "/capabilities [--refresh]",
    ),
    SlashCommand("plan", "Toggle editable sprint planning", "/plan [on|off]"),
    SlashCommand(
        "goal",
        "Run a durable session objective until verified, paused, or bounded",
        "/goal [pause|resume|clear|OBJECTIVE]",
    ),
    SlashCommand("skills", "List available instruction skills", "/skills [query]"),
    SlashCommand(
        "plugins",
        "List or manage local, Git, and catalog plugins",
        "/plugins [install TARGET [--replace] [--ref REF]|update NAME|update --all|enable NAME|disable NAME|uninstall NAME --yes]",
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
        aliases=("tasks",),
    ),
    SlashCommand(
        "processes",
        "List or stop managed background processes",
        "/processes [stop JOB_ID]",
        aliases=("ps", "jobs"),
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
        "Inspect modes/rules or change this session's mode",
        "/permissions [modes|MODE|allow TOOL|ask TOOL|deny TOOL|revoke TOOL|remove RULE_ID]",
    ),
    SlashCommand("sandbox", "Show active sandbox capabilities", "/sandbox"),
    SlashCommand(
        "browser",
        "Attach or return browser tools at runtime",
        "/browser [status|inspect [URL]|connect [URL] [--reuse-storage-state]|disconnect|reset-profile]",
    ),
    SlashCommand("doctor", "Run local diagnostics", "/doctor"),
    SlashCommand(
        "mcp",
        "Inspect, authorize, or reload live MCP servers and capabilities",
        "/mcp [status [--json]|refresh [SERVER]|login SERVER|logout SERVER|tools|resources|prompts|watch SERVER URI|unwatch SERVER URI|watches [SERVER]|tasks|cancel SERVER TASK_ID]",
    ),
    SlashCommand(
        "memory",
        "Inspect, index, search, export, or clear memory",
        "/memory [status|index PATH|index-workspace [LIMIT]|search QUERY|export|clear]",
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
        normalized = parts[0].casefold()
        match = get_close_matches(normalized, _COMMAND_LOOKUP, n=1, cutoff=0.6)
        suggestion = f" Did you mean /{match[0]}?" if match else ""
        raise ValueError(
            f"Unknown command: /{parts[0]}.{suggestion} Use /help for commands."
        )
    return command, parts[1:]


def matching_commands(
    query: str | None = None,
    *,
    commands: tuple[SlashCommand, ...] = COMMANDS,
) -> tuple[SlashCommand, ...]:
    """Return slash commands matching a free-text query."""

    normalized_query = " ".join((query or "").split()).casefold()
    return tuple(
        command
        for command in commands
        if not normalized_query or _command_matches(command, normalized_query)
    )


def render_help(
    query: str | None = None,
    *,
    commands: tuple[SlashCommand, ...] = COMMANDS,
) -> str:
    """Render a stable, compact command reference."""

    matches = matching_commands(query, commands=commands)
    if not matches:
        return f"No slash commands match {query!r}."
    width = min(max(len(command.usage) for command in matches), 48)
    lines: list[str] = []
    for command in matches:
        suffix = command.description + _render_aliases(command.aliases)
        if len(command.usage) > width:
            lines.extend(
                textwrap.wrap(
                    command.usage,
                    width=96,
                    subsequent_indent="  ",
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            )
            lines.append(f"  {suffix}")
        else:
            lines.append(f"{command.usage:<{width}}  {suffix}")
    return "\n".join(lines)


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
