import asyncio
from unittest.mock import Mock

import pytest

from safety.guard import SafetyGuard
from sandbox import SANDBOX_TIER_BWRAP, SandboxBackendUnavailable, SandboxInvocation
from tools.process import BackgroundProcessTool


@pytest.mark.asyncio
async def test_background_process_start_poll_and_close(tmp_path) -> None:
    tool = BackgroundProcessTool(SafetyGuard(tmp_path))
    started = await tool.run(action="start", command="printf hello")
    job_id = started.output.split()[1]
    await asyncio.sleep(0.05)
    polled = await tool.run(action="poll", job_id=job_id)
    assert "hello" in polled.output
    listed = await tool.run(action="list")
    assert job_id in listed.output
    await tool.aclose()


@pytest.mark.asyncio
async def test_background_process_uses_sandbox_manager(tmp_path) -> None:
    manager = Mock()
    manager.tier = SANDBOX_TIER_BWRAP
    manager.prepare.return_value = SandboxInvocation(
        ("/bin/sh", "-c", "printf isolated"),
        tmp_path,
        SANDBOX_TIER_BWRAP,
        "test-sandbox",
    )
    tool = BackgroundProcessTool(SafetyGuard(tmp_path), sandbox_manager=manager)

    started = await tool.run(action="start", command="printf ignored")
    job_id = started.output.split()[1]
    await asyncio.sleep(0.05)
    polled = await tool.run(action="poll", job_id=job_id)

    assert "isolated" in polled.output
    assert "[test-sandbox]" in polled.output
    manager.prepare.assert_called_once()
    await tool.aclose()


@pytest.mark.asyncio
async def test_background_process_fails_closed_when_sandbox_disappears(
    tmp_path,
) -> None:
    manager = Mock()
    manager.tier = SANDBOX_TIER_BWRAP
    manager.prepare.side_effect = SandboxBackendUnavailable("backend stopped")
    tool = BackgroundProcessTool(SafetyGuard(tmp_path), sandbox_manager=manager)

    result = await tool.run(action="start", command="printf unsafe")

    assert result.success is False
    assert "command was not started" in (result.error or "")
    assert not tool.jobs
