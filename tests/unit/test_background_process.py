import asyncio
import shlex
import sys
from unittest.mock import Mock

import pytest

from ash.safety.guard import SafetyGuard
from ash.sandbox import SANDBOX_TIER_BWRAP, SandboxBackendUnavailable, SandboxInvocation
from ash.tools.process import (
    BACKGROUND_OUTPUT_TRUNCATION_MARKER,
    MAX_BACKGROUND_OUTPUT_CHARS,
    BackgroundProcessTool,
)


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
async def test_background_process_forwards_allowlisted_environment(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEV_SERVER_PORT", "4312")
    monkeypatch.setenv("PRIVATE_TOKEN", "must-not-leak")
    script = (
        "import os; "
        "print(os.getenv('DEV_SERVER_PORT', 'missing')); "
        "print(os.getenv('PRIVATE_TOKEN', 'missing'))"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    tool = BackgroundProcessTool(
        SafetyGuard(tmp_path), environment_allowlist=["DEV_SERVER_PORT"]
    )

    started = await tool.run(action="start", command=command)
    job_id = started.output.split()[1]
    output = ""
    for _ in range(20):
        await asyncio.sleep(0.02)
        polled = await tool.run(action="poll", job_id=job_id)
        output += polled.output
        if "4312\nmissing" in output:
            break

    assert "4312\nmissing" in output
    await tool.aclose()


@pytest.mark.asyncio
async def test_background_process_handles_long_lines_and_bounds_output(tmp_path) -> None:
    script = (
        "import sys; "
        f"sys.stdout.write('x' * {MAX_BACKGROUND_OUTPUT_CHARS + 1024})"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    tool = BackgroundProcessTool(SafetyGuard(tmp_path))

    started = await tool.run(action="start", command=command)
    job_id = started.output.split()[1]
    await asyncio.sleep(0.1)

    job = tool.jobs[job_id]
    polled = await tool.run(action="poll", job_id=job_id)

    assert job.process.returncode == 0
    assert job.output_size == MAX_BACKGROUND_OUTPUT_CHARS
    assert job.output_truncated is True
    assert BACKGROUND_OUTPUT_TRUNCATION_MARKER.rstrip() in polled.output
    assert polled.truncated is True
    assert len("".join(job.output)) == (
        MAX_BACKGROUND_OUTPUT_CHARS + len(BACKGROUND_OUTPUT_TRUNCATION_MARKER)
    )
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
    manager.prepare.assert_called_once_with(
        ["/bin/sh", "-c", "printf ignored"],
        cwd=tmp_path,
        passthrough_env_names=(),
    )
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
