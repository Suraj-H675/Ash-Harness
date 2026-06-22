"""Trusted declarative command-hook loading."""

from __future__ import annotations

import asyncio
import json
import os
import re
from functools import partial
from pathlib import Path
from typing import Any, Callable, cast

from hooks.registry import (
    HookCallbackResult,
    HookRegistry,
    PostToolUseHook,
    PreToolUseHook,
    SessionStartHook,
)
from sandbox.process_utils import process_group_options, terminate_process_tree


def load_command_hooks(paths: list[Path], *, timeout_seconds: float = 10.0) -> HookRegistry:
    registry = HookRegistry(timeout_seconds=timeout_seconds)
    for path in paths:
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Hook config must be an object: {path}")
        for item in payload.get("pre_tool", []):
            matcher, command = _parse(item, path)

            async def pre(name: str, arguments: dict[str, Any], command=command) -> None:
                await _run(command, {"event": "pre_tool", "tool": name, "arguments": arguments})

            registry.register_pre_tool(PreToolUseHook(matcher, pre))
        for item in payload.get("post_tool", []):
            matcher, command = _parse(item, path)

            async def post(name: str, arguments: dict[str, Any], result: Any, command=command) -> None:
                serializable = result.model_dump() if hasattr(result, "model_dump") else result
                await _run(command, {"event": "post_tool", "tool": name, "arguments": arguments, "result": serializable})

            registry.register_post_tool(PostToolUseHook(matcher, post))
        for item in payload.get("session_start", []):
            _, command = _parse(item, path, matcher_required=False)

            callback = cast(
                Callable[[], HookCallbackResult],
                partial(_session_start, command),
            )
            registry.register_session_start(SessionStartHook(callback))
    return registry


def _parse(item: Any, path: Path, *, matcher_required: bool = True) -> tuple[re.Pattern[str], list[str]]:
    if not isinstance(item, dict) or not isinstance(item.get("command"), list):
        raise ValueError(f"Invalid hook entry in {path}")
    command = [str(part) for part in item["command"]]
    if not command:
        raise ValueError(f"Hook command cannot be empty in {path}")
    pattern = str(item.get("matcher", ".*"))
    if matcher_required and not pattern:
        raise ValueError(f"Hook matcher cannot be empty in {path}")
    return re.compile(pattern), command


async def _run(command: list[str], payload: dict[str, Any]) -> str | None:
    allowed = {"PATH", "HOME", "USER", "TMPDIR", "TEMP", "SystemRoot", "COMSPEC"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        **process_group_options(),
    )
    try:
        stdout, stderr = await process.communicate(json.dumps(payload).encode())
    except asyncio.CancelledError:
        await terminate_process_tree(process)
        raise
    if process.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace").strip() or f"hook exited {process.returncode}")
    text = stdout.decode("utf-8", errors="replace").strip()
    return text or None


async def _session_start(command: list[str]) -> str | None:
    return await _run(command, {"event": "session_start"})
