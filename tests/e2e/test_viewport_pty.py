from __future__ import annotations

import os
import pty
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_default_scrollback_does_not_enable_mouse_reporting(tmp_path: Path) -> None:
    code = """
import asyncio
from pathlib import Path
from ash.ui.prompt import PromptInput

async def main():
    prompt = PromptInput(history_path=Path(%r))
    try:
        value = await prompt.read("native> ")
    finally:
        prompt.close()
    print("SCROLLBACK_RESULT=" + value, flush=True)

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
            if not sent and b"native>" in captured:
                os.write(master_fd, b"hello\r")
                sent = True
            if b"SCROLLBACK_RESULT=hello" in captured:
                break
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master_fd)

    raw = bytes(captured)
    assert b"SCROLLBACK_RESULT=hello" in raw
    for sequence in (
        b"\x1b[?1000h",
        b"\x1b[?1002h",
        b"\x1b[?1003h",
        b"\x1b[?1006h",
        b"\x1b[?1015h",
    ):
        assert sequence not in raw


@pytest.mark.skipif(
    os.name == "nt"
    or shutil.which("tmux") is None
    or os.environ.get("ASH_RUN_PTY_TESTS") != "1",
    reason="set ASH_RUN_PTY_TESTS=1 with a POSIX tmux pseudo-terminal",
)
def test_viewport_restores_terminal_after_live_resize(tmp_path: Path) -> None:
    session = f"ash-viewport-{os.getpid()}-{time.monotonic_ns()}"
    code = """
import asyncio
from pathlib import Path
from ash.ui.prompt import PromptInput
from ash.ui.transcript import Transcript

async def main():
    prompt = PromptInput(
        history_path=Path(%r),
        transcript=Transcript(),
        tui_mode="viewport",
    )
    try:
        value = await prompt.read("smoke> ")
    finally:
        prompt.close()
    print("VIEWPORT_RESULT=" + value, flush=True)
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
            prompt_visible = any(
                line.strip() == "smoke" for line in resized_capture.splitlines()
            )
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
            if "VIEWPORT_RESULT=hello" in capture:
                break
            time.sleep(0.1)
        assert "VIEWPORT_RESULT=hello" in capture
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
        tui_mode="viewport",
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
