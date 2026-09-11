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

from ash.safe_io import strict_json_loads
from ash.hooks.registry import (
    HookBlock,
    HookCallbackResult,
    HookEvent,
    HookRegistry,
    LifecycleHook,
    MAX_INJECTED_CONTEXT_CHARS,
    PostToolUseHook,
    PreToolUseHook,
    SessionStartHook,
)
from ash.safe_io import read_bounded_bytes
from ash.safety.path_scope import lexical_target_path, path_has_link_component
from ash.sandbox.process_utils import (
    ProcessTreeUnavailable,
    communicate_process,
    prepare_process_tree,
    settle_process_tree_after_cancellation,
)


MAX_HOOK_CONFIG_BYTES = 1024 * 1024
MAX_HOOK_PAYLOAD_BYTES = 1024 * 1024
MAX_HOOK_OUTPUT_BYTES = 1024 * 1024
LIFECYCLE_EVENTS: tuple[HookEvent, ...] = (
    "session_end",
    "turn_start",
    "turn_end",
    "turn_error",
    "pre_model",
    "post_model",
    "context_compacted",
    "config_changed",
    "permission_changed",
    "tool_error",
)
HOOK_CONFIG_EVENTS = frozenset({"pre_tool", "post_tool", "session_start", *LIFECYCLE_EVENTS})


@dataclass(frozen=True)
class HookConfigSource:
    path: Path
    cwd: Path | None = None
    environment: tuple[tuple[str, str], ...] = ()
    trusted_root: Path | None = None


def load_command_hooks(
    paths: list[Path | HookConfigSource], *, timeout_seconds: float = 10.0
) -> HookRegistry:
    registry = HookRegistry(timeout_seconds=timeout_seconds)
    for item in paths:
        source = item if isinstance(item, HookConfigSource) else HookConfigSource(item)
        path = source.path
        if not path.is_file():
            continue
        if source.trusted_root is not None:
            trusted_root = source.trusted_root.expanduser().resolve()
            lexical = lexical_target_path(path, trusted_root)
            try:
                lexical.relative_to(trusted_root)
            except ValueError as exc:
                raise ValueError(
                    f"hook config is outside its trusted root: {path}"
                ) from exc
            link = path_has_link_component(lexical, trusted_root)
            if link is not None:
                raise ValueError(
                    f"hook config path contains a symlink or junction: {link}"
                )
        try:
            raw = read_bounded_bytes(
                path,
                MAX_HOOK_CONFIG_BYTES,
                label="hook config",
            )
        except ValueError as exc:
            if "exceeds" in str(exc):
                raise ValueError(f"Hook config exceeds 1 MiB: {path}") from exc
            raise
        payload = strict_json_loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"Hook config must be an object: {path}")
        unknown_events = set(payload) - HOOK_CONFIG_EVENTS
        if unknown_events:
            raise ValueError(
                f"Hook config contains unknown events in {path}: "
                + ", ".join(sorted(str(event) for event in unknown_events))
            )
        for item in _entries(payload, "pre_tool", path):
            matcher, command = _parse(item, path)

            async def pre(
                name: str,
                arguments: dict[str, Any],
                command=command,
                source=source,
            ) -> None:
                response = await _run(
                    command,
                    {"event": "pre_tool", "tool": name, "arguments": arguments},
                    source=source,
                )
                _enforce_pre_tool_response(response)

            registry.register_pre_tool(PreToolUseHook(matcher, pre, str(path)))
        for item in _entries(payload, "post_tool", path):
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

            registry.register_post_tool(PostToolUseHook(matcher, post, str(path)))
        for item in _entries(payload, "session_start", path):
            _, command = _parse(item, path, matcher_required=False)

            callback = cast(
                Callable[[dict[str, Any]], HookCallbackResult],
                partial(_session_start, command, source),
            )
            registry.register_session_start(SessionStartHook(callback, str(path)))
        for event in LIFECYCLE_EVENTS:
            for item in _entries(payload, event, path):
                matcher, command = _parse(
                    item,
                    path,
                    matcher_required=event == "tool_error",
                )
                lifecycle = cast(
                    Callable[[dict[str, Any]], HookCallbackResult],
                    partial(_lifecycle, command, source, matcher, event),
                )
                registry.register_lifecycle(
                    LifecycleHook(event, lifecycle, source=str(path))
                )
    return registry


def _entries(payload: dict[str, Any], key: str, path: Path) -> list[Any]:
    entries = payload.get(key, [])
    if not isinstance(entries, list):
        raise ValueError(f"Hook event {key!r} must be a list in {path}")
    return entries


