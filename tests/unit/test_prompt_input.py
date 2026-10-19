import asyncio
import io
import os
import select
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

import ash.ui.history as history_module
import ash.ui.prompt as prompt_module
from ash.repo.parser import Symbol
from ash.ui.input_signals import PromptInterrupted
from ash.ui.prompt import AshCompleter, PromptChoice, PromptInput
from ash.ui.transcript import Transcript


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class TtyPipe:
    encoding = "utf-8"

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._fd


def _watch_reader(
    monkeypatch: pytest.MonkeyPatch,
    fd: int,
) -> tuple[asyncio.Event, list[bool]]:
    loop = asyncio.get_running_loop()
    registered = asyncio.Event()
    removed: list[bool] = []
    add_reader = loop.add_reader
    remove_reader = loop.remove_reader

    def observe_add_reader(registered_fd, callback, *args):
        result = add_reader(registered_fd, callback, *args)
        if registered_fd == fd:
            registered.set()
        return result

    def observe_remove_reader(registered_fd):
        result = remove_reader(registered_fd)
        if registered_fd == fd:
            removed.append(result)
        return result

    monkeypatch.setattr(loop, "add_reader", observe_add_reader)
    monkeypatch.setattr(loop, "remove_reader", observe_remove_reader)
    return registered, removed


class TrackingMouseOutput(DummyOutput):
    def __init__(self) -> None:
        super().__init__()
        self.mouse_enabled = 0
        self.mouse_disabled = 0

    def enable_mouse_support(self) -> None:
        self.mouse_enabled += 1

    def disable_mouse_support(self) -> None:
        self.mouse_disabled += 1


@pytest.fixture
def cursor_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        prompt_module,
        "terminal_supports_cursor_ui",
        lambda **kwargs: True,
    )


def test_redirected_input_uses_line_fallback() -> None:
    stream = io.StringIO("hello\n")
    prompt = PromptInput(input_stream=stream)
    assert prompt.interactive is False
    assert asyncio.run(prompt.read()) == "hello"


def test_redirected_eof_is_reported() -> None:
    prompt = PromptInput(input_stream=io.StringIO(""))
    with pytest.raises(EOFError):
        asyncio.run(prompt.read())


def test_invalid_input_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="input_mode"):
        PromptInput(input_stream=io.StringIO(""), input_mode="modal")


def test_symbol_completion_queries_ready_snapshot_without_refresh(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeRepoMap:
        files = []
        ready = True

        def find_definitions(self, name: str, **kwargs):
            calls.append({"name": name, **kwargs})
            return [
                Symbol(
                    name="Service",
                    kind="class",
                    file_path=str(tmp_path / "service.py"),
                    start_line=1,
                    end_line=10,
                )
            ]

    completer = AshCompleter(
        ["/help"],
        tmp_path,
        repo_map=FakeRepoMap(),
    )
    completions = list(
        completer.get_completions(
            Document("@symbol:Ser"),
            CompleteEvent(completion_requested=True),
        )
    )

    assert [item.text for item in completions] == ["@symbol:Service"]
    assert calls == [
        {
            "name": "Ser",
            "case_sensitive": True,
            "refresh": False,
        }
    ]


def test_symbol_completion_does_not_wait_for_repo_map_startup(
    tmp_path: Path,
) -> None:
    class BuildingRepoMap:
        ready = False

        @property
        def files(self):
            raise AssertionError("completion must not touch an unready map")

        def find_definitions(self, *args, **kwargs):
            raise AssertionError("completion must not wait for an unready map")

    completer = AshCompleter(
        ["/help"],
        tmp_path,
        repo_map=BuildingRepoMap(),
    )

    assert list(
        completer.get_completions(
            Document("@symbol:Ser"),
            CompleteEvent(completion_requested=True),
        )
    ) == []


@pytest.mark.asyncio
async def test_prompt_bracketed_paste_preserves_multiline_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cursor_ui: None,
) -> None:
    with create_pipe_input() as pipe:
        prompt = PromptInput(
            input_stream=TtyStringIO(),
            history_path=tmp_path / "history",
            input=pipe,
            output=DummyOutput(),
        )
        pending = asyncio.create_task(prompt.read())
        pipe.send_bytes(b"\x1b[200~first\nsecond\x1b[201~")
        pipe.send_text("\r")

        assert await pending == "first\nsecond"


