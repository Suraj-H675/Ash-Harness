"""Cross-platform subprocess group creation and termination helpers."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ash.safety.environment import resolve_host_executable


ProcessStreamCallback = Callable[[str, str], None]
INHERIT_PROCESS_GROUP_ENV = "ASH_INTERNAL_INHERIT_PROCESS_GROUP"
WINDOWS_TASKKILL_TIMEOUT_SECONDS = 5.0


class ProcessTreeError(RuntimeError):
    """Base error for a managed process-tree lifecycle failure."""


class ProcessTreeUnavailable(ProcessTreeError):
    """A managed process cannot be launched safely on this host."""


class ProcessTreeTerminationError(ProcessTreeError):
    """A managed process tree could not be confirmed terminated."""


@dataclass(frozen=True)
class ProcessTreePlan:
    """Preflight decision shared by one managed process's launch and cleanup."""

    spawn_options: dict[str, Any]
    taskkill_path: str | None
    workspace_root: Path
    platform: str

    @property
    def is_windows(self) -> bool:
        return self.platform == "win32"


def prepare_process_tree(
    *, workspace_root: str | Path | None = None
) -> ProcessTreePlan:
    """Preflight and prepare a subprocess whose descendants Ash must clean up."""

    workspace = Path(workspace_root or Path.cwd()).expanduser().resolve()
    platform_name = sys.platform
    taskkill_path: str | None = None
    if platform_name == "win32":
        taskkill_path = resolve_host_executable(
            "taskkill", workspace_root=workspace, cwd=workspace
        )
        if taskkill_path is None:
            raise ProcessTreeUnavailable(
                "reliable Windows descendant cleanup is unavailable: "
                "taskkill was not found"
            )
    return ProcessTreePlan(
        spawn_options=process_group_options(),
        taskkill_path=taskkill_path,
        workspace_root=workspace,
        platform=platform_name,
    )


class ProcessOutputLimitExceeded(RuntimeError):
    """A managed subprocess exceeded its configured capture budget."""

    def __init__(
        self,
        message: str,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        cleanup_error: ProcessTreeError | None = None,
    ):
        if cleanup_error is not None:
            message += f"; process-tree cleanup failed: {cleanup_error}"
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.cleanup_error = cleanup_error


def process_group_options() -> dict[str, Any]:
    """Options that place a child in an independently terminable group."""

    if os.environ.get(INHERIT_PROCESS_GROUP_ENV) == "1":
        return {}
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 1.0,
    workspace_root: str | Path | None = None,
    plan: ProcessTreePlan | None = None,
) -> None:
    """Terminate a subprocess and descendants, escalating to a hard kill."""

    if not isinstance(process.pid, int) or process.pid <= 0:
        return
    windows = plan.is_windows if plan is not None else sys.platform == "win32"
    if windows:
        if plan is None or not plan.taskkill_path:
            raise ProcessTreeUnavailable(
                "managed Windows process-tree cleanup requires successful preflight"
            )
        await _terminate_windows_process_tree(
            process,
            taskkill_path=plan.taskkill_path,
            grace_seconds=grace_seconds,
        )
        return

    workspace = (
        plan.workspace_root
        if plan is not None
        else Path(workspace_root or Path.cwd()).resolve()
    )
    descendants = _descendant_pids(process.pid, workspace_root=workspace)
    _signal_posix_processes(process.pid, descendants, signal.SIGTERM)
    await asyncio.gather(
        _wait_for_returncode(process, grace_seconds),
        _wait_for_pids(descendants, grace_seconds),
    )
    survivors = [pid for pid in descendants if _pid_exists(pid)]
    if process.returncode is None or survivors:
        _signal_posix_processes(process.pid, survivors, signal.SIGKILL)
        await asyncio.gather(
            _wait_for_returncode(process, grace_seconds),
            _wait_for_pids(survivors, grace_seconds),
        )


