"""Cross-platform subprocess group creation and termination helpers."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from typing import Any


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
