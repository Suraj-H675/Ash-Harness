"""Trusted declarative command-hook loading."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from string import Template
from typing import Any, Callable, cast

from hooks.registry import (
    HookCallbackResult,
    HookRegistry,
    PostToolUseHook,
    PreToolUseHook,
    SessionStartHook,
)
from sandbox.process_utils import (
    communicate_process,
    process_group_options,
    terminate_process_tree,
)


@dataclass(frozen=True)
class HookConfigSource:
    path: Path
    cwd: Path | None = None
    environment: tuple[tuple[str, str], ...] = ()


def load_command_hooks(
    paths: list[Path | HookConfigSource], *, timeout_seconds: float = 10.0
) -> HookRegistry:
    registry = HookRegistry(timeout_seconds=timeout_seconds)
    for item in paths:
        source = item if isinstance(item, HookConfigSource) else HookConfigSource(item)
        path = source.path
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Hook config must be an object: {path}")
        for item in payload.get("pre_tool", []):
            matcher, command = _parse(item, path)

            async def pre(
                name: str,
                arguments: dict[str, Any],
                command=command,
                source=source,
            ) -> None:
                await _run(
                    command,
                    {"event": "pre_tool", "tool": name, "arguments": arguments},
                    source=source,
                )

            registry.register_pre_tool(PreToolUseHook(matcher, pre))
        for item in payload.get("post_tool", []):
            matcher, command = _parse(item, path)

            async def post(
                name: str,
                arguments: dict[str, Any],
                result: Any,
                command=command,
                source=source,
            ) -> None:
                serializable = (
                    result.model_dump() if hasattr(result, "model_dump") else result
                )
                await _run(
                    command,
                    {
                        "event": "post_tool",
                        "tool": name,
                        "arguments": arguments,
                        "result": serializable,
                    },
                    source=source,
                )

            registry.register_post_tool(PostToolUseHook(matcher, post))
        for item in payload.get("session_start", []):
            _, command = _parse(item, path, matcher_required=False)

            callback = cast(
                Callable[[], HookCallbackResult],
                partial(_session_start, command, source),
            )
            registry.register_session_start(SessionStartHook(callback))
    return registry


def _parse(
    item: Any, path: Path, *, matcher_required: bool = True
) -> tuple[re.Pattern[str], list[str]]:
    if not isinstance(item, dict) or not isinstance(item.get("command"), list):
        raise ValueError(f"Invalid hook entry in {path}")
    command = [str(part) for part in item["command"]]
    if not command:
        raise ValueError(f"Hook command cannot be empty in {path}")
    pattern = str(item.get("matcher", ".*"))
    if matcher_required and not pattern:
        raise ValueError(f"Hook matcher cannot be empty in {path}")
    return re.compile(pattern), command


async def _run(
    command: list[str],
    payload: dict[str, Any],
    *,
    source: HookConfigSource,
) -> str | None:
    allowed = {"PATH", "HOME", "USER", "TMPDIR", "TEMP", "SystemRoot", "COMSPEC"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update(source.environment)
    expanded_command = [_expand(part, environment) for part in command]
    process = await asyncio.create_subprocess_exec(
        *expanded_command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        cwd=source.cwd,
        **process_group_options(),
    )
    try:
        stdout, stderr = await communicate_process(
            process, input_data=json.dumps(payload).encode()
        )
    except asyncio.CancelledError:
        await terminate_process_tree(process)
        raise
    if process.returncode != 0:
        raise RuntimeError(
            stderr.decode(errors="replace").strip()
            or f"hook exited {process.returncode}"
        )
    text = stdout.decode("utf-8", errors="replace").strip()
    return text or None


async def _session_start(command: list[str], source: HookConfigSource) -> str | None:
    return await _run(command, {"event": "session_start"}, source=source)


def _expand(value: str, environment: dict[str, str]) -> str:
    return Template(value).safe_substitute(environment)