async def settle_process_tree_after_cancellation(
    process: asyncio.subprocess.Process,
    *,
    plan: ProcessTreePlan | None = None,
    grace_seconds: float = 1.0,
) -> tuple[ProcessTreeError | None, bool]:
    """Finish process cleanup before a caller propagates cancellation."""

    cleanup = asyncio.create_task(
        terminate_process_tree(
            process,
            grace_seconds=grace_seconds,
            plan=plan,
        )
    )
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    try:
        cleanup.result()
    except ProcessTreeError as exc:
        return exc, cancelled
    except BaseException as exc:
        return (
            ProcessTreeTerminationError(
                "managed process-tree cleanup raised "
                f"{type(exc).__name__}"
            ),
            cancelled,
        )
    return None, cancelled


async def _terminate_windows_process_tree(
    process: asyncio.subprocess.Process,
    *,
    taskkill_path: str,
    grace_seconds: float,
) -> None:
    # CPython's Windows asyncio transport owns the Popen instance that keeps
    # the original process object alive.  Keep that owner in a local for the
    # complete PID-based taskkill operation; an integer PID alone is not an
    # adequate identity pin.
    identity_owner = _windows_async_identity_owner(process)
    if process.returncode is not None:
        raise ProcessTreeTerminationError(
            "managed root already exited; descendant cleanup is unconfirmed"
        )

    failure: str | None = None
    killer: asyncio.subprocess.Process | None = None
    try:
        killer = await asyncio.create_subprocess_exec(
            taskkill_path,
            "/PID",
            str(identity_owner.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            status = await asyncio.wait_for(
                killer.wait(), timeout=WINDOWS_TASKKILL_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            failure = "taskkill timed out"
        except OSError as exc:
            failure = f"taskkill could not execute ({type(exc).__name__})"
        else:
            if status != 0:
                failure = f"taskkill exited with status {status}"
    except OSError as exc:
        failure = f"taskkill could not execute ({type(exc).__name__})"

    if failure is not None:
        if killer is not None:
            await _best_effort_async_kill(killer, grace_seconds)
        await _best_effort_async_root_kill(process, grace_seconds)
        raise ProcessTreeTerminationError(
            f"Windows descendant cleanup could not be confirmed: {failure}"
        )

    try:
        root_exited = await _wait_for_returncode(process, grace_seconds)
    except (OSError, ProcessLookupError) as exc:
        await _best_effort_async_root_kill(process, grace_seconds)
        raise ProcessTreeTerminationError(
            "Windows descendant cleanup could not be confirmed: "
            "managed root could not be reaped"
        ) from exc
    if not root_exited:
        await _best_effort_async_root_kill(process, grace_seconds)
        raise ProcessTreeTerminationError(
            "Windows descendant cleanup could not be confirmed: "
            "managed root did not exit"
        )


def _windows_async_identity_owner(
    process: asyncio.subprocess.Process,
) -> subprocess.Popen[Any]:
    """Return the CPython Popen owner that pins an async Windows process."""

    transport = getattr(process, "_transport", None)
    owner = getattr(transport, "_proc", None)
    if not isinstance(owner, subprocess.Popen) or owner.pid != process.pid:
        raise ProcessTreeUnavailable(
            "managed Windows process-tree cleanup requires a pinned "
            "subprocess process handle"
        )
    return owner


async def _best_effort_async_root_kill(
    process: asyncio.subprocess.Process,
    timeout: float,
) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
    try:
        await _wait_for_returncode(process, timeout)
    except (OSError, ProcessLookupError):
        pass


async def _best_effort_async_kill(
    process: asyncio.subprocess.Process,
    timeout: float,
) -> None:
    """Reap an auxiliary taskkill process after its wait deadline expires."""

    if process.returncode is None:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except (OSError, ProcessLookupError, asyncio.TimeoutError):
        pass


def terminate_process_tree_sync(
    process: subprocess.Popen[Any],
    *,
    plan: ProcessTreePlan | None = None,
    timeout_seconds: float = WINDOWS_TASKKILL_TIMEOUT_SECONDS,
    workspace_root: str | Path | None = None,
) -> None:
    """Synchronously terminate a managed subprocess and its descendants."""

    if not isinstance(process.pid, int) or process.pid <= 0:
        return
    windows = plan.is_windows if plan is not None else sys.platform == "win32"
    if windows:
        if plan is None or not plan.taskkill_path:
            raise ProcessTreeUnavailable(
                "managed Windows process-tree cleanup requires successful preflight"
            )
        _terminate_windows_process_tree_sync(
            process,
            taskkill_path=plan.taskkill_path,
            timeout_seconds=timeout_seconds,
        )
        return

    if process.poll() is not None:
        return
    options = plan.spawn_options if plan is not None else process_group_options()
    if options.get("start_new_session"):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            process.terminate()
    else:
        process.terminate()
    try:
        process.wait(timeout=timeout_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    if options.get("start_new_session"):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
    else:
        process.kill()
    process.wait(timeout=timeout_seconds)


def _terminate_windows_process_tree_sync(
    process: subprocess.Popen[Any],
    *,
    taskkill_path: str,
    timeout_seconds: float,
) -> None:
    # Keep the Popen handle owner strongly referenced until taskkill has
    # completed, the root has been reaped, and cleanup has been classified.
    identity_owner = process
    if identity_owner.poll() is not None:
        raise ProcessTreeTerminationError(
            "managed root already exited; descendant cleanup is unconfirmed"
        )
    failure: str | None = None
    try:
        completed = subprocess.run(
            [taskkill_path, "/PID", str(identity_owner.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        failure = "taskkill timed out"
    except OSError as exc:
        failure = f"taskkill could not execute ({type(exc).__name__})"
    else:
        if completed.returncode != 0:
            failure = f"taskkill exited with status {completed.returncode}"

    if failure is not None:
        _best_effort_sync_root_kill(identity_owner, timeout_seconds)
        raise ProcessTreeTerminationError(
            f"Windows descendant cleanup could not be confirmed: {failure}"
        )
    try:
        identity_owner.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _best_effort_sync_root_kill(identity_owner, timeout_seconds)
        raise ProcessTreeTerminationError(
            "Windows descendant cleanup could not be confirmed: "
            "managed root did not exit"
        )
    except OSError as exc:
        _best_effort_sync_root_kill(identity_owner, timeout_seconds)
        raise ProcessTreeTerminationError(
            "Windows descendant cleanup could not be confirmed: "
            "managed root could not be reaped"
        ) from exc


def _best_effort_sync_root_kill(
    process: subprocess.Popen[Any],
    timeout_seconds: float,
) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=timeout_seconds)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        pass


def _signal_posix_processes(root: int, descendants: list[int], signum: int) -> None:
    getpgrp = getattr(os, "getpgrp", None)
    getpgid = getattr(os, "getpgid", None)
    killpg = getattr(os, "killpg", None)
    if getpgrp is None or getpgid is None or killpg is None:
        return
    own_group = getpgrp()
    groups: set[int] = set()
    individual: list[int] = []
    for pid in [root, *descendants]:
        try:
            group = getpgid(pid)
        except (OSError, TypeError, ValueError):
            continue
        if group == own_group:
            individual.append(pid)
        else:
            groups.add(group)
    for group in groups:
        try:
            killpg(group, signum)
        except ProcessLookupError:
            pass
    for pid in reversed(individual):
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass


def _descendant_pids(
    root: int,
    *,
    workspace_root: str | Path | None = None,
) -> list[int]:
    parents: dict[int, list[int]] = {}
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                status = (entry / "status").read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue
            pid = ppid = None
            for line in status.splitlines():
                if line.startswith("Pid:"):
                    pid = int(line.split()[1])
                elif line.startswith("PPid:"):
                    ppid = int(line.split()[1])
            if pid is not None and ppid is not None:
                parents.setdefault(ppid, []).append(pid)
    else:
        workspace = Path(workspace_root or Path.cwd()).resolve()
        ps = resolve_host_executable("ps", workspace_root=workspace, cwd=workspace)
        if ps is None:
            completed = None
        else:
            try:
                completed = subprocess.run(
                    [ps, "-axo", "pid=,ppid="],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
            except (OSError, subprocess.TimeoutExpired):
                completed = None
        for line in completed.stdout.splitlines() if completed is not None else ():
            fields = line.split()
            if len(fields) == 2 and all(field.isdigit() for field in fields):
                pid, ppid = (int(field) for field in fields)
                parents.setdefault(ppid, []).append(pid)

    descendants: list[int] = []
    pending = list(parents.get(root, ()))
    while pending:
        pid = pending.pop()
        descendants.append(pid)
        pending.extend(parents.get(pid, ()))
    return descendants


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_for_pids(pids: list[int], timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while any(_pid_exists(pid) for pid in pids):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


async def _wait_for_returncode(
    process: asyncio.subprocess.Process, timeout: float
) -> bool:
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        pass
    deadline = asyncio.get_running_loop().time() + timeout
    while process.returncode is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    return process.returncode is not None


async def communicate_process(
    process: asyncio.subprocess.Process,
    *,
    input_data: bytes | None = None,
    stream_callback: ProcessStreamCallback | None = None,
    max_output_bytes: int | None = None,
    process_tree_plan: ProcessTreePlan | None = None,
) -> tuple[bytes, bytes]:
    """Drain pipes without relying on a racy subprocess waiter notification."""

    async def write_stdin() -> None:
        if process.stdin is None:
            return
        try:
            if input_data is not None:
                process.stdin.write(input_data)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    if max_output_bytes is not None and max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    output_limit_exceeded = False
    captured_total = 0
    read_total = 0
    termination_started = False

    async def terminate_after_output_limit() -> None:
        nonlocal termination_started
        if termination_started:
            return
        termination_started = True
        try:
            await terminate_process_tree(process, plan=process_tree_plan)
        except ProcessTreeError as exc:
            raise ProcessOutputLimitExceeded(
                f"subprocess output exceeded {max_output_bytes} bytes",
                cleanup_error=exc,
            ) from exc

    async def read_stream(
        stream: asyncio.StreamReader | None,
        stream_name: str,
    ) -> bytes:
        nonlocal captured_total, output_limit_exceeded, read_total
        if stream is None:
            return b""
        chunks: list[bytes] = []
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            read_total += len(chunk)
            if max_output_bytes is None or captured_total < max_output_bytes:
                remaining = (
                    len(chunk)
                    if max_output_bytes is None
                    else max_output_bytes - captured_total
                )
                chunks.append(chunk[:remaining])
                captured_total += min(len(chunk), remaining)
            if max_output_bytes is not None and read_total > max_output_bytes:
                output_limit_exceeded = True
                await terminate_after_output_limit()
            text = chunk.decode("utf-8", errors="replace")
            if stream_callback is not None:
                try:
                    stream_callback(stream_name, text)
                except Exception:
                    # Rendering and observer failures must never kill user commands.
                    pass
        return b"".join(chunks)

    async def wait_for_returncode() -> None:
        # Threaded child watchers can lose a waiter's wakeup in PID namespaces
        # even after the transport records the exit code. Polling avoids that race.
        while process.returncode is None:
            await asyncio.sleep(0.01)

    tasks = [
        asyncio.create_task(read_stream(process.stdout, "stdout")),
        asyncio.create_task(read_stream(process.stderr, "stderr")),
        asyncio.create_task(write_stdin()),
        asyncio.create_task(wait_for_returncode()),
    ]
    try:
        gathered = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    stdout, stderr, _, _ = gathered
    if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
        raise RuntimeError("subprocess stream reader returned invalid data")
    if output_limit_exceeded:
        raise ProcessOutputLimitExceeded(
            f"subprocess output exceeded {max_output_bytes} bytes",
            stdout=stdout,
            stderr=stderr,
        )
    return stdout, stderr
