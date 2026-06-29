import pytest

from cli.slash import parse_slash_command, render_help


def test_parse_normal_prompt_returns_none() -> None:
    assert parse_slash_command("fix the tests") is None


def test_parse_alias_and_quoted_argument() -> None:
    command, arguments = parse_slash_command('/rename "release work"')
    assert command.name == "rename"
    assert arguments == ["release work"]

    alias, _ = parse_slash_command("/clear")
    assert alias.name == "new"


def test_unknown_command_has_helpful_error() -> None:
    with pytest.raises(ValueError, match="/help"):
        parse_slash_command("/wat")


def test_help_lists_core_session_commands() -> None:
    rendered = render_help()
    assert "/sessions [query]" in rendered
    assert "/resume <session-id>" in rendered
    assert "/status" in rendered
    assert "/cancel" in rendered
    assert "/review [worktree|staged|commit REF|branch BASE]" in rendered
    assert "/plan [on|off]" in rendered
    assert "/hooks" in rendered
    assert "/help [query]" in rendered
    assert "aliases: /clear" in rendered


def test_help_filters_by_command_alias_and_description() -> None:
    assert "/review [worktree|staged|commit REF|branch BASE]" in render_help("git")
    assert "/new" in render_help("clear")
    assert "/exit" in render_help("/quit")
    assert render_help("definitely-not-a-command") == (
        "No slash commands match 'definitely-not-a-command'."
    )
