from __future__ import annotations

import os
import pty
import re
import select
import shutil
import subprocess
import sys
import time
import json
import struct
from pathlib import Path

import pytest


def _plain_terminal_output(raw: bytes) -> bytes:
    return re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", raw).replace(
        b"\r\n", b"\n"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_fullscreen_prompt_limits_mouse_modes_and_restores_terminal(
    tmp_path: Path,
) -> None:
    code = """
import asyncio
from pathlib import Path
from ash.ui.prompt import PromptInput

async def main():
    prompt = PromptInput(history_path=Path(%r))
    try:
        prompt.clear_visible_screen()
        value = await prompt.read("native> ")
    finally:
        await prompt.aclose()
    print("ASH_RESULT=" + value, flush=True)

asyncio.run(main())
""" % str(tmp_path / "history")
    master_fd, slave_fd = pty.openpty()
    environment = os.environ.copy()
    environment.pop("NO_COLOR", None)
    environment["TERM"] = "xterm-256color"
    environment["COLORTERM"] = "truecolor"
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=Path(__file__).parents[2],
        env=environment,
        close_fds=True,
    )
    os.close(slave_fd)
    captured = bytearray()
    sent = False
    deadline = time.monotonic() + 5
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(master_fd, 65_536)
                except OSError:
                    break
                if not chunk:
                    break
                captured.extend(chunk)
            if not sent and "›".encode() in captured:
                os.write(master_fd, b"hello\r")
                sent = True
            if b"ASH_RESULT=hello" in captured:
                break
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master_fd)

    raw = bytes(captured)
    assert b"ASH_RESULT=hello" in raw
    assert b"\x1b[3J" not in raw
    assert b"\x1b[?1049h" in raw
    assert b"\x1b[?1049l" in raw
    assert b"\x1b[?1000h" in raw and b"\x1b[?1000l" in raw
    assert b"\x1b[?1006h" in raw and b"\x1b[?1006l" in raw
    for sequence in (
        b"\x1b[?1002h",
        b"\x1b[?1003h",
        b"\x1b[?1015h",
    ):
        assert sequence not in raw


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_pty_wheel_scrolls_history_without_changing_composer_or_prompt_history(
    tmp_path: Path,
) -> None:
    code = r"""
import asyncio
import json
from pathlib import Path
from ash.ui.prompt import PromptInput
from ash.ui.transcript import Transcript

async def main():
    transcript = Transcript()
    for index in range(40):
        transcript.append("user", f"question {index}", title="you")
        transcript.append("assistant", f"answer {index}", title="ash")
    prompt = PromptInput(
        history_path=Path("__HISTORY__"),
        status_provider=lambda: "pty-model · repo",
        context_provider=lambda: (50, 100),
        transcript=transcript,
    )
    surface = prompt._surface
    assert surface is not None
    history = surface.input_buffer.history
    history.append_string("previous prompt")
    pending = asyncio.create_task(prompt.read())
    await asyncio.sleep(0.15)

    def capture():
        screen = surface.application.renderer._last_screen
        assert screen is not None
        rows = {
            row: "".join(cell.char for _column, cell in sorted(cells.items())).rstrip()
            for row, cells in screen.data_buffer.items()
        }
        composer = next(row for row, value in rows.items() if "›" in value)
        status = next(row for row, value in rows.items() if "50%" in value)
        return {"rows": rows, "composer": composer, "status": status}

    before = capture()
    Path("__READY__").write_text("ready", encoding="utf-8")
    deadline = asyncio.get_running_loop().time() + 4
    while not surface.transcript_view.detached:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("SGR wheel event did not detach transcript view")
        await asyncio.sleep(0.02)
    after = capture()
    Path("__STATES__").write_text(json.dumps({
        "before": before,
        "after": after,
        "input": surface.input_buffer.text,
        "history": history.get_strings(),
    }), encoding="utf-8")
    value = await pending
    await prompt.aclose()
    print("WHEEL_RESULT=" + value, flush=True)

asyncio.run(main())
""".replace("__HISTORY__", str(tmp_path / "history")).replace(
        "__READY__", str(tmp_path / "wheel-ready")
    ).replace("__STATES__", str(tmp_path / "wheel-states.json"))
    master_fd, slave_fd = pty.openpty()
    import fcntl
    import termios

    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 40, 0, 0))
    environment = os.environ.copy()
    environment["TERM"] = "xterm-256color"
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=Path(__file__).parents[2],
        env=environment,
        close_fds=True,
    )
    os.close(slave_fd)
    captured = bytearray()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if select.select([master_fd], [], [], 0.05)[0]:
                try:
                    chunk = os.read(master_fd, 65_536)
                except OSError:
                    break
                if not chunk:
                    break
                captured.extend(chunk)
            if (tmp_path / "wheel-ready").exists():
                os.write(master_fd, b"draft")
                time.sleep(0.1)
                os.write(master_fd, b"\x1b[<64;10;3M")
                break
        else:
            raise AssertionError(bytes(captured).decode("utf-8", errors="replace")[-3000:])

        deadline = time.monotonic() + 5
        while not (tmp_path / "wheel-states.json").exists() and time.monotonic() < deadline:
            if select.select([master_fd], [], [], 0.05)[0]:
                try:
                    captured.extend(os.read(master_fd, 65_536))
                except OSError:
                    break
        states = json.loads((tmp_path / "wheel-states.json").read_text())
        assert states["after"]["composer"] == states["before"]["composer"]
        assert states["after"]["status"] == states["before"]["status"]
        assert states["input"] == "draft"
        assert states["history"] == ["previous prompt"]
        os.write(master_fd, b"\r")
        deadline = time.monotonic() + 5
        while b"WHEEL_RESULT=draft" not in captured and time.monotonic() < deadline:
            if select.select([master_fd], [], [], 0.05)[0]:
                try:
                    captured.extend(os.read(master_fd, 65_536))
                except OSError:
                    break
        assert b"WHEEL_RESULT=draft" in captured
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master_fd)

    raw = bytes(captured)
    assert b"\x1b[?1000h" in raw and b"\x1b[?1000l" in raw
    assert b"\x1b[?1006h" in raw and b"\x1b[?1006l" in raw
    assert b"\x1b[3J" not in raw


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_fullscreen_prompt_keeps_composer_at_bottom_after_live_resize(
    tmp_path: Path,
) -> None:
    import fcntl
    import termios

    ready_path = tmp_path / "resize-ready"
    state_path = tmp_path / "resize-state.json"
    code = r"""
import asyncio
import json
from pathlib import Path
from ash.ui.prompt import PromptInput

async def main():
    prompt = PromptInput(history_path=Path("__HISTORY__"), context_provider=lambda: (25, 100))
    surface = prompt._surface
    assert surface is not None
    pending = asyncio.create_task(prompt.read())
    await asyncio.sleep(0.15)

    def capture():
        size = surface.application.output.get_size()
        composer_info = surface.composer_input_window.render_info
        status_info = surface.status_window.render_info
        composer = None if composer_info is None else composer_info._y_offset
        status = None if status_info is None else status_info._y_offset
        return {
            "size": [size.rows, size.columns],
            "composer": composer,
            "status": status,
            "input": surface.input_buffer.text,
            "completion_open": surface.input_buffer.complete_state is not None,
        }

    before = capture()
    Path("__READY__").write_text("ready", encoding="utf-8")
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        after = capture()
        if (
            after["size"] != before["size"]
            and after["composer"] == after["size"][0] - 2
            and after["status"] == after["size"][0] - 1
        ):
            break
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("fullscreen layout did not settle at resized dimensions")
        await asyncio.sleep(0.05)
    Path("__STATES__").write_text(json.dumps({"before": before, "after": after}), encoding="utf-8")
    value = await pending
    await prompt.aclose()
    print("RESIZE_RESULT=" + value, flush=True)

asyncio.run(main())
""".replace("__HISTORY__", str(tmp_path / "resize-history")).replace(
        "__READY__", str(ready_path)
    ).replace("__STATES__", str(state_path))
    environment = os.environ.copy()
    environment["TERM"] = "xterm-256color"
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    pid, master_fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 14, 60, 0, 0))
        os.chdir(Path(__file__).parents[2])
        os.execvpe(sys.executable, [sys.executable, "-c", code], environment)
    captured = bytearray()
    child_status = None
    try:
        deadline = time.monotonic() + 5
        while not ready_path.exists() and time.monotonic() < deadline:
            if select.select([master_fd], [], [], 0.05)[0]:
                try:
                    captured.extend(os.read(master_fd, 65_536))
                except OSError:
                    break
        assert ready_path.exists(), bytes(captured).decode("utf-8", errors="replace")[-2000:]
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 8, 32, 0, 0))
        deadline = time.monotonic() + 6
        while not state_path.exists() and time.monotonic() < deadline:
            if select.select([master_fd], [], [], 0.05)[0]:
                try:
                    captured.extend(os.read(master_fd, 65_536))
                except OSError:
                    break
        assert state_path.exists(), bytes(captured).decode("utf-8", errors="replace")[-2000:]
        states = json.loads(state_path.read_text())
        assert states["before"]["size"][0] == 14
        assert states["after"]["size"] == [8, 32]
        assert states["before"]["composer"] is not None, states
        assert states["before"]["status"] is not None, states
        assert states["before"]["composer"] == 12
        assert states["before"]["status"] == 13
        assert states["after"]["composer"] is not None, states
        assert states["after"]["status"] is not None, states
        assert states["after"]["composer"] == 6
        assert states["after"]["status"] == 7
        assert states["after"]["input"] == ""
        assert states["after"]["completion_open"] is False
        os.write(master_fd, b"ok\r")
        deadline = time.monotonic() + 5
        while b"RESIZE_RESULT=ok" not in captured and time.monotonic() < deadline:
            if select.select([master_fd], [], [], 0.05)[0]:
                try:
                    captured.extend(os.read(master_fd, 65_536))
                except OSError:
                    break
        assert b"RESIZE_RESULT=ok" in captured
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            finished, child_status = os.waitpid(pid, os.WNOHANG)
            if finished:
                break
            if select.select([master_fd], [], [], 0.05)[0]:
                try:
                    captured.extend(os.read(master_fd, 65_536))
                except OSError:
                    break
        assert child_status is not None
    finally:
        if child_status is None:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        os.close(master_fd)

    raw = bytes(captured)
    assert b"\x1b[?1049h" in raw and b"\x1b[?1049l" in raw
    assert b"\x1b[?1000h" in raw and b"\x1b[?1000l" in raw
    assert b"\x1b[?1006h" in raw and b"\x1b[?1006l" in raw
    assert b"\x1b[3J" not in raw
    assert os.waitstatus_to_exitcode(child_status) == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_fullscreen_prompt_restores_terminal_after_interruption(
    tmp_path: Path,
) -> None:
    code = """
import asyncio
from pathlib import Path
from ash.ui.prompt import PromptInput

async def main():
    prompt = PromptInput(history_path=Path(%r))
    pending = asyncio.create_task(prompt.read())
    await asyncio.sleep(0.15)
    pending.cancel()
    try:
        await pending
    except asyncio.CancelledError:
        pass
    finally:
        await prompt.aclose()
    print("CANCELLED_CLEANLY", flush=True)

asyncio.run(main())
""" % str(tmp_path / "cancel-history")
    master_fd, slave_fd = pty.openpty()
    environment = os.environ.copy()
    environment["TERM"] = "xterm-256color"
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=Path(__file__).parents[2],
        env=environment,
        close_fds=True,
    )
    os.close(slave_fd)
    captured = bytearray()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and b"CANCELLED_CLEANLY" not in captured:
            if select.select([master_fd], [], [], 0.05)[0]:
                try:
                    captured.extend(os.read(master_fd, 65_536))
                except OSError:
                    break
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master_fd)

    raw = bytes(captured)
    assert b"CANCELLED_CLEANLY" in raw
    assert b"\x1b[?1000h" in raw and b"\x1b[?1000l" in raw
    assert b"\x1b[?1006h" in raw and b"\x1b[?1006l" in raw
    assert b"\x1b[?1049h" in raw and b"\x1b[?1049l" in raw
    assert b"\x1b[3J" not in raw


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_repl_failure_restores_fullscreen_terminal_modes(tmp_path: Path) -> None:
    code = """
import asyncio
from pathlib import Path
import ash.cli as cli
from ash.ui.prompt import PromptInput
from ash.ui.terminal import TerminalUI

class Loop:
    def __init__(self, ui):
        self.ui = ui

    async def start_session(self, session_id):
        return None

    async def aclose(self):
        return None

async def main():
    ui = TerminalUI()
    prompt = PromptInput(history_path=Path(%r))
    ui.bind_prompt_surface(prompt.invalidate, prompt.write_terminal, prompt.aclose)

    async def failed_repl(loop, config, sandbox_manager):
        del loop, config, sandbox_manager
        pending = asyncio.create_task(prompt.read())
        await asyncio.sleep(0.15)
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        raise RuntimeError("injected REPL failure")

    cli._repl = failed_repl
    result = await cli._bootstrap_and_repl(Loop(ui), object(), object(), session_id=None)
    print(f"REPL_EXIT={result}", flush=True)

asyncio.run(main())
""" % str(tmp_path / "failure-history")
    master_fd, slave_fd = pty.openpty()
    environment = os.environ.copy()
    environment["TERM"] = "xterm-256color"
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=Path(__file__).parents[2],
        env=environment,
        close_fds=True,
    )
    os.close(slave_fd)
    captured = bytearray()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and b"REPL_EXIT=" not in captured:
            if select.select([master_fd], [], [], 0.05)[0]:
                try:
                    captured.extend(os.read(master_fd, 65_536))
                except OSError:
                    break
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master_fd)

    raw = bytes(captured)
    assert b"REPL_EXIT=" in raw
    assert b"\x1b[?1049h" in raw and b"\x1b[?1049l" in raw
    assert b"\x1b[?1000h" in raw and b"\x1b[?1000l" in raw
    assert b"\x1b[?1006h" in raw and b"\x1b[?1006l" in raw
    assert b"\x1b[3J" not in raw


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_fullscreen_surface_renders_live_turn_with_wheel_mouse_modes(
    tmp_path: Path,
) -> None:
    code = """
import asyncio
from pathlib import Path
from ash.ui.prompt import PromptInput
from ash.ui.terminal import TerminalUI

async def main():
    ui = TerminalUI()
    prompt = PromptInput(
        history_path=Path(%r),
        status_provider=lambda: "gpt-test · effort medium · ~/Ash-Harness",
        context_provider=lambda: (32_000, 64_000),
        thinking_provider=ui.prompt_thinking_view,
        transcript=ui.transcript,
    )
    ui.bind_prompt_surface(prompt.invalidate, prompt.write_terminal)
    ui.record_user_input("hello from user")
    with ui.begin_turn():
        async def stream_response():
            await asyncio.sleep(0.2)
            ui.print_thought("Inspecting the repository")
            ui.print_token("I found the issue ")
            await asyncio.sleep(0.2)
            ui.print_token("and I am applying the fix.")

        stream_task = asyncio.create_task(stream_response())
        value = await prompt.read("steer> ")
        await stream_task
        await prompt.aclose()
    ui.finalize_turn()
    ui.commit_completed_turn()
    print("HYBRID_RESULT=" + value, flush=True)

asyncio.run(main())
""" % str(tmp_path / "hybrid-history")
    master_fd, slave_fd = pty.openpty()
    environment = os.environ.copy()
    environment.pop("NO_COLOR", None)
    environment["TERM"] = "xterm-256color"
    environment["COLORTERM"] = "truecolor"
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=Path(__file__).parents[2],
        env=environment,
        close_fds=True,
    )
    os.close(slave_fd)
    captured = bytearray()
    sent = False
    deadline = time.monotonic() + 5
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(master_fd, 65_536)
                except OSError:
                    break
                if not chunk:
                    break
                captured.extend(chunk)
            if (
                not sent
                and b"I found the issue" in captured
                and b"and I am applying the fix." in captured
            ):
                os.write(master_fd, b"continue\r")
                sent = True
            if b"HYBRID_RESULT=continue" in captured:
                break
        assert sent, bytes(captured).decode("utf-8", errors="replace")[-3000:]
        assert b"HYBRID_RESULT=continue" in captured, bytes(captured).decode(
            "utf-8", errors="replace"
        )[-3000:]
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master_fd)

    raw = bytes(captured)
    plain = _plain_terminal_output(raw).decode("utf-8").replace("\r", "")
    assert b"HYBRID_RESULT=continue" in raw
    for marker in (
        b"hello from user",
        b"ASH",
        b"Inspecting the repository",
        b"I found the issue",
        b"and I am applying the fix.",
        b"gpt-test",
        b"effort medium",
        b"50%",
    ):
        assert marker in raw
    assert raw.index(b"hello from user") < raw.index(b"ASH")
    assert raw.index(b"ASH") < raw.index(b"I found the issue")
    assert raw.index(b"I found the issue") < raw.index(b"and I am applying the fix.")
    assert "> hello from user" in plain
    assert plain.index("> hello from user") < plain.index("ASH")
    assert plain.index("ASH") < plain.index("I found the issue")
    assert "gpt-test · effort medium · ~/Ash-Harness" in plain
    assert "50%" in plain
    assert b"YOU" not in raw
    assert b"\x1b[3J" not in raw
    assert b"\x1b[?1000h" in raw and b"\x1b[?1000l" in raw
    assert b"\x1b[?1006h" in raw and b"\x1b[?1006l" in raw
    for sequence in (b"\x1b[?1002h", b"\x1b[?1003h", b"\x1b[?1015h"):
        assert sequence not in raw


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_activity_dock_animates_and_collapses_on_a_narrow_real_pty(
    tmp_path: Path,
) -> None:
    import fcntl
    import termios

    ready_path = tmp_path / "dock-ready"
    states_path = tmp_path / "dock-states.json"
    code = r"""
import asyncio
import json
from pathlib import Path
from rich.console import Console
from ash.ui.prompt import PromptInput
from ash.ui.terminal import TerminalUI

async def main():
    ui = TerminalUI(
        console=Console(force_terminal=True, color_system="truecolor", no_color=False),
    )
    prompt = PromptInput(
        history_path=Path("__HISTORY__"),
        status_provider=lambda: "gpt-test · effort medium · ~/Ash-Harness",
        context_provider=lambda: (32, 64),
        dock_provider=ui.prompt_dock_view,
        transcript=ui.transcript,
        no_color=False,
    )
    ui.bind_prompt_surface(prompt.invalidate, prompt.write_terminal)
    ui.record_user_input("123456789012345678901234567890🙂\nsecond line")
    snapshots = []

    def capture(name):
        surface = prompt._surface
        assert surface is not None
        screen = surface.application.renderer._last_screen
        assert screen is not None
        rows = []
        for y in range(12):
            cells = screen.data_buffer.get(y, {})
            rows.append("".join(cell.char for _x, cell in sorted(cells.items())).rstrip())
        snapshots.append({"name": name, "rows": rows})

    with ui.begin_turn():
        pending = asyncio.create_task(prompt.read("steer> "))
        await asyncio.sleep(0.1)
        ui.emit_event({"type": "turn.started"})
        ui.emit_event({"type": "model.request.started", "operation_id": "r1"})
        await asyncio.sleep(0.05)
        capture("thinking-a")
        await asyncio.sleep(0.5)
        capture("thinking-b")
        await asyncio.sleep(0.5)
        capture("thinking-c")
        ui.print_thought("checking the narrow terminal layout")
        await asyncio.sleep(0.05)
        capture("reasoning-with-activity")
        ui.emit_event({"type": "model.request.completed", "operation_id": "r1"})
        await asyncio.sleep(0.05)
        capture("reasoning-only")
        ui.emit_event({"type": "tool.requested", "tool": "read_file", "call_id": "f1"})
        ui.emit_event({"type": "tool.started", "tool": "read_file", "call_id": "f1"})
        await asyncio.sleep(0.05)
        capture("inspecting")
        ui.emit_event({"type": "tool.output", "tool": "read_file", "call_id": "f1", "delta": "tool output\n"})
        await asyncio.sleep(0.05)
        capture("tool-output-still-active")
        ui.emit_event({"type": "tool.completed", "tool": "read_file", "call_id": "f1", "success": True})
        await asyncio.sleep(0.05)
        capture("tool-completed")
        ui.emit_event({"type": "turn.completed"})
        await asyncio.sleep(0.05)
        capture("idle")
        Path("__READY__").write_text("ready", encoding="utf-8")
        value = await pending
    ui.finalize_turn()
    await prompt.aclose()
    Path("__STATES__").write_text(json.dumps(snapshots), encoding="utf-8")
    print("DOCK_RESULT=" + value, flush=True)

asyncio.run(main())
"""
    code = (
        code.replace("__HISTORY__", str(tmp_path / "history"))
        .replace("__READY__", str(ready_path))
        .replace("__STATES__", str(states_path))
    )
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 30, 0, 0))
    environment = os.environ.copy()
    environment.pop("NO_COLOR", None)
    environment["TERM"] = "xterm-256color"
    environment["COLORTERM"] = "truecolor"
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=Path(__file__).parents[2],
        env=environment,
        close_fds=True,
    )
    os.close(slave_fd)
    captured = bytearray()
    deadline = time.monotonic() + 8
    sent = False
    try:
        while time.monotonic() < deadline:
            if select.select([master_fd], [], [], 0.05)[0]:
                try:
                    chunk = os.read(master_fd, 65_536)
                except OSError:
                    break
                if not chunk:
                    break
                captured.extend(chunk)
            if ready_path.exists() and not sent:
                os.write(master_fd, b"done\r")
                sent = True
            if b"DOCK_RESULT=done" in captured:
                break
        assert sent, bytes(captured).decode("utf-8", errors="replace")[-3000:]
        assert b"DOCK_RESULT=done" in captured
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master_fd)

    raw = bytes(captured)
    states = {
        snapshot["name"]: snapshot["rows"]
        for snapshot in json.loads(states_path.read_text())
    }
    thinking_frames = [
        next(row for row in states[name] if row.startswith("Thinking"))
        for name in ("thinking-a", "thinking-b", "thinking-c")
    ]
    assert len(set(thinking_frames)) == 3
    assert any("Inspecting" in row for row in states["inspecting"])
    assert any("checking the n" in row for row in states["reasoning-with-activity"])
    assert any("Reasoning:" in row for row in states["reasoning-only"])
    assert any("Inspecting" in row for row in states["tool-output-still-active"])
    assert any("Reasoning:" in row for row in states["tool-completed"])
    active_composer_row = next(
        index for index, row in enumerate(states["inspecting"]) if "›" in row
    )
    idle_composer_row = next(
        index for index, row in enumerate(states["idle"]) if "›" in row
    )
    assert idle_composer_row == active_composer_row
    assert not any(
        marker in row
        for row in states["idle"]
        for marker in ("Thinking", "Inspecting", "Reasoning:")
    )
    assert any(row.startswith("> 123456789012345678") for row in states["idle"])
    assert any("🙂" in row for row in states["idle"])
    assert any("> second line" in row for row in states["idle"])
    assert b"48;5;236" in raw
    assert b"\x1b[?1049h" in raw and b"\x1b[?1049l" in raw
    assert b"\x1b[?1000h" in raw and b"\x1b[?1000l" in raw
    assert b"\x1b[?1006h" in raw and b"\x1b[?1006l" in raw
    for sequence in (b"\x1b[?1002h", b"\x1b[?1003h", b"\x1b[?1015h"):
        assert sequence not in raw


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_resume_and_clear_replace_the_active_conversation_view(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from ash.core.session import Message, SessionStore

    workspace = tmp_path / "workspace"
    child_home = tmp_path / "home"
    db_directory = tmp_path / "db"
    workspace.mkdir()
    child_home.mkdir()
    db_directory.mkdir()
    environment = {
        "PATH": os.defpath,
        "HOME": str(child_home),
        "USERPROFILE": str(child_home),
        "TMPDIR": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
        "ASH_DB_DIRECTORY": str(db_directory),
        "ASH_MODEL": "lmstudio/local-model",
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
    }
    denied = subprocess.run(
        [sys.executable, "-m", "ash", "trust", "remove", str(workspace)],
        cwd=workspace,
        env=environment,
        capture_output=True,
        timeout=30,
    )
    assert denied.returncode == 0, denied.stderr.decode("utf-8", errors="replace")

    store = SessionStore(db_directory / "sessions.db")
    active = store.create_session(str(workspace), model="lmstudio/local-model")
    selected = store.create_session(str(workspace), model="lmstudio/local-model")
    timestamp = datetime.now(timezone.utc)
    store.save_message(
        active.session_id,
        Message(role="user", content="abandoned question", timestamp=timestamp),
    )
    store.save_message(
        active.session_id,
        Message(role="assistant", content="abandoned answer", timestamp=timestamp),
    )
    store.save_message(
        selected.session_id,
        Message(role="user", content="selected question", timestamp=timestamp),
    )
    store.save_message(
        selected.session_id,
        Message(role="assistant", content="selected answer", timestamp=timestamp),
    )

    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-m", "ash", "--session", active.session_id],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=workspace,
        env=environment,
        close_fds=True,
    )
    os.close(slave_fd)
    captured = bytearray()

    def expect(marker: bytes, *, after: int = 0) -> None:
        deadline = time.monotonic() + 30
        while marker not in captured[after:]:
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"terminal did not show {marker!r}: "
                    + bytes(captured).decode("utf-8", errors="replace")[-3000:]
                )
            if select.select([master_fd], [], [], 0.05)[0]:
                try:
                    chunk = os.read(master_fd, 65_536)
                except OSError as exc:
                    raise AssertionError("terminal closed before expected output") from exc
                if not chunk:
                    raise AssertionError("terminal ended before expected output")
                captured.extend(chunk)

    try:
        expect(b"abandoned answer")
        expect("›".encode())
        os.write(master_fd, f"/resume {selected.session_id}\r".encode())
        expect(b"selected answer")
        selected_answer_end = captured.index(b"selected answer") + len(
            b"selected answer"
        )
        time.sleep(0.2)
        os.write(master_fd, b"/clear\r")
        expect(b"Ready", after=selected_answer_end)
        time.sleep(0.1)
        os.write(master_fd, b"/exit\r")
        process.wait(timeout=10)
        while select.select([master_fd], [], [], 0.1)[0]:
            try:
                chunk = os.read(master_fd, 65_536)
            except OSError:
                break
            if not chunk:
                break
            captured.extend(chunk)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master_fd)

    raw = bytes(captured)
    plain = _plain_terminal_output(raw).decode("utf-8").replace("\r", "")
    selected_position = raw.index(b"selected question", raw.index(b"abandoned answer"))
    assert raw.index(b"abandoned answer") < selected_position
    assert b"\x1b[3J" not in raw
    assert re.search(r"> abandoned question *\n *\nASH\n· abandoned answer", plain)
    assert b"selected question" in raw
    assert b"selected answer" in raw
    assert "Recent conversation" not in plain
    assert b"Started session" not in raw
    assert process.returncode == 0