@pytest.mark.asyncio
async def test_prompt_uses_mouse_reporting_for_scrollable_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cursor_ui: None,
) -> None:
    with create_pipe_input() as pipe:
        output = TrackingMouseOutput()
        prompt = PromptInput(
            input_stream=TtyStringIO(),
            history_path=tmp_path / "history",
            input=pipe,
            output=output,
        )
        pending = asyncio.create_task(prompt.read())
        pipe.send_text("hello\r")

        assert await pending == "hello"
        assert output.mouse_enabled == 1
        await prompt.aclose()
        assert output.mouse_disabled == 1


@pytest.mark.asyncio
async def test_enter_submits_slash_arguments_while_completion_menu_is_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cursor_ui: None,
) -> None:
    with create_pipe_input() as pipe:
        prompt = PromptInput(
            input_stream=TtyStringIO(),
            history_path=tmp_path / "history",
            input=pipe,
            output=DummyOutput(),
        )
        pending = asyncio.create_task(prompt.read())
        await asyncio.sleep(0.05)
        pipe.send_text("/res")
        assert prompt._surface is not None
        for _attempt in range(20):
            if prompt._surface.input_buffer.text == "/res":
                break
            await asyncio.sleep(0.01)
        for _attempt in range(20):
            if prompt._surface.input_buffer.complete_state is not None:
                break
            await asyncio.sleep(0.01)
        state = prompt._surface.input_buffer.complete_state
        assert state is not None and state.completions

        pipe.send_text("\r")
        assert await pending == "/res"
        await prompt.aclose()


@pytest.mark.asyncio
async def test_sgr_wheel_scrolls_transcript_without_editing_input_or_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cursor_ui: None,
) -> None:
    with create_pipe_input() as pipe:
        transcript = Transcript()
        for index in range(40):
            transcript.append("user", f"question {index}", title="you")
            transcript.append("assistant", f"answer {index}", title="ash")
        prompt = PromptInput(
            input_stream=TtyStringIO(),
            history_path=tmp_path / "history",
            input=pipe,
            output=DummyOutput(),
            transcript=transcript,
        )
        assert prompt._surface is not None
        history = prompt._surface.input_buffer.history
        history.append_string("previous prompt")
        pending = asyncio.create_task(prompt.read())
        await asyncio.sleep(0.05)

        pipe.send_bytes(b"\x1b[<66;10;5M")
        await asyncio.sleep(0.05)
        assert prompt._surface.transcript_view.follow_latest is True
        pipe.send_bytes(b"\x1b[<64;10;5M")
        await asyncio.sleep(0.05)

        assert prompt._surface.transcript_view.detached is True
        assert prompt._surface.input_buffer.text == ""
        assert history.get_strings() == ["previous prompt"]
        pipe.send_text("hello\r")
        assert await pending == "hello"
        await prompt.aclose()


@pytest.mark.asyncio
async def test_choice_selector_uses_keyboard_without_mouse_capture(
    tmp_path: Path,
    cursor_ui: None,
) -> None:
    with create_pipe_input() as pipe:
        output = DummyOutput()
        prompt = PromptInput(
            input_stream=TtyStringIO(),
            history_path=tmp_path / "history",
            input=pipe,
            output=output,
        )
        pending = asyncio.create_task(
            prompt.choose(
                "write_file permission",
                (
                    PromptChoice("y", "Allow once", "This request only."),
                    PromptChoice("n", "Deny", "Reject this request."),
                ),
                default_value="n",
            )
        )
        await asyncio.sleep(0.05)

        pipe.send_text("\r")

        assert await pending == "n"


@pytest.mark.asyncio
async def test_choice_selector_interrupt_is_distinct_from_dismissal(
    tmp_path: Path,
    cursor_ui: None,
) -> None:
    with create_pipe_input() as pipe:
        prompt = PromptInput(
            input_stream=TtyStringIO(),
            history_path=tmp_path / "history",
            input=pipe,
            output=DummyOutput(),
        )
        pending = asyncio.create_task(
            prompt.choose(
                "write_file permission",
                (PromptChoice("y", "Allow once"),),
            )
        )
        try:
            await asyncio.sleep(0.05)
            pipe.send_bytes(b"\x03")

            done, _ = await asyncio.wait({pending}, timeout=0.5)
            assert pending in done
            with pytest.raises(PromptInterrupted):
                pending.result()
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await prompt.aclose()


