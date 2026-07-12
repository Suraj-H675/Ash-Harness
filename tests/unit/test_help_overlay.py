import pytest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

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
