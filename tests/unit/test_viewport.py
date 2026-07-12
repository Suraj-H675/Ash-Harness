from pathlib import Path

import pytest
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ash.ui.transcript import Transcript
from ash.ui.viewport import (
    RichTranscriptFormatter,
    TranscriptViewport,
    format_transcript,
)


class SizedDummyOutput(DummyOutput):
    def __init__(self, columns: int, rows: int) -> None:
        self.columns = columns
        self.rows = rows

    def get_size(self) -> Size:
        return Size(rows=self.rows, columns=self.columns)


def _plain(value) -> str:
    return "".join(fragment[1] for fragment in to_formatted_text(value))


def test_format_transcript_preserves_semantics_and_streaming_state() -> None:
    transcript = Transcript()
    transcript.append("user", "inspect this", title="you")
    active = transcript.begin("assistant", title="ash")
    transcript.append_delta(active, "working")
    transcript.append("tool", "read_file [completed]", title="read_file")

    rendered = _plain(format_transcript(transcript.snapshot()))

    assert "you > inspect this" in rendered
    assert "ash > working  ..." in rendered
    assert "read_file > read_file [completed]" in rendered


def test_rich_transcript_formatter_renders_markdown_and_caches_cells() -> None:
    transcript = Transcript()
    transcript.append(
        "assistant",
        "**bold**\n\n```python\nprint('ok')\n```",
        title="ash",
    )
    formatter = RichTranscriptFormatter()

    first = _plain(formatter.format(transcript.snapshot(), width=80))
    cache_size = len(formatter._cache)
    second = _plain(formatter.format(transcript.snapshot(), width=80))

    assert "**bold**" not in first
    assert "bold" in first
    assert "print('ok')" in first
    assert second == first
    assert len(formatter._cache) == cache_size == 1


@pytest.mark.asyncio
async def test_viewport_submits_input_and_can_be_reused(tmp_path: Path) -> None:
    transcript = Transcript()
    with create_pipe_input() as pipe:
        viewport = TranscriptViewport(
            transcript,
            history_path=tmp_path / "history",
            input=pipe,
            output=DummyOutput(),
        )
        first = viewport.read("first> ")
        pipe.send_text("hello\r")
        assert await first == "hello"

        second = viewport.read("second> ")
        pipe.send_text("again\r")
        assert await second == "again"
        viewport.close()


@pytest.mark.asyncio
async def test_viewport_honors_custom_multiline_binding(tmp_path: Path) -> None:
    with create_pipe_input() as pipe:
        viewport = TranscriptViewport(
            Transcript(),
            history_path=tmp_path / "history",
            keybindings={"newline": ["c-o"], "open_editor": ["c-x c-e"]},
            input=pipe,
            output=DummyOutput(),
        )
        pending = viewport.read()
        pipe.send_text("first")
        pipe.send_bytes(b"\x0f")  # Ctrl+O
        pipe.send_text("second\r")

        assert await pending == "first\nsecond"
        viewport.close()


@pytest.mark.asyncio
async def test_viewport_interrupt_and_eof_restore_input_ownership(
    tmp_path: Path,
) -> None:
    with create_pipe_input() as pipe:
        viewport = TranscriptViewport(
            Transcript(),
            history_path=tmp_path / "history",
            input=pipe,
            output=DummyOutput(),
        )
        interrupted = viewport.read()
        pipe.send_bytes(b"\x03")
        with pytest.raises(KeyboardInterrupt):
            await interrupted

        ended = viewport.read()
        pipe.send_bytes(b"\x04")
        with pytest.raises(EOFError):
            await ended
        viewport.close()


def test_viewport_rejects_concurrent_read(tmp_path: Path) -> None:
    viewport = TranscriptViewport(
        Transcript(),
        history_path=tmp_path / "history",
        output=DummyOutput(),
    )
    viewport._running = True

    async def read_again() -> None:
        await viewport.read()

    with pytest.raises(RuntimeError, match="already owns"):
        import asyncio

        asyncio.run(read_again())
    viewport.close()


@pytest.mark.asyncio
async def test_viewport_survives_narrow_wide_resize_and_live_updates(
    tmp_path: Path,
) -> None:
    import asyncio

    transcript = Transcript()
    transcript.append(
        "assistant",
        "A long response with Unicode: 你好世界. " * 20,
        title="ash",
    )
    output = SizedDummyOutput(columns=32, rows=8)
    with create_pipe_input() as pipe:
        viewport = TranscriptViewport(
            transcript,
            history_path=tmp_path / "history",
            status_provider=lambda: "model | mode | context | session | workspace",
            input=pipe,
            output=output,
        )
        pending = asyncio.create_task(viewport.read())
        await asyncio.sleep(0.05)
        assert pending.done() is False

        transcript.append("tool", "read_file [completed]", title="read_file")
        output.columns = 200
        output.rows = 60
        viewport.application.invalidate()
        await asyncio.sleep(0.05)
        assert pending.done() is False

        pipe.send_bytes(b"\x1b[5~")  # PageUp
        pipe.send_bytes(b"\x1b[F")  # End / follow tail
        pipe.send_text("resize intact\r")
        assert await pending == "resize intact"
        viewport.close()