@pytest.mark.asyncio
async def test_choice_selector_escape_dismisses_active_choice(
    tmp_path: Path,
    cursor_ui: None,
) -> None:
    with create_pipe_input() as pipe:
        prompt = PromptInput(
            input_stream=TtyStringIO(),
            history_path=tmp_path / "history",
            input=pipe,
            output=DummyOutput(),
        )
        pending = asyncio.create_task(
            prompt.choose(
                "write_file permission",
                (PromptChoice("y", "Allow once"),),
            )
        )
        try:
            await asyncio.sleep(0.05)
            pipe.send_bytes(b"\x1b")

            assert await asyncio.wait_for(pending, timeout=1.5) is None
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await prompt.aclose()


@pytest.mark.asyncio
async def test_closing_surface_releases_active_choice(
    tmp_path: Path,
    cursor_ui: None,
) -> None:
    with create_pipe_input() as pipe:
        prompt = PromptInput(
            input_stream=TtyStringIO(),
            history_path=tmp_path / "history",
            input=pipe,
            output=DummyOutput(),
        )
        pending = asyncio.create_task(
            prompt.choose(
                "write_file permission",
                (PromptChoice("y", "Allow once"),),
            )
        )
        try:
            await asyncio.sleep(0.05)
            await prompt.aclose()

            done, _ = await asyncio.wait({pending}, timeout=0.5)
            assert pending in done
            assert pending.result() is None

            with pytest.raises(EOFError):
                await prompt.choose(
                    "queued write_file permission",
                    (PromptChoice("y", "Allow once"),),
                )
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("sync_close", [False, True])
@pytest.mark.asyncio
async def test_closing_surface_during_start_releases_startup_waiter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cursor_ui: None,
    sync_close: bool,
) -> None:
    with create_pipe_input() as pipe:
        prompt = PromptInput(
            input_stream=TtyStringIO(),
            history_path=tmp_path / "history",
            input=pipe,
            output=DummyOutput(),
        )
        assert prompt._surface is not None

        async def blocked_run_async(
            *, pre_run: object, handle_sigint: bool
        ):
            del pre_run, handle_sigint
            await asyncio.Event().wait()

        monkeypatch.setattr(
            prompt._surface.application,
            "run_async",
            blocked_run_async,
        )
        starting = asyncio.create_task(prompt._surface.start())
        try:
            await asyncio.sleep(0)
            assert prompt._surface._app_ready is not None
            assert not prompt._surface._app_ready.done()

            if sync_close:
                prompt.close()
            else:
                await prompt.aclose()

            done, _ = await asyncio.wait({starting}, timeout=0.5)
            assert starting in done
            with pytest.raises(EOFError):
                await starting
        finally:
            if not starting.done():
                starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)


@pytest.mark.asyncio
async def test_inline_surface_restart_initializes_terminal_pump_again(
    tmp_path: Path, cursor_ui: None
) -> None:
    with create_pipe_input() as pipe:
        prompt = PromptInput(
            input_stream=TtyStringIO(),
            history_path=tmp_path / "history",
            input=pipe,
            output=DummyOutput(),
        )
        assert prompt._surface is not None
        surface = prompt._surface
        try:
            await asyncio.wait_for(surface.start(), timeout=1)
            first_wakeup = surface._terminal_wakeup
            assert first_wakeup is not None
            await asyncio.wait_for(
                prompt.suspend_for_overlay(lambda: None), timeout=1
            )
            await asyncio.wait_for(surface.start(), timeout=1)
            assert surface._terminal_wakeup is not first_wakeup
        finally:
            await prompt.aclose()


def test_screen_reader_mode_uses_reduced_dynamic_prompt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailSurface:
        def __init__(self, **kwargs) -> None:
            raise AssertionError("screen-reader mode must not create prompt-toolkit UI")

    monkeypatch.setattr(prompt_module, "InlinePromptSurface", FailSurface)
    prompt = PromptInput(
        input_stream=TtyStringIO(),
        history_path=tmp_path / "history",
        screen_reader_mode=True,
    )

    assert prompt.screen_reader_mode is True
    assert prompt.supports_live_surface is False
    assert prompt._surface is None
    assert prompt._completer is None


