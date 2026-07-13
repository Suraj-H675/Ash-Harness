"""Cross-platform subprocess group creation and termination helpers."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


ProcessStreamCallback = Callable[[str, str], None]
INHERIT_PROCESS_GROUP_ENV = "ASH_INTERNAL_INHERIT_PROCESS_GROUP"


class ProcessOutputLimitExceeded(RuntimeError):
    """A managed subprocess exceeded its configured capture budget."""


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
) -> None:
    """Terminate a subprocess and descendants, escalating to a hard kill."""

    if process.returncode is not None:
        return
    if sys.platform == "win32":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
        await _wait_for_returncode(process, grace_seconds)
        return

    descendants = _descendant_pids(process.pid)
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


def _signal_posix_processes(root: int, descendants: list[int], signum: int) -> None:
    own_group = os.getpgrp()
    groups: set[int] = set()
    individual: list[int] = []
    for pid in [root, *descendants]:
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            continue
        if group == own_group:
            individual.append(pid)
        else:
            groups.add(group)
    for group in groups:
        try:
            os.killpg(group, signum)
        except ProcessLookupError:
            pass
    for pid in reversed(individual):
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass


def _descendant_pids(root: int) -> list[int]:
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
        try:
            completed = subprocess.run(
                ["ps", "-axo", "pid=,ppid="],
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

    stdout, stderr, _, _ = await asyncio.gather(
        read_stream(process.stdout, "stdout"),
        read_stream(process.stderr, "stderr"),
        write_stdin(),
        wait_for_returncode(),
    )
    if output_limit_exceeded:
        raise ProcessOutputLimitExceeded(
            f"subprocess output exceeded {max_output_bytes} bytes"
        )
    return stdout, stderr
