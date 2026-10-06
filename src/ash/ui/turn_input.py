"""Concurrent terminal input routing for live turns, steering, and approvals."""

from __future__ import annotations

import asyncio
import json
import signal
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
from ash.tools.base import sensitive_tool_argument_fields
from ash.ui.input_signals import PromptInterrupted
from ash.ui.notifications import NotificationEvent, NotificationSink
from ash.ui.prompt import PromptChoice

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
        spawn_agent = self.loop.tools.get("spawn_agent")
        set_foreground_broker = getattr(
            spawn_agent, "set_foreground_approval_broker", None
        )
        previous_foreground_broker = getattr(
            spawn_agent, "foreground_approval_broker", None
        )
        self.loop.on_tool_approval = self._request_approval
        self.loop.on_plan_approval = self._request_plan_approval
        if callable(set_foreground_broker):
            set_foreground_broker(self._request_subagent_approval)
        previous_sigint: Any = None
        signal_handler_installed = False
        interrupt_wait: asyncio.Task[bool] | None = None
        if getattr(self.prompt_input, "linear_mode", False):
            current_loop = asyncio.get_running_loop()
            previous_sigint = signal.getsignal(signal.SIGINT)
            interrupt_event = asyncio.Event()
            try:
                def on_sigint(_signum: int, _frame: Any) -> None:
                    current_loop.call_soon_threadsafe(interrupt_event.set)

                signal.signal(signal.SIGINT, on_sigint)
                signal_handler_installed = True
                interrupt_wait = asyncio.create_task(interrupt_event.wait())
            except (OSError, RuntimeError, ValueError):
                pass
        turn = asyncio.create_task(
            self.loop.run_turn(user_input, user_metadata=user_metadata)
        )
        try:
            while not turn.done():
                steering_read = asyncio.create_task(self.prompt_input.read("steer> "))
                self._steering_read = steering_read
                wait_tasks: set[asyncio.Task[Any]] = {turn, steering_read}
                if interrupt_wait is not None:
                    wait_tasks.add(interrupt_wait)
                done, _ = await asyncio.wait(
                    wait_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if interrupt_wait is not None and interrupt_wait in done:
                    await self._cancel_steering_read()
                    await self._cancel_turn(turn)
                    return None
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
                except (KeyboardInterrupt, PromptInterrupted):
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
                    pending_count = self.loop.queue_steering(steering)
                except (ValueError, OverflowError) as exc:
                    self.write_status(f"Steering rejected: {exc}")
                    continue
                self.write_status(f"Steering queued ({pending_count} pending).")
            response = await turn
            self.ui.commit_completed_turn()
            message = "Ash turn complete."
            if self.notification_include_preview and response.strip():
                message = f"Ash finished: {response}"
            self._notify(NotificationEvent.TURN_COMPLETE, message)
            return response
        finally:
            await self._cancel_steering_read()
            if not turn.done():
                await self._cancel_turn(turn)
            if interrupt_wait is not None and not interrupt_wait.done():
                interrupt_wait.cancel()
                await asyncio.gather(interrupt_wait, return_exceptions=True)
            if signal_handler_installed:
                signal.signal(signal.SIGINT, previous_sigint)
            self.loop.on_tool_approval = previous_approval
            self.loop.on_plan_approval = previous_plan_approval
            if callable(set_foreground_broker):
                set_foreground_broker(previous_foreground_broker)

    async def review_mcp_sampling(
        self, server: str, stage: str, payload: dict[str, Any]
    ) -> bool:
        """Review one MCP sampling request/response using the single prompt reader."""

        self._approval_active = True
        self._approval_complete.clear()
        await self._cancel_steering_read()
        self._notify(
            NotificationEvent.APPROVAL_REQUIRED,
            f"MCP server {server} requests sampling review",
        )
        try:
            label = "request" if stage == "request" else "response"
            rendered = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            self.write_status(
                f"MCP sampling {label} from {server}:\n{rendered}"
            )
            answer = (
                await self.prompt_input.read(
                    f"Approve MCP sampling {label}? [y/N] "
                )
            ).strip().casefold()
            return answer in {"y", "yes"}
        except (EOFError, KeyboardInterrupt, PromptInterrupted, TypeError, ValueError):
            return False
        finally:
            self._approval_active = False
            self._approval_complete.set()

    async def request_mcp_elicitation(
        self, server: str, message: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Collect and review one MCP form using the single prompt reader."""

        self._approval_active = True
        self._approval_complete.clear()
        await self._cancel_steering_read()
        self._notify(
            NotificationEvent.APPROVAL_REQUIRED,
            f"MCP server {server} requests information",
        )
        try:
            self.write_status(f"MCP form from {server}: {message}")
            properties = schema.get("properties", {})
            required = set(schema.get("required", []))
            previous: dict[str, Any] = {}
            while True:
                values: dict[str, Any] = {}
                for name, field_schema in properties.items():
                    if not isinstance(field_schema, dict):
                        return {"action": "cancel"}
                    label = str(field_schema.get("title") or name)
                    allowed = field_schema.get("enum")
                    if allowed is None and field_schema.get("type") == "array":
                        allowed = field_schema.get("items", {}).get("enum")
                    if isinstance(allowed, list) and allowed:
                        self.write_status(
                            f"{label} options: " + ", ".join(map(str, allowed))
                        )
                    while True:
                        default = previous.get(name, field_schema.get("default"))
                        suffix = f" [{default}]" if default is not None else ""
                        raw = await self.prompt_input.read(f"{label}{suffix}: ")
                        if not raw:
                            if default is not None:
                                values[name] = default
                                break
                            if name not in required:
                                break
                            self.write_status(f"{label} requires a value.")
                            continue
                        try:
                            values[name] = self.ui._parse_mcp_form_value(
                                raw, field_schema
                            )
                            break
                        except (TypeError, ValueError) as exc:
                            self.write_status(f"Invalid {label}: {exc}")
                previous = values
                summary = "\n".join(
                    f"  {key} = {value!r}" for key, value in values.items()
                )
                self.write_status(
                    "Review MCP form response:\n" + (summary or "  (no values)")
                )
                action = (
                    await self.prompt_input.read(
                        "Submit MCP form [y], edit [e], decline [n], cancel [c]? "
                    )
                ).strip().casefold()
                if action in {"y", "yes"}:
                    return {"action": "accept", "content": values}
                if action in {"e", "edit"}:
                    continue
                if action in {"n", "no", "decline"}:
                    return {"action": "decline"}
                return {"action": "cancel"}
        except (EOFError, KeyboardInterrupt, PromptInterrupted):
            return {"action": "cancel"}
        finally:
            self._approval_active = False
            self._approval_complete.set()

    async def _request_approval(
        self, tool_name: str, arguments: dict[str, object]
    ) -> bool | str:
        decision = self.loop.permission_policy.evaluate(tool_name, dict(arguments))
        if decision.action == PolicyAction.ALLOW:
            return True
        if self.ui.is_tool_approved_for_session(tool_name):
            return True
        return await self._prompt_tool_approval(tool_name, arguments)

    async def _request_subagent_approval(
        self, agent_id: str, tool_name: str, arguments: dict[str, object]
    ) -> bool | str:
        return await self._prompt_tool_approval(
            tool_name, arguments, requester=f"Subagent {agent_id}"
        )

    async def _prompt_tool_approval(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        requester: str | None = None,
    ) -> bool | str:
        self._approval_active = True
        self._approval_complete.clear()
        await self._cancel_steering_read()
        approval_owner = requester or "Ash"
        self._notify(
            NotificationEvent.APPROVAL_REQUIRED,
            f"{approval_owner} needs approval: {tool_name}",
        )
        if requester is not None:
            self.write_status(f"{requester} requests approval for {tool_name}.")
        try:
            with self.ui.suspend_live_render():
                self.ui.show_tool_approval(
                    tool_name,
                    arguments,
                    auto=False,
                    diff_mode=self.diff_mode,
                )
                if getattr(self.prompt_input, "supports_choice_ui", False):
                    answer = await self._select_approval(tool_name)
                else:
                    choices = (
                        "Approve [y] once, [s] scope/session, [a] tool/session, "
                        "[p] scope/project, [e] edit exact scope/project, "
                        "[x] deny scope/project, [f] deny with feedback"
                    )
                    if tool_name == "run_command":
                        choices += ", [c] command prefix/project"
                    answer = (
                        (await self.prompt_input.read(f"{choices}, [N] deny? "))
                        .strip()
                        .casefold()
                    )
            if answer is None:
                return False
            if answer in {"y", "yes"}:
                return True
            if answer in {"a", "always", "session", "tool"}:
                try:
                    self.loop.permission_policy.add_session_rule(
                        PermissionRule.create(RuleEffect.ALLOW, tool_name)
                    )
                except OverflowError as exc:
                    self.write_status(f"Session approval denied: {exc}.")
                    return False
                self.ui.approve_tool_for_session(tool_name)
                self.write_status(f"Allowed {tool_name} for this session.")
                return True
            if answer in {"s", "scope"}:
                rule = self._exact_scope_rule(RuleEffect.ALLOW, tool_name, arguments)
                try:
                    self.loop.permission_policy.add_session_rule(rule)
                except OverflowError as exc:
                    self.write_status(f"Session approval denied: {exc}.")
                    return False
                self.write_status(
                    f"Allowed scoped {tool_name} calls for this session ({rule.rule_id})."
                )
                return True
            if answer in {"p", "persist", "project"}:
                rule = self._exact_scope_rule(RuleEffect.ALLOW, tool_name, arguments)
                self._persist_rule(rule)
                return True
            if answer in {"e", "edit"}:
                scoped_arguments = await self._edit_exact_scope(tool_name, arguments)
                if scoped_arguments is None:
                    return False
                rule = self._exact_scope_rule(
                    RuleEffect.ALLOW,
                    tool_name,
                    scoped_arguments,
                )
                self._persist_rule(rule)
                self.write_status(
                    f"Approved edited project scope ({rule.rule_id}): "
                    + ", ".join(scoped_arguments)
                )
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
        except (EOFError, KeyboardInterrupt, PromptInterrupted):
            return False
        finally:
            self._approval_active = False
            self._approval_complete.set()

    async def _select_approval(self, tool_name: str) -> str | None:
        primary = (
            PromptChoice(
                "y",
                "Allow once",
                "Approve only this tool request.",
            ),
            PromptChoice(
                "s",
                "Allow this scope for session",
                "Allow matching safe arguments until this Ash session ends.",
            ),
            PromptChoice(
                "n",
                "Deny",
                "Reject only this request.",
            ),
            PromptChoice(
                "f",
                "Deny and guide Ash",
                "Reject this request and provide corrective feedback.",
            ),
            PromptChoice(
                "more",
                "More approval options…",
                "Broader session/project rules and command-prefix approvals.",
            ),
        )
        advanced = [
            PromptChoice(
                "a",
                "Allow this tool for session",
                "Allow every use of this tool until this Ash session ends.",
            ),
            PromptChoice(
                "p",
                "Allow this scope for project",
                "Persist the current exact safe scope for this project.",
            ),
            PromptChoice(
                "e",
                "Edit scope and allow for project",
                "Edit the exact persisted scope before approving it.",
            ),
            PromptChoice(
                "x",
                "Deny this scope for project",
                "Persist a deny rule for this exact scope in this project.",
            ),
        ]
        if tool_name == "run_command":
            advanced.append(
                PromptChoice(
                    "c",
                    "Allow command prefix for project",
                    "Persist an allow rule for a verified shell-command prefix.",
                )
            )
        advanced.append(
            PromptChoice(
                "back",
                "Back",
                "Return to the common approval choices.",
            )
        )

        while True:
            selected = await self.prompt_input.choose(
                f"{tool_name} permission",
                primary,
                default_value="n",
            )
            if selected != "more":
                return selected
            selected = await self.prompt_input.choose(
                f"{tool_name} permission · advanced",
                tuple(advanced),
                default_value="back",
            )
            if selected == "back":
                continue
            return selected

    def _exact_scope_rule(
        self,
        effect: RuleEffect,
        tool_name: str,
        arguments: dict[str, object],
    ) -> PermissionRule:
        sensitive = self._sensitive_scope_fields(tool_name, arguments)
        if sensitive:
            raise PermissionGrantError(
                "sensitive tool arguments cannot be persisted in an exact scope: "
                + ", ".join(sorted(sensitive))
            )
        return PermissionRule.create(
            effect,
            tool_name,
            build_exact_scope_matchers(arguments),
        )

    def _sensitive_scope_fields(
        self,
        tool_name: str,
        arguments: dict[str, object],
    ) -> set[str]:
        tool = self.loop.tools.get(tool_name)
        if tool is None:
            return set()
        return set(sensitive_tool_argument_fields(tool)).intersection(arguments)

    async def _edit_exact_scope(
        self,
        tool_name: str,
        arguments: dict[str, object],
    ) -> dict[str, object] | None:
        """Full-screen editor flow for selecting exact argument scopes."""

        sensitive = self._sensitive_scope_fields(tool_name, arguments)
        if sensitive:
            self.write_status(
                "Sensitive argument fields cannot be persisted and were excluded "
                "from the editable scope: " + ", ".join(sorted(sensitive))
            )
        keys = sorted(set(arguments) - sensitive)
        if not keys:
            self.write_status("No exact arguments are available to scope.")
            return None
        selected: set[str] = set()
        for key in keys:
            preview = str(arguments[key]).replace("\n", " ")[:200]
            while True:
                answer = (
                    (
                        await self.prompt_input.read(
                            f"Scope {key}={preview!r} exactly? "
                            "[y/N/all/none/cancel]> "
                        )
                    )
                    .strip()
                    .casefold()
                )
                if answer in {"all"}:
                    selected.update(keys)
                    break
                if answer in {"none"}:
                    selected.clear()
                    break
                if answer in {"cancel", "c", "q", "quit"}:
                    self.write_status("Exact-scope editing cancelled.")
                    return None
                if answer in {"y", "yes"}:
                    selected.add(key)
                    break
                if answer in {"n", "no", ""}:
                    break
                self.write_status("Answer y, n, all, none, or cancel.")
        if not selected:
            self.write_status("Edited scope has no arguments; denied.")
            return None
        return {key: arguments[key] for key in sorted(selected)}

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
                except (EOFError, KeyboardInterrupt, PromptInterrupted):
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
        self.write_status(_cancellation_recovery_status(self.loop))


def _cancellation_recovery_status(loop: AshLoop) -> str:
    summary = loop.recovery_summary
    if summary is None or not summary.interrupted_turns:
        return "Turn cancelled."
    if summary.needs_attention:
        return (
            "Turn cancelled. Recovery needs attention: "
            f"{len(summary.unknown_calls)} unknown tool outcome(s), "
            f"{len(summary.unresolved_files)} unresolved file(s). "
            "Run /recovery before retrying side effects."
        )
    if summary.compensated_calls:
        return (
            "Turn cancelled. Ash compensated "
            f"{summary.compensated_calls} interrupted tool call(s). "
            "Run /recovery for details."
        )
    return "Turn cancelled."
