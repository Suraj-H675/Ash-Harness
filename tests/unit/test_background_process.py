import asyncio

import pytest

from safety.guard import SafetyGuard
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
