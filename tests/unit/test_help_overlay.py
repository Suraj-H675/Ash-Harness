import pytest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.output import DummyOutput
from rich.cells import cell_len

from ash.commands.slash import SlashCommand
from ash.ui.help_overlay import HelpOverlay


@pytest.mark.asyncio
async def test_help_overlay_filters_and_closes() -> None:
    with create_pipe_input() as pipe:
        overlay = HelpOverlay(input=pipe, output=DummyOutput())
        pending = overlay.run()
        pipe.send_text("git\r")

        await pending

    assert overlay.search_buffer.text == "git"
    assert overlay._filtered
    assert all("git" in overlay._search_text(command) for command in overlay._filtered)


def test_help_overlay_searches_injected_custom_commands() -> None:
    overlay = HelpOverlay(
        [
            SlashCommand("help", "Show help", "/help"),
            SlashCommand(
                "project:review",
                "Review release notes",
                "/project:review [arguments]",
            ),
        ],
        initial_query="release",
        output=DummyOutput(),
    )

    assert [command.name for command in overlay._filtered] == ["project:review"]


def test_help_overlay_uses_supplied_command_sequence() -> None:
    overlay = HelpOverlay(
        [
            SlashCommand("alpha", "first command", "/alpha"),
            SlashCommand("beta", "second command", "/beta"),
        ],
        initial_query="second",
        output=DummyOutput(),
    )

    assert [command.name for command in overlay._filtered] == ["beta"]


def test_help_overlay_renders_alias_details() -> None:
    overlay = HelpOverlay(
        [SlashCommand("new", "Start a new session", "/new", aliases=("clear",))],
        output=DummyOutput(),
    )

    rendered = "".join(fragment[1] for fragment in overlay._render_detail())

    assert "/new" in rendered
    assert "Start a new session" in rendered
    assert "Aliases: /clear" in rendered


def test_help_overlay_list_keeps_long_commands_within_terminal_width() -> None:
    overlay = HelpOverlay(initial_query="mcp", output=DummyOutput())

    rendered = "".join(fragment[1] for fragment in to_formatted_text(overlay._render_list()))
    line = rendered.splitlines()[0]
    detail = "".join(
        fragment[1] for fragment in to_formatted_text(overlay._render_detail())
    )

    assert cell_len(line) <= 80
    assert line.lstrip().startswith("> /mcp")
    assert "/mcp [status [--json]" in detail


def test_help_overlay_keeps_ash_identity() -> None:
    overlay = HelpOverlay(output=DummyOutput())

    title = overlay.application.layout.container.children[0].content.text
    rendered = "".join(fragment[1] for fragment in to_formatted_text(title))

    assert rendered.startswith("ASH  ·  Slash commands")
