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
    assert "/review [worktree|staged|commit REF|branch BASE]" in rendered
