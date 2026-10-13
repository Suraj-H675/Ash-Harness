"""Cancellation coverage for managed subprocess transport startup."""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from ash.safety.guard import SafetyGuard
from ash.sandbox.process_utils import ProcessTreePlan, spawn_process_tree
from ash.tools.command import RunCommandTool
from ash.tools.process import BackgroundProcessTool


@pytest.mark.skipif(os.name != "posix", reason="managed process groups are POSIX-only")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entrypoint", ["scoped", "pty", "background-pipes", "background-pty"]
)
async def test_cancellation_during_real_pipe_setup_cleans_owned_process_group(
    entrypoint: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation waits for asyncio to return a handle before cleaning the tree."""

    import asyncio.base_subprocess

    original_connect_pipes = (
        asyncio.base_subprocess.BaseSubprocessTransport._connect_pipes
    )
    pipe_setup_started = asyncio.Event()
    release_pipe_setup = asyncio.Event()
    pipe_setup_task: asyncio.Task[Any] | None = None

    async def gated_connect_pipes(transport: Any, waiter: asyncio.Future[None]) -> None:
        nonlocal pipe_setup_task
        pipe_setup_task = asyncio.current_task()
        pipe_setup_started.set()
        await release_pipe_setup.wait()
        await original_connect_pipes(transport, waiter)

    monkeypatch.setattr(
        asyncio.base_subprocess.BaseSubprocessTransport,
        "_connect_pipes",
        gated_connect_pipes,
    )

    marker = tmp_path / "owned-descendant.txt"
    script = f"""\
import os
import time

tag = {str(marker)!r}
child = os.fork()
if child == 0:
    with open(tag, "w", encoding="ascii") as marker_file:
        marker_file.write(f"{{os.getpid()}}:{{os.getpgrp()}}")
while True:
    time.sleep(1)
"""
    command = shlex.join((sys.executable, "-c", script))
    guard = SafetyGuard(tmp_path)
    run_tool = RunCommandTool(guard, project_root=tmp_path)
    background_tool = BackgroundProcessTool(guard)
    process_group: int | None = None
    launch_task: asyncio.Task[Any] | None = None

    async def launch() -> Any:
        if entrypoint == "scoped":
            return await run_tool._run_scoped(
                command,
                30,
                None,
                env={"PATH": os.environ.get("PATH", os.defpath)},
                stream_callback=None,  # type: ignore[arg-type]
                expected_cwd_identity=None,
            )
        if entrypoint == "pty":
            return await run_tool._run_pty(
                ["/bin/sh", "-c", command],
                30,
                None,
                env={"PATH": os.environ.get("PATH", os.defpath)},
                passthrough_env_names=(),
                stream_callback=None,  # type: ignore[arg-type]
                expected_cwd_identity=None,
            )
        return await background_tool.run(
            action="start",
            command=command,
            pty=entrypoint == "background-pty",
        )

    async def wait_for_descendant() -> tuple[int, int]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if marker.exists():
                try:
                    pid, group = marker.read_text(encoding="ascii").split(":", 1)
                except ValueError:
                    await asyncio.sleep(0.01)
                    continue
                return int(pid), int(group)
            await asyncio.sleep(0.01)
        raise AssertionError(
            "subprocess descendant did not publish its process-group tag"
        )

    def process_states(
        *, group: int | None = None, pid: int | None = None
    ) -> list[str]:
        if (group is None) == (pid is None):
            raise ValueError("exactly one of group or pid is required")
        selection = (
            ["-eo", "pgid=,stat="]
            if group is not None
            else ["-o", "stat=", "-p", str(pid)]
        )
        result = subprocess.run(
            ["ps", *selection],
            check=True,
            capture_output=True,
            text=True,
        )
        if pid is not None:
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        states = [
            columns[1]
            for line in result.stdout.splitlines()
            if len(columns := line.split()) >= 2 and int(columns[0]) == group
        ]
        return states

    def group_has_live_members(group: int) -> bool:
        states = process_states(group=group)
        return any(not state.startswith("Z") for state in states)

    def pid_is_live(pid: int) -> bool:
        return any(not state.startswith("Z") for state in process_states(pid=pid))

    async def wait_for_group_exit(group: int) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not await asyncio.to_thread(group_has_live_members, group):
                return
            await asyncio.sleep(0.02)
        raise AssertionError(
            f"owned process group {group} still had live members after cancellation"
        )

    try:
        launch_task = asyncio.create_task(launch())
        await asyncio.wait_for(pipe_setup_started.wait(), timeout=5)
        descendant_pid, process_group = await wait_for_descendant()

        launch_task.cancel()
        await asyncio.sleep(0.05)
        remained_pending_until_handle = not launch_task.done()
        descendant_remained_live = await asyncio.to_thread(pid_is_live, descendant_pid)

        release_pipe_setup.set()
        done, _ = await asyncio.wait({launch_task}, timeout=8)
        assert launch_task in done, (
            "cancellation did not settle after asyncio subprocess startup "
            "was allowed to finish"
        )
        with pytest.raises(asyncio.CancelledError):
            launch_task.result()
        assert remained_pending_until_handle, (
            "caller cancellation escaped while asyncio had started the child "
            "but had not returned its Process handle"
        )
        assert descendant_remained_live, (
            "tagged process-group descendant exited before startup cleanup could run"
        )
        await wait_for_group_exit(process_group)
    finally:
        release_pipe_setup.set()
        if process_group is not None:
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if launch_task is not None:
            if not launch_task.done():
                launch_task.cancel()
            await asyncio.gather(launch_task, return_exceptions=True)
        if pipe_setup_task is not None and not pipe_setup_task.done():
            await asyncio.gather(pipe_setup_task, return_exceptions=True)
        await background_tool.aclose()


@pytest.mark.asyncio
async def test_cancellation_before_private_startup_step_never_spawns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller cancellation can cancel the private task before launch begins."""

    import ash.sandbox.process_utils as process_utils

    original_create_task = asyncio.create_task
    startup_task_created = asyncio.Event()
    release_startup_task = asyncio.Event()
    spawn_calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def fake_create_subprocess_exec(*args: str, **kwargs: Any) -> Any:
        spawn_calls.append((args, kwargs))
        raise AssertionError("cancelled startup must not dispatch a process")

    def defer_startup_task(
        coroutine: Any,
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        async def wait_then_enter_startup() -> Any:
            try:
                await release_startup_task.wait()
            except asyncio.CancelledError:
                coroutine.close()
                raise
            return await coroutine

        startup_task_created.set()
        return original_create_task(wait_then_enter_startup(), name=name)

    monkeypatch.setattr(process_utils.asyncio, "create_task", defer_startup_task)
    monkeypatch.setattr(
        process_utils.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    plan = ProcessTreePlan({}, None, tmp_path, sys.platform)
    caller = original_create_task(
        spawn_process_tree(plan, sys.executable, "-c", "pass")
    )

    try:
        await asyncio.wait_for(startup_task_created.wait(), timeout=1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=1)
        assert spawn_calls == []
    finally:
        release_startup_task.set()
        if not caller.done():
            caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_cancelled", [False, True])
async def test_process_ready_callback_failure_always_settles_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_cancelled: bool,
) -> None:
    import ash.sandbox.process_utils as process_utils

    process = object()
    cleanup_calls: list[tuple[object, ProcessTreePlan, float]] = []
    plan = ProcessTreePlan({}, None, tmp_path, sys.platform)

    async def fake_create_subprocess_exec(*_args: str, **_kwargs: Any) -> Any:
        return process

    async def settle_process_tree(
        target: object,
        *,
        plan: ProcessTreePlan,
        grace_seconds: float,
    ) -> tuple[None, bool]:
        cleanup_calls.append((target, plan, grace_seconds))
        return None, cleanup_cancelled

    def fail_ready(_process: Any) -> None:
        raise RuntimeError("volume marker failed")

    monkeypatch.setattr(
        process_utils.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(
        process_utils,
        "settle_process_tree_after_cancellation",
        settle_process_tree,
    )

    if cleanup_cancelled:
        with pytest.raises(asyncio.CancelledError) as raised:
            await spawn_process_tree(
                plan,
                "docker",
                "volume",
                "create",
                cleanup_grace_seconds=0.1,
                on_process_ready=fail_ready,
            )
        assert isinstance(raised.value.__cause__, RuntimeError)
        assert "Process-ready callback failed" in " ".join(raised.value.__notes__)
    else:
        with pytest.raises(RuntimeError, match="volume marker failed"):
            await spawn_process_tree(
                plan,
                "docker",
                "volume",
                "create",
                cleanup_grace_seconds=0.1,
                on_process_ready=fail_ready,
            )

    assert cleanup_calls == [(process, plan, 0.1)]
