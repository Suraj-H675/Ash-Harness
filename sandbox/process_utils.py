"""Cross-platform subprocess group creation and termination helpers."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from typing import Any


ProcessStreamCallback = Callable[[str, str], None]


class ProcessOutputLimitExceeded(RuntimeError):
    """A managed subprocess exceeded its configured capture budget."""


def process_group_options() -> dict[str, Any]:
    """Options that place a child in an independently terminable group."""

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
        await process.wait()
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    await process.wait()


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
