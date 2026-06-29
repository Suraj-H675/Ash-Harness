"""Concurrent terminal input routing for live turns, steering, and approvals."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from core.loop import AshLoop

if TYPE_CHECKING:
    from ui.prompt import PromptInput
    from ui.terminal import TerminalUI


class InteractiveTurnController:
    """Run one turn while multiplexing one terminal reader safely."""

    def __init__(
        self,
        loop: AshLoop,
        prompt_input: PromptInput,
        ui: TerminalUI,
        *,
        write_status: Callable[[str], None] = print,
    ) -> None:
        self.loop = loop
        self.prompt_input = prompt_input
        self.ui = ui
        self.write_status = write_status
        self._steering_read: asyncio.Task[str] | None = None
        self._approval_active = False
        self._approval_complete = asyncio.Event()
        self._approval_complete.set()

    async def run(self, user_input: str) -> str | None:
        """Return the final response, or ``None`` when the user cancels."""

        previous_approval = self.loop.on_tool_approval
        self.loop.on_tool_approval = self._request_approval
        turn = asyncio.create_task(self.loop.run_turn(user_input))
        try:
            while not turn.done():
                steering_read = asyncio.create_task(self.prompt_input.read("steer> "))
                self._steering_read = steering_read
                done, _ = await asyncio.wait(
                    {turn, steering_read},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if turn in done:
                    await self._cancel_steering_read()
                    return await turn

                try:
                    steering = steering_read.result().strip()
                except asyncio.CancelledError:
                    if self._approval_active:
                        await self._approval_complete.wait()
                        continue
                    raise
                except KeyboardInterrupt:
                    await self._cancel_turn(turn)
                    return None

                if not steering:
                    continue
                if steering.casefold() == "/cancel":
                    await self._cancel_turn(turn)
                    return None
                if steering.startswith("/"):
                    self.write_status(
                        "Only /cancel is available while a turn is running."
                    )
                    continue
                try:
                    pending = self.loop.queue_steering(steering)
                except (ValueError, OverflowError) as exc:
                    self.write_status(f"Steering rejected: {exc}")
                    continue
                self.write_status(f"Steering queued ({pending} pending).")
            return await turn
        finally:
            await self._cancel_steering_read()
            if not turn.done():
                await self._cancel_turn(turn)
            self.loop.on_tool_approval = previous_approval

    async def _request_approval(
        self, tool_name: str, arguments: dict[str, object]
    ) -> bool:
        if self.ui.is_tool_approved_for_session(tool_name):
            self.ui.show_tool_approval(tool_name, arguments, auto=True)
            return True

        self._approval_active = True
        self._approval_complete.clear()
        await self._cancel_steering_read()
        self.ui.show_tool_approval(tool_name, arguments, auto=False)
        try:
            answer = (
                (await self.prompt_input.read("Approve [y/a/N]? ")).strip().casefold()
            )
        except (EOFError, KeyboardInterrupt):
            return False
        finally:
            self._approval_active = False
            self._approval_complete.set()
        if answer in {"a", "always", "session"}:
            self.ui.approve_tool_for_session(tool_name)
            return True
        return answer in {"y", "yes"}

    async def _cancel_steering_read(self) -> None:
        task = self._steering_read
        self._steering_read = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _cancel_turn(self, turn: asyncio.Task[str]) -> None:
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        self.write_status("Turn cancelled.")
