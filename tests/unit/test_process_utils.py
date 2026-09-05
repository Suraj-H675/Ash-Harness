from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest

from ash.sandbox.process_utils import (
    INHERIT_PROCESS_GROUP_ENV,
    ProcessOutputLimitExceeded,
    communicate_process,
    process_group_options,
    terminate_process_tree,
)


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
    process.wait = AsyncMock(return_value=0)
    killer = Mock()
    killer.wait = AsyncMock(return_value=0)
    create = AsyncMock(return_value=killer)

    with (
        patch("ash.sandbox.process_utils.sys.platform", "win32"),
        patch("ash.sandbox.process_utils.asyncio.create_subprocess_exec", create),
    ):
        await terminate_process_tree(process)

    assert create.await_args.args[:5] == (
        "taskkill",
        "/PID",
        "4321",
        "/T",
        "/F",
    )
    process.wait.assert_awaited_once()


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
