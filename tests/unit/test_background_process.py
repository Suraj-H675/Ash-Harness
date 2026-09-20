import asyncio
import os
import shlex
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.sandbox import (
    BubblewrapSandbox,
    SANDBOX_TIER_BWRAP,
    SandboxBackendUnavailable,
    SandboxInvocation,
    SandboxManager,
    has_bwrap,
)
from ash.core.redaction import LONG_TOKEN_WITHHELD_MARKER
from ash.tools.process import (
    BACKGROUND_OUTPUT_TRUNCATION_MARKER,
    MAX_BACKGROUND_COMMAND_CHARS,
    MAX_BACKGROUND_INPUT_CHARS,
    MAX_BACKGROUND_JOBS,
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
async def test_background_process_stream_redacts_secret_split_across_polls(
    tmp_path,
) -> None:
    first_fragment = "xai-" + "a" * 10
    second_fragment = "a" * 70
    script = (
        "import sys,time; "
        f"sys.stdout.write({first_fragment!r}); sys.stdout.flush(); "
        "time.sleep(0.4); "
        f"sys.stdout.write({second_fragment!r} + '\\n'); sys.stdout.flush()"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    tool = BackgroundProcessTool(SafetyGuard(tmp_path))

    started = await tool.run(action="start", command=command)
    job_id = started.output.split()[1]
    await asyncio.sleep(0.12)
    first = await tool.run(action="poll", job_id=job_id)
    await tool.jobs[job_id].process.wait()
    await asyncio.gather(*tool.jobs[job_id].readers)
    second = await tool.run(action="poll", job_id=job_id)

    first_payload = first.output.split("\n", 1)[1] if "\n" in first.output else ""
    second_payload = second.output.split("\n", 1)[1]
    assert first_payload == ""
    assert first_fragment not in first_payload
    assert second_fragment not in second_payload
    assert "[REDACTED]" in second_payload
    await tool.aclose()


@pytest.mark.asyncio
async def test_background_process_redacts_secret_in_status_command(tmp_path) -> None:
    provider_key = "xai-" + "a" * 80
    command = f"printf %s {shlex.quote(provider_key)}"
    tool = BackgroundProcessTool(SafetyGuard(tmp_path))

    started = await tool.run(action="start", command=command)
    job_id = started.output.split()[1]
    await tool.jobs[job_id].process.wait()
    await asyncio.gather(*tool.jobs[job_id].readers)
    listed = await tool.run(action="list")
    polled = await tool.run(action="poll", job_id=job_id)

    assert provider_key not in listed.output
    assert provider_key not in polled.output
    assert "[REDACTED]" in listed.output
    assert "[REDACTED]" in polled.output
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
    job = tool.jobs[job_id]
    # Await process transport completion and pipe draining together: some event
    # loops surface child exit before pipe EOF, while others surface EOF first.
    await asyncio.wait_for(
        asyncio.gather(job.process.wait(), *job.readers), timeout=5.0
    )
    polled = await tool.run(action="poll", job_id=job_id)

    assert job.process.returncode == 0
    assert job.output_size == len(LONG_TOKEN_WITHHELD_MARKER)
    assert job.output_truncated is True
    assert LONG_TOKEN_WITHHELD_MARKER in polled.output
    assert BACKGROUND_OUTPUT_TRUNCATION_MARKER.rstrip() not in polled.output
    assert polled.truncated is True
    await tool.aclose()


@pytest.mark.asyncio
async def test_background_process_limits_running_job_count_and_argument_size(tmp_path) -> None:
    tool = BackgroundProcessTool(SafetyGuard(tmp_path))
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(60)')}"
    for _ in range(MAX_BACKGROUND_JOBS):
        started = await tool.run(action="start", command=command)
        assert started.success is True

    rejected = await tool.run(action="start", command=command)
    assert rejected.success is False
    assert f"{MAX_BACKGROUND_JOBS} running background jobs" in (rejected.error or "")

    with pytest.raises(ValueError):
        await tool.run(
            action="start",
            command="x" * (MAX_BACKGROUND_COMMAND_CHARS + 1),
        )
    with pytest.raises(ValueError):
        await tool.run(
            action="write",
            job_id="missing",
            input="x" * (MAX_BACKGROUND_INPUT_CHARS + 1),
        )
    await tool.aclose()


@pytest.mark.asyncio
async def test_finished_background_jobs_remain_pollable_without_consuming_capacity(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ash.tools.process.MAX_BACKGROUND_JOBS", 2)
    tool = BackgroundProcessTool(SafetyGuard(tmp_path))

    for _ in range(2):
        started = await tool.run(action="start", command="true")
        job = tool.jobs[started.output.split()[1]]
        await job.process.wait()
        await asyncio.gather(*job.readers)

    retained_ids = tuple(tool.jobs)
    assert len(retained_ids) == 2
    polled = await tool.run(action="poll", job_id=retained_ids[-1])
    assert "exited(0)" in polled.output

    replacement = await tool.run(action="start", command="true")
    assert replacement.success is True
    assert retained_ids[-1] in tool.jobs
    await tool.aclose()


@pytest.mark.asyncio
async def test_background_process_prunes_oldest_terminal_history(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ash.tools.process.MAX_BACKGROUND_HISTORY_JOBS", 2)
    tool = BackgroundProcessTool(SafetyGuard(tmp_path))
    job_ids: list[str] = []

    for _ in range(3):
        started = await tool.run(action="start", command="true")
        job_id = started.output.split()[1]
        job_ids.append(job_id)
        job = tool.jobs[job_id]
        await job.process.wait()
        await asyncio.gather(*job.readers)

    assert job_ids[0] not in tool.jobs
    assert tuple(tool.jobs) == tuple(job_ids[1:])
    latest = await tool.run(action="poll", job_id=job_ids[-1])
    assert "exited(0)" in latest.output
    await tool.aclose()


@pytest.mark.asyncio
async def test_stopped_background_job_does_not_consume_running_capacity(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ash.tools.process.MAX_BACKGROUND_JOBS", 1)
    tool = BackgroundProcessTool(SafetyGuard(tmp_path))
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(60)')}"

    started = await tool.run(action="start", command=command)
    job_id = started.output.split()[1]
    stopped = await tool.run(action="stop", job_id=job_id)
    assert stopped.success is True
    assert job_id in tool.jobs

    replacement = await tool.run(action="start", command=command)
    assert replacement.success is True
    await tool.aclose()


@pytest.mark.asyncio
async def test_background_process_uses_sandbox_manager(tmp_path) -> None:
    manager = Mock()
    manager.tier = SANDBOX_TIER_BWRAP
    manager.prepare_launch.return_value = nullcontext(
        SandboxInvocation(
            ("/bin/sh", "-c", "printf isolated"),
            tmp_path,
            SANDBOX_TIER_BWRAP,
            "test-sandbox",
        )
    )
    tool = BackgroundProcessTool(SafetyGuard(tmp_path), sandbox_manager=manager)

    started = await tool.run(action="start", command="printf ignored")
    job_id = started.output.split()[1]
    await asyncio.sleep(0.05)
    polled = await tool.run(action="poll", job_id=job_id)

    assert "isolated" in polled.output
    assert "[test-sandbox]" in polled.output
    manager.prepare_launch.assert_called_once_with(
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
    manager.prepare_launch.side_effect = SandboxBackendUnavailable("backend stopped")
    tool = BackgroundProcessTool(SafetyGuard(tmp_path), sandbox_manager=manager)

    result = await tool.run(action="start", command="printf unsafe")

    assert result.success is False
    assert "command was not started" in (result.error or "")
    assert not tool.jobs


@pytest.mark.asyncio
async def test_background_process_pins_bwrap_workspace_across_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not has_bwrap():
        pytest.skip("bwrap not installed or usable on this host")

    workspace = tmp_path / "workspace"
    replacement = tmp_path / "replacement"
    saved = tmp_path / "workspace-saved"
    workspace.mkdir()
    replacement.mkdir()
    (workspace / "marker.txt").write_text("ORIGINAL", encoding="utf-8")
    (replacement / "marker.txt").write_text("REPLACEMENT", encoding="utf-8")
    manager = SandboxManager(
        workspace_root=workspace,
        preferred_tier=SANDBOX_TIER_BWRAP,
        backend_preference="native",
    )
    assert manager.backend_name == "bubblewrap"
    tool = BackgroundProcessTool(SafetyGuard(workspace), sandbox_manager=manager)
    original_wrap = BubblewrapSandbox.wrap
    swapped = False

    def swap_after_wrap(
        backend: BubblewrapSandbox,
        command: list[str] | tuple[str, ...],
        **kwargs: object,
    ) -> list[str]:
        nonlocal swapped
        argv = original_wrap(backend, command, **kwargs)
        if not swapped:
            workspace.rename(saved)
            workspace.symlink_to(replacement, target_is_directory=True)
            swapped = True
        return argv

    monkeypatch.setattr(BubblewrapSandbox, "wrap", swap_after_wrap)

    started = await tool.run(action="start", command="cat marker.txt")
    assert started.success is True, started.error
    job_id = started.output.split()[1]
    await asyncio.sleep(0.1)
    polled = await tool.run(action="poll", job_id=job_id)

    assert "ORIGINAL" in polled.output
    assert "REPLACEMENT" not in polled.output
    await tool.aclose()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_background_process_refuses_workspace_replaced_after_tool_creation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-saved"
    replacement = tmp_path / "replacement"
    workspace.mkdir()
    replacement.mkdir()
    tool = BackgroundProcessTool(SafetyGuard(workspace))
    workspace.rename(saved)
    replacement.rename(workspace)

    result = await tool.run(
        action="start",
        command="printf unsafe > marker.txt",
    )

    assert result.success is False
    assert "working directory identity changed" in (result.error or "")
    assert not (saved / "marker.txt").exists()
    assert not (workspace / "marker.txt").exists()
    assert not tool.jobs
    await tool.aclose()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_background_process_cwd_swap_cannot_escape_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = workspace / "work"
    cwd.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    saved = workspace / "work-saved"
    guard = SafetyGuard(workspace)
    real_validate_path = guard.validate_path
    swapped = False

    def validate_then_swap(path):
        nonlocal swapped
        resolved = real_validate_path(path)
        if path == "work" and not swapped:
            swapped = True
            cwd.rename(saved)
            try:
                cwd.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return resolved

    monkeypatch.setattr(guard, "validate_path", validate_then_swap)
    tool = BackgroundProcessTool(guard)

    with pytest.raises(SafetyViolation, match="outside project scope"):
        await tool.run(
            action="start",
            command="printf safe > marker.txt",
            cwd="work",
        )

    assert swapped is True
    assert not (outside / "marker.txt").exists()
    assert not (saved / "marker.txt").exists()
    assert not tool.jobs


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_background_process_uses_held_cwd_after_path_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox import process_utils as process_utils_module

    real_prepare = process_utils_module._prepare_posix_cwd_launch

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = workspace / "work"
    cwd.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    saved = workspace / "work-saved"
    swapped = False

    def prepare_after_swap(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            cwd.rename(saved)
            try:
                cwd.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr(
        process_utils_module,
        "_prepare_posix_cwd_launch",
        prepare_after_swap,
    )
    tool = BackgroundProcessTool(SafetyGuard(workspace))

    started = await tool.run(
        action="start",
        command="printf safe > marker.txt",
        cwd="work",
    )
    job_id = started.output.split()[1]
    job = tool.jobs[job_id]
    await job.process.wait()
    await asyncio.gather(*job.readers)

    assert swapped is True
    assert not (outside / "marker.txt").exists()
    assert (saved / "marker.txt").read_text(encoding="utf-8") == "safe"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor cwd")
@pytest.mark.asyncio
async def test_background_process_fails_closed_without_descriptor_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox import process_utils as process_utils_module
    from ash.sandbox.process_utils import ProcessTreeUnavailable

    def unavailable(*args, **kwargs):
        raise ProcessTreeUnavailable("race-resistant cwd launch unavailable")

    monkeypatch.setattr(
        process_utils_module,
        "_prepare_posix_cwd_launch",
        unavailable,
    )
    monkeypatch.setattr(
        "ash.tools.process.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=AssertionError("command must not launch")),
    )
    tool = BackgroundProcessTool(SafetyGuard(tmp_path))

    result = await tool.run(action="start", command="printf unsafe")

    assert result.success is False
    assert "race-resistant cwd launch unavailable" in (result.error or "")
    assert not tool.jobs


@pytest.mark.asyncio
async def test_windows_background_resolves_powershell_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    host_bin = tmp_path / "host-bin"
    workspace.mkdir()
    host_bin.mkdir()
    workspace_powershell = workspace / "powershell.exe"
    host_powershell = host_bin / "powershell.exe"
    workspace_powershell.write_text("shadowed", encoding="utf-8")
    host_powershell.write_text("trusted", encoding="utf-8")
    workspace_powershell.chmod(0o755)
    host_powershell.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        f"{workspace}{os.pathsep}{host_bin}",
    )

    stdout = asyncio.StreamReader()
    stderr = asyncio.StreamReader()
    stdout.feed_eof()
    stderr.feed_eof()
    process = Mock(pid=1234, returncode=0, stdin=None, stdout=stdout, stderr=stderr)
    process.wait = AsyncMock(return_value=0)
    with (
        patch("ash.tools.process.platform.system", return_value="Windows"),
        patch(
            "ash.tools.process.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ) as spawn,
        patch(
            "ash.tools.process.terminate_process_tree",
            new=AsyncMock(return_value=None),
        ),
    ):
        tool = BackgroundProcessTool(SafetyGuard(workspace))
        result = await tool.run(action="start", command="Write-Output intended")
        await asyncio.gather(*tool.jobs[next(iter(tool.jobs))].readers)

    assert result.success is True
    assert spawn.await_args.args[0] == str(host_powershell.resolve())
    assert spawn.await_args.args[0] != str(workspace_powershell)


@pytest.mark.asyncio
async def test_windows_background_fails_before_spawn_without_host_powershell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake = workspace / "powershell.exe"
    fake.write_text("shadowed", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(workspace))

    with (
        patch("ash.tools.process.platform.system", return_value="Windows"),
        patch(
            "ash.tools.process.asyncio.create_subprocess_exec",
            new=AsyncMock(),
        ) as spawn,
    ):
        tool = BackgroundProcessTool(SafetyGuard(workspace))
        result = await tool.run(action="start", command="Write-Output unsafe")

    assert result.success is False
    assert "was not started" in (result.error or "")
    spawn.assert_not_awaited()
