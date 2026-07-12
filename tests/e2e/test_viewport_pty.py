from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


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
        subprocess.run(["tmux", "send-keys", "-t", target, "-l", "hello"], check=True)
        subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True)

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
