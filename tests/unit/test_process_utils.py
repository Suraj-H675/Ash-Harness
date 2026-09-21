from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from pathlib import Path
import weakref
from unittest.mock import AsyncMock, Mock, patch

import pytest

from ash.sandbox.process_utils import (
    INHERIT_PROCESS_GROUP_ENV,
    ProcessOutputLimitExceeded,
    ProcessTreePlan,
    ProcessTreeTerminationError,
    ProcessTreeUnavailable,
    communicate_process,
    _descendant_pids,
    _prepare_windows_cwd_launch,
    _resolve_windows_launch_executable,
    prepare_process_tree,
    prepare_scoped_process_launch,
    process_group_options,
    terminate_process_tree,
    terminate_process_tree_sync,
)
from ash.safety.guard import SafetyGuard


def _pinned_popen(pid: int) -> subprocess.Popen[object]:
    owner = object.__new__(subprocess.Popen)
    owner.pid = pid
    return owner


def _pin_async_process(process: Mock, pid: int) -> None:
    process._transport = SimpleNamespace(_proc=_pinned_popen(pid))


def test_process_group_options_use_new_session_on_posix() -> None:
    with patch("ash.sandbox.process_utils.sys.platform", "linux"):
        assert process_group_options() == {"start_new_session": True}


def test_process_group_options_use_new_process_group_on_windows() -> None:
    with (
        patch("ash.sandbox.process_utils.sys.platform", "win32"),
        patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True),
    ):
        assert process_group_options() == {"creationflags": 512}


def test_process_group_options_can_inherit_automation_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(INHERIT_PROCESS_GROUP_ENV, "1")
    assert process_group_options() == {}


def test_prepare_windows_cwd_launch_wraps_command_with_expected_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    cwd = workspace / "work"
    workspace.mkdir()
    cwd.mkdir()
    host_python = tmp_path / "host-python.exe"
    resolved_tool = tmp_path / "resolved-tool.exe"
    monkeypatch.setattr(
        "ash.sandbox.process_utils._resolve_trusted_python_launcher",
        lambda workspace_root, *, search_path: str(host_python),
    )
    monkeypatch.setattr(
        "ash.sandbox.process_utils._resolve_windows_launch_executable",
        lambda command, *, search_path: str(resolved_tool),
    )

    launch = _prepare_windows_cwd_launch(
        cwd,
        ["tool.exe", "--flag"],
        guard=SafetyGuard(workspace),
        search_path="ignored",
        expected_identity=(12, 34),
    )

    assert launch.cwd == str(cwd)
    assert launch.argv[:4] == (str(host_python), "-I", "-S", "-c")
    assert launch.argv[-5:] == (
        "12",
        "34",
        str(resolved_tool),
        "tool.exe",
        "--flag",
    )


def test_resolve_windows_launch_executable_uses_parent_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = tmp_path / "tool.exe"
    calls: list[tuple[str, str | None]] = []

    def which(command: str, *, path: str | None = None) -> str | None:
        calls.append((command, path))
        return str(resolved)

    monkeypatch.setattr("ash.sandbox.process_utils.shutil.which", which)

    result = _resolve_windows_launch_executable(
        ["tool", "--flag"],
        search_path="host-path",
    )

    assert result == os.path.abspath(resolved)
    assert calls == [("tool", "host-path")]


def test_resolve_windows_launch_executable_fails_before_trampoline_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ash.sandbox.process_utils.shutil.which",
        lambda command, *, path=None: None,
    )

    with pytest.raises(FileNotFoundError):
        _resolve_windows_launch_executable(["missing-tool"], search_path="host-path")


