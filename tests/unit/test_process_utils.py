from __future__ import annotations

import subprocess
from unittest.mock import AsyncMock, Mock, patch

import pytest

from sandbox.process_utils import process_group_options, terminate_process_tree


def test_process_group_options_use_new_session_on_posix() -> None:
    with patch("sandbox.process_utils.sys.platform", "linux"):
        assert process_group_options() == {"start_new_session": True}


def test_process_group_options_use_new_process_group_on_windows() -> None:
    with (
        patch("sandbox.process_utils.sys.platform", "win32"),
        patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True),
    ):
        assert process_group_options() == {"creationflags": 512}


@pytest.mark.asyncio
async def test_windows_termination_kills_entire_process_tree() -> None:
    process = Mock(pid=4321, returncode=None)
    process.wait = AsyncMock(return_value=0)
    killer = Mock()
    killer.wait = AsyncMock(return_value=0)
    create = AsyncMock(return_value=killer)

    with (
        patch("sandbox.process_utils.sys.platform", "win32"),
        patch("sandbox.process_utils.asyncio.create_subprocess_exec", create),
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
    process = Mock(pid=1234, returncode=None)
    process.wait = AsyncMock(return_value=0)

    with (
        patch("sandbox.process_utils.sys.platform", "linux"),
        patch("sandbox.process_utils.os.killpg") as killpg,
    ):
        await terminate_process_tree(process)

    killpg.assert_called_once_with(1234, __import__("signal").SIGTERM)
    process.wait.assert_awaited_once()
