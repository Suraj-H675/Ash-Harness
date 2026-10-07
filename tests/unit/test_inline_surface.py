import asyncio

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.formatted_text.utils import fragment_list_to_text
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth

from ash.ui.inline_surface import (
    ActivityDockView,
    InlinePromptSurface,
    format_context_bar,
)
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
            status_provider=lambda: "model · effort high · ~/repo",
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
        status_text = rows[status_row]

        assert composer_row == 18
        assert status_row == 19
        assert status_text.index("model") < status_text.index("25%")

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
        assert reasoning_row + 1 == composer_row_after

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


def test_status_and_context_stay_inline_and_fit_narrow_terminals() -> None:
    output = SizedDummyOutput()
    output.columns = 40
    surface = InlinePromptSurface(
        history=InMemoryHistory(),
        completer=None,
        status_provider=lambda: "model · effort high · ~/very/long/project",
        context_provider=lambda: (50, 100),
        thinking_provider=lambda _width: (0, ""),
        input_mode="emacs",
        keybindings={"newline": ["c-j"], "open_editor": ["c-x c-e"]},
        theme=get_theme("dark"),
        no_color=True,
        output=output,
    )

    status = fragment_list_to_text(to_formatted_text(surface._status_text()))
    assert status.startswith(" model · effort high · ~/ve…")
    assert "  ██░░  50% " in status
    assert len(status) == 40

    output.columns = 20
    narrow = fragment_list_to_text(to_formatted_text(surface._status_text()))
    assert len(narrow) == 20
    assert "█░  50% " in narrow


def test_dock_animation_is_width_stable_and_reduced_motion_is_static() -> None:
    output = SizedDummyOutput()
    view = ActivityDockView(
        revision=1,
        activity="Inspecting",
        reasoning="checking the import path",
    )
    surface = InlinePromptSurface(
        history=InMemoryHistory(),
        completer=None,
        status_provider=lambda: "",
        context_provider=lambda: (0, 100),
        dock_provider=lambda: view,
        input_mode="emacs",
        keybindings={"newline": ["c-j"], "open_editor": ["c-x c-e"]},
        theme=get_theme("dark"),
        no_color=True,
        output=output,
    )

    frames = []
    for phase in range(3):
        surface._dot_phase = phase
        rendered = fragment_list_to_text(
            to_formatted_text(surface._format_dock(view, 60))
        )
        frames.append(rendered)
    assert len({get_cwidth(frame) for frame in frames}) == 1
    assert all("Inspecting" in frame for frame in frames)
    assert all("checking the import path" in frame for frame in frames)

    surface.reduced_motion = True
    static = fragment_list_to_text(to_formatted_text(surface._format_dock(view, 60)))
    assert static == "Inspecting  ·  checking the import path"

    reasoning_only = ActivityDockView(
        revision=2,
        activity=None,
        reasoning="provider reasoning",
    )
    narrow = fragment_list_to_text(
        to_formatted_text(surface._format_dock(reasoning_only, 8))
    )
    assert narrow.startswith("Reason")
    assert "provider" not in narrow


@pytest.mark.asyncio
async def test_dock_animates_without_events_and_collapses_when_idle() -> None:
    with create_pipe_input() as pipe:
        output = SizedDummyOutput()
        state = [ActivityDockView(revision=0, activity=None, reasoning="")]
        surface = InlinePromptSurface(
            history=InMemoryHistory(),
            completer=None,
            status_provider=lambda: "model · effort high · ~/repo",
            context_provider=lambda: (0, 100),
            dock_provider=lambda: state[0],
            input_mode="emacs",
            keybindings={"newline": ["c-j"], "open_editor": ["c-x c-e"]},
            theme=get_theme("dark"),
            no_color=True,
            input=pipe,
            output=output,
        )
        pending = asyncio.create_task(surface.read())
        await asyncio.sleep(0.05)

        def capture_rows() -> dict[int, str]:
            screen = surface.application.renderer._last_screen
            assert screen is not None
            return {
                y: "".join(cell.char for _x, cell in sorted(cells.items())).rstrip()
                for y, cells in screen.data_buffer.items()
            }

        initial = capture_rows()
        composer_row = next(y for y, text in initial.items() if "›" in text)
        assert composer_row == 18

        state[0] = ActivityDockView(
            revision=1,
            activity="Inspecting",
            reasoning="checking the import path",
        )
        surface.invalidate()
        await asyncio.sleep(0.05)
        first = capture_rows()
        first_status_row = next(y for y, text in first.items() if "Inspecting" in text)
        active_composer_row = next(y for y, text in first.items() if "›" in text)
        assert first_status_row + 1 == active_composer_row
        assert "checking the import path" in first[first_status_row]

        await asyncio.sleep(0.5)
        second = capture_rows()
        second_status = next(text for text in second.values() if "Inspecting" in text)
        await asyncio.sleep(0.5)
        third = capture_rows()
        third_status = next(text for text in third.values() if "Inspecting" in text)
        assert first[first_status_row] != second_status
        assert second_status != third_status

        state[0] = ActivityDockView(revision=2, activity=None, reasoning="")
        surface.invalidate()
        await asyncio.sleep(0.05)
        idle = capture_rows()
        assert not any("Inspecting" in text for text in idle.values())
        assert next(y for y, text in idle.items() if "›" in text) == composer_row

        pipe.send_text("done\r")
        assert await pending == "done"
