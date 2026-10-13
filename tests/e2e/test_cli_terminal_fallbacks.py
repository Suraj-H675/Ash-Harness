from __future__ import annotations

import os
import select
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _terminal_environment(
    home: Path,
    workspace: Path,
    *,
    model: str | None,
) -> dict[str, str]:
    environment = {
        "PATH": os.defpath,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(home.parent),
        "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "NO_COLOR": "1",
        "TERM": "dumb",
        "ASH_DB_DIRECTORY": str(home / "db"),
        "ASH_SCREEN_READER_MODE": "true",
    }
    if model is not None:
        environment["ASH_MODEL"] = model
    return environment


def _set_workspace_trust(
    workspace: Path,
    environment: dict[str, str],
    *,
    trusted: bool,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ash",
            "trust",
            "add" if trusted else "remove",
            str(workspace),
        ],
        cwd=workspace,
        env=environment,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _open_pty_cli(
    workspace: Path,
    environment: dict[str, str],
    *,
    rows: int = 24,
    columns: int = 80,
) -> tuple[int, subprocess.Popen[bytes]]:
    import fcntl
    import pty
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(
        slave,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", rows, columns, 0, 0),
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "ash"],
        cwd=workspace,
        env=environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    return master, process


def _read_until(
    master: int,
    process: subprocess.Popen[bytes],
    captured: bytearray,
    marker: bytes,
    *,
    timeout: float = 20,
) -> None:
    deadline = time.monotonic() + timeout
    while marker not in captured and process.poll() is None:
        if time.monotonic() >= deadline:
            break
        if select.select([master], [], [], 0.05)[0]:
            try:
                chunk = os.read(master, 65_536)
            except OSError:
                break
            if not chunk:
                break
            captured.extend(chunk)
    assert marker in captured, (
        f"Ash did not show {marker!r}; exit={process.poll()}; "
        f"output={bytes(captured[-3000:])!r}"
    )


def _stop_pty_cli(master: int, process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    os.close(master)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_first_run_does_not_prompt_when_stdout_is_redirected(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    environment = _terminal_environment(home, workspace, model=None)

    import pty
    import termios
    import fcntl

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    process = subprocess.Popen(
        [sys.executable, "-m", "ash"],
        cwd=workspace,
        env=environment,
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    os.close(slave)

    try:
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.write(master, b"q\r")
            process.communicate(timeout=5)
            pytest.fail("Ash waited for a setup answer sent to redirected stdout")

        assert process.returncode == 2
        assert b"Ash is not configured" in stderr
        assert b"interactive terminal" in stderr
        assert b"Press Enter to run setup" not in stdout
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        os.close(master)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_linear_help_search_matches_multiple_terms(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    environment = _terminal_environment(
        home,
        workspace,
        model="lmstudio/local-model",
    )
    _set_workspace_trust(workspace, environment, trusted=False)
    master, process = _open_pty_cli(workspace, environment)
    captured = bytearray()

    try:
        _read_until(master, process, captured, b"> ", timeout=30)
        os.write(master, b"/help model switch\r")
        _read_until(master, process, captured, b"/model [provider/model]")
        assert b"No slash commands match" not in captured
        os.write(master, b"/exit\r")
        process.wait(timeout=10)
        assert process.returncode == 0
    finally:
        _stop_pty_cli(master, process)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_config_diagnostic_escapes_controls_from_workspace_path(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace-\x1b[2J-terminal-control"
    home.mkdir()
    workspace.mkdir()
    environment = _terminal_environment(
        home,
        workspace,
        model="lmstudio/local-model",
    )
    _set_workspace_trust(workspace, environment, trusted=True)
    project_config = workspace / ".ash" / "config.toml"
    project_config.parent.mkdir()
    project_config.write_text('mystery_setting = "ignored"\n', encoding="utf-8")
    master, process = _open_pty_cli(workspace, environment)
    captured = bytearray()

    try:
        _read_until(
            master,
            process,
            captured,
            b"Warning: ignored unknown project config key",
            timeout=30,
        )
        start = captured.index(b"Warning: ignored unknown project config key")
        end = captured.find(b"\r\n", start)
        diagnostic = bytes(captured[start : end if end >= 0 else None])
        assert b"\x1b" not in diagnostic
        assert b"\\x1b[2J" in diagnostic
        os.write(master, b"/exit\r")
        process.wait(timeout=10)
        assert process.returncode == 0
    finally:
        _stop_pty_cli(master, process)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_first_run_trust_prompt_escapes_workspace_path_controls(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace-\x1b[2J-control"
    home.mkdir()
    workspace.mkdir()
    environment = _terminal_environment(home, workspace, model="lmstudio/local-model")
    master, process = _open_pty_cli(workspace, environment)
    captured = bytearray()
    try:
        _read_until(master, process, captured, b"Trust project-controlled")
        _read_until(master, process, captured, b"? [y/N]")
        prompt_start = captured.index(b"Trust project-controlled")
        prompt_end = captured.index(b"? [y/N]", prompt_start)
        prompt = bytes(captured[prompt_start:prompt_end])
        assert b"\x1b" not in prompt
        assert b"\\x1b[2J" in prompt
        os.write(master, b"n\r")
        _read_until(master, process, captured, b"> ", timeout=30)
        os.write(master, b"/exit\r")
        process.wait(timeout=10)
        assert process.returncode == 0
    finally:
        _stop_pty_cli(master, process)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_first_run_local_provider_picker_ctrl_c_cancels_setup(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    environment = _terminal_environment(home, workspace, model=None)
    environment["TERM"] = "xterm-256color"
    environment.pop("ASH_SCREEN_READER_MODE", None)
    master, process = _open_pty_cli(
        workspace,
        environment,
        rows=18,
        columns=52,
    )
    captured = bytearray()

    try:
        _read_until(master, process, captured, b"? [y/N] ")
        os.write(master, b"y\r")
        _read_until(master, process, captured, b"Press Enter to run setup")
        os.write(master, b"\r")
        _read_until(
            master,
            process,
            captured,
            b"How do you want to connect Ash?",
        )
        os.write(master, b"Local runtimes\r")
        _read_until(
            master,
            process,
            captured,
            b"Choose an inference provider",
        )
        assert b"Ctrl-C cancel" in captured
        os.write(master, b"\x03")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pytest.fail("Ctrl-C returned to setup instead of canceling the wizard")

        # A process exit can precede the PTY's final readable/EOF notification.
        drain_deadline = time.monotonic() + 1
        while time.monotonic() < drain_deadline:
            remaining = drain_deadline - time.monotonic()
            if not select.select([master], [], [], min(0.1, remaining))[0]:
                continue
            try:
                chunk = os.read(master, 65_536)
            except OSError:
                break
            if not chunk:
                break
            captured.extend(chunk)

        assert process.returncode == 1
        assert b"Setup cancelled." in captured
    finally:
        _stop_pty_cli(master, process)