def test_windows_cwd_trampoline_runs_command_for_matching_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    cwd = workspace / "work"
    workspace.mkdir()
    cwd.mkdir()
    expected = cwd.stat()
    monkeypatch.setattr(
        "ash.sandbox.process_utils._resolve_trusted_python_launcher",
        lambda workspace_root, *, search_path: sys.executable,
    )
    monkeypatch.setattr(
        "ash.sandbox.process_utils._resolve_windows_launch_executable",
        lambda command, *, search_path: sys.executable,
    )
    launch = _prepare_windows_cwd_launch(
        cwd,
        ["shadowed-python", "-c", "print('verified-cwd')"],
        guard=SafetyGuard(workspace),
        search_path=None,
        expected_identity=(expected.st_dev, expected.st_ino),
    )

    completed = subprocess.run(
        launch.argv,
        cwd=launch.cwd,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "verified-cwd"


def test_windows_cwd_trampoline_refuses_mismatched_actual_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    cwd = workspace / "work"
    workspace.mkdir()
    cwd.mkdir()
    expected = cwd.stat()
    marker = cwd / "marker"
    monkeypatch.setattr(
        "ash.sandbox.process_utils._resolve_trusted_python_launcher",
        lambda workspace_root, *, search_path: sys.executable,
    )
    monkeypatch.setattr(
        "ash.sandbox.process_utils._resolve_windows_launch_executable",
        lambda command, *, search_path: sys.executable,
    )
    launch = _prepare_windows_cwd_launch(
        cwd,
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('marker').write_text('executed')",
        ],
        guard=SafetyGuard(workspace),
        search_path=None,
        expected_identity=(expected.st_dev, expected.st_ino + 1),
    )

    completed = subprocess.run(
        launch.argv,
        cwd=launch.cwd,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 126
    assert completed.stderr.strip() == "working directory identity changed"
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited cwd semantics")
def test_windows_scoped_cwd_rejects_swapped_directory_before_user_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    cwd = workspace / "work"
    outside = tmp_path / "outside"
    workspace.mkdir()
    cwd.mkdir()
    outside.mkdir()
    expected = cwd.stat()
    original = workspace / "work-original"
    cwd.rename(original)
    try:
        cwd.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")
    monkeypatch.setattr(
        "ash.sandbox.process_utils._resolve_trusted_python_launcher",
        lambda workspace_root, *, search_path: sys.executable,
    )
    monkeypatch.setattr(
        "ash.sandbox.process_utils._resolve_windows_launch_executable",
        lambda command, *, search_path: sys.executable,
    )
    marker = outside / "marker"

    with prepare_scoped_process_launch(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('marker').write_text('executed')",
        ],
        cwd=cwd,
        guard=SafetyGuard(workspace),
        expected_cwd_identity=(expected.st_dev, expected.st_ino),
    ) as launch:
        completed = subprocess.run(
            launch.argv,
            cwd=launch.cwd,
            capture_output=True,
            text=True,
            check=False,
        )

    assert completed.returncode == 126
    assert completed.stderr.strip() == "working directory identity changed"
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited cwd semantics")
def test_windows_current_directory_stays_bound_and_is_inherited_by_child(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "work"
    moved = tmp_path / "moved"
    ready = tmp_path / "ready"
    proceed = tmp_path / "proceed"
    child_identity = tmp_path / "child-identity"
    cwd.mkdir()
    expected = cwd.stat()
    helper = """
import os
import subprocess
import sys
import time
from pathlib import Path

ready = Path(sys.argv[1])
proceed = Path(sys.argv[2])
child_identity = Path(sys.argv[3])
observed = os.stat('.')
ready.write_text(f'{observed.st_dev}:{observed.st_ino}', encoding='utf-8')
deadline = time.monotonic() + 10
while not proceed.exists():
    if time.monotonic() >= deadline:
        raise SystemExit(124)
    time.sleep(0.01)
child_code = (
    "import os,sys; from pathlib import Path; "
    "st=os.stat('.'); "
    "Path(sys.argv[1]).write_text(f'{st.st_dev}:{st.st_ino}', encoding='utf-8')"
)
raise SystemExit(
    subprocess.run(
        [sys.executable, '-I', '-S', '-c', child_code, str(child_identity)],
        cwd=None,
        check=False,
    ).returncode
)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            helper,
            str(ready),
            str(proceed),
            str(child_identity),
        ],
        cwd=cwd,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if process.poll() is not None:
                pytest.fail(f"cwd helper exited early with {process.returncode}")
            if time.monotonic() >= deadline:
                pytest.fail("cwd helper did not become ready")
            time.sleep(0.01)

        assert ready.read_text(encoding="utf-8") == f"{expected.st_dev}:{expected.st_ino}"
        with pytest.raises(OSError):
            cwd.rename(moved)
        proceed.write_text("go", encoding="utf-8")
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert child_identity.read_text(encoding="utf-8") == (
        f"{expected.st_dev}:{expected.st_ino}"
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor cwd")
def test_non_linux_posix_cwd_launch_skips_workspace_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    host_bin = tmp_path / "host-bin"
    cwd = workspace / "work"
    workspace.mkdir()
    host_bin.mkdir()
    cwd.mkdir()
    workspace_python = workspace / "python3"
    host_python = host_bin / "python3"
    workspace_python.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    host_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    workspace_python.chmod(0o755)
    host_python.chmod(0o755)

    monkeypatch.setattr("ash.sandbox.process_utils.sys.platform", "darwin")
    monkeypatch.setattr("ash.sandbox.process_utils.sys.executable", str(workspace_python))

    with prepare_scoped_process_launch(
        ["/bin/sh", "-c", "true"],
        cwd=cwd,
        guard=SafetyGuard(workspace),
        search_path=os.pathsep.join((str(workspace), str(host_bin))),
    ) as launch:
        assert launch.cwd is None
        assert launch.argv[0] == str(host_python.resolve())
        assert launch.argv[1:4] == ("-I", "-S", "-c")
        assert launch.argv[-3:] == ("/bin/sh", "-c", "true")
        assert len(launch.pass_fds) == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor cwd")
def test_non_linux_posix_cwd_launch_fails_without_trusted_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    cwd = workspace / "work"
    workspace.mkdir()
    cwd.mkdir()
    workspace_python = workspace / "python3"
    workspace_python.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    workspace_python.chmod(0o755)

    monkeypatch.setattr("ash.sandbox.process_utils.sys.platform", "darwin")
    monkeypatch.setattr("ash.sandbox.process_utils.sys.executable", str(workspace_python))

    with pytest.raises(ProcessTreeUnavailable, match="trusted host Python"):
        with prepare_scoped_process_launch(
            ["/bin/sh", "-c", "true"],
            cwd=cwd,
            guard=SafetyGuard(workspace),
            search_path=str(workspace),
        ):
            pytest.fail("untrusted workspace Python must not be used")


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor cwd")
def test_non_linux_posix_cwd_launch_uses_held_inode_after_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    cwd = workspace / "work"
    saved = workspace / "work-saved"
    outside = tmp_path / "outside"
    workspace.mkdir()
    cwd.mkdir()
    outside.mkdir()
    monkeypatch.setattr("ash.sandbox.process_utils.sys.platform", "darwin")

    with prepare_scoped_process_launch(
        ["/bin/sh", "-c", "printf safe > marker.txt; pwd"],
        cwd=cwd,
        guard=SafetyGuard(workspace),
        search_path=os.environ.get("PATH"),
    ) as launch:
        cwd.rename(saved)
        try:
            cwd.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"Symlink creation is unavailable: {exc}")
        completed = subprocess.run(
            launch.argv,
            cwd=launch.cwd,
            env={"PATH": os.defpath},
            pass_fds=launch.pass_fds,
            capture_output=True,
            check=False,
            text=True,
        )

    assert completed.returncode == 0
    assert not (outside / "marker.txt").exists()
    assert (saved / "marker.txt").read_text(encoding="utf-8") == "safe"
    assert Path(completed.stdout.strip()).resolve() == saved.resolve()


@pytest.mark.asyncio
async def test_windows_termination_kills_entire_process_tree() -> None:
    process = Mock(pid=4321, returncode=None)
    _pin_async_process(process, 4321)

    async def finish() -> int:
        process.returncode = 0
        return 0

    process.wait = AsyncMock(side_effect=finish)
    killer = Mock()
    killer.wait = AsyncMock(return_value=0)
    create = AsyncMock(return_value=killer)
    plan = ProcessTreePlan(
        spawn_options={"creationflags": 512},
        taskkill_path="C:/Windows/System32/taskkill.exe",
        workspace_root=Path.cwd(),
        platform="win32",
    )

    with (
        patch("ash.sandbox.process_utils.sys.platform", "win32"),
        patch("ash.sandbox.process_utils.asyncio.create_subprocess_exec", create),
    ):
        await terminate_process_tree(process, plan=plan)

    assert create.await_args.args[:5] == (
        "C:/Windows/System32/taskkill.exe",
        "/PID",
        "4321",
        "/T",
        "/F",
    )
    process.wait.assert_awaited_once()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows process tree")
@pytest.mark.asyncio
async def test_windows_native_termination_reaps_real_descendant(tmp_path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    plan = prepare_process_tree(workspace_root=tmp_path)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        (
            "import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "print(child.pid, flush=True); time.sleep(60)"
        ),
        stdout=asyncio.subprocess.PIPE,
        **plan.spawn_options,
    )
    child_handle = None
    try:
        assert process.stdout is not None
        child_pid = int(
            (await asyncio.wait_for(process.stdout.readline(), timeout=5)).decode()
        )

        win_dll = getattr(ctypes, "WinDLL")
        kernel32 = win_dll("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        wait = kernel32.WaitForSingleObject
        wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait.restype = wintypes.DWORD
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        terminate = kernel32.TerminateProcess
        terminate.argtypes = [wintypes.HANDLE, wintypes.UINT]
        terminate.restype = wintypes.BOOL
        synchronize = 0x00100000
        process_terminate = 0x00000001
        wait_object_0 = 0x00000000
        wait_timeout = 0x00000102
        child_handle = open_process(synchronize | process_terminate, False, child_pid)
        assert child_handle

        await terminate_process_tree(process, plan=plan)
        assert process.returncode is not None
        assert int(wait(child_handle, 5_000)) == wait_object_0
    finally:
        if child_handle:
            if int(wait(child_handle, 0)) == wait_timeout:
                terminate(child_handle, 1)
                wait(child_handle, 5_000)
            close_handle(child_handle)
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_windows_async_cleanup_requires_a_pinned_process_owner() -> None:
    process = Mock(pid=4321, returncode=None)
    plan = ProcessTreePlan({}, "taskkill.exe", Path.cwd(), "win32")
    create = AsyncMock()

    with (
        patch(
            "ash.sandbox.process_utils.asyncio.create_subprocess_exec", create
        ),
        pytest.raises(ProcessTreeUnavailable, match="pinned subprocess process handle"),
    ):
        await terminate_process_tree(process, plan=plan)

    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_windows_async_cleanup_pins_original_owner_through_taskkill_launch() -> None:
    process = Mock(pid=4321, returncode=None)
    owner = _pinned_popen(4321)
    transport = SimpleNamespace(_proc=owner)
    process._transport = transport
    owner_ref = weakref.ref(owner)
    del owner
    observed: list[bool] = []
    killer = Mock()
    killer.wait = AsyncMock(return_value=0)

    async def launch(*args: object, **kwargs: object) -> Mock:
        transport._proc = None
        process.returncode = 17
        observed.append(owner_ref() is not None)
        return killer

    process.wait = AsyncMock(return_value=17)
    plan = ProcessTreePlan({}, "taskkill.exe", Path.cwd(), "win32")

    with patch(
        "ash.sandbox.process_utils.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=launch),
    ):
        await terminate_process_tree(process, plan=plan)

    assert observed == [True]


@pytest.mark.asyncio
async def test_windows_termination_rejects_workspace_shadowed_taskkill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "taskkill"
    marker = tmp_path / "marker"
    fake.write_text(f"#!/bin/sh\nprintf owned > {marker}\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    process = Mock(pid=4321, returncode=None)
    process.kill = Mock()

    with patch("ash.sandbox.process_utils.sys.platform", "win32"):
        with pytest.raises(ProcessTreeUnavailable, match="taskkill was not found"):
            prepare_process_tree(workspace_root=tmp_path)

    process.kill.assert_not_called()
    assert not marker.exists()


def test_windows_tree_preflight_retains_resolved_backend(
    tmp_path: Path,
) -> None:
    with (
        patch("ash.sandbox.process_utils.sys.platform", "win32"),
        patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True),
        patch(
            "ash.sandbox.process_utils._resolve_windows_taskkill",
            return_value="C:/Windows/System32/taskkill.exe",
        ),
    ):
        plan = prepare_process_tree(workspace_root=tmp_path)

    assert plan.platform == "win32"
    assert plan.taskkill_path == "C:/Windows/System32/taskkill.exe"
    assert plan.spawn_options == {"creationflags": 512}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("killer_setup", "expected"),
    [
        ("nonzero", "status 7"),
        ("timeout", "timed out"),
        ("exec_error", "could not execute"),
    ],
)
async def test_windows_async_cleanup_reports_taskkill_failure_and_kills_root(
    tmp_path: Path,
    killer_setup: str,
    expected: str,
) -> None:
    process = Mock(pid=4321, returncode=None)
    _pin_async_process(process, 4321)

    async def finish() -> int:
        process.returncode = -9
        return -9

    process.wait = AsyncMock(side_effect=finish)
    process.kill = Mock()
    if killer_setup == "nonzero":
        killer = Mock()
        killer.wait = AsyncMock(return_value=7)
        create = AsyncMock(return_value=killer)
    elif killer_setup == "timeout":
        killer = Mock()
        killer.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        create = AsyncMock(return_value=killer)
    else:
        create = AsyncMock(side_effect=OSError("missing taskkill"))
    plan = ProcessTreePlan({}, "taskkill.exe", tmp_path, "win32")

    with patch(
        "ash.sandbox.process_utils.asyncio.create_subprocess_exec", create
    ), pytest.raises(ProcessTreeTerminationError, match=expected):
        await terminate_process_tree(process, plan=plan)

    process.kill.assert_called_once_with()


@pytest.mark.asyncio
async def test_windows_async_cleanup_wraps_root_reap_error() -> None:
    process = Mock(pid=4321, returncode=None)
    _pin_async_process(process, 4321)
    process.wait = AsyncMock(side_effect=OSError("reap failed"))
    process.kill = Mock()
    killer = Mock()
    killer.wait = AsyncMock(return_value=0)
    plan = ProcessTreePlan({}, "taskkill.exe", Path.cwd(), "win32")

    with (
        patch(
            "ash.sandbox.process_utils.asyncio.create_subprocess_exec",
            AsyncMock(return_value=killer),
        ),
        pytest.raises(ProcessTreeTerminationError, match="could not be reaped"),
    ):
        await terminate_process_tree(process, plan=plan)

    process.kill.assert_called_once_with()


@pytest.mark.asyncio
async def test_windows_async_cleanup_does_not_target_exited_root() -> None:
    process = Mock(pid=4321, returncode=17)
    _pin_async_process(process, 4321)
    process.kill = Mock()
    create = AsyncMock()
    plan = ProcessTreePlan({}, "taskkill.exe", Path.cwd(), "win32")

    with (
        patch("ash.sandbox.process_utils.asyncio.create_subprocess_exec", create),
        pytest.raises(ProcessTreeTerminationError, match="already exited"),
    ):
        await terminate_process_tree(process, plan=plan)

    create.assert_not_awaited()
    process.kill.assert_not_called()


def test_windows_sync_cleanup_checks_taskkill_status_and_root_state(
    tmp_path: Path,
) -> None:
    plan = ProcessTreePlan({}, "taskkill.exe", tmp_path, "win32")
    process = Mock(pid=4321)
    process.poll.return_value = None
    process.wait.return_value = 0
    process.kill = Mock()

    with (
        patch(
            "ash.sandbox.process_utils.subprocess.run",
            return_value=subprocess.CompletedProcess([], 9),
        ) as run,
        pytest.raises(ProcessTreeTerminationError, match="status 9"),
    ):
        terminate_process_tree_sync(process, plan=plan, timeout_seconds=1)

    run.assert_called_once_with(
        ["taskkill.exe", "/PID", "4321", "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=1,
    )
    process.kill.assert_called_once_with()

    exited = Mock(pid=4321)
    exited.poll.return_value = 17
    with (
        patch("ash.sandbox.process_utils.subprocess.run") as stale_run,
        pytest.raises(ProcessTreeTerminationError, match="already exited"),
    ):
        terminate_process_tree_sync(exited, plan=plan)
    stale_run.assert_not_called()


def test_windows_sync_cleanup_keeps_original_popen_pinned_through_taskkill(
    tmp_path: Path,
) -> None:
    plan = ProcessTreePlan({}, "taskkill.exe", tmp_path, "win32")
    observed: list[bool] = []

    def invoke() -> weakref.ReferenceType[subprocess.Popen[object]]:
        process = _pinned_popen(4321)
        process_poll = Mock(return_value=None)
        process.poll = process_poll
        process.wait = Mock(return_value=0)
        process.kill = Mock()
        process_ref = weakref.ref(process)

        def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
            observed.append(process_ref() is not None)
            process_poll.return_value = 17
            return subprocess.CompletedProcess([], 0)

        with patch(
            "ash.sandbox.process_utils.subprocess.run", side_effect=run
        ):
            terminate_process_tree_sync(process, plan=plan, timeout_seconds=1)
        return process_ref

    process_ref = invoke()

    assert observed == [True]
    assert process_ref() is None


def test_windows_sync_cleanup_wraps_root_reap_error() -> None:
    plan = ProcessTreePlan({}, "taskkill.exe", Path.cwd(), "win32")
    process = Mock(pid=4321)
    process.poll.return_value = None
    process.wait.side_effect = OSError("reap failed")
    process.kill = Mock()

    with (
        patch(
            "ash.sandbox.process_utils.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ),
        pytest.raises(ProcessTreeTerminationError, match="could not be reaped"),
    ):
        terminate_process_tree_sync(process, plan=plan)

    process.kill.assert_called_once_with()


def test_posix_ps_fallback_rejects_workspace_shadowed_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "ps"
    marker = tmp_path / "marker"
    fake.write_text(f"#!/bin/sh\nprintf owned > {marker}\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

    with patch("ash.sandbox.process_utils.Path.is_dir", return_value=False):
        _descendant_pids(os.getpid(), workspace_root=tmp_path)

    assert not marker.exists()


@pytest.mark.asyncio
async def test_posix_termination_targets_process_group() -> None:
    # Above the PID range on supported POSIX and Windows systems.
    sentinel_pid = 2**63 - 1
    process = Mock(pid=sentinel_pid, returncode=None)

    async def finish() -> int:
        process.returncode = 0
        return 0

    process.wait = AsyncMock(side_effect=finish)

    with (
        patch("ash.sandbox.process_utils.sys.platform", "linux"),
        patch(
            "ash.sandbox.process_utils.os.getpgid", return_value=sentinel_pid
        ),
        patch("ash.sandbox.process_utils.os.getpgrp", return_value=999),
        patch("ash.sandbox.process_utils.os.killpg") as killpg,
    ):
        await terminate_process_tree(process)

    killpg.assert_called_once_with(sentinel_pid, __import__("signal").SIGTERM)
    process.wait.assert_awaited_once()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process tree semantics")
@pytest.mark.asyncio
async def test_shared_group_termination_kills_only_target_descendant_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(INHERIT_PROCESS_GROUP_ENV, "1")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        (
            "import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "print(child.pid, flush=True); time.sleep(60)"
        ),
        stdout=asyncio.subprocess.PIPE,
        **process_group_options(),
    )
    assert process.stdout is not None
    child_pid = int((await process.stdout.readline()).decode().strip())

    await terminate_process_tree(process, grace_seconds=0.2)

    assert process.returncode is not None
    for _ in range(100):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("descendant survived target tree termination")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX command syntax")
@pytest.mark.asyncio
async def test_communicate_process_preserves_bounded_output_on_overflow() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('x' * 120000)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    with pytest.raises(ProcessOutputLimitExceeded) as raised:
        await communicate_process(process, max_output_bytes=100_000)

    assert process.returncode == 0
    assert len(raised.value.stdout) == 100_000
    assert raised.value.stderr == b""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX command syntax")
@pytest.mark.asyncio
async def test_communicate_process_stops_a_chatty_child_on_output_overflow() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys,time; sys.stdout.write('x' * 1000000); sys.stdout.flush(); time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_group_options(),
    )

    with pytest.raises(ProcessOutputLimitExceeded):
        await asyncio.wait_for(
            communicate_process(process, max_output_bytes=100_000),
            timeout=5,
        )

    assert process.returncode is not None


@pytest.mark.asyncio
async def test_communicate_process_awaits_siblings_when_cleanup_fails() -> None:
    stdout = asyncio.StreamReader()
    stderr = asyncio.StreamReader()
    stdout.feed_data(b"x" * 4096)
    process = Mock(
        pid=4321,
        returncode=None,
        stdin=None,
        stdout=stdout,
        stderr=stderr,
    )

    with patch(
        "ash.sandbox.process_utils.terminate_process_tree",
        AsyncMock(side_effect=ProcessTreeTerminationError("simulated failure")),
    ), pytest.raises(ProcessOutputLimitExceeded, match="cleanup failed"):
        await communicate_process(process, max_output_bytes=1000)

    current = asyncio.current_task()
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not current
        and not task.done()
        and task.get_coro().__qualname__.startswith("communicate_process.")
    ]
