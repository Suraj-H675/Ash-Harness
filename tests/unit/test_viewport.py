from pathlib import Path

import pytest
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ui.transcript import Transcript
from ui.viewport import TranscriptViewport, format_transcript


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
