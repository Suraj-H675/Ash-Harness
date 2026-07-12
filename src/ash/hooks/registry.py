"""Hook registry for Ash extensibility."""

from __future__ import annotations

import re
import inspect
import asyncio
import copy
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal

from ash.core.redaction import redact_text

HookResult = str | None
HookCallbackResult = str | Awaitable[str] | None
MAX_INJECTED_CONTEXT_CHARS = 64 * 1024
HookEvent = Literal[
    "session_end",
    "turn_start",
    "turn_end",
    "turn_error",
    "pre_model",
    "post_model",
    "tool_error",
]


class HookBlock(RuntimeError):
    """Raised by a trusted pre-tool hook to deny a tool call."""


class Hook(ABC):
    """Base class for all hooks."""

    @abstractmethod
    async def run(self, **kwargs: Any) -> HookResult:
        raise NotImplementedError


@dataclass(frozen=True)
class HookDiagnostic:
    event: str
    source: str
    error: str
    timestamp: datetime


@dataclass
class LifecycleHook(Hook):
    """Observer for a versioned runtime lifecycle event."""

    event: HookEvent
    callback: Callable[[dict[str, Any]], HookCallbackResult]
    source: str = "python"

    async def run(self, **kwargs: Any) -> HookResult:
        result = self.callback(dict(kwargs["payload"]))
        if inspect.isawaitable(result):
            return await result
        return result


@dataclass
class PreToolUseHook(Hook):
    """Called before a tool is executed. Raise HookBlock to prevent execution."""

    matcher: re.Pattern[str]  # e.g. re.compile(r"Write|Edit")
    callback: Callable[[str, dict[str, Any]], Awaitable[None]]
    source: str = "python"

    async def run(self, **kwargs: Any) -> HookResult:
        tool_name = str(kwargs["tool_name"])
        arguments = copy.deepcopy(kwargs["arguments"])
        if self.matcher.search(tool_name):
            await self.callback(tool_name, arguments)
        return None


@dataclass
class PostToolUseHook(Hook):
    """Called after a tool executes."""

    matcher: re.Pattern[str]
    callback: Callable[[str, dict[str, Any], Any], Awaitable[None]]
    source: str = "python"

    async def run(self, **kwargs: Any) -> HookResult:
        tool_name = str(kwargs["tool_name"])
        arguments = copy.deepcopy(kwargs["arguments"])
        result = copy.deepcopy(kwargs["result"])
        if self.matcher.search(tool_name):
            await self.callback(tool_name, arguments, result)
        return None


@dataclass
class SessionStartHook(Hook):
    """Called when a new session starts. May return a string to inject into the system prompt."""

    callback: Callable[..., HookCallbackResult]
    source: str = "python"

    async def run(self, **kwargs: Any) -> HookResult:
        payload = dict(kwargs.get("payload", {}))
        try:
            accepts_payload = bool(inspect.signature(self.callback).parameters)
        except (TypeError, ValueError):
            accepts_payload = False
        result = self.callback(payload) if accepts_payload else self.callback()
        if inspect.isawaitable(result):
            return await result
        return result