@pytest.mark.parametrize("term", ["dumb", "unknown"])
def test_terminal_capability_rejects_limited_terminals(term: str) -> None:
    assert (
        prompt_module.terminal_supports_cursor_ui(
            input_stream=TtyStringIO(),
            output_stream=TtyStringIO(),
            environment={"TERM": term},
        )
        is False
    )


def test_terminal_capability_requires_tty_output() -> None:
    assert (
        prompt_module.terminal_supports_cursor_ui(
            input_stream=TtyStringIO(),
            output_stream=io.StringIO(),
            environment={"TERM": "xterm-256color"},
        )
        is False
    )


@pytest.mark.asyncio
async def test_limited_terminal_uses_linear_reader_without_prompt_toolkit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailSurface:
        def __init__(self, **kwargs) -> None:
            raise AssertionError("limited terminals must not create prompt-toolkit UI")

    output = TtyStringIO()
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(prompt_module, "InlinePromptSurface", FailSurface)
    prompt = PromptInput(input_stream=TtyStringIO("hello\n"))

    assert prompt.linear_mode is True
    assert prompt.supports_full_screen_ui is False
    assert await prompt.read("limited> ") == "hello"
    assert output.getvalue() == "limited> "


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_screen_reader_linear_reader_works_in_a_cooked_pty(
    tmp_path: Path,
) -> None:
    import pty

    master, slave = pty.openpty()
    source_root = str(Path(__file__).parents[2] / "src")
    environment = {
        "HOME": str(tmp_path),
        "NO_COLOR": "1",
        "PATH": os.defpath,
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": source_root,
        "TERM": "dumb",
    }
    code = (
        "import asyncio\n"
        "from ash.ui.prompt import PromptInput\n"
        "async def main():\n"
        "    prompt = PromptInput(screen_reader_mode=True)\n"
        "    result = await prompt.read('accessible> ')\n"
        "    print(f'RESULT:{result}', flush=True)\n"
        "asyncio.run(main())\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        env=environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    captured = bytearray()

    def read_until(marker: bytes) -> None:
        deadline = time.monotonic() + 5
        while marker not in captured:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if select.select([master], [], [], remaining)[0]:
                try:
                    chunk = os.read(master, 65_536)
                except OSError:
                    break
                if not chunk:
                    break
                captured.extend(chunk)
        assert marker in captured, bytes(captured[-3000:])

    try:
        read_until(b"accessible> ")
        os.write(master, b"hello\r")
        read_until(b"RESULT:hello")
        assert process.wait(timeout=5) == 0
        assert b"\x1b[" not in captured
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        os.close(master)


@pytest.mark.asyncio
async def test_screen_reader_mode_reads_linearly_without_prompt_toolkit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    prompt = PromptInput(
        input_stream=TtyPipe(read_fd),
        history_path=tmp_path / "history",
        screen_reader_mode=True,
    )
    try:
        pending = asyncio.create_task(prompt.read("accessible> "))
        await asyncio.sleep(0)
        os.write(write_fd, b"hello\n")

        assert await pending == "hello"
        assert output.getvalue() == "accessible> "
    finally:
        os.close(write_fd)
        os.close(read_fd)


@pytest.mark.asyncio
async def test_linear_reader_preserves_utf8_split_across_pipe_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    registered, _ = _watch_reader(monkeypatch, read_fd)
    first_read = asyncio.Event()
    chunks: list[bytes] = []
    real_read = os.read

    def observe_read(fd: int, size: int) -> bytes:
        chunk = real_read(fd, size)
        if fd == read_fd:
            chunks.append(chunk)
            if len(chunks) == 1:
                first_read.set()
        return chunk

    monkeypatch.setattr(prompt_module.os, "read", observe_read)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    prompt = PromptInput(
        input_stream=TtyPipe(read_fd),
        history_path=tmp_path / "history",
        screen_reader_mode=True,
    )
    pending = asyncio.create_task(prompt.read("accessible> "))
    try:
        await registered.wait()
        os.write(write_fd, b"hello-\xf0\x9f")
        await first_read.wait()
        assert not pending.done()

        os.write(write_fd, b"\x98\x80-world\n")

        assert await pending == "hello-😀-world"
        assert chunks == [b"hello-\xf0\x9f", b"\x98\x80-world\n"]
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        os.close(write_fd)
        os.close(read_fd)


@pytest.mark.asyncio
async def test_linear_reader_buffers_multiple_crlf_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    registered, _ = _watch_reader(monkeypatch, read_fd)
    monkeypatch.setattr(prompt_module, "MAX_LINEAR_INPUT_BYTES", 5, raising=False)
    first_read = asyncio.Event()
    reads: list[bytes] = []
    real_read = os.read

    def observe_read(fd: int, size: int) -> bytes:
        chunk = real_read(fd, size)
        if fd == read_fd:
            reads.append(chunk)
            if len(reads) == 1:
                first_read.set()
        return chunk

    monkeypatch.setattr(prompt_module.os, "read", observe_read)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    prompt = PromptInput(
        input_stream=TtyPipe(read_fd),
        history_path=tmp_path / "history",
        screen_reader_mode=True,
    )
    pending = asyncio.create_task(prompt.read("first> "))
    try:
        await registered.wait()
        os.write(write_fd, b"first\r")
        await first_read.wait()
        assert not pending.done()
        os.write(write_fd, b"\nthird\n")

        assert await pending == "first"
        assert await prompt.read("third> ") == "third"
        assert reads == [b"first\r", b"\nthird", b"\n"]
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        os.close(write_fd)
        os.close(read_fd)


@pytest.mark.asyncio
async def test_linear_reader_returns_final_unterminated_pipe_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    registered, _ = _watch_reader(monkeypatch, read_fd)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    prompt = PromptInput(
        input_stream=TtyPipe(read_fd),
        history_path=tmp_path / "history",
        screen_reader_mode=True,
    )
    pending = asyncio.create_task(prompt.read("accessible> "))
    try:
        await registered.wait()
        os.write(write_fd, b"final input")
        os.close(write_fd)
        write_fd = -1

        assert await pending == "final input"
        with pytest.raises(EOFError):
            await prompt.read("accessible> ")
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if write_fd >= 0:
            os.close(write_fd)
        os.close(read_fd)


@pytest.mark.asyncio
async def test_linear_reader_discards_oversized_pipe_line_before_next_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    registered, _ = _watch_reader(monkeypatch, read_fd)
    stdout = io.StringIO()
    stderr = io.StringIO()
    read_sizes: list[int] = []
    real_read = os.read

    def observe_read(fd: int, size: int) -> bytes:
        if fd == read_fd:
            read_sizes.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(prompt_module, "MAX_LINEAR_INPUT_BYTES", 8, raising=False)
    monkeypatch.setattr(prompt_module.os, "read", observe_read)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    prompt = PromptInput(
        input_stream=TtyPipe(read_fd),
        history_path=tmp_path / "history",
        screen_reader_mode=True,
    )
    pending = asyncio.create_task(prompt.read("accessible> "))
    try:
        await registered.wait()
        os.write(
            write_fd,
            "😀😀\r\n😀😀x\nsafe\n".encode("utf-8"),
        )

        assert await pending == "😀😀"
        assert await prompt.read("accessible> ") == "safe"
        assert "discarded" in stderr.getvalue()
        assert stdout.getvalue() == "accessible> accessible> accessible> "
        assert read_sizes and all(size == 9 for size in read_sizes)
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        os.close(write_fd)
        os.close(read_fd)


@pytest.mark.asyncio
async def test_linear_reader_bounds_text_stream_reads_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrackingTtyStringIO(TtyStringIO):
        def __init__(self, value: str) -> None:
            super().__init__(value)
            self.readline_sizes: list[int] = []

        def readline(self, size: int = -1) -> str:
            self.readline_sizes.append(size)
            return super().readline(size)

    stream = TrackingTtyStringIO("12345\nsafe\r\n")
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(prompt_module, "MAX_LINEAR_INPUT_BYTES", 4, raising=False)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    prompt = PromptInput(input_stream=stream, screen_reader_mode=True)

    assert await prompt.read("accessible> ") == "safe"
    assert stream.readline_sizes
    assert all(0 < size <= 5 for size in stream.readline_sizes)
    assert "discarded" in stderr.getvalue()


@pytest.mark.asyncio
async def test_linear_reader_removes_pipe_listener_on_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    registered, removed = _watch_reader(monkeypatch, read_fd)
    reads: list[bytes] = []
    real_read = os.read

    def observe_read(fd: int, size: int) -> bytes:
        chunk = real_read(fd, size)
        if fd == read_fd:
            reads.append(chunk)
        return chunk

    monkeypatch.setattr(prompt_module.os, "read", observe_read)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    prompt = PromptInput(
        input_stream=TtyPipe(read_fd),
        history_path=tmp_path / "history",
        screen_reader_mode=True,
    )
    pending = asyncio.create_task(prompt.read("accessible> "))
    try:
        await registered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        assert removed == [True]
        os.write(write_fd, b"after cancellation\n")
        assert await prompt.read("accessible> ") == "after cancellation"
        assert reads == [b"after cancellation\n"]
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        os.close(write_fd)
        os.close(read_fd)


def test_prompt_completion_updates_after_plugin_reload(
    tmp_path, monkeypatch: pytest.MonkeyPatch, cursor_ui: None
) -> None:
    prompt = PromptInput(
        input_stream=TtyStringIO(),
        history_path=tmp_path / "history",
        extra_commands=["old:command"],
    )

    prompt.set_extra_commands(["example:review"])
    completer = prompt._completer
    assert completer is not None
    completions = list(
        completer.get_completions(
            Document("/example:r"), CompleteEvent(completion_requested=True)
        )
    )

    assert [completion.text for completion in completions] == ["/example:review"]
    assert [completion.display_meta_text for completion in completions] == [
        "custom command"
    ]


def test_prompt_completion_preserves_custom_command_description(
    tmp_path, monkeypatch: pytest.MonkeyPatch, cursor_ui: None
) -> None:
    prompt = PromptInput(
        input_stream=TtyStringIO(),
        history_path=tmp_path / "history",
        extra_commands={"project:review": "Review release notes"},
    )

    completions = list(
        prompt._completer.get_completions(  # type: ignore[union-attr]
            Document("/project:r"), CompleteEvent(completion_requested=True)
        )
    )

    assert [completion.text for completion in completions] == ["/project:review"]
    assert [completion.display_meta_text for completion in completions] == [
        "Review release notes"
    ]


def test_builtin_slash_completion_has_description_while_filtering_prefix(
    tmp_path,
) -> None:
    completer = AshCompleter(["/model", "/models", "/status"], tmp_path)

    completions = list(
        completer.get_completions(
            Document("/mo"), CompleteEvent(completion_requested=True)
        )
    )

    assert [completion.text for completion in completions] == ["/model", "/models"]
    assert all(completion.display_meta_text for completion in completions)


def test_bare_slash_completion_keeps_curated_builtin_order(tmp_path) -> None:
    completer = AshCompleter(
        ["/agents", "/browser", "/help", "/status", "/model", "/custom:review"],
        tmp_path,
    )

    completions = list(
        completer.get_completions(
            Document("/"), CompleteEvent(completion_requested=True)
        )
    )

    assert [completion.text for completion in completions[:4]] == [
        "/help",
        "/status",
        "/model",
        "/agents",
    ]
    assert completions[-1].text == "/custom:review"


def test_empty_composer_only_suggests_commands_on_explicit_completion(tmp_path) -> None:
    completer = AshCompleter(["/help", "/status"], tmp_path)
    empty = Document("")

    assert list(completer.get_completions(empty, CompleteEvent())) == []
    assert [
        completion.text
        for completion in completer.get_completions(
            empty, CompleteEvent(completion_requested=True)
        )
    ] == ["/help", "/status"]


def test_slash_argument_completion_and_builtin_metadata_are_contextual(
    tmp_path,
) -> None:
    completer = AshCompleter(
        ["/model", "/permissions", "/mcp", "/status"],
        tmp_path,
        command_descriptions={"/status": "custom collision"},
    )
    event = CompleteEvent(completion_requested=True)

    model = list(completer.get_completions(Document("/model "), event))
    permissions = list(
        completer.get_completions(Document("/permissions a"), event)
    )
    mcp = list(completer.get_completions(Document("/mcp status "), event))
    plugin_install = list(
        completer.get_completions(Document("/plugins install demo "), event)
    )
    plugin_uninstall = list(
        completer.get_completions(Document("/plugins uninstall demo "), event)
    )
    browser_connect = list(
        completer.get_completions(
            Document("/browser connect http://127.0.0.1:9222 "),
            event,
        )
    )
    status = list(completer.get_completions(Document("/sta"), event))

    assert "openai/" in [completion.text for completion in model]
    assert {completion.text for completion in permissions} >= {
        "allow",
        "ask",
        "auto_edit",
        "auto_approve",
    }
    assert [completion.text for completion in mcp] == ["--json"]
    assert [completion.text for completion in plugin_install] == [
        "--replace",
        "--ref",
    ]
    assert [completion.text for completion in plugin_uninstall] == ["--yes"]
    assert [completion.text for completion in browser_connect] == [
        "--reuse-storage-state"
    ]
    assert [completion.display_meta_text for completion in status] == [
        "Show session and runtime status"
    ]

    effort_completer = AshCompleter(["/effort"], tmp_path)
    effort_completer.effort_provider = lambda: ("low", "high")
    effort = list(
        effort_completer.get_completions(Document("/effort "), event)
    )
    assert [completion.text for completion in effort] == [
        "default",
        "low",
        "high",
    ]
    effort_completer.effort_provider = lambda: ()
    assert list(effort_completer.get_completions(Document("/effort "), event)) == []


def test_model_completion_includes_custom_providers_and_known_models(tmp_path) -> None:
    completer = AshCompleter(
        ["/model"],
        tmp_path,
        model_choices=[
            "openai/gpt-6-astra",
            "my-gateway/agent-model",
            "my-gateway/fast-model",
        ],
    )
    event = CompleteEvent(completion_requested=True)

    providers = list(completer.get_completions(Document("/model my"), event))
    models = list(
        completer.get_completions(Document("/model my-gateway/"), event)
    )

    assert [(item.text, item.display_meta_text) for item in providers] == [
        ("my-gateway/", "Configured custom provider")
    ]
    assert [item.text for item in models] == [
        "my-gateway/agent-model",
        "my-gateway/fast-model",
    ]


def test_prompt_keeps_curated_builtin_command_order_before_custom_commands(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    cursor_ui: None,
) -> None:
    prompt = PromptInput(
        input_stream=TtyStringIO(),
        history_path=tmp_path / "history",
        extra_commands=["zzz:custom", "aaa:custom"],
    )

    completions = list(
        prompt._completer.get_completions(  # type: ignore[union-attr]
            Document("/"), CompleteEvent(completion_requested=True)
        )
    )
    words = [completion.text for completion in completions]

    assert words[:9] == [
        "/help",
        "/status",
        "/usage",
        "/cost",
        "/effort",
        "/settings",
        "/cancel",
        "/model",
        "/models",
    ]
    assert words[-2:] == ["/aaa:custom", "/zzz:custom"]


def test_path_completion_scans_a_bounded_number_of_entries(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    original_iterdir = prompt_module.Path.iterdir

    def fake_iterdir(path):
        if path == root:
            return (root / f"file-{index:05d}.txt" for index in range(20_000))
        return original_iterdir(path)

    monkeypatch.setattr(prompt_module.Path, "iterdir", fake_iterdir)
    completer = AshCompleter([], root)

    completions = list(
        completer.get_completions(
            Document("@file-"), CompleteEvent(completion_requested=True)
        )
    )

    assert len(completions) == prompt_module.MAX_PATH_COMPLETIONS


def test_interactive_prompt_history_rejects_symlink(
    tmp_path, monkeypatch, cursor_ui: None
) -> None:
    target = tmp_path / "target"
    target.write_text("keep", encoding="utf-8")
    history_path = tmp_path / "history"
    try:
        history_path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="symlinked prompt history"):
        PromptInput(input_stream=TtyStringIO(), history_path=history_path)
    assert target.read_text(encoding="utf-8") == "keep"


def test_interactive_prompt_history_is_private_and_nofollow(
    tmp_path, monkeypatch, cursor_ui: None
) -> None:
    history_path = tmp_path / "history"
    prompt = PromptInput(input_stream=TtyStringIO(), history_path=history_path)
    assert prompt._surface is not None
    history = prompt._surface.input_buffer.history

    history.append_string("first\nsecond")
    assert "first" in history_path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(history_path.stat().st_mode) == 0o600

    target = tmp_path / "outside"
    target.write_text("keep", encoding="utf-8")
    history_path.unlink()
    try:
        history_path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises((OSError, ValueError), match="symlink|link|loop|follow"):
        history.append_string("redirected")
    assert target.read_text(encoding="utf-8") == "keep"


def test_interactive_prompt_history_redacts_secrets_before_persistence(
    tmp_path,
) -> None:
    history_path = tmp_path / "history"
    history = history_module.PrivateFileHistory(history_path)
    provider_secret = "sk-proj-" + "A" * 32
    signed_marker = "signed-history-marker"
    raw = (
        f"token={provider_secret} "
        "https://storage.example/object?"
        f"X-Amz-Signature={signed_marker}&view=complete"
    )

    history.append_string(raw)

    persisted = history_path.read_text(encoding="utf-8")
    loaded = list(history.load_history_strings())
    assert provider_secret not in persisted
    assert signed_marker not in persisted
    assert provider_secret not in loaded[0]
    assert signed_marker not in loaded[0]
    assert "[REDACTED]" in persisted
    assert "view=complete" in persisted


def test_interactive_prompt_history_retention_is_bounded(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(history_module, "MAX_HISTORY_FILE_BYTES", 700)
    monkeypatch.setattr(history_module, "MAX_HISTORY_ENTRY_BYTES", 240)
    history_path = tmp_path / "history"
    history = history_module.PrivateFileHistory(history_path)

    for index in range(12):
        history.append_string(f"entry-{index}-" + ("x" * 90))

    loaded = list(history.load_history_strings())
    assert history_path.stat().st_size <= 700
    assert loaded[0].startswith("entry-11-")
    assert all(not item.startswith("entry-0-") for item in loaded)


def test_interactive_prompt_history_bounds_single_persisted_entry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(history_module, "MAX_HISTORY_FILE_BYTES", 700)
    monkeypatch.setattr(history_module, "MAX_HISTORY_ENTRY_BYTES", 160)
    history_path = tmp_path / "history"
    history = history_module.PrivateFileHistory(history_path)

    history.append_string("prefix-" + ("y" * 2_000))

    loaded = list(history.load_history_strings())
    assert history_path.stat().st_size <= 700
    assert len(history_path.read_bytes()) < 300
    assert loaded[0].startswith("prefix-")
    assert loaded[0].endswith("[history entry truncated]")


def test_interactive_prompt_history_repairs_existing_posix_permissions(
    tmp_path, monkeypatch, cursor_ui: None
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permissions are unavailable")
    history_path = tmp_path / "history"
    history_path.write_text("", encoding="utf-8")
    history_path.chmod(0o644)

    prompt = PromptInput(input_stream=TtyStringIO(), history_path=history_path)
    assert prompt._surface is not None
    assert stat.S_IMODE(history_path.stat().st_mode) == 0o600


def test_prompt_history_parent_swap_cannot_redirect_append(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "state"
    moved = tmp_path / "state-original"
    outside = tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    history_path = parent / "history"
    history = history_module.PrivateFileHistory(history_path)
    real_validate = history_module.validate_history_path
    swapped = False

    def validate_then_swap(path):
        nonlocal swapped
        real_validate(path)
        if not swapped:
            swapped = True
            parent.rename(moved)
            try:
                parent.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")

    monkeypatch.setattr(history_module, "validate_history_path", validate_then_swap)

    with pytest.raises((OSError, ValueError)):
        history.append_string("must stay local")

    assert swapped is True
    assert not (outside / "history").exists()


def test_prompt_history_closes_file_when_parent_validation_fails_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history_path = tmp_path / "history"
    history_path.write_text("", encoding="utf-8")
    history = history_module.PrivateFileHistory(history_path)
    opened: list[int] = []
    real_open_file = history_module.AnchoredDirectory.open_file
    real_validation_path = history_module.AnchoredDirectory.validation_path
    validation_calls = 0

    def capture_open_file(self, *args, **kwargs):
        descriptor = real_open_file(self, *args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def fail_second_validation(self):
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 2:
            raise history_module.AnchoredFilesystemError("parent identity changed")
        return real_validation_path(self)

    monkeypatch.setattr(history_module.AnchoredDirectory, "open_file", capture_open_file)
    monkeypatch.setattr(
        history_module.AnchoredDirectory,
        "validation_path",
        fail_second_validation,
    )

    with pytest.raises(ValueError, match="redirected prompt history path"):
        tuple(history.load_history_strings())

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_prompt_history_ancestor_symlink_cannot_create_external_parent(
    tmp_path, monkeypatch: pytest.MonkeyPatch, cursor_ui: None
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    history_path = alias / "nested" / "history"

    with pytest.raises((OSError, ValueError)):
        PromptInput(input_stream=TtyStringIO(), history_path=history_path)

    assert not (outside / "nested").exists()