@pytest.mark.skipif(
    os.name == "nt"
    or shutil.which("tmux") is None
    or os.environ.get("ASH_RUN_PTY_TESTS") != "1",
    reason="set ASH_RUN_PTY_TESTS=1 with a POSIX tmux pseudo-terminal",
)
def test_interactive_prompt_survives_live_resize(tmp_path: Path) -> None:
    session = f"ash-prompt-{os.getpid()}-{time.monotonic_ns()}"
    code = """
import asyncio
from pathlib import Path
from ash.ui.prompt import PromptInput

async def main():
    prompt = PromptInput(
        history_path=Path(%r),
    )
    try:
        value = await prompt.read("smoke> ")
    finally:
        await prompt.aclose()
    print("PROMPT_RESULT=" + value, flush=True)
    await asyncio.sleep(5)

asyncio.run(main())
""" % str(tmp_path / "history")
    target = f"{session}:0.0"
    try:
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-x",
                "100",
                "-y",
                "30",
                "-s",
                session,
                sys.executable,
                "-c",
                code,
            ],
            check=True,
            cwd=Path(__file__).parents[2],
        )
        subprocess.run(
            ["tmux", "set-option", "-t", session, "remain-on-exit", "on"],
            check=True,
        )
        time.sleep(0.3)
        subprocess.run(
            ["tmux", "resize-window", "-t", session, "-x", "40", "-y", "10"],
            check=True,
        )

        resized_capture = ""
        resized = False
        resize_deadline = time.monotonic() + 5
        while time.monotonic() < resize_deadline:
            pane_size = subprocess.run(
                [
                    "tmux",
                    "display-message",
                    "-p",
                    "-t",
                    target,
                    "#{pane_width}x#{pane_height}",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            resized_capture = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", target],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            prompt_visible = "SMOKE> ›" in resized_capture
            if pane_size == "40x10" and prompt_visible:
                resized = True
                break
            time.sleep(0.05)
        assert resized, resized_capture

        subprocess.run(["tmux", "send-keys", "-t", target, "-l", "hello"], check=True)
        subprocess.run(["tmux", "send-keys", "-t", target, "C-m"], check=True)

        capture = ""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            capture = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", target],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            if "PROMPT_RESULT=hello" in capture:
                break
            time.sleep(0.1)
        assert "PROMPT_RESULT=hello" in capture
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            check=False,
            capture_output=True,
        )


@pytest.mark.skipif(
    os.name == "nt"
    or shutil.which("tmux") is None
    or os.environ.get("ASH_RUN_PTY_TESTS") != "1",
    reason="set ASH_RUN_PTY_TESTS=1 with a POSIX tmux pseudo-terminal",
)
def test_screen_reader_mode_is_linear_in_real_terminal(tmp_path: Path) -> None:
    session = f"ash-screen-reader-{os.getpid()}-{time.monotonic_ns()}"
    code = """
import asyncio
from pathlib import Path
from ash.ui.prompt import PromptInput
from ash.ui.terminal import TerminalUI

async def main():
    ui = TerminalUI(screen_reader_mode=True, show_token_meter=True)
    with ui.begin_turn():
        ui.print_thought("checking accessibility")
        ui.print_token("**accessible output**")
    ui.finalize_turn()

    prompt = PromptInput(
        history_path=Path(%r),
        screen_reader_mode=True,
    )
    try:
        value = await prompt.read("accessible> ")
    finally:
        prompt.close()
    print("SCREEN_READER_RESULT=" + value, flush=True)
    await asyncio.sleep(5)

asyncio.run(main())
""" % str(tmp_path / "screen-reader-history")
    target = f"{session}:0.0"
    try:
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-x",
                "80",
                "-y",
                "24",
                "-s",
                session,
                sys.executable,
                "-c",
                code,
            ],
            check=True,
            cwd=Path(__file__).parents[2],
        )
        subprocess.run(
            ["tmux", "set-option", "-t", session, "remain-on-exit", "on"],
            check=True,
        )

        capture = ""
        ready_deadline = time.monotonic() + 5
        while time.monotonic() < ready_deadline:
            capture = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", target],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            if "accessible>" in capture:
                break
            time.sleep(0.05)
        assert "accessible>" in capture
        assert "Reasoning: checking accessibility" in capture
        assert "accessible output" in capture
        assert "╭" not in capture

        subprocess.run(["tmux", "send-keys", "-t", target, "-l", "hello"], check=True)
        subprocess.run(["tmux", "send-keys", "-t", target, "C-m"], check=True)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            capture = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", target],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            if "SCREEN_READER_RESULT=hello" in capture:
                break
            time.sleep(0.05)
        assert "SCREEN_READER_RESULT=hello" in capture
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            check=False,
            capture_output=True,
        )
