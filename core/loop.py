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

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, ContextManager, Protocol
from uuid import uuid4

from core.recovery import CircuitBreaker, CircuitBreakerError
from core.session import (
    AuditAction,
    AuditResult,
    Message,
    Session,
    SessionStore,
    ToolCallRecord,
)
from core.redaction import redact_text, redact_value
from ash_logging import get_logger
from mcp.server import load_mcp_servers
from providers.base import ProviderABC, TokenCounterLike
from providers.capabilities import ProviderCapabilities
from repo.repomap import RepoMap
from safety.guard import SafetyGuard
from safety.policy import PermissionPolicy, PolicyAction, READ_ONLY_TOOLS
from tools.base import BaseTool, ToolMiddleware, ToolMiddlewareSkip, ToolResult
from tools.git import auto_commit_turn
from ui.parser import Event, StreamingXMLParser
from rich.console import Console

if TYPE_CHECKING:
    from config import AshConfig
    from context.turn import TurnContext
    from core.planner import Planner
    from core.sprint import SprintExecution
    from hooks import HookRegistry
    from memory.vector import (
        Chunk,
        EmbeddingAdapter,
        VectorHit,
        VectorSearchPipeline,
    )
    from tools.registry import ToolRegistry

_log = get_logger(__name__)

if TYPE_CHECKING:
    from core.planner import Planner
    from core.sprint import SprintExecution


ToolApprovalCallback = Callable[
    [str, dict[str, Any]],  # tool_name, arguments
    Awaitable[bool],  # True = approve, False = deny
]