def _parse(
    item: Any, path: Path, *, matcher_required: bool = True
) -> tuple[re.Pattern[str], list[str]]:
    if not isinstance(item, dict) or not isinstance(item.get("command"), list):
        raise ValueError(f"Invalid hook entry in {path}")
    if not all(isinstance(part, str) for part in item["command"]):
        raise ValueError(f"Hook command arguments must be strings in {path}")
    command = list(item["command"])
    if not command:
        raise ValueError(f"Hook command cannot be empty in {path}")
    if len(command) > 128 or any(len(part) > 4096 for part in command):
        raise ValueError(f"Hook command is too large in {path}")
    raw_pattern = item.get("matcher", ".*")
    if not isinstance(raw_pattern, str):
        raise ValueError(f"Hook matcher must be a string in {path}")
    pattern = raw_pattern
    if matcher_required and not pattern:
        raise ValueError(f"Hook matcher cannot be empty in {path}")
    return re.compile(pattern), command


def validate_command_hooks_payload(payload: Any, path: Path) -> None:
    """Validate a hook configuration already obtained from immutable bytes."""

    if not isinstance(payload, dict):
        raise ValueError(f"Hook config must be an object: {path}")
    unknown_events = set(payload) - HOOK_CONFIG_EVENTS
    if unknown_events:
        raise ValueError(
            f"Hook config contains unknown events in {path}: "
            + ", ".join(sorted(str(event) for event in unknown_events))
        )
    ordered_events = ("pre_tool", "post_tool", "session_start", *LIFECYCLE_EVENTS)
    for event in ordered_events:
        for item in _entries(payload, event, path):
            _parse(item, path, matcher_required=event == "tool_error")


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
    encoded_payload = json.dumps(payload).encode()
    if len(encoded_payload) > MAX_HOOK_PAYLOAD_BYTES:
        raise ValueError("hook payload exceeds 1 MiB")
    try:
        process_tree_plan = prepare_process_tree(workspace_root=source.cwd)
    except ProcessTreeUnavailable as exc:
        raise RuntimeError(f"hook process was not started: {exc}") from exc
    process = await asyncio.create_subprocess_exec(
        *expanded_command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        cwd=source.cwd,
        **process_tree_plan.spawn_options,
    )
    try:
        stdout, stderr = await communicate_process(
            process,
            input_data=encoded_payload,
            max_output_bytes=MAX_HOOK_OUTPUT_BYTES,
            process_tree_plan=process_tree_plan,
        )
    except asyncio.CancelledError as cancellation:
        cleanup_error, cleanup_cancelled = (
            await settle_process_tree_after_cancellation(
                process, plan=process_tree_plan
            )
        )
        if cleanup_error is not None:
            cancellation.add_note(f"Process-tree cleanup failed: {cleanup_error}")
        if cleanup_cancelled:
            cancellation.add_note("Process-tree cleanup was cancelled")
        raise
    if process.returncode != 0:
        raise RuntimeError(
            stderr.decode(errors="replace").strip()
            or f"hook exited {process.returncode}"
        )
    text = stdout.decode("utf-8", errors="replace").strip()
    return text or None


async def _session_start(
    command: list[str], source: HookConfigSource, payload: dict[str, Any]
) -> str | None:
    response = await _run(command, payload, source=source)
    if not response or not response.startswith("{"):
        return response
    try:
        payload = strict_json_loads(response)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("session_start hook returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(
        payload.get("additional_context", ""), str
    ):
        raise ValueError(
            "session_start hook JSON must contain string additional_context"
        )
    context = cast(str, payload.get("additional_context", ""))
    if len(context) > MAX_INJECTED_CONTEXT_CHARS:
        raise ValueError("session_start additional_context exceeds 65536 characters")
    return context or None


async def _lifecycle(
    command: list[str],
    source: HookConfigSource,
    matcher: re.Pattern[str],
    event: HookEvent,
    payload: dict[str, Any],
) -> None:
    if event == "tool_error" and not matcher.search(str(payload.get("tool", ""))):
        return
    await _run(command, payload, source=source)


def _enforce_pre_tool_response(response: str | None) -> None:
    if not response:
        return
    if not response.startswith("{"):
        return
    try:
        payload = strict_json_loads(response)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("pre_tool hook output must be a JSON object or empty") from exc
    if not isinstance(payload, dict):
        raise ValueError("pre_tool hook output must be a JSON object")
    decision = payload.get("decision", "allow")
    if decision not in {"allow", "deny"}:
        raise ValueError("pre_tool hook decision must be allow or deny")
    if decision == "deny":
        reason = payload.get("reason", "blocked by pre_tool hook")
        if not isinstance(reason, str):
            raise ValueError("pre_tool hook reason must be a string")
        raise HookBlock(reason[:4096])


def _expand(value: str, environment: dict[str, str]) -> str:
    return Template(value).safe_substitute(environment)
