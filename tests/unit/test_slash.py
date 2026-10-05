import ast
import inspect
from pathlib import Path

import pytest

import ash.cli as cli_module
from ash.commands.slash import COMMANDS, SlashCommand, parse_slash_command, render_help


def test_parse_normal_prompt_returns_none() -> None:
    assert parse_slash_command("fix the tests") is None


def test_parse_alias_and_quoted_argument() -> None:
    command, arguments = parse_slash_command('/rename "release work"')
    assert command.name == "rename"
    assert arguments == ["release work"]

    alias, _ = parse_slash_command("/clear")
    assert alias.name == "new"

    expected_aliases = {
        "/reset": "new",
        "/continue": "resume",
        "/branch": "fork",
        "/checkpoint": "rewind",
        "/compress": "compact",
        "/tasks": "agents",
    }
    for text, canonical in expected_aliases.items():
        parsed, _ = parse_slash_command(text)
        assert parsed.name == canonical


def test_unknown_command_has_helpful_error() -> None:
    with pytest.raises(ValueError, match="/help"):
        parse_slash_command("/wat")


def test_unknown_command_suggests_close_match() -> None:
    with pytest.raises(ValueError, match=r"Did you mean /model\?"):
        parse_slash_command("/modle")


def test_help_lists_core_session_commands() -> None:
    rendered = render_help()
    assert "/sessions [search QUERY|prune DAYS]" in rendered
    assert "/rewind <message-count> [--files]" in rendered
    assert "/resume [session]" in rendered
    assert "/status" in rendered
    assert "/usage" in rendered
    assert "/settings" in rendered
    assert "/cancel" in rendered
    assert "/retry" in rendered
    assert "/review [worktree|staged|commit REF|branch BASE]" in rendered
    assert "/diff [--staged|--turn] [path]" in rendered
    assert "/copy" in rendered
    assert "/plan [on|off]" in rendered
    assert "/processes [stop JOB_ID]" in rendered
    assert "/goal [pause|resume|clear|OBJECTIVE]" in rendered
    assert "/hooks" in rendered
    assert "/reload-plugins" in rendered
    assert "update NAME" in rendered
    assert "update --all" in rendered
    assert "/help [query]" in rendered
    assert "aliases: /clear, /reset" in rendered
    assert "/capabilities [--refresh]" in rendered
    assert (
        "/memory [status|index PATH|index-workspace [LIMIT]|search QUERY|export|clear]"
        in rendered
    )
    assert max(len(line) for line in rendered.splitlines()) < 140


def test_help_places_long_usage_description_on_next_line() -> None:
    rendered = render_help("mcp")
    lines = rendered.splitlines()

    assert lines[0].startswith("/mcp [status")
    assert any(
        line.startswith("  Inspect, authorize, or reload live MCP servers")
        for line in lines[1:]
    )


def test_help_filters_by_command_alias_and_description() -> None:
    assert "/review [worktree|staged|commit REF|branch BASE]" in render_help("git")
    assert "/diff [--staged|--turn] [path]" in render_help("checkpoint")
    assert "/new" in render_help("clear")
    assert "/exit" in render_help("/quit")
    assert render_help("definitely-not-a-command") == (
        "No slash commands match 'definitely-not-a-command'."
    )


def test_help_lists_mcp_authorization_actions() -> None:
    rendered = render_help("mcp")

    assert "/mcp [status [--json]|refresh [SERVER]|login SERVER|logout SERVER|" in rendered
    assert "Inspect, authorize, or reload live MCP servers" in rendered


def test_help_lists_browser_runtime_actions() -> None:
    rendered = render_help("browser")

    assert (
        "/browser [status|inspect [URL]|connect [URL] [--reuse-storage-state]|"
        "disconnect|reset-profile]" in rendered
    )
    assert "Attach or return browser tools at runtime" in rendered


def test_help_can_render_runtime_custom_command_catalog() -> None:
    commands = (
        *COMMANDS,
        SlashCommand(
            "project:review",
            "Review this project's release notes",
            "/project:review [arguments]",
        ),
    )

    rendered = render_help("release", commands=commands)

    assert "/project:review [arguments]" in rendered
    assert "Review this project's release notes" in rendered


def test_every_registered_slash_command_has_a_repl_handler() -> None:
    source_path = Path(inspect.getsourcefile(cli_module) or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    handled: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not isinstance(node.left, ast.Attribute) or node.left.attr != "name":
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                handled.add(comparator.value)
            elif isinstance(comparator, (ast.Set, ast.Tuple, ast.List)):
                handled.update(
                    element.value
                    for element in comparator.elts
                    if isinstance(element, ast.Constant)
                    and isinstance(element.value, str)
                )

    registered = {command.name for command in COMMANDS}
    assert registered - handled == set()