class LoopUI(Protocol):
    console: Console

    @property
    def has_approval_callback(self) -> bool: ...

    def begin_turn(self) -> ContextManager[Any]: ...
    def finalize_turn(self) -> None: ...
    def print_token(self, text: str) -> None: ...
    def print_thought(self, text: str) -> None: ...
    def update_token_count(self, current: int, maximum: int | None = None) -> None: ...
    def emit_event(self, payload: dict[str, Any]) -> None: ...
    def request_tool_approval(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> bool: ...
    def show_plan(self, execution: Any) -> bool: ...


DEFAULT_MAX_TURN_ITERATIONS = 10
FILE_WRITE_TOOLS = {
    "write_file",
    "replace_file_content",
    "replace_file_edits",
    "whole_edit",
    "apply_patch",
}


def _provider_capabilities(provider: Any) -> ProviderCapabilities:
    capabilities = getattr(provider, "capabilities", None)
    return (
        capabilities
        if isinstance(capabilities, ProviderCapabilities)
        else ProviderCapabilities()
    )


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


def _normalize_native_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    """Convert a provider-native tool call into Ash's canonical shape."""

    call_id = str(call.get("call_id") or call.get("id") or uuid4())
    name = str(call.get("name") or "")
    arguments = call.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {"__invalid_json__": arguments}
    if not isinstance(arguments, dict):
        arguments = {"value": arguments}
    return {
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def _audit_action_for_tool(tool_name: str) -> AuditAction:
    if tool_name == "run_command":
        return "command_run"
    if tool_name in FILE_WRITE_TOOLS:
        return "file_write"
    return "tool_call"


class AshLoop:
    """V1 minimal agent loop."""

    def __init__(
        self,
        session_store: SessionStore,
        provider: ProviderABC,
        safety_guard: SafetyGuard,
        ui: LoopUI,
        project_root: Path,
        *,
        tools: dict[str, BaseTool] | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        system_prompt: str | None = None,
        additional_instructions: str = "",
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
        turn_context: "TurnContext | None" = None,
        memory_nudge_interval: int = 0,
        tools_registry: "ToolRegistry | None" = None,
        skill_nudge_interval: int = 0,
        continuous_mode: bool = False,
        max_continuous_turns: int = 10,
        safety_tier: str = "interactive",
        enable_semantic_memory: bool = False,
        memory_backend: str = "auto",
        embedding_provider: str = "auto",
        openai_api_key: str = "",
        onnx_model_path: Path | None = None,
        chroma_persist_dir: Path | None = None,
        mcp_config_path: Path | None = None,
        config: "AshConfig | None" = None,
    ) -> None:
        self.session_store = session_store
        self.provider = provider
        self.safety_guard = safety_guard
        self.ui = ui
        self.project_root = project_root
        self.tools: dict[str, BaseTool] = dict(tools or {})
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.system_prompt = system_prompt or _default_system_prompt(project_root)
        if additional_instructions:
            self.system_prompt = f"{self.system_prompt}\n\n{additional_instructions}"
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
        self.turn_context = turn_context
        self.memory_nudge_interval = memory_nudge_interval
        self._turns_since_nudge = 0
        self.tools_registry = tools_registry
        if tools_registry is not None:
            from tools.skills import configure_runtime

            configure_runtime(
                tools_provider=lambda: list(tools_registry.as_dict().values()),
                root_provider=lambda: self.project_root,
            )
        self._config = config
        self._last_context_tokens = 0
        self._last_context_budget: Any | None = None
        self.skill_nudge_interval = skill_nudge_interval
        self._iterations_since_skill_use = 0
        self.continuous_mode = continuous_mode
        self.max_continuous_turns = max_continuous_turns
        self._continuous_turns = 0
        self.safety_tier = safety_tier
        self.permission_policy = PermissionPolicy(safety_tier)
        self.enable_semantic_memory = enable_semantic_memory
        self._vector_pipeline: "VectorSearchPipeline | None" = None
        self._pending_memory_context: str = ""
        if enable_semantic_memory:
            self._init_vector_pipeline(
                memory_backend=memory_backend,
                embedding_provider=embedding_provider,
                openai_api_key=openai_api_key,
                onnx_model_path=onnx_model_path,
                chroma_persist_dir=chroma_persist_dir,
            )
        self.current_session: Session | None = None
        self.recovered_turns = 0
        self._mcp_runtime: Any | None = None
        self._mcp_configs = {}
        if mcp_config_path is not None and mcp_config_path.exists():
            self._mcp_configs = load_mcp_servers(mcp_config_path)

    def __del__(self) -> None:
        # Async resources are released by ``aclose``; no subprocess work is
        # attempted from the garbage collector.
        return None

    async def __aenter__(self) -> "AshLoop":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Deterministically release provider and subprocess resources."""

        if self._mcp_runtime is not None:
            await self._mcp_runtime.close()
            self._mcp_runtime = None
        await asyncio.gather(
            *(tool.aclose() for tool in self.tools.values()),
            return_exceptions=True,
        )
        await self.provider.aclose()

    # --- session lifecycle ------------------------------------------------

    async def start_session(self, session_id: str | None = None) -> Session:
        """Create a new session or restore one by id."""

        if self._mcp_configs and self._mcp_runtime is None:
            from mcp.runtime import MCPRuntime

            self._mcp_runtime = MCPRuntime(self._mcp_configs, self.safety_guard)
            self.tools.update(await self._mcp_runtime.start())
            for name, error in self._mcp_runtime.errors.items():
                _log.warning("MCP server %s unavailable: %s", name, error)

        if session_id is not None:
            self.current_session = self.session_store.load_session(session_id)
            self.recovered_turns = self.session_store.reconcile_interrupted_turns(
                session_id
            )
            return self.current_session

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

        session = self.session_store.create_session(
            str(self.project_root), model=self.provider.model_name
        )
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

        from context.turn import TurnContext

        self.turn_context = TurnContext(
            session_id=session.session_id,
            turn_id=str(uuid4()),
        )
        self.session_store.start_turn(
            session.session_id, self.turn_context.turn_id, user_input
        )

        # 0. Optional V5 sprint planning phase. Triggered only when the
        # loop is configured with a planner AND the user input looks
        # like a multi-step request. On approval, the sprint is
        # persisted to SQLite and the contract's goal replaces the raw
        # user input for the execution turn. On rejection, the turn
        # short-circuits with a polite "plan rejected" message.
        if self.enable_sprint_planning and self.planner is not None:
            from core.sprint import (
                looks_like_sprint_request,
            )

            if looks_like_sprint_request(user_input):
                execution = await self._planning_phase(user_input)
                self.session_store.save_sprint(session.session_id, execution)
                approved = self.ui.show_plan(execution)
                if not approved:
                    execution.abort("rejected by user")
                    self.session_store.save_sprint(session.session_id, execution)
                    self.session_store.complete_turn(self.turn_context.turn_id)
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
        self.session_store.save_message(
            session.session_id,
            user_message.model_copy(
                update={"content": redact_text(user_message.content)}
            ),
        )
        # Keep the in-memory session mirror in sync so subsequent
        # _build_messages() calls in the same turn see the history.
        session.messages.append(user_message)

        # 2. Stream/execute loop bounded by max_turn_iterations.
        final_text = ""
        total_prompt_tokens = 0
        total_completion_tokens = 0
        for _ in range(self.max_turn_iterations):
            # Optionally search semantic memory and inject relevant context.
            self._pending_memory_context = ""
            if self.enable_semantic_memory and self._vector_pipeline is not None:
                hits = await self.semantic_search(user_input, top_k=3)
                if hits:
                    self._pending_memory_context = "\n\n".join(
                        f"// From {hit.file_path}:\n{hit.content[:500]}" for hit in hits
                    )
            messages = self._build_messages(session)
            (
                assistant_text,
                tool_calls,
                turn_prompt_tokens,
                turn_completion_tokens,
            ) = await self._stream_one_completion(messages)
            total_prompt_tokens += turn_prompt_tokens
            total_completion_tokens += turn_completion_tokens

            # Persist the assistant turn.
            assistant_message = Message(
                role="assistant",
                content=assistant_text,
                timestamp=_utc_now(),
                metadata={"tool_calls": tool_calls} if tool_calls else {},
            )
            self.session_store.save_message(
                session.session_id,
                assistant_message.model_copy(
                    update={
                        "content": redact_text(assistant_message.content),
                        "metadata": redact_value(assistant_message.metadata),
                    }
                ),
            )
            session.messages.append(assistant_message)

            if not tool_calls:
                final_text = assistant_text
                if (
                    self.continuous_mode
                    and self._continuous_turns < self.max_continuous_turns
                ):
                    self._continuous_turns += 1
                    follow_up = "Continue the previous task. What is the next step?"
                    self.session_store.complete_turn(self.turn_context.turn_id)
                    return await self.run_turn(follow_up)
                break

            # Independent read-only calls may execute concurrently; all other
            # calls retain deterministic sequential side-effect ordering.
            try:
                results = await self._execute_tool_calls(tool_calls, session)
            except CircuitBreakerError:
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

            # Memory nudge check — injected periodically after tool results.
            self._turns_since_nudge += 1
            if (
                self.memory_nudge_interval > 0
                and self._turns_since_nudge >= self.memory_nudge_interval
            ):
                nudge = self._build_memory_nudge()
                if nudge:
                    session.messages.append(
                        Message(role="system", content=nudge, timestamp=_utc_now())
                    )
                self._turns_since_nudge = 0

            # The assistant produced tool calls; loop and let the model
            # observe the results on the next completion.
            final_text = assistant_text
        else:
            # Loop exhausted without a terminal text response.
            final_text = (
                f"{final_text}\n\n"
                "[Turn reached max iterations without a final text response.]"
            ).strip()

        # Save accumulated token usage to session DB.
        prompt = int(total_prompt_tokens) if total_prompt_tokens else 0
        completion = int(total_completion_tokens) if total_completion_tokens else 0
        if prompt > 0 or completion > 0:
            pricing: dict[str, float] = {}
            if self._config is not None:
                pricing = self._config.model_pricing_usd_per_million.get(
                    self._config.model, {}
                )
            turn_cost_usd = (
                prompt * float(pricing.get("input", 0.0))
                + completion * float(pricing.get("output", 0.0))
            ) / 1_000_000
            self.session_store.save_session_token_stats(
                session.session_id,
                prompt,
                completion,
                turn_cost_usd,
            )

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
        self.session_store.complete_turn(self.turn_context.turn_id)
        return final_text

    # --- sprint planning helpers (Sprint 12 / V5) ---------------------

    async def _planning_phase(self, user_input: str) -> "SprintExecution":
        """Call the planner to decompose ``user_input`` into a contract."""

        from core.planner import Planner

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

    def _tools_to_openai_format(self, tools: dict[str, Any]) -> list[dict[str, Any]]:
        """Convert Ash tools dict to OpenAI tools format for API tool calling."""
        result = []
        for tool in tools.values():
            if not hasattr(tool, "name") or not hasattr(tool, "description"):
                continue
            schema: dict[str, Any] = {}
            if hasattr(tool, "args_schema") and tool.args_schema is not None:
                args_schema = tool.args_schema
                # Support both Pydantic v2 (model_json_schema) and v1-style models.
                if hasattr(args_schema, "model_json_schema"):
                    schema = args_schema.model_json_schema()
                elif hasattr(args_schema, "schema"):
                    schema = args_schema.schema()
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": schema,
                    },
                }
            )
        return result

    def _estimate_tool_schema_tokens(self) -> int:
        """Estimate tool declaration tokens reserved outside chat messages."""

        if not self.tools:
            return 0
        if _provider_capabilities(self.provider).native_tools:
            payload: Any = self._tools_to_openai_format(self.tools)
        else:
            payload = [
                {"name": tool.name, "description": getattr(tool, "description", "")}
                for tool in self.tools.values()
            ]
        return max(0, int(self.provider.count_tokens(json.dumps(payload, default=str))))

    async def _stream_one_completion(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], int, int]:
        """Stream one completion, returning (text, tool_calls, prompt_tokens, completion_tokens)."""

        # Build OpenAI-format tools list for providers that support native tool_calls.
        openai_tools = (
            self._tools_to_openai_format(self.tools)
            if self.tools and _provider_capabilities(self.provider).native_tools
            else None
        )

        parser = StreamingXMLParser()
        text_chunks: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        prompt_tokens = 0
        completion_tokens = 0
        native_tool_calls_from_api: list[dict[str, Any]] = []

        try:
            with self.ui.begin_turn():
                async for chunk in self.provider.stream_chat(
                    messages, tools=openai_tools
                ):
                    for fragment in (chunk.content, chunk.tool_call_delta):
                        if not fragment:
                            continue
                        for event in parser.feed(fragment):
                            self._handle_event(event, text_chunks, tool_calls)
                    # Collect native tool calls from the API (these carry real IDs).
                    if chunk.native_tool_calls:
                        native_tool_calls_from_api.extend(chunk.native_tool_calls)
                    # Capture usage from the final chunk (when is_done=True).
                    if chunk.is_done:
                        prompt_tokens = chunk.prompt_tokens
                        completion_tokens = chunk.completion_tokens
                # Drain the parser's remaining buffer at end-of-stream.
                for event in parser.feed(""):
                    self._handle_event(event, text_chunks, tool_calls)
        finally:
            self.ui.finalize_turn()

        # If the API returned native tool calls (OpenAI-compatible with tool_calls
        # support), use those instead of the XML-parsed ones — they carry the
        # real tool_call_id that the API requires on tool-result messages.
        if native_tool_calls_from_api:
            tool_calls = [
                _normalize_native_tool_call(call) for call in native_tool_calls_from_api
            ]

        return "".join(text_chunks), tool_calls, prompt_tokens, completion_tokens

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

        if len(tool_calls) > 1 and all(
            call.get("name") in READ_ONLY_TOOLS for call in tool_calls
        ):
            grouped = await asyncio.gather(
                *(self._execute_tool_calls([call], session) for call in tool_calls)
            )
            return [result for group in grouped for result in group]

        results: list[dict[str, Any]] = []
        for call in tool_calls:
            tool_name = call["name"]
            arguments = call["arguments"]
            _log.debug(
                "executing tool {!r} with argument keys {}",
                tool_name,
                sorted(arguments),
            )
            record = ToolCallRecord(
                call_id=call["call_id"],
                tool_name=tool_name,
                arguments=redact_value(arguments),
                approved=False,
                executed=False,
                timestamp=_utc_now(),
            )
            event_base = {
                "call_id": record.call_id,
                "tool": tool_name,
                "arguments": record.arguments,
            }
            self.ui.emit_event({"type": "tool.requested", **event_base})

            decision = self.permission_policy.evaluate(tool_name, arguments)
            if self.on_tool_approval is not None:
                approved = await self.on_tool_approval(tool_name, arguments)
            elif self.ui.has_approval_callback:
                approved = self.ui.request_tool_approval(tool_name, arguments)
            elif decision.action == PolicyAction.ALLOW:
                approved = True
            elif decision.action == PolicyAction.DENY:
                approved = False
            else:
                approved = self.ui.request_tool_approval(tool_name, arguments)
            record.approved = approved
            if approved:
                self._append_tool_audit(
                    session,
                    action_type="user_approval",
                    target_resource=tool_name,
                    details={
                        "call_id": record.call_id,
                        "arguments": record.arguments,
                        "decision": decision.action.value,
                        "reason": decision.reason,
                    },
                    result="APPROVED",
                )
            if not approved:
                record.executed = False
                record.error = (
                    decision.reason
                    if decision.action == PolicyAction.DENY
                    else "Denied by user"
                )
                self.session_store.save_tool_call(session.session_id, record)
                self._append_tool_audit(
                    session,
                    action_type=(
                        "safety_block"
                        if decision.action == PolicyAction.DENY
                        else "user_approval"
                    ),
                    target_resource=tool_name,
                    details={
                        "call_id": record.call_id,
                        "arguments": record.arguments,
                        "decision": decision.action.value,
                        "reason": record.error,
                    },
                    result=(
                        "BLOCKED_BY_GUARD"
                        if decision.action == PolicyAction.DENY
                        else "DENIED"
                    ),
                )
                self.ui.emit_event(
                    {
                        "type": "tool.denied",
                        **event_base,
                        "reason": record.error,
                    }
                )
                results.append(
                    {
                        "success": False,
                        "output": "",
                        "error": record.error,
                    }
                )
                continue

            tool = self.tools.get(tool_name)
            if tool is None:
                record.error = f"Unknown tool: {tool_name}"
                self.session_store.save_tool_call(session.session_id, record)
                self._append_tool_audit(
                    session,
                    action_type="tool_call",
                    target_resource=tool_name,
                    details={
                        "call_id": record.call_id,
                        "arguments": record.arguments,
                        "error": record.error,
                    },
                    result="FAILURE",
                )
                self.circuit_breaker.record_failure(tool_name)
                self.ui.emit_event(
                    {"type": "tool.error", **event_base, "error": record.error}
                )
                results.append(
                    {
                        "success": False,
                        "output": "",
                        "error": f"Unknown tool: {tool_name}",
                    }
                )
                continue

            self.ui.emit_event({"type": "tool.started", **event_base})
            try:
                if self.hooks is not None:
                    await self.hooks.fire_pre_tool(tool_name, arguments)
                await self._apply_middlewares_before(tool_name, arguments, tool)
                result_dict = await _execute_with_retry(tool, tool_name, arguments)
                tool_result = ToolResult(
                    success=result_dict["success"],
                    output=result_dict["output"],
                    error=result_dict["error"],
                    truncated=result_dict.get("truncated", False),
                    token_count=result_dict.get("token_count", 0),
                )
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
                self._append_tool_audit(
                    session,
                    action_type=_audit_action_for_tool(tool_name),
                    target_resource=tool_name,
                    details={
                        "call_id": record.call_id,
                        "arguments": record.arguments,
                        "error": redact_text(str(exc)),
                    },
                    result="FAILURE",
                )
                self.circuit_breaker.record_failure(tool_name)
                self.ui.emit_event(
                    {
                        "type": "tool.error",
                        **event_base,
                        "error": redact_text(str(exc)),
                    }
                )
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
            self._append_tool_audit(
                session,
                action_type=_audit_action_for_tool(tool_name),
                target_resource=tool_name,
                details={
                    "call_id": record.call_id,
                    "arguments": record.arguments,
                    "success": tool_result.success,
                    "output": redact_text(tool_result.output),
                    "error": redact_text(tool_result.error or ""),
                    "truncated": tool_result.truncated,
                },
                result="SUCCESS" if tool_result.success else "FAILURE",
            )
            self.ui.emit_event(
                {
                    "type": "tool.completed",
                    **event_base,
                    "success": tool_result.success,
                    "output": tool_result.output,
                    "error": tool_result.error,
                    "truncated": tool_result.truncated,
                }
            )

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

            # Skill nudge check — suggest skills after N iterations of disuse.
            was_skill = self.tools_registry is not None and any(
                e.name == tool_name for e in self.tools_registry.skill_index()
            )
            if was_skill:
                self._iterations_since_skill_use = 0
            else:
                self._iterations_since_skill_use += 1
                if (
                    self.skill_nudge_interval > 0
                    and self._iterations_since_skill_use >= self.skill_nudge_interval
                ):
                    nudge = self._build_skill_nudge()
                    if nudge and self.current_session:
                        self.current_session.messages.append(
                            Message(role="system", content=nudge, timestamp=_utc_now())
                        )
                    self._iterations_since_skill_use = 0

        return results

    def _append_tool_audit(
        self,
        session: Session,
        *,
        action_type: AuditAction,
        target_resource: str,
        details: dict[str, Any],
        result: AuditResult,
    ) -> None:
        self.session_store.append_audit_log(
            session.session_id,
            action_type=action_type,
            target_resource=target_resource,
            details=redact_value(details),
            result=result,
        )

    # --- prompt assembly --------------------------------------------------

    def _build_memory_context(self, recent_summaries: list[str]) -> str:
        """Format N most recent session transcripts as a context string."""
        lines = ["The following sessions are prior context for this project:"]
        for i, summary in enumerate(recent_summaries, 1):
            lines.append(f"\n--- Prior Session {i} ---\n{summary[:2000]}")
        return "".join(lines)

    def _build_memory_nudge(self) -> str:
        if not self.current_session:
            return ""
        recent = self.current_session.messages[-10:]
        summary = f"[Memory nudge — {len(recent)} messages in recent turns]"
        return summary

    def _build_skill_nudge(self) -> str:
        if self.tools_registry is None:
            return ""
        skill_index = self.tools_registry.skill_index()
        if not skill_index:
            return ""
        suggestions = [f"- {s.name}: {s.description}" for s in skill_index[:3]]
        return "[Skill nudge] Consider using:\n" + "\n".join(suggestions)

    # --- semantic memory -----------------------------------------------------

    def _init_vector_pipeline(
        self,
        memory_backend: str,
        embedding_provider: str,
        openai_api_key: str,
        onnx_model_path: Path | None,
        chroma_persist_dir: Path | None,
    ) -> None:
        """Initialize the vector search pipeline based on config."""
        from memory import (
            VectorSearchPipeline,
            InMemoryVectorIndex,
            DeterministicEmbedding,
        )

        adapter: "EmbeddingAdapter"
        if embedding_provider == "onnx":
            from memory import ONNXLocalEmbedding

            adapter = ONNXLocalEmbedding(
                model_path=onnx_model_path or Path(".ash/model.onnx")
            )
        elif embedding_provider == "openai":
            from memory import OpenAIEmbedding

            adapter = OpenAIEmbedding(api_key=openai_api_key)
        else:
            adapter = DeterministicEmbedding()

        vector_index: Any
        lexical_index = None
        if memory_backend == "chroma":
            from memory import ChromaIndex

            vector_index = ChromaIndex(chroma_persist_dir or Path(".ash/chroma"))
        else:
            vector_index = InMemoryVectorIndex()
        if memory_backend == "fts5":
            from memory import FTS5FallbackIndex

            base = chroma_persist_dir or Path(".ash/chroma")
            lexical_index = FTS5FallbackIndex(db_path=base.parent / "memory-fts5.db")
        self._vector_pipeline = VectorSearchPipeline(
            adapter=adapter,
            vector_index=vector_index,
            lexical_index=lexical_index,
            vector_enabled=memory_backend != "fts5",
        )

    async def index_file_for_memory(self, file_path: Path) -> None:
        """Index a file into the semantic memory pipeline."""
        if self._vector_pipeline is None:
            return

        chunks = self._chunk_file(file_path)
        await self._vector_pipeline.index_chunks(chunks, str(file_path))

    async def semantic_search(self, query: str, top_k: int = 5) -> list["VectorHit"]:
        """Search semantic memory for relevant context."""
        if self._vector_pipeline is None:
            return []
        hits, _ = await self._vector_pipeline.search(query, top_k=top_k)
        return hits

    def _chunk_file(self, file_path: Path) -> list["Chunk"]:
        """Split a file into memory-indexable chunks."""

        from context.compaction import Chunk

        content = file_path.read_text(errors="replace")
        lines = content.splitlines()
        chunks: list[Chunk] = []
        for i in range(0, len(lines), 50):
            chunk_lines = lines[i : i + 50]
            chunks.append(
                Chunk(
                    file_path=str(file_path),
                    start_line=i + 1,
                    end_line=i + len(chunk_lines),
                    content="\n".join(chunk_lines),
                )
            )
        return chunks

    # --- message building ---------------------------------------------------

    def _build_messages(
        self,
        session: Session,
        *,
        force_compaction: bool = False,
    ) -> list[dict[str, Any]]:
        """Build the messages payload for the provider."""

        system_content = self.system_prompt
        repo_section = ""
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

        memory_section = ""
        if self._pending_memory_context:
            memory_section = f"## Relevant Context\n{self._pending_memory_context}"

        if self._config is not None:
            from context.history import ContextBudgetAllocator, HistoryCompactor

            maximum_context = min(
                self._config.max_context_tokens,
                _provider_capabilities(self.provider).context_window
                or self._config.max_context_tokens,
            )
            allocator = ContextBudgetAllocator(
                max_context_tokens=maximum_context,
                completion_reserve=self._config.max_completion_tokens,
                weights=self._config.context_budget_weights,
            )
            budget_limits = allocator.allocate()
            budget_usage: dict[str, int] = {}
            truncated: set[str] = set()

            tool_schema_tokens = self._estimate_tool_schema_tokens()
            budget_usage["tools"] = tool_schema_tokens

            system_fit = allocator.fit_text(
                system_content,
                limit=budget_limits["system"],
                count_tokens=self.provider.count_tokens,
            )
            budget_usage["system"] = system_fit.tokens
            if system_fit.truncated:
                truncated.add("system")
            system_parts = [system_fit.text]

            if repo_section:
                repo_fit = allocator.fit_text(
                    repo_section,
                    limit=budget_limits["repo_map"],
                    count_tokens=self.provider.count_tokens,
                )
                budget_usage["repo_map"] = repo_fit.tokens
                if repo_fit.truncated:
                    truncated.add("repo_map")
                system_parts.append(repo_fit.text)
            else:
                budget_usage["repo_map"] = 0

            if memory_section:
                memory_fit = allocator.fit_text(
                    memory_section,
                    limit=budget_limits["memory"],
                    count_tokens=self.provider.count_tokens,
                )
                budget_usage["memory"] = memory_fit.tokens
                if memory_fit.truncated:
                    truncated.add("memory")
                system_parts.append(memory_fit.text)
            else:
                budget_usage["memory"] = 0

            system_content = "\n\n".join(part for part in system_parts if part)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_content}
            ]
            for message in session.messages:
                msg_dict: dict[str, Any] = {
                    "role": message.role,
                    "content": message.content,
                }
                if message.role == "assistant" and message.metadata.get("tool_calls"):
                    msg_dict["tool_calls"] = message.metadata["tool_calls"]
                # OpenAI requires tool_call_id on role=tool messages.
                if message.role == "tool" and message.metadata.get("call_id"):
                    msg_dict["tool_call_id"] = message.metadata["call_id"]
                messages.append(msg_dict)

            reserved_tokens = (
                budget_usage["system"]
                + budget_usage["repo_map"]
                + budget_usage["memory"]
                + budget_usage["tools"]
            )
            provider_input_limit = max(1, allocator.input_limit - tool_schema_tokens)
            compactor = HistoryCompactor(
                max_context_tokens=maximum_context,
                completion_reserve=self._config.max_completion_tokens,
                threshold=self._config.context_compaction_threshold,
                recent_messages=self._config.context_recent_messages,
                max_tool_output_chars=self._config.max_tool_result_tokens * 4,
                input_token_limit=provider_input_limit,
            )
            result = compactor.compact(
                messages,
                count_tokens=self.provider.count_tokens,
                previous_summary=session.context_summary,
                force=force_compaction,
            )
            self._last_context_tokens = result.estimated_tokens
            budget_usage["history"] = max(0, result.estimated_tokens - reserved_tokens)
            if budget_usage["history"] > budget_limits["history"]:
                truncated.add("history")
            self._last_context_budget = allocator.report(
                limits=budget_limits,
                usage=budget_usage,
                truncated=truncated,
            )
            if self.turn_context is not None:
                self.turn_context.set("context_budget", self._last_context_budget)
            self.ui.update_token_count(
                result.estimated_tokens,
                max(1, maximum_context - self._config.max_completion_tokens),
            )
            if result.compacted and result.summary != session.context_summary:
                session.context_summary = result.summary
                self.session_store.save_context_summary(
                    session.session_id,
                    result.summary,
                )
            return result.messages
        messages = [{"role": "system", "content": system_content}]
        if repo_section:
            messages[0]["content"] = f"{messages[0]['content']}\n\n{repo_section}"
        if memory_section:
            messages[0]["content"] = f"{messages[0]['content']}\n\n{memory_section}"
        for message in session.messages:
            msg_dict = {
                "role": message.role,
                "content": message.content,
            }
            if message.role == "assistant" and message.metadata.get("tool_calls"):
                msg_dict["tool_calls"] = message.metadata["tool_calls"]
            if message.role == "tool" and message.metadata.get("call_id"):
                msg_dict["tool_call_id"] = message.metadata["call_id"]
            messages.append(msg_dict)
        return messages

    def compact_current_context(self) -> tuple[int, bool]:
        """Force compaction for the active session and return token estimate."""

        if self.current_session is None:
            raise RuntimeError("No active session")
        before = self.current_session.context_summary
        self._build_messages(self.current_session, force_compaction=True)
        return self._last_context_tokens, self.current_session.context_summary != before

    # --- provider switching -------------------------------------------------

    def switch_provider(self, provider: str, model: str) -> None:
        """Switch to a different provider and model. Rebuilds provider instance."""
        from ash.cli import _build_provider  # lazy import to avoid circular

        if self._config is None:
            raise RuntimeError("AshLoop was not constructed with a config object")

        model_str = f"{provider}/{model}"
        new_config = self._config.model_copy(update={"model": model_str})
        self.provider = _build_provider(new_config)
        self._config = new_config
        # Re-configure skills runtime with new provider.
        if self.tools_registry is not None:
            from tools.skills import configure_runtime

            registry = self.tools_registry
            configure_runtime(
                tools_provider=lambda: list(registry.as_dict().values()),
                root_provider=lambda: self.project_root,
            )

    def switch_model(self, model: str) -> None:
        """Switch to a model string. If model contains '/', treat as provider/model.
        Otherwise, prepend the current provider."""
        from ash.cli import _build_provider  # lazy import to avoid circular

        if self._config is None:
            raise RuntimeError("AshLoop was not constructed with a config object")

        if "/" in model:
            # Full provider/model string
            new_config = self._config.model_copy(update={"model": model})
        else:
            # Model-only — prepend current provider
            current_provider = self._config.model.split("/", 1)[0]
            new_config = self._config.model_copy(
                update={"model": f"{current_provider}/{model}"}
            )
        self.provider = _build_provider(new_config)
        self._config = new_config


MAX_RETRIES = 2
BASE_DELAY_SECONDS = 1.0


async def _execute_with_retry(
    tool: "BaseTool",
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    last_error: str | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            tool_result: "ToolResult" = await tool.run(**arguments)
            return {
                "success": tool_result.success,
                "output": tool_result.output,
                "error": tool_result.error,
                "truncated": tool_result.truncated,
                "token_count": tool_result.token_count,
            }
        except Exception as exc:
            last_error = str(exc)
            if attempt < MAX_RETRIES:
                delay = BASE_DELAY_SECONDS * (2**attempt)
                await asyncio.sleep(delay)
            continue
    return {
        "success": False,
        "output": "",
        "error": f"Failed after {MAX_RETRIES + 1} attempts: {last_error}",
    }
