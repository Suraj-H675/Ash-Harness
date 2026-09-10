from __future__ import annotations

import asyncio
import os
import subprocess
import sys
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
    prepare_process_tree,
    process_group_options,
    terminate_process_tree,
    terminate_process_tree_sync,
)


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
            "ash.sandbox.process_utils.resolve_host_executable",
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
