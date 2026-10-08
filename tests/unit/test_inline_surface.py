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
from ash.ui.transcript import Transcript


class SizedDummyOutput(DummyOutput):
    columns = 80

    def get_size(self) -> Size:
        return Size(rows=20, columns=self.columns)

    def get_rows_below_cursor_position(self) -> int:
        return 20


def test_context_bar_represents_usage_and_clamps_bounds() -> None:
    half = format_context_bar(50, 100)
    assert fragment_list_to_text(to_formatted_text(half)) == "────╌╌╌╌  50% "

    full = format_context_bar(120, 100)
    assert fragment_list_to_text(to_formatted_text(full)) == "──────── 100% "

    empty = format_context_bar(-5, 0)
    assert fragment_list_to_text(to_formatted_text(empty)) == "╌╌╌╌╌╌╌╌   0% "
    ascii_bar = format_context_bar(50, 100, ascii_only=True)
    assert fragment_list_to_text(to_formatted_text(ascii_bar)) == "----....  50% "


@pytest.mark.asyncio
async def test_composer_and_status_dock_to_bottom_in_fullscreen_app() -> None:
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

        assert surface.application.full_screen is True
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
        await surface.aclose()


def test_transcript_resize_preserves_the_detached_reading_anchor() -> None:
    from ash.ui.transcript_view import TranscriptView

    transcript = Transcript()
    content = (
        "alpha beta gamma delta epsilon zeta eta theta iota kappa "
        "lambda mu nu xi omicron pi rho sigma tau upsilon"
    )
    entry_id = transcript.append("user", content, title="you")
    view = TranscriptView(transcript)
    view.create_content(12, 2)
    view.scroll(-2)

    assert view.detached
    original_offset = view._anchor_offset
    view.create_content(7, 2)
    resized_top = view.top_row(2)
    resized_anchor = view._rows[resized_top]

    assert resized_anchor.entry_id == entry_id
    assert resized_anchor.offset >= original_offset
    assert resized_anchor.offset - original_offset < 7
    view.close()


def test_transcript_reader_anchor_stays_inside_shrinking_history() -> None:
    from ash.ui.transcript_view import TranscriptView

    transcript = Transcript()
    entry_id = transcript.begin("user")
    transcript.append_delta(entry_id, "\n".join(str(i) for i in range(25)))
    view = TranscriptView(transcript)
    view.create_content(10, 10)
    view.scroll(-5)
    assert view.detached

    transcript.replace_content(entry_id, "\n".join(str(i) for i in range(12)))
    view.create_content(10, 10)
    assert view.top_row(10) == 2
    assert view.detached
    view.close()


def test_transcript_wrap_preserves_graphemes_and_narrow_cell_width() -> None:
    from rich.cells import cell_len
    from ash.ui.transcript_view import TranscriptView, _wrap_line_spans

    samples = ("中文字符", "a👨‍💻b", "👩🏾‍🔧", "⚙️", "e\u0301e\u0301")
    for sample in samples:
        for width in (1, 2, 3, 4):
            wrapped = _wrap_line_spans(sample, width)
            assert all(cell_len(chunk) <= width for chunk, _offset in wrapped)
            if width >= 2:
                assert "".join(chunk for chunk, _offset in wrapped) == sample
            for kind in ("user", "assistant", "tool"):
                transcript = Transcript()
                transcript.append(kind, sample)
                view = TranscriptView(transcript)
                content = view.create_content(width, 10)
                rows = [
                    "".join(part for _style, part in content.get_line(i))
                    for i in range(content.line_count)
                ]
                assert all(cell_len(row) <= width for row in rows)
                view.close()


def test_long_transcript_line_wraps_without_reprocessing_each_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.ui.transcript_view as transcript_view_module
    from ash.ui.transcript_view import TranscriptView

    transcript = Transcript()
    text = "x" * 40_000
    entry_id = transcript.begin("assistant", title="ash")
    transcript.append_delta(entry_id, text)
    view = TranscriptView(transcript)
    original_get_cwidth = transcript_view_module.get_cwidth
    calls = 0

    def counted_get_cwidth(value: str) -> int:
        nonlocal calls
        calls += 1
        return original_get_cwidth(value)

    monkeypatch.setattr(transcript_view_module, "get_cwidth", counted_get_cwidth)
    content = view.create_content(80, 24)
    visible_end = "".join(
        text for _style, text in content.get_line(content.line_count - 1)
    )

    assert content.line_count == 502
    assert visible_end.endswith(text[-78:])
    assert calls < len(text) * 2
    view.close()




def test_finalized_assistant_markdown_preserves_rich_styles_and_width() -> None:
    from ash.ui.transcript_view import TranscriptView

    transcript = Transcript()
    tick = chr(96)
    fence = tick * 3
    content = (
        "# Heading\n\n"
        "This is **bold** and *italic* and "
        + tick
        + "code"
        + tick
        + ".\n\n"
        + fence
        + "python\nprint('hi')\n"
        + fence
    )
    transcript.append("assistant", content, title="ash")
    view = TranscriptView(transcript)

    rendered = view.create_content(40, 20)
    lines = [rendered.get_line(index) for index in range(rendered.line_count)]
    styles = [style for line in lines for style, _text in line]
    texts = ["".join(text for _style, text in line) for line in lines]

    assert texts[0] == "ASH"
    assert texts[1].startswith("· ")
    assert any("bold" in style for style in styles)
    assert any("italic" in style for style in styles)
    assert any("bg:#272822" in style for style in styles)
    assert all(get_cwidth(text) <= 40 for text in texts)
    view.close()

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
    assert "  ──╌╌  50% " in status
    assert len(status) == 40

    output.columns = 20
    narrow = fragment_list_to_text(to_formatted_text(surface._status_text()))
    assert len(narrow) == 20
    assert "─╌  50% " in narrow


