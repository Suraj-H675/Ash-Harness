from __future__ import annotations

import json
import os
import re
import select
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ash.core.session import SessionStore
from ash.safety.grants import load_permission_rules


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
    args: tuple[str, ...] = (),
    python_executable: str | None = None,
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
        [python_executable or sys.executable, "-m", "ash", *args],
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


@pytest.mark.parametrize("output_format", ("json", "stream-json"))
def test_headless_bare_resume_keeps_structured_error_output_clean(
    tmp_path: Path, output_format: str
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    environment = _terminal_environment(home, workspace, model="lmstudio/local-model")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ash",
            "--resume",
            "-p",
            "hello",
            "--output-format",
            output_format,
        ],
        cwd=workspace,
        env=environment,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 1, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "--resume" in events[0]["error"]["message"]


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
def test_model_switch_with_opaque_history_requires_explicit_context_restart(
    tmp_path: Path,
) -> None:
    """Do not move an opaque provider's state into another model in the REPL."""
    from datetime import datetime, timezone

    from ash.core.session import Message

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (home / "db").mkdir()
    environment = _terminal_environment(home, workspace, model="lmstudio/local-model")
    _set_workspace_trust(workspace, environment, trusted=False)
    store = SessionStore(home / "db" / "sessions.db")
    source = store.create_session(str(workspace), model="lmstudio/local-model")
    store.save_message(
        source.session_id,
        Message(
            role="assistant",
            content="Original model answer",
            timestamp=datetime.now(timezone.utc),
            metadata={
                "provider_state": [
                    {
                        "type": "reasoning", "id": "rs-1", "summary": [],
                        "encrypted_content": "old-provider-replay-state",
                    }
                ]
            },
        ),
    )
    original = store.load_session(source.session_id)
    master, process = _open_pty_cli(
        workspace, environment, columns=120, args=("--session", source.session_id)
    )
    observed = bytearray()

    try:
        _read_until(master, process, observed, b"> ", timeout=30)
        os.write(master, b"/model lmstudio/backup-model\r")
        _read_until(master, process, observed, b"/restart-context", timeout=20)
        assert process.poll() is None
        assert store.session_model(source.session_id) == "lmstudio/local-model"
        assert store.load_session(source.session_id).messages == original.messages

        os.write(master, b"/restart-context Reinspect files before continuing\r")
        _read_until(master, process, observed, b"Started a new reasoning window")
        children = [
            row for row in store.list_sessions(project_path=str(workspace))
            if row.session_id != source.session_id
        ]
        assert len(children) == 1
        child_id = children[0].session_id
        assert store.load_session(child_id).parent_session_id == source.session_id

        os.write(master, b"/model lmstudio/backup-model\r")
        _read_until(master, process, observed, b"Switched to lmstudio/backup-model")
        assert store.session_model(child_id) == "lmstudio/backup-model"
        assert store.session_model(source.session_id) == "lmstudio/local-model"
        assert store.load_session(source.session_id).messages == original.messages
        os.write(master, b"/exit\r")
        process.wait(timeout=10)
        assert process.returncode == 0
    finally:
        _stop_pty_cli(master, process)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_manual_compact_refusal_keeps_opaque_session_alive(
    tmp_path: Path,
) -> None:
    """A refused opaque-state compaction must not terminate the real REPL."""
    from datetime import datetime, timezone

    from ash.core.session import Message

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (home / "db").mkdir()
    environment = _terminal_environment(home, workspace, model="lmstudio/local-model")
    _set_workspace_trust(workspace, environment, trusted=False)
    store = SessionStore(home / "db" / "sessions.db")
    source = store.create_session(str(workspace), model="lmstudio/local-model")
    now = datetime.now(timezone.utc)
    store.save_message(
        source.session_id,
        Message(role="user", content="Previous coding task", timestamp=now),
    )
    store.save_message(
        source.session_id,
        Message(
            role="assistant",
            content="Prior answer",
            timestamp=now,
            metadata={
                "provider_state": [
                    {"type": "reasoning", "encrypted_content": "opaque-replay"}
                ]
            },
        ),
    )
    original = store.load_session(source.session_id)
    master, process = _open_pty_cli(
        workspace, environment, columns=120,
        args=("--session", source.session_id),
    )
    observed = bytearray()

    try:
        _read_until(master, process, observed, b"> ", timeout=30)
        os.write(master, b"/compact\r")
        _read_until(master, process, observed, b"/restart-context", timeout=15)
        assert process.poll() is None, "refusing /compact exited the interactive CLI"
        os.write(master, b"/status\r")
        _read_until(
            master, process, observed,
            f"Session: {source.session_id}".encode(), timeout=15,
        )
        assert store.load_session(source.session_id).messages == original.messages
        assert store.load_session(source.session_id).context_summary == ""
        os.write(master, b"/exit\r")
        process.wait(timeout=10)
        assert process.returncode == 0
    finally:
        _stop_pty_cli(master, process)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_restart_context_in_real_repl_preserves_parent_and_permission_mode(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timezone

    from ash.core.session import Message, SessionStore

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (home / "db").mkdir()
    environment = _terminal_environment(
        home, workspace, model="lmstudio/local-model"
    )
    _set_workspace_trust(workspace, environment, trusted=False)
    store = SessionStore(home / "db" / "sessions.db")
    parent = store.create_session(
        str(workspace), model="lmstudio/local-model", permission_mode="plan"
    )
    store.save_message(
        parent.session_id,
        Message(
            role="user",
            content="Prior conversation should stay in the parent",
            timestamp=datetime.now(timezone.utc),
        ),
    )
    parent_before = store.load_session(parent.session_id)
    master, process = _open_pty_cli(
        workspace, environment, columns=120,
        args=("--session", parent.session_id),
    )
    captured = bytearray()

    try:
        _read_until(master, process, captured, b"> ", timeout=30)
        os.write(master, b"/restart-context Verify the current code before edits\r")
        _read_until(
            master, process, captured,
            b"Started a new reasoning window", timeout=30,
        )
        children = [
            row for row in store.list_sessions(project_path=str(workspace))
            if row.session_id != parent.session_id
        ]
        assert len(children) == 1
        child = store.load_session(children[0].session_id)
        assert child.parent_session_id == parent.session_id
        assert child.permission_mode == "plan"
        assert child.context_summary == ""
        assert len(child.messages) == 1
        assert child.messages[0].role == "user"
        assert "Verify the current code before edits" in child.messages[0].content
        assert "Prior conversation" not in child.messages[0].content
        assert store.load_session(parent.session_id).messages == parent_before.messages

        os.write(master, b"/status\r")
        _read_until(
            master, process, captured,
            f"Session: {child.session_id}".encode(), timeout=20,
        )
        os.write(master, b"/exit\r")
        process.wait(timeout=10)
        assert process.returncode == 0
    finally:
        _stop_pty_cli(master, process)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_restart_context_in_real_repl_reconciles_failed_turn_without_replay(
    tmp_path: Path,
) -> None:
    """Real CLI handoff from a context-failed turn requires recovered journal."""
    from ash.core.session import SessionStore

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (home / "db").mkdir()
    environment = _terminal_environment(
        home, workspace, model="lmstudio/local-model"
    )
    _set_workspace_trust(workspace, environment, trusted=False)
    store = SessionStore(home / "db" / "sessions.db")
    parent = store.create_session(
        str(workspace), model="lmstudio/local-model", permission_mode="plan"
    )
    store.start_turn(parent.session_id, "turn-context-error", "fix two modules")
    store.interrupt_turn("turn-context-error")
    assert store.interrupted_recovery_reports(parent.session_id) == []

    master, process = _open_pty_cli(
        workspace, environment, columns=120,
        args=("--session", parent.session_id),
    )
    captured = bytearray()
    try:
        _read_until(master, process, captured, b"> ", timeout=30)
        os.write(master, b"/restart-context Inspect the two modules before editing\r")
        _read_until(
            master, process, captured,
            b"Started a new reasoning window", timeout=30,
        )
        children = [
            row for row in store.list_sessions(project_path=str(workspace))
            if row.session_id != parent.session_id
        ]
        assert len(children) == 1
        child = store.load_session(children[0].session_id)
        assert child.parent_session_id == parent.session_id
        assert child.permission_mode == "plan"
        assert child.tool_calls == []
        assert child.fork_message_count == 0
        assert len(child.messages) == 1
        assert "Inspect the two modules" in child.messages[0].content
        assert store.interrupted_recovery_reports(parent.session_id)[0][
            "status"
        ] == "interrupted"
        assert store.load_session(parent.session_id).tool_calls == []
        os.write(master, b"/exit\r")
        process.wait(timeout=10)
        assert process.returncode == 0
    finally:
        _stop_pty_cli(master, process)


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_restart_context_real_repl_refuses_unknown_dispatched_action(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timezone

    from ash.core.session import SessionStore, ToolCallRecord

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (home / "db").mkdir()
    environment = _terminal_environment(
        home, workspace, model="lmstudio/local-model"
    )
    _set_workspace_trust(workspace, environment, trusted=False)
    store = SessionStore(home / "db" / "sessions.db")
    parent = store.create_session(str(workspace), model="lmstudio/local-model")
    store.start_turn(parent.session_id, "turn-unknown", "run outside command")
    store.save_tool_call(parent.session_id, ToolCallRecord(
        call_id="uncertain-external", tool_name="run_command",
        arguments={"command_line": "external-effect"}, approved=True,
        dispatched=True, executed=False, result=None, error=None,
        timestamp=datetime.now(timezone.utc),
    ), turn_id="turn-unknown")
    store.interrupt_turn("turn-unknown")

    master, process = _open_pty_cli(
        workspace, environment, columns=120,
        args=("--session", parent.session_id),
    )
    captured = bytearray()
    try:
        _read_until(master, process, captured, b"> ", timeout=30)
        os.write(master, b"/restart-context Recheck external state safely\r")
        _read_until(master, process, captured, b"quiescent", timeout=30)
        assert b"Started a new reasoning window" not in captured
        assert len(store.list_sessions(project_path=str(workspace))) == 1
        reports = store.interrupted_recovery_reports(parent.session_id)
        assert reports[0]["status"] == "needs_attention"
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
        # The title alone does not prove that the picker accepts input.
        # Check a filter update before testing Ctrl-C cancellation.
        filter_start = len(captured)
        os.write(master, b"zzq9")
        _read_until(master, process, captured, b"No matches for 'zzq9'")
        assert b"No matches for 'zzq9'" in captured[filter_start:]
        os.write(master, b"\x03")
        # Drain during shutdown: on macOS, the last slave close can lose
        # unread output; on Linux, the PTY may close before the child exits.
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.02)[0]:
                try:
                    chunk = os.read(master, 65_536)
                except OSError:
                    break
                if not chunk:
                    break
                captured.extend(chunk)
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pytest.fail("Ctrl-C did not exit setup")
        while select.select([master], [], [], 0)[0]:
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal contract")
def test_fresh_cli_setup_reaches_approved_edit_test_and_safe_resume(
    tmp_path: Path,
) -> None:
    """One real first-run TTY journey; CI can target the installed wheel."""
    python_executable = sys.executable
    installed_python = os.environ.get("ASH_FIRST_USE_WHEEL_PYTHON")
    if installed_python is not None:
        # Do not accept a source-checkout interpreter masquerading as the
        # installed artifact. -I ignores PYTHONPATH and user site packages.
        # Keep the venv executable pathname: resolving its symlink to the base
        # interpreter would drop the venv's own site-packages on CPython/uv.
        wheel_executable = Path(installed_python).expanduser().absolute()
        assert wheel_executable.is_file(), wheel_executable
        python_executable = str(wheel_executable)
        identity = subprocess.run(
            [
                python_executable,
                "-I",
                "-c",
                "import ash, pathlib, sys; "
                "p = pathlib.Path(ash.__file__).resolve(); "
                "prefix = pathlib.Path(sys.prefix).resolve(); "
                "assert p.is_relative_to(prefix), (p, prefix); "
                "assert 'site-packages' in p.parts, p",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert identity.returncode == 0, (identity.stdout, identity.stderr)

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    source = workspace / "calc.py"
    source.write_text("def add_one(n):\n    return n - 1\n", encoding="utf-8")
    (workspace / "qa_visible.py").write_text(
        "from calc import add_one\nassert add_one(2) == 3\n", encoding="utf-8"
    )
    (workspace / "protected.txt").write_text("unchanged\n", encoding="utf-8")

    requests: list[dict[str, object]] = []
    test_command = "python3 -B qa_visible.py"

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path != "/v1/models":
                self.send_error(404)
                return
            self.send_json({
                "data": [
                    {"id": "first-use-model"},
                    {"id": "second-use-model"},
                ]
            })

        def send_json(self, payload: object) -> None:
            content = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            step = len(requests)
            if step == 1:
                output = (
                    '<call_tool name="replace_file_content">'
                    '<arg name="file_path">calc.py</arg>'
                    '<arg name="target_content">    return n - 1\n</arg>'
                    '<arg name="replacement_content">    return n + 1\n</arg>'
                    "</call_tool>"
                )
            elif step == 2:
                output = (
                    '<call_tool name="run_command">'
                    f'<arg name="command_line">{test_command}</arg>'
                    '<arg name="timeout_seconds">15</arg>'
                    "</call_tool>"
                )
            else:
                output = "FIRST-CODING-OK" if step == 3 else "RESUMED-OK"
            chunk = {
                "id": f"first-use-{step}",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": request.get("model", "first-use-model"),
                "choices": [
                    {"index": 0, "delta": {"content": output}, "finish_reason": "stop"}
                ],
            }
            body = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    environment = _terminal_environment(home, workspace, model=None)
    # macOS runners can resolve python3 to Apple's developer-tools shim.
    # Use the actual selected interpreter's bin dir; Linux bubblewrap still
    # resets command PATH to its isolated /usr/bin:/bin as intended.
    environment["PATH"] = os.pathsep.join(
        (str(Path(python_executable).parent), os.defpath)
    )
    if installed_python is not None:
        # A source PYTHONPATH would defeat the whole installed-wheel check.
        environment.pop("PYTHONPATH", None)
    environment.update(
        {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    )
    master: int | None = None
    process: subprocess.Popen[bytes] | None = None
    try:
        master, process = _open_pty_cli(
            workspace, environment, python_executable=python_executable
        )
        captured = bytearray()

        def reply(marker: bytes, answer: bytes, *, timeout: float = 25) -> None:
            fresh = bytearray()
            _read_until(master, process, fresh, marker, timeout=timeout)
            captured.extend(fresh)
            os.write(master, answer + b"\r")

        reply(b"? [y/N] ", b"n")
        reply(b"Press Enter to run setup", b"")
        fresh = bytearray()
        _read_until(master, process, fresh, b"Custom endpoint", timeout=25)
        captured.extend(fresh)
        match = re.search(rb"\[(\d+)\] Custom endpoint", fresh)
        assert match is not None, fresh[-2000:]
        os.write(master, match.group(1) + b"\r")
        reply("Provider › ".encode(), b"openai-compatible")
        reply(b"Provider name (e.g.", b"first-use-route")
        reply(b"Base URL (e.g.", f"http://127.0.0.1:{server.server_port}/v1".encode())
        reply(b"API key (optional, press Enter to skip)", b"")
        reply(b"Select or type a model name", b"1")

        fresh = bytearray()
        _read_until(master, process, fresh, "ASH · type /help".encode(), timeout=30)
        captured.extend(fresh)
        assert b"Setup saved" in captured
        assert requests == [], "setup must not make a model completion request"
        os.write(master, b"Fix calc.py and execute qa_visible.py\r")

        reply(b"Approve [y] once", b"y", timeout=30)
        reply(b"Approve [y] once", b"y", timeout=30)
        fresh = bytearray()
        _read_until(master, process, fresh, b"FIRST-CODING-OK", timeout=30)
        after_response = fresh.split(b"FIRST-CODING-OK", 1)[1]
        if b"> " not in after_response:
            _read_until(master, process, bytearray(), b"> ", timeout=15)
        assert source.read_text(encoding="utf-8") == (
            "def add_one(n):\n    return n + 1\n"
        )
        assert (workspace / "protected.txt").read_text() == "unchanged\n"
        assert len(requests) == 3

        # Reviewing Ash's own checkpoint must work in a fresh, non-Git
        # directory. It is a local read-only command, not another model turn.
        os.write(master, b"/diff --turn\r")
        review = bytearray()
        _read_until(master, process, review, b"+    return n + 1", timeout=15)
        assert b"--- a/calc.py" in review
        assert b"+++ b/calc.py" in review
        assert b"-    return n - 1" in review
        assert b"+    return n + 1" in review
        assert len(requests) == 3
        if not review.endswith(b"> "):
            _read_until(master, process, bytearray(), b"> ", timeout=10)

        os.write(master, b"/model first-use-route/second-use-model\r")
        switched = bytearray()
        _read_until(
            master, process, switched,
            b"Switched to first-use-route/second-use-model", timeout=15,
        )
        assert b"for this session only" in switched
        if not switched.endswith(b"> "):
            _read_until(master, process, bytearray(), b"> ", timeout=10)

        os.write(master, b"Confirm the alternate model\r")
        next_turn = bytearray()
        _read_until(master, process, next_turn, b"RESUMED-OK", timeout=30)
        assert len(requests) == 4
        assert requests[3]["model"] == "second-use-model"
        if not next_turn.endswith(b"> "):
            _read_until(master, process, bytearray(), b"> ", timeout=10)

        os.write(master, b"/exit\r")
        process.wait(timeout=12)
        assert process.returncode == 0

        config_path = home / ".ash" / "ash.toml"
        assert "first-use-route" in config_path.read_text(encoding="utf-8")
        persisted_env = (home / ".ash" / ".env").read_text(encoding="utf-8")
        assert "first-use-route/first-use-model" in persisted_env
        assert "second-use-model" not in persisted_env
        assert "http://127.0.0.1:" in config_path.read_text(encoding="utf-8")
        trust_store = json.loads(
            (home / ".ash" / "trusted-workspaces.json").read_text()
        )
        assert trust_store["workspaces"][str(workspace)] is False
        assert load_permission_rules(workspace) == []

        # Model discovery from setup is not a completion request; doctor must
        # be able to verify the persisted route without consuming a turn.
        doctor = subprocess.run(
            [python_executable, "-m", "ash", "doctor", "--connect", "--json"],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=25,
        )
        assert doctor.returncode == 0, (doctor.stdout, doctor.stderr)
        checks = json.loads(doctor.stdout)["checks"]
        assert any(
            check["name"] == "connectivity" and check["status"] == "pass"
            for check in checks
        ), checks
        assert len(requests) == 4, "doctor must not call the completion endpoint"

        store = SessionStore(home / "db" / "sessions.db")
        sessions = store.list_sessions(project_path=str(workspace), limit=5)
        assert len(sessions) == 1
        session_id = sessions[0].session_id
        calls = store.load_session(session_id).tool_calls
        assert calls[1].arguments["command_line"] == test_command
        assert [
            (
                call.tool_name,
                call.approved,
                call.dispatched,
                call.executed,
                call.success,
                call.error,
            )
            for call in calls
        ] == [
            ("replace_file_content", True, True, True, True, None),
            ("run_command", True, True, True, True, None),
        ]
        assert store.verify_audit_log(session_id) == []

        resumed = subprocess.run(
            [
                python_executable,
                "-m",
                "ash",
                "--continue",
                "-p",
                "Check completion",
                "--output-format",
                "json",
            ],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=25,
        )
        assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
        response = json.loads(resumed.stdout)
        assert response["session_id"] == session_id
        assert response["response"] == "RESUMED-OK"
        assert len(requests) == 5
        assert requests[4]["model"] == "second-use-model"
        assert source.read_text(encoding="utf-8") == (
            "def add_one(n):\n    return n + 1\n"
        )
        assert len(store.load_session(session_id).tool_calls) == 2
        assert store.verify_audit_log(session_id) == []
    finally:
        if master is not None and process is not None:
            _stop_pty_cli(master, process)
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
