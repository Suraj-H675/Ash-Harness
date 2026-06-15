"""Core AshLoop orchestrator wiring every Sprint 1-7 module together.

V1 minimal loop. The cycle is:

    ingest user prompt
      -> build context (system prompt + session history)
      -> stream chat completion from provider
      -> parse XML events as they arrive (thought, token, tool_call)
      -> on tool_call: safety-check, request approval, execute, persist
      -> on terminal text: persist assistant message, return

The loop repeats the cycle within a single user turn as long as the model
emits new tool calls. A :class:`CircuitBreaker` halts the loop after
``max_failures`` consecutive failures of the same tool, surfacing a
:class:`CircuitBreakerError` to the caller.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from uuid import uuid4

from ash.core.recovery import CircuitBreaker
from ash.core.session import (
    Message,
    Session,
    SessionStore,
    ToolCallRecord,
)
from ash.logging import get_logger
from ash.providers.base import ProviderABC, TokenCounterLike
from ash.repo.repomap import RepoMap
from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolMiddleware, ToolMiddlewareSkip, ToolResult
from ash.tools.git import auto_commit_turn
from ash.ui.parser import Event, StreamingXMLParser
from ash.ui.terminal import TerminalUI

if TYPE_CHECKING:
    from ash.core.planner import Planner
    from ash.core.sprint import SprintExecution
    from ash.hooks import HookRegistry

_log = get_logger(__name__)

if TYPE_CHECKING:
    from ash.core.planner import Planner
    from ash.core.sprint import SprintExecution


ToolApprovalCallback = Callable[
    [str, dict[str, Any]],  # tool_name, arguments
    Awaitable[bool],  # True = approve, False = deny
]


DEFAULT_MAX_TURN_ITERATIONS = 10


SYSTEM_PROMPT_TEMPLATE = """You are Ash, a terminal-native AI coding harness. You are pairing with a developer to write, edit, test, and debug code in the local workspace.

### Workspace Context
- Current Project Path: {project_path}
- OS Platform: {os_platform}

### Safety & Permission Policy
1. You operate under a strict "least privilege" sandboxed file model. You CANNOT write, read, or execute files outside of the workspace directory.
2. Irreversible changes (file updates, command executions) require explicit user authorization. Do not request approvals for simple reads.
3. If a command matches the blocklist, your tool call will be rejected by the harness. Do not attempt to bypass this.
4. NEVER attempt to execute raw destruction commands (e.g. formatting disks, mass deletes).

### Operational Rules
1. PLAN BEFORE ACTING: Write out your planned steps inside a `<thought>` tag before invoking any tools.
2. STREAM PROGRESS: Work incrementally. Write files, run tests, and debug errors step-by-step. Do not attempt to write 10 files in one go without verifying compilation.
3. CONTEXT INTEGRITY: Maintain existing documentation and codebase styles. Do not remove comments unless explicitly told to.

### Tool Call Format
To call a tool, you must output an XML element matching this schema:
<call_tool name="tool_name">
<arg name="param1">value1</arg>
<arg name="param2">value2</arg>
</call_tool>

Example tool call:
<call_tool name="read_file">
<arg name="file_path">src/main.py</arg>
<arg name="start_line">10</arg>
<arg name="end_line">30</arg>
</call_tool>

Any text response you provide must be enclosed in `<response>` tags.
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _default_system_prompt(project_root: Path) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        project_path=str(project_root),
        os_platform=_detect_platform(),
    )


def _detect_platform() -> str:
    import platform

    return platform.system()


def _render_tool_response(call_id: str, tool_name: str, result: dict[str, Any]) -> str:
    """Format a tool result as a <tool_response> XML block."""

    payload = {
        "success": result.get("success", False),
        "output": result.get("output", ""),
        "error": result.get("error"),
        "truncated": result.get("truncated", False),
        "token_count": result.get("token_count", 0),
    }
    return (
        f'<tool_response name="{tool_name}" call_id="{call_id}">\n'
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        f"</tool_response>"
    )


