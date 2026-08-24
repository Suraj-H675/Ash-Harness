"""Concurrent terminal input routing for live turns, steering, and approvals."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ash.core.loop import AshLoop
from ash.safety.grants import (
    ArgumentMatcher,
    MatchOperator,
    PermissionGrantError,
    PermissionRule,
    RuleEffect,
    add_permission_rule,
    build_command_prefix_matcher,
    build_exact_scope_matchers,
)
from ash.safety.policy import PolicyAction
from ash.ui.notifications import NotificationEvent, NotificationSink

if TYPE_CHECKING:
    from ash.ui.prompt import PromptInput
    from ash.ui.terminal import TerminalUI


class InteractiveTurnController:
    """Run one turn while multiplexing one terminal reader safely."""

    def __init__(
        self,
        loop: AshLoop,
        prompt_input: PromptInput,
        ui: TerminalUI,
        *,
        write_status: Callable[[str], None] = print,
        notifier: NotificationSink | None = None,
        notification_include_preview: bool = False,
    ) -> None:
        self.loop = loop
        self.prompt_input = prompt_input
        self.ui = ui
        self.write_status = write_status
        self.notifier = notifier
        self.notification_include_preview = notification_include_preview
        self.diff_mode = getattr(loop, "_config", None) and getattr(
            loop._config, "approval_diff_mode", "unified"
        ) or "unified"
        self._steering_read: asyncio.Task[str] | None = None
        self._approval_active = False
        self._approval_complete = asyncio.Event()
        self._approval_complete.set()

    async def run(
        self,
        user_input: str,
        *,
        user_metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Return the final response, or ``None`` when the user cancels."""

        self.ui.record_user_input(user_input)
        previous_approval = self.loop.on_tool_approval
        previous_plan_approval = self.loop.on_plan_approval
        self.loop.on_tool_approval = self._request_approval
        self.loop.on_plan_approval = self._request_plan_approval
        turn = asyncio.create_task(
            self.loop.run_turn(user_input, user_metadata=user_metadata)
        )
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
                    break

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
            response = await turn
            message = "Ash turn complete."
            if self.notification_include_preview and response.strip():
                message = f"Ash finished: {response}"
            self._notify(NotificationEvent.TURN_COMPLETE, message)
            return response
        finally:
            await self._cancel_steering_read()
            if not turn.done():
                await self._cancel_turn(turn)
            self.loop.on_tool_approval = previous_approval
            self.loop.on_plan_approval = previous_plan_approval

    async def _request_approval(
        self, tool_name: str, arguments: dict[str, object]
    ) -> bool | str:
        decision = self.loop.permission_policy.evaluate(tool_name, dict(arguments))
        if decision.action == PolicyAction.ALLOW:
            return True
        if self.ui.is_tool_approved_for_session(tool_name):
            return True

        self._approval_active = True
        self._approval_complete.clear()
        await self._cancel_steering_read()
        self._notify(
            NotificationEvent.APPROVAL_REQUIRED,
            f"Ash needs approval: {tool_name}",
        )
        self.ui.show_tool_approval(
            tool_name,
            arguments,
            auto=False,
            diff_mode=self.diff_mode,
        )
        try:
            choices = (
                "Approve [y] once, [s] scope/session, [a] tool/session, "
                "[p] scope/project, [x] deny scope/project, [f] deny with feedback"
            )
            if tool_name == "run_command":
                choices += ", [c] command prefix/project"
            answer = (
                (await self.prompt_input.read(f"{choices}, [N] deny? "))
                .strip()
                .casefold()
            )
            if answer in {"y", "yes"}:
                return True
            if answer in {"a", "always", "session", "tool"}:
                self.loop.permission_policy.add_session_rule(
                    PermissionRule.create(RuleEffect.ALLOW, tool_name)
                )
                self.ui.approve_tool_for_session(tool_name)
                self.write_status(f"Allowed {tool_name} for this session.")
                return True
            if answer in {"s", "scope"}:
                rule = self._exact_scope_rule(RuleEffect.ALLOW, tool_name, arguments)
                self.loop.permission_policy.add_session_rule(rule)
                self.write_status(
                    f"Allowed scoped {tool_name} calls for this session ({rule.rule_id})."
                )
                return True
            if answer in {"p", "persist", "project"}:
                rule = self._exact_scope_rule(RuleEffect.ALLOW, tool_name, arguments)
                self._persist_rule(rule)
                return True
            if answer in {"x", "never", "block"}:
                rule = self._exact_scope_rule(RuleEffect.DENY, tool_name, arguments)
                self._persist_rule(rule)
                return False
            if answer in {"f", "feedback"}:
                feedback = (await self.prompt_input.read("Denial feedback> ")).strip()
                if not feedback:
                    return False
                return feedback[:500]
            if answer in {"c", "command"} and tool_name == "run_command":
                command_line = arguments.get("command_line")
                if not isinstance(command_line, str):
                    raise PermissionGrantError(
                        "run_command request has no string command_line"
                    )
                prefix_text = (
                    await self.prompt_input.read(
                        "Approve command prefix (shell words; blank cancels)> "
                    )
                ).strip()
                if not prefix_text:
                    return False
                matchers: list[ArgumentMatcher] = [
                    build_command_prefix_matcher(command_line, prefix_text)
                ]
                if arguments.get("cwd") is not None:
                    matchers.append(
                        ArgumentMatcher(
                            "cwd",
                            MatchOperator.EXACT,
                            arguments["cwd"],
                        )
                    )
                rule = PermissionRule.create(
                    RuleEffect.ALLOW,
                    tool_name,
                    matchers,
                )
                self._persist_rule(rule)
                return True
            return False
        except PermissionGrantError as exc:
            self.write_status(f"Permission scope rejected: {exc}")
            return False
        except (EOFError, KeyboardInterrupt):
            return False
        finally:
            self._approval_active = False
            self._approval_complete.set()

    @staticmethod
    def _exact_scope_rule(
        effect: RuleEffect,
        tool_name: str,
        arguments: dict[str, object],
    ) -> PermissionRule:
        return PermissionRule.create(
            effect,
            tool_name,
            build_exact_scope_matchers(arguments),
        )

    def _persist_rule(self, rule: PermissionRule) -> None:
        rules = add_permission_rule(self.loop.project_root, rule)
        self.loop.permission_policy.set_persistent_rules(rules)
        self.loop.notify_permission_rules_changed(
            source="approval",
            rule_count=len(rules),
        )
        self.write_status(
            f"Saved {rule.effect.value} rule {rule.rule_id} for {rule.tool_name}."
        )

    async def _request_plan_approval(self, execution) -> bool:
        self._approval_active = True
        self._approval_complete.clear()
        await self._cancel_steering_read()
        self._notify(
            NotificationEvent.APPROVAL_REQUIRED,
            "Ash needs plan approval.",
        )
        try:
            while True:
                self.ui.show_plan_review(execution)
                try:
                    answer = (
                        (await self.prompt_input.read("Plan [y/e/N]? "))
                        .strip()
                        .casefold()
                    )
                except (EOFError, KeyboardInterrupt):
                    return False
                if answer in {"y", "yes"}:
                    return True
                if answer not in {"e", "edit"}:
                    return False
                try:
                    self.ui.edit_plan(execution)
                except Exception as exc:  # noqa: BLE001 - editor errors deny safely
                    self.write_status(f"Plan edit failed: {exc}")
                    return False
        finally:
            self._approval_active = False
            self._approval_complete.set()

    def _notify(self, event: NotificationEvent, message: str) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.notify(event, message)
        except Exception:  # noqa: BLE001 - optional notifications cannot break turns
            return

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