def test_status_row_never_exceeds_narrow_terminal_width() -> None:
    output = SizedDummyOutput()
    surface = InlinePromptSurface(
        history=InMemoryHistory(),
        completer=None,
        status_provider=lambda: "a long model and path status",
        context_provider=lambda: (100, 100),
        thinking_provider=lambda _width: (0, ""),
        input_mode="emacs",
        keybindings={"newline": ["c-j"], "open_editor": ["c-x c-e"]},
        theme=get_theme("light"),
        no_color=True,
        output=output,
    )
    for width in range(1, 17):
        output.columns = width
        rendered = fragment_list_to_text(to_formatted_text(surface._status_text()))
        assert get_cwidth(rendered) <= width
        if width >= 4:
            assert "100%" in rendered


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
        def capture_rows() -> dict[int, str]:
            screen = surface.application.renderer._last_screen
            if screen is None:
                return {}
            return {
                y: "".join(cell.char for _x, cell in sorted(cells.items())).rstrip()
                for y, cells in screen.data_buffer.items()
            }

        async def wait_for_render(predicate) -> dict[int, str]:
            ready = asyncio.get_running_loop().create_future()

            def on_render(_app) -> None:
                rows = capture_rows()
                if predicate(rows) and not ready.done():
                    ready.set_result(rows)

            surface.application.after_render += on_render
            try:
                surface.invalidate()
                return await asyncio.wait_for(ready, timeout=2)
            finally:
                surface.application.after_render -= on_render

        pending = asyncio.create_task(surface.read())
        initial = await wait_for_render(
            lambda rows: any("›" in text for text in rows.values())
        )
        composer_row = next(y for y, text in initial.items() if "›" in text)
        assert composer_row == 18

        state[0] = ActivityDockView(
            revision=1,
            activity="Inspecting",
            reasoning="checking the import path",
        )
        first = await wait_for_render(
            lambda rows: any("Inspecting" in text for text in rows.values())
        )
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
        assert len({first[first_status_row], second_status, third_status}) >= 2

        state[0] = ActivityDockView(revision=2, activity=None, reasoning="")
        idle = await wait_for_render(
            lambda rows: any("›" in text for text in rows.values())
            and not any("Inspecting" in text for text in rows.values())
        )
        assert not any("Inspecting" in text for text in idle.values())
        assert next(y for y, text in idle.items() if "›" in text) == composer_row

        pipe.send_text("done\r")
        assert await pending == "done"
        await surface.aclose()


@pytest.mark.asyncio
async def test_transcript_scroll_keeps_bottom_chrome_pinned_and_does_not_follow_reader() -> None:
    with create_pipe_input() as pipe:
        transcript = Transcript()
        for index in range(40):
            transcript.append("user", f"question {index}", title="you")
            transcript.append("assistant", f"answer {index}", title="ash")
        output = SizedDummyOutput()
        surface = InlinePromptSurface(
            history=InMemoryHistory(),
            completer=None,
            status_provider=lambda: "model · repo",
            context_provider=lambda: (10, 100),
            input_mode="emacs",
            keybindings={"newline": ["c-j"], "open_editor": ["c-x c-e"]},
            theme=get_theme("dark"),
            no_color=True,
            input=pipe,
            output=output,
            transcript=transcript,
        )
        pending = asyncio.create_task(surface.read())
        await asyncio.sleep(0.05)

        def screen_rows() -> dict[int, str]:
            screen = surface.application.renderer._last_screen
            assert screen is not None
            return {
                row: "".join(cell.char for _column, cell in sorted(cells.items()))
                for row, cells in screen.data_buffer.items()
            }

        initial = screen_rows()
        composer_row = next(row for row, text in initial.items() if "›" in text)
        status_row = next(row for row, text in initial.items() if "model" in text)
        assert composer_row == 18
        assert status_row == 19

        surface.transcript_view.scroll(-7)
        await asyncio.sleep(0.05)
        detached_rows = screen_rows()
        assert surface.transcript_view.detached is True
        assert "Ctrl+End" in "\n".join(detached_rows.values())
        assert next(row for row, text in detached_rows.items() if "›" in text) == composer_row
        assert next(row for row, text in detached_rows.items() if "model" in text) == status_row

        anchor = surface.transcript_view._anchor_id
        live_id = transcript.begin("assistant", title="ash")
        transcript.append_delta(live_id, "new streamed content")
        await asyncio.sleep(0.05)
        assert surface.transcript_view.detached is True
        assert surface.transcript_view._anchor_id == anchor

        surface.transcript_view.scroll_to_latest()
        transcript.finalize(live_id)
        await asyncio.sleep(0.05)
        assert surface.transcript_view.follow_latest is True
        current_rows = "\n".join(screen_rows().values())
        assert "new streamed content" in current_rows
        pipe.send_text("done\r")
        assert await pending == "done"
        await surface.aclose()