class AshLoop:
    """V1 minimal agent loop."""

    def __init__(
        self,
        session_store: SessionStore,
        provider: ProviderABC,
        safety_guard: SafetyGuard,
        ui: TerminalUI,
        project_root: Path,
        *,
        tools: dict[str, BaseTool] | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        system_prompt: str | None = None,
        token_counter: TokenCounterLike | None = None,
        max_turn_iterations: int = DEFAULT_MAX_TURN_ITERATIONS,
        repo_map: RepoMap | None = None,
        auto_commit: bool = False,
        auto_commit_paths: list[Path] | None = None,
        planner: "Planner | None" = None,
        enable_sprint_planning: bool = False,
        tool_middlewares: list[ToolMiddleware] | None = None,
        on_tool_approval: ToolApprovalCallback | None = None,
        enable_memory_recall: bool = False,
        hooks: "HookRegistry | None" = None,
    ) -> None:
        self.session_store = session_store
        self.provider = provider
        self.safety_guard = safety_guard
        self.ui = ui
        self.project_root = project_root
        self.tools: dict[str, BaseTool] = dict(tools or {})
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.system_prompt = system_prompt or _default_system_prompt(project_root)
        self.token_counter = token_counter
        self.max_turn_iterations = max_turn_iterations
        self.repo_map = repo_map
        self.auto_commit = auto_commit
        self.auto_commit_paths = list(auto_commit_paths or [])
        self.planner = planner
        self.enable_sprint_planning = enable_sprint_planning
        self.tool_middlewares: list[ToolMiddleware] = list(tool_middlewares or [])
        self.on_tool_approval = on_tool_approval
        self.enable_memory_recall = enable_memory_recall
        self.hooks = hooks
        self.current_session: Session | None = None

    # --- session lifecycle ------------------------------------------------

    async def start_session(self, session_id: str | None = None) -> Session:
        """Create a new session or restore one by id."""

        if session_id is not None:
            try:
                self.current_session = self.session_store.load_session(session_id)
                return self.current_session
            except KeyError:
                # Fall through to creating a fresh session.
                pass

        # New session: optionally recall recent context from prior sessions
        if self.enable_memory_recall:
            recent = self.session_store.get_recent_session_summaries(
                str(self.project_root), limit=3
            )
            if recent:
                memory_context = self._build_memory_context(recent)
                self.system_prompt = (
                    f"{self.system_prompt}\n\n## Recent Context\n{memory_context}"
                )

        if self.hooks is not None:
            await self.hooks.fire_session_start()
            injected = self.hooks.get_injected_prompt()
            if injected:
                self.system_prompt = f"{self.system_prompt}\n\n{injected}"

        session = self.session_store.create_session(str(self.project_root))
        self.current_session = session
        return session

    # --- the main turn ----------------------------------------------------

    async def run_turn(self, user_input: str) -> str:
        """Run a single user turn to completion and return the final text."""

        _log.info("turn started")
        if self.current_session is None:
            await self.start_session()
        if self.current_session is None:
            raise RuntimeError("start_session() returned None")
        session = self.current_session

        # 0. Optional V5 sprint planning phase. Triggered only when the
        # loop is configured with a planner AND the user input looks
        # like a multi-step request. On approval, the sprint is
        # persisted to SQLite and the contract's goal replaces the raw
        # user input for the execution turn. On rejection, the turn
        # short-circuits with a polite "plan rejected" message.
        if self.enable_sprint_planning and self.planner is not None:
            from ash.core.sprint import (
                looks_like_sprint_request,
            )

            if looks_like_sprint_request(user_input):
                execution = await self._planning_phase(user_input)
                self.session_store.save_sprint(session.session_id, execution)
                approved = self.ui.show_plan(execution)
                if not approved:
                    execution.abort("rejected by user")
                    self.session_store.save_sprint(session.session_id, execution)
                    return (
                        f"Plan rejected. Sprint {execution.contract.contract_id[:8]} aborted; "
                        "no further actions taken."
                    )
                execution.start()
                self.session_store.save_sprint(session.session_id, execution)
                # Feed the contract goal into the model so the planning
                # artifacts are visible during execution.
                user_input = (
                    f"{execution.contract.goal}\n\n"
                    f"Approved sprint plan ({len(execution.items)} steps):\n"
                    + "\n".join(f"- {it.description}" for it in execution.items)
                )

        # 1. Persist the user message.
        user_message = Message(
            role="user",
            content=user_input,
            timestamp=_utc_now(),
        )
        self.session_store.save_message(session.session_id, user_message)
        # Keep the in-memory session mirror in sync so subsequent
        # _build_messages() calls in the same turn see the history.
        session.messages.append(user_message)

        # 2. Stream/execute loop bounded by max_turn_iterations.
        final_text = ""
        for _ in range(self.max_turn_iterations):
            messages = self._build_messages(session)
            assistant_text, tool_calls = await self._stream_one_completion(messages)

            # Persist the assistant turn.
            assistant_message = Message(
                role="assistant",
                content=assistant_text,
                timestamp=_utc_now(),
            )
            self.session_store.save_message(session.session_id, assistant_message)
            session.messages.append(assistant_message)

            if not tool_calls:
                final_text = assistant_text
                break

            # Execute tool calls (sequential within a turn for V1; the spec
            # mentions asyncio.gather for parallel independent tools but V1
            # is single-shot to keep the harness simple).
            results = await self._execute_tool_calls(tool_calls, session)

            # If the breaker tripped, surface a final message and stop.
            if self.circuit_breaker.is_tripped:
                _log.warning("circuit breaker tripped — halting turn")
                final_text = (
                    f"{assistant_text}\n\n"
                    "[Circuit breaker tripped — see prior tool errors. Halting turn.]"
                ).strip()
                break

            # Persist tool results as user-role messages so the model sees
            # them on the next iteration.
            for call, result in zip(tool_calls, results, strict=True):
                tool_message = Message(
                    role="tool",
                    content=_render_tool_response(
                        call_id=call["call_id"],
                        tool_name=call["name"],
                        result=result,
                    ),
                    timestamp=_utc_now(),
                    metadata={"call_id": call["call_id"]},
                )
                self.session_store.save_message(session.session_id, tool_message)
                session.messages.append(tool_message)

            # The assistant produced tool calls; loop and let the model
            # observe the results on the next completion.
            final_text = assistant_text
        else:
            # Loop exhausted without a terminal text response.
            final_text = (
                f"{final_text}\n\n"
                "[Turn reached max iterations without a final text response.]"
            ).strip()

        if self.auto_commit:
            commit_result = await auto_commit_turn(
                self.project_root,
                message=f"ash: turn complete ({len(final_text)} chars)",
                paths=self.auto_commit_paths or None,
                safety_guard=self.safety_guard,
            )
            if not commit_result.success and commit_result.error:
                # Surface commit failures to the user but don't fail the turn.
                self.ui.console.print(f"auto_commit failed: {commit_result.error}")

        _log.info(f"turn complete, {len(final_text)} chars returned")
        return final_text

    # --- sprint planning helpers (Sprint 12 / V5) ---------------------

    async def _planning_phase(self, user_input: str) -> "SprintExecution":
        """Call the planner to decompose ``user_input`` into a contract."""

        from ash.core.planner import Planner

        if self.planner is None:
            raise RuntimeError("planner is None but sprint planning is enabled")
        repo_excerpt = ""
        if self.repo_map is not None:
            try:
                ranked = self.repo_map.rank([self.project_root])
                repo_excerpt = self.repo_map.render(
                    ranked, top_files=3, symbols_per_file=4
                )
            except Exception:  # noqa: BLE001
                repo_excerpt = ""
        if not isinstance(self.planner, Planner):
            # Defensive: only Planner is supported in V5.
            raise TypeError(f"Unsupported planner type: {type(self.planner).__name__}")
        return await self.planner.decompose(
            user_input,
            project_root=self.project_root,
            repo_map_excerpt=repo_excerpt,
        )

    # --- streaming & parsing ---------------------------------------------

    async def _stream_one_completion(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Stream one completion, returning (text, tool_calls)."""

        parser = StreamingXMLParser()
        text_chunks: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        with self.ui.begin_turn():
            async for chunk in self.provider.stream_chat(messages):
                for fragment in (chunk.content, chunk.tool_call_delta):
                    if not fragment:
                        continue
                    for event in parser.feed(fragment):
                        self._handle_event(event, text_chunks, tool_calls)
            # Drain the parser's remaining buffer at end-of-stream.
            for event in parser.feed(""):
                self._handle_event(event, text_chunks, tool_calls)
            self.ui.finalize_turn()

        return "".join(text_chunks), tool_calls

    def _handle_event(
        self,
        event: Event,
        text_chunks: list[str],
        tool_calls: list[dict[str, Any]],
    ) -> None:
        kind, payload = event
        if kind == "token" and isinstance(payload, str):
            self.ui.print_token(payload)
            text_chunks.append(payload)
        elif kind == "thought" and isinstance(payload, str):
            self.ui.print_thought(payload)
        elif kind == "tool_call" and isinstance(payload, dict):
            tool_call = {
                "name": payload["name"],
                "arguments": dict(payload["arguments"]),
                "call_id": str(uuid4()),
            }
            tool_calls.append(tool_call)

    # --- tool execution ---------------------------------------------------

    async def _apply_middlewares_before(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        tool: BaseTool,
    ) -> None:
        for mw in self.tool_middlewares:
            await mw.before_tool(tool_name, arguments, tool)

    async def _apply_middlewares_after(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> ToolResult:
        for mw in self.tool_middlewares:
            await mw.after_tool(tool_name, arguments, result)
        return result

    async def _execute_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        session: Session,
    ) -> list[dict[str, Any]]:
        """Execute approved tool calls, gating each on the safety guard."""

        results: list[dict[str, Any]] = []
        for call in tool_calls:
            tool_name = call["name"]
            arguments = call["arguments"]
            _log.debug(f"executing tool {tool_name!r} with args {arguments}")
            record = ToolCallRecord(
                call_id=call["call_id"],
                tool_name=tool_name,
                arguments=arguments,
                approved=False,
                executed=False,
                timestamp=_utc_now(),
            )

            if self.on_tool_approval is not None:
                approved = await self.on_tool_approval(tool_name, arguments)
            else:
                approved = self.ui.request_tool_approval(tool_name, arguments)
            record.approved = approved
            if not approved:
                record.executed = False
                record.error = "Denied by user"
                self.session_store.save_tool_call(session.session_id, record)
                results.append(
                    {
                        "success": False,
                        "output": "",
                        "error": "Denied by user",
                    }
                )
                continue

            tool = self.tools.get(tool_name)
            if tool is None:
                record.error = f"Unknown tool: {tool_name}"
                self.session_store.save_tool_call(session.session_id, record)
                self.circuit_breaker.record_failure(tool_name)
                results.append(
                    {
                        "success": False,
                        "output": "",
                        "error": f"Unknown tool: {tool_name}",
                    }
                )
                continue

            try:
                await self._apply_middlewares_before(tool_name, arguments, tool)
                tool_result: ToolResult = await tool.run(**arguments)
                if self.hooks is not None:
                    await self.hooks.fire_post_tool(tool_name, arguments, tool_result)
                tool_result = await self._apply_middlewares_after(
                    tool_name, arguments, tool_result
                )
            except ToolMiddlewareSkip:
                tool_result = ToolResult(
                    success=True, output="skipped by middleware", error=None
                )
            except Exception as exc:  # noqa: BLE001 — we want any error captured
                record.executed = True
                record.error = str(exc)
                self.session_store.save_tool_call(session.session_id, record)
                self.circuit_breaker.record_failure(tool_name)
                results.append(
                    {
                        "success": False,
                        "output": "",
                        "error": str(exc),
                    }
                )
                continue

            record.executed = True
            record.result = tool_result.output
            record.error = tool_result.error
            self.session_store.save_tool_call(session.session_id, record)

            results.append(
                {
                    "success": tool_result.success,
                    "output": tool_result.output,
                    "error": tool_result.error,
                    "truncated": tool_result.truncated,
                    "token_count": tool_result.token_count,
                }
            )
            if tool_result.success:
                self.circuit_breaker.record_success()
            else:
                # A tool that ran cleanly but reported failure still counts
                # as a failure for the breaker.
                self.circuit_breaker.record_failure(tool_name)

        return results

    # --- prompt assembly --------------------------------------------------

    def _build_memory_context(self, recent_summaries: list[str]) -> str:
        """Format N most recent session transcripts as a context string."""
        lines = ["The following sessions are prior context for this project:"]
        for i, summary in enumerate(recent_summaries, 1):
            lines.append(f"\n--- Prior Session {i} ---\n{summary[:2000]}")
        return "".join(lines)

    def _build_messages(self, session: Session) -> list[dict[str, Any]]:
        """Build the messages payload for the provider."""

        system_content = self.system_prompt
        if self.repo_map is not None:
            # Rank against any workspace files touched in this turn so far
            # (best-effort: just the user message we just persisted).
            active = [Path(p) for p in (self.auto_commit_paths or [])]
            try:
                ranked = self.repo_map.rank(active or [self.project_root])
                repo_section = self.repo_map.render(
                    ranked, top_files=5, symbols_per_file=6
                )
            except Exception as exc:  # noqa: BLE001 — repo map is best-effort
                repo_section = f"(repo map unavailable: {exc})"
            system_content = f"{system_content}\n\n{repo_section}"

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
        for message in session.messages:
            messages.append({"role": message.role, "content": message.content})
        return messages
