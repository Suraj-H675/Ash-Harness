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
def test_default_interface_does_not_enable_mouse_reporting(tmp_path: Path) -> None:
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
        prompt.close()
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
    assert b"\x1b[2J" in raw
    assert b"\x1b[3J" not in raw
    for sequence in (
        b"\x1b[?1000h",
        b"\x1b[?1002h",
        b"\x1b[?1003h",
        b"\x1b[?1006h",
        b"\x1b[?1015h",
    ):
        assert sequence not in raw


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_hybrid_surface_renders_live_turn_and_composer_without_mouse_capture(
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
        prompt.close()
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
            if not sent and b"I found the issue and I am applying the fix." in captured:
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
    plain = _plain_terminal_output(raw).decode("utf-8")
    assert b"HYBRID_RESULT=continue" in raw
    for marker in (
        b"hello from user",
        b"ASH",
        b"Inspecting the repository",
        b"I found the issue and I am applying the fix.",
        b"gpt-test",
        b"effort medium",
        b"50%",
    ):
        assert marker in raw
    assert raw.index(b"hello from user") < raw.index(
        b"I found the issue and I am applying the fix."
    )
    assert raw.count(b"I found the issue and I am applying the fix.") == 1
    assert "> hello from user" in plain
    assert plain.index("> hello from user") < plain.index("ASH")
    assert plain.index("ASH") < plain.index("· I found the issue and I am applying the fix.")
    assert "gpt-test · effort medium · ~/Ash-Harness" in plain
    assert "50%" in plain
    assert b"YOU" not in raw
    assert b"STEER" not in raw
    assert b"\x1b[3J" not in raw
    for sequence in (
        b"\x1b[?1000h",
        b"\x1b[?1002h",
        b"\x1b[?1003h",
        b"\x1b[?1006h",
        b"\x1b[?1015h",
    ):
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
    prompt.close()
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
    plain = _plain_terminal_output(raw).decode("utf-8", errors="replace")
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
    assert "1234567890123456789012345678" in plain
    assert "90🙂" in plain
    assert "second line" in plain
    assert b"48;2;48;48;48" in raw
    assert b"\x1b[?1049h" not in raw
    for sequence in (
        b"\x1b[?1000h",
        b"\x1b[?1002h",
        b"\x1b[?1003h",
        b"\x1b[?1006h",
        b"\x1b[?1015h",
    ):
        assert sequence not in raw


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_resuming_replaces_visible_chat_and_preserves_scrollback(tmp_path: Path) -> None:
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
        expect("›".encode(), after=selected_answer_end)
        os.write(master_fd, b"/exit\r")
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master_fd)

    raw = bytes(captured)
    plain = _plain_terminal_output(raw).decode("utf-8")
    clear_position = raw.index(b"\x1b[2J", raw.index(b"abandoned answer"))
    selected_position = raw.index(b"selected question", clear_position)
    assert raw.index(b"abandoned answer") < clear_position < selected_position
    assert b"\x1b[3J" not in raw
    assert b"abandoned" not in raw[clear_position:]
    assert re.search(r"> abandoned question *\n\nASH\n· abandoned answer", plain)
    assert re.search(r"> selected question *\n\nASH\n· selected answer", plain)
    assert "Recent conversation" not in plain
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
        prompt.close()
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
            prompt_visible = "smoke>" in resized_capture
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