class HookRegistry:
    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("hook timeout must be positive")
        self.timeout_seconds = timeout_seconds
        self._pre_tool: list[PreToolUseHook] = []
        self._post_tool: list[PostToolUseHook] = []
        self._session_start: list[SessionStartHook] = []
        self._lifecycle: dict[HookEvent, list[LifecycleHook]] = {}
        self._injected_prompt: str = ""  # accumulated injected prompt from hooks
        self._diagnostics: list[HookDiagnostic] = []
        self._event_sink: Callable[[dict[str, Any]], None] | None = None

    def register_pre_tool(self, hook: PreToolUseHook) -> None:
        self._pre_tool.append(hook)

    def register_post_tool(self, hook: PostToolUseHook) -> None:
        self._post_tool.append(hook)

    def register_session_start(self, hook: SessionStartHook) -> None:
        self._session_start.append(hook)

    def register_lifecycle(self, hook: LifecycleHook) -> None:
        self._lifecycle.setdefault(hook.event, []).append(hook)

    def set_event_sink(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        self._event_sink = sink

    @property
    def diagnostics(self) -> tuple[HookDiagnostic, ...]:
        return tuple(self._diagnostics)

    def _record_failure(self, event: str, source: str, exc: BaseException) -> None:
        safe_error = redact_text(str(exc))[:4096]
        diagnostic = HookDiagnostic(
            event=event,
            source=source,
            error=safe_error,
            timestamp=datetime.now(timezone.utc),
        )
        self._diagnostics.append(diagnostic)
        if len(self._diagnostics) > 100:
            del self._diagnostics[:-100]
        if self._event_sink is not None:
            try:
                self._event_sink(
                    {
                        "type": "hook.error",
                        "hook_event": event,
                        "source": source,
                        "error": safe_error,
                    }
                )
            except Exception:
                pass

    @staticmethod
    def _external_cancellation_requested() -> bool:
        task = asyncio.current_task()
        return task is not None and task.cancelling() > 0

    async def fire_pre_tool(self, tool_name: str, arguments: dict[str, Any]) -> None:
        for hook in self._pre_tool:
            try:
                await asyncio.wait_for(
                    hook.run(tool_name=tool_name, arguments=arguments),
                    timeout=self.timeout_seconds,
                )
            except asyncio.CancelledError as exc:
                if self._external_cancellation_requested():
                    raise
                raise RuntimeError("pre_tool hook cancelled itself") from exc

    async def fire_post_tool(
        self, tool_name: str, arguments: dict[str, Any], result: Any
    ) -> None:
        for hook in self._post_tool:
            try:
                await asyncio.wait_for(
                    hook.run(tool_name=tool_name, arguments=arguments, result=result),
                    timeout=self.timeout_seconds,
                )
            except asyncio.CancelledError:
                if self._external_cancellation_requested():
                    raise
                self._record_failure(
                    "post_tool", hook.source, RuntimeError("hook cancelled itself")
                )
            except Exception as exc:  # noqa: BLE001 - observers are isolated
                self._record_failure("post_tool", hook.source, exc)

    async def fire_session_start(self, payload: dict[str, Any] | None = None) -> None:
        self._injected_prompt = ""  # reset at start of each session
        wire_payload = {
            **(payload or {}),
            "schema_version": 1,
            "event": "session_start",
        }
        for hook in self._session_start:
            try:
                prompt_addition = (
                    await asyncio.wait_for(
                        hook.run(payload=wire_payload), timeout=self.timeout_seconds
                    )
                    or ""
                )
                if not isinstance(prompt_addition, str):
                    raise TypeError(
                        "session_start hook result must be a string or null"
                    )
                if (
                    len(self._injected_prompt) + len(prompt_addition)
                    > MAX_INJECTED_CONTEXT_CHARS
                ):
                    raise ValueError(
                        "combined session_start context exceeds 65536 characters"
                    )
            except asyncio.CancelledError:
                if self._external_cancellation_requested():
                    raise
                self._record_failure(
                    "session_start",
                    hook.source,
                    RuntimeError("hook cancelled itself"),
                )
                continue
            except Exception as exc:  # noqa: BLE001 - startup observers are isolated
                self._record_failure("session_start", hook.source, exc)
                continue
            if prompt_addition:
                self._injected_prompt += (
                    ("\n" + prompt_addition)
                    if self._injected_prompt
                    else prompt_addition
                )

    def get_injected_prompt(self) -> str:
        """Return the accumulated injected prompt from session-start hooks."""
        return self._injected_prompt

    async def fire_lifecycle(
        self,
        event: HookEvent,
        payload: dict[str, Any],
    ) -> None:
        """Fire non-mutating lifecycle observers without breaking runtime work."""

        wire_payload = {
            **copy.deepcopy(payload),
            "schema_version": 1,
            "event": event,
        }
        for hook in self._lifecycle.get(event, ()):
            try:
                await asyncio.wait_for(
                    hook.run(payload=wire_payload), timeout=self.timeout_seconds
                )
            except asyncio.CancelledError:
                if self._external_cancellation_requested():
                    raise
                self._record_failure(
                    event, hook.source, RuntimeError("hook cancelled itself")
                )
            except Exception as exc:  # noqa: BLE001 - observers are isolated
                self._record_failure(event, hook.source, exc)
