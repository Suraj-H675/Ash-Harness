import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.output import DummyOutput
from rich.cells import cell_len

from ash.commands.slash import SlashCommand
from ash.ui.help_overlay import HelpOverlay


class SizedDummyOutput(DummyOutput):
    def __init__(self, columns: int) -> None:
        self.columns = columns

    def get_size(self) -> Size:
        return Size(rows=24, columns=self.columns)


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


@pytest.mark.parametrize("columns", [1, 2, 3, 4, 6, 8, 12, 18, 28, 40])
def test_help_overlay_keeps_commands_visible_on_tiny_terminals(
    monkeypatch, columns
) -> None:
    overlay = HelpOverlay(
        [SlashCommand("inspect", "Show diagnostics", "/inspect")],
        output=SizedDummyOutput(columns),
    )
    monkeypatch.setattr("ash.ui.help_overlay.get_app_or_none", lambda: overlay.application)

    row = "".join(fragment[1] for fragment in overlay._render_list()).rstrip("\n")
    assert cell_len(row) <= columns
    assert row.startswith(">")
    if 4 <= columns <= 12:
        assert "/" in row


def test_help_overlay_escapes_untrusted_custom_command_metadata() -> None:
    overlay = HelpOverlay(
        [
            SlashCommand(
                "plugin:run\x1b[2J\u202eforged\u202c",
                "Run a plugin\x1b]0;owned\x07\nmore info",
                "/plugin:run\x1b[2J [target]",
                aliases=("plugin-alias\x1b[3J",),
            )
        ],
        output=DummyOutput(),
    )
    row = "".join(fragment[1] for fragment in overlay._render_list())
    detail = "".join(fragment[1] for fragment in overlay._render_detail())
    assert "\x1b" not in row + detail
    assert "\u202e" not in row + detail
    assert "\\x1b[2J" in row + detail
    assert "\\u202e" in detail
    assert "\\x1b[3J" in detail
    assert "\\x0a" in row
