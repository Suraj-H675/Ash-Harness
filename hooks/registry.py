"""Hook registry for Ash extensibility."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

HookResult = (
    str | Awaitable[str] | None
)  # session-start hooks may return injected prompt text


class Hook(ABC):
    """Base class for all hooks."""

    @abstractmethod
    async def run(self, **kwargs: Any) -> HookResult:
        raise NotImplementedError


@dataclass
class PreToolUseHook(Hook):
    """Called before a tool is executed. Raise HookBlock to prevent execution."""

    matcher: re.Pattern[str]  # e.g. re.compile(r"Write|Edit")
    callback: Callable[[str, dict[str, Any]], Awaitable[None]]

    async def run(
        self, tool_name: str, arguments: dict[str, Any], **kwargs: Any
    ) -> HookResult:
        if self.matcher.search(tool_name):
            await self.callback(tool_name, arguments)


@dataclass
class PostToolUseHook(Hook):
    """Called after a tool executes."""

    matcher: re.Pattern[str]
    callback: Callable[[str, dict[str, Any], Any], Awaitable[None]]

    async def run(
        self, tool_name: str, arguments: dict[str, Any], result: Any, **kwargs: Any
    ) -> HookResult:
        if self.matcher.search(tool_name):
            await self.callback(tool_name, arguments, result)


@dataclass
class SessionStartHook(Hook):
    """Called when a new session starts. May return a string to inject into the system prompt."""

    callback: Callable[[], HookResult]  # may return str | Awaitable[str] | None

    async def run(self, **kwargs: Any) -> HookResult:
        result = self.callback()
        if hasattr(result, "__await__"):  # handle async callback
            return await result
        return result  # type: ignore[return-value]


class HookRegistry:
    def __init__(self) -> None:
        self._pre_tool: list[PreToolUseHook] = []
        self._post_tool: list[PostToolUseHook] = []
        self._session_start: list[SessionStartHook] = []
        self._injected_prompt: str = ""  # accumulated injected prompt from hooks

    def register_pre_tool(self, hook: PreToolUseHook) -> None:
        self._pre_tool.append(hook)

    def register_post_tool(self, hook: PostToolUseHook) -> None:
        self._post_tool.append(hook)

    def register_session_start(self, hook: SessionStartHook) -> None:
        self._session_start.append(hook)

    async def fire_pre_tool(self, tool_name: str, arguments: dict[str, Any]) -> None:
        for hook in self._pre_tool:
            await hook.run(tool_name=tool_name, arguments=arguments)

    async def fire_post_tool(
        self, tool_name: str, arguments: dict[str, Any], result: Any
    ) -> None:
        for hook in self._post_tool:
            await hook.run(tool_name=tool_name, arguments=arguments, result=result)

    async def fire_session_start(self) -> None:
        self._injected_prompt = ""  # reset at start of each session
        for hook in self._session_start:
            prompt_addition = await hook.run() or ""
            if prompt_addition:
                self._injected_prompt += (
                    ("\n" + prompt_addition)
                    if self._injected_prompt
                    else prompt_addition
                )

    def get_injected_prompt(self) -> str:
        """Return the accumulated injected prompt from session-start hooks."""
        return self._injected_prompt
