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
    stream_callback: ProcessStreamCallback | None = None,
) -> tuple[bytes, bytes]:
    """Collect both pipes while optionally forwarding decoded chunks."""

    if stream_callback is None:
        return await process.communicate()

    async def read_stream(
        stream: asyncio.StreamReader | None,
        stream_name: str,
    ) -> bytes:
        if stream is None:
            return b""
        chunks: list[bytes] = []
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            chunks.append(chunk)
            text = chunk.decode("utf-8", errors="replace")
            try:
                stream_callback(stream_name, text)
            except Exception:
                # Rendering and observer failures must never kill user commands.
                pass
        return b"".join(chunks)

    stdout, stderr, _ = await asyncio.gather(
        read_stream(process.stdout, "stdout"),
        read_stream(process.stderr, "stderr"),
        process.wait(),
    )
    return stdout, stderr
