import asyncio

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ash.ui.inline_surface import InlinePromptSurface, format_context_bar
from ash.ui.theme import get_theme


class SizedDummyOutput(DummyOutput):
    columns = 80

    def get_size(self) -> Size:
        return Size(rows=20, columns=self.columns)

    def get_rows_below_cursor_position(self) -> int:
        return 20


def test_context_bar_represents_usage_and_clamps_bounds() -> None:
    half = format_context_bar(50, 100)
    assert fragment_list_to_text(to_formatted_text(half)) == "████░░░░  50% "

    full = format_context_bar(120, 100)
    assert fragment_list_to_text(to_formatted_text(full)) == "████████ 100% "

    empty = format_context_bar(-5, 0)
    assert fragment_list_to_text(to_formatted_text(empty)) == "░░░░░░░░   0% "


@pytest.mark.asyncio
async def test_composer_and_status_dock_to_bottom_without_fullscreen() -> None:
    with create_pipe_input() as pipe:
        thinking = [""]
        revision = [0]

        def thinking_provider(_width: int):
            return revision[0], thinking[0]

        surface = InlinePromptSurface(
            history=InMemoryHistory(),
            completer=None,
            status_provider=lambda: "model · reasoning · ~/repo",
            context_provider=lambda: (25, 100),
            thinking_provider=thinking_provider,
            input_mode="emacs",
            keybindings={
                "newline": ["c-j"],
                "open_editor": ["c-x c-e"],
            },
            theme=get_theme("dark"),
            no_color=True,
            input=pipe,
            output=SizedDummyOutput(),
        )
        pending = asyncio.create_task(surface.read())
        await asyncio.sleep(0.05)

        screen = surface.application.renderer._last_screen
        assert screen is not None
        rows = {
            y: "".join(
                cell.char for _x, cell in sorted(cells.items())
            ).rstrip()
            for y, cells in screen.data_buffer.items()
        }
        composer_row = next(y for y, text in rows.items() if "›" in text)
        status_row = next(y for y, text in rows.items() if "model" in text)

        assert composer_row == 18
        assert status_row == 19

        thinking[0] = "thinking content"
        revision[0] += 1
        surface.invalidate()
        await asyncio.sleep(0.05)
        screen = surface.application.renderer._last_screen
        assert screen is not None
        rows = {
            y: "".join(cell.char for _x, cell in sorted(cells.items())).rstrip()
            for y, cells in screen.data_buffer.items()
        }
        reasoning_row = next(
            y for y, text in rows.items() if "thinking content" in text
        )
        composer_row_after = next(y for y, text in rows.items() if "›" in text)
        assert composer_row_after == composer_row
        assert reasoning_row + 2 == composer_row_after

        thinking[0] = ""
        revision[0] += 1
        surface.invalidate()
        await asyncio.sleep(0.05)
        screen = surface.application.renderer._last_screen
        assert screen is not None
        rows = {
            y: "".join(cell.char for _x, cell in sorted(cells.items())).rstrip()
            for y, cells in screen.data_buffer.items()
        }
        assert next(y for y, text in rows.items() if "›" in text) == composer_row

        pipe.send_text("done\r")
        assert await pending == "done"


def test_composer_height_counts_wrapped_rows() -> None:
    output = SizedDummyOutput()
    output.columns = 20
    surface = InlinePromptSurface(
        history=InMemoryHistory(),
        completer=None,
        status_provider=lambda: "",
        context_provider=lambda: (0, 100),
        thinking_provider=lambda _width: (0, ""),
        input_mode="emacs",
        keybindings={"newline": ["c-j"], "open_editor": ["c-x c-e"]},
        theme=get_theme("dark"),
        no_color=True,
        output=output,
    )
    surface.input_buffer.set_document(
        surface.input_buffer.document.__class__("x" * 40, 40),
        bypass_readonly=True,
    )

    assert surface._composer_height() == 3


def test_context_bar_compacts_on_narrow_terminal() -> None:
    output = SizedDummyOutput()
    output.columns = 40
    surface = InlinePromptSurface(
        history=InMemoryHistory(),
        completer=None,
        status_provider=lambda: "model · reasoning · ~/very/long/project",
        context_provider=lambda: (50, 100),
        thinking_provider=lambda _width: (0, ""),
        input_mode="emacs",
        keybindings={"newline": ["c-j"], "open_editor": ["c-x c-e"]},
        theme=get_theme("dark"),
        no_color=True,
        output=output,
    )

    assert surface._context_width() == 10
    assert fragment_list_to_text(to_formatted_text(surface._context_text())) == (
        "██░░  50% "
    )
