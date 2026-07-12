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
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    ContextManager,
    Protocol,
    Sequence,
)
from uuid import uuid4

from core.recovery import CircuitBreaker, CircuitBreakerError
from core.events import EventContext, envelope_event
from core.session import (
    AuditAction,
    AuditResult,
    Message,
    Session,
    SessionStore,
    ToolCallRecord,
    normalize_project_path,
)
from core.redaction import redact_text, redact_value
from ash_logging import get_logger
from mcp.server import MCPServerConfig, load_mcp_servers
from providers.base import ProviderABC, TokenCounterLike
from providers.capabilities import ProviderCapabilities
from providers.messages import CanonicalToolCall, normalize_messages
from providers.retry import (
    ProviderCircuitBreaker,
    classify_provider_failure,
    retry_delay,
)
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
    from hooks.registry import HookEvent
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
PlanApprovalCallback = Callable[["SprintExecution"], Awaitable[bool]]


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
REPO_MAP_FILE_TOOLS = {*FILE_WRITE_TOOLS, "read_file"}
MAX_ACTIVE_REPO_FILES = 20


def _provider_capabilities(provider: Any) -> ProviderCapabilities:
    capabilities = getattr(provider, "capabilities", None)
    return (
        capabilities
        if isinstance(capabilities, ProviderCapabilities)
        else ProviderCapabilities()
    )


def _provider_circuit_key(provider: ProviderABC) -> str:
    nested = getattr(provider, "providers", None)
    if isinstance(nested, list) and nested:
        identities = [
            f"{getattr(item, 'provider_family', 'custom')}/{item.model_name}"
            for item in nested
        ]
        return "failover:" + ",".join(identities)
    return f"{getattr(provider, 'provider_family', 'custom')}/{provider.model_name}"


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


def _normalize_native_tool_call(
    call: CanonicalToolCall | dict[str, Any],
) -> dict[str, Any]:
    """Convert a provider-native tool call into Ash's canonical shape."""

    canonical = (
        call if isinstance(call, CanonicalToolCall) else CanonicalToolCall.model_validate(call)
    )
    return canonical.to_wire()


def _audit_action_for_tool(tool_name: str) -> AuditAction:
    if tool_name == "run_command":
        return "command_run"
    if tool_name in FILE_WRITE_TOOLS:
        return "file_write"
    return "tool_call"


def _canonical_message_content(message: Message) -> Any:
    image_blocks = message.metadata.get("image_blocks")
    if message.role != "user" or not isinstance(image_blocks, list):
        return message.content
    return [
        {"type": "text", "text": message.content},
        *image_blocks,
    ]


def _calculate_turn_cost(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    pricing: dict[str, float],
) -> float:
    """Calculate configured cost while avoiding double-charging cached input."""

    prompt = max(0, prompt_tokens)
    cache_read = min(max(0, cache_read_tokens), prompt)
    cache_write = min(max(0, cache_write_tokens), prompt - cache_read)
    uncached = prompt - cache_read - cache_write
    input_rate = float(pricing.get("input", 0.0))
    return (
        uncached * input_rate
        + cache_read * float(pricing.get("cache_read", input_rate))
        + cache_write * float(pricing.get("cache_write", input_rate))
        + max(0, completion_tokens) * float(pricing.get("output", 0.0))
    ) / 1_000_000


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
        provider_circuit_breaker: ProviderCircuitBreaker | None = None,
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
        on_plan_approval: PlanApprovalCallback | None = None,
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
        mcp_configs: dict[str, MCPServerConfig] | None = None,
        config: "AshConfig | None" = None,
        max_steering_messages: int = 20,
    ) -> None:
        self.session_store = session_store
        self.provider = provider
        self.safety_guard = safety_guard
        self.ui = ui
        self.project_root = project_root
        self.tools: dict[str, BaseTool] = dict(tools or {})
        self._started_tool_ids: set[int] = set()
        self._plugin_tool_names = {
            name
            for name, tool in self.tools.items()
            if bool(getattr(tool, "plugin_runtime_tool", False))
        }
        self._pending_runtime_events: list[dict[str, Any]] = []
        self._pending_runtime_event_ids: set[str] = set()
        set_event_enricher = getattr(self.ui, "set_event_enricher", None)
        if callable(set_event_enricher):
            set_event_enricher(self._envelope_event)
        for tool in self.tools.values():
            tool.set_event_sink(self._emit_event)
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.system_prompt = system_prompt or _default_system_prompt(project_root)
        if additional_instructions:
            self.system_prompt = f"{self.system_prompt}\n\n{additional_instructions}"
        self._base_system_prompt = self.system_prompt
        self.token_counter = token_counter
        self.max_turn_iterations = max_turn_iterations
        if max_steering_messages < 1:
            raise ValueError("max_steering_messages must be at least 1")
        self.max_steering_messages = max_steering_messages
        self._steering_messages: deque[str] = deque()
        self._turn_running = False
        self.repo_map = repo_map
        self.auto_commit = auto_commit
        self.auto_commit_paths = list(auto_commit_paths or [])
        self._turn_modified_paths: set[Path] = set()
        self._repo_map_active_files: list[Path] = []
        for path in self.auto_commit_paths:
            candidate = path if path.is_absolute() else self.project_root / path
            self._remember_repo_file(candidate)
        self._repo_map_dirty = False
        self.planner = planner
        self.enable_sprint_planning = enable_sprint_planning
        self.tool_middlewares: list[ToolMiddleware] = list(tool_middlewares or [])
        self.on_tool_approval = on_tool_approval
        self.on_plan_approval = on_plan_approval
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
        self.provider_circuit_breaker = (
            provider_circuit_breaker
            or ProviderCircuitBreaker(
                failure_threshold=int(
                    getattr(config, "provider_circuit_failure_threshold", 5)
                ),
                cooldown_seconds=float(
                    getattr(config, "provider_circuit_cooldown_seconds", 30.0)
                ),
            )
        )
        self._provider_circuit_key = _provider_circuit_key(provider)
        self._last_context_tokens = 0
        self._last_context_budget: Any | None = None
        self._last_turn_prompt_tokens = 0
        self._last_turn_completion_tokens = 0
        self._last_cache_read_tokens = 0
        self._last_cache_write_tokens = 0
        self._last_estimated_prompt_tokens = 0
        self._last_estimated_completion_tokens = 0
        self._last_usage_source = "unavailable"
        self._last_turn_cost_usd = 0.0
        self._last_estimated_cost_usd = 0.0
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
        self.recovery_summary: Any | None = None
        self._mcp_runtime: Any | None = None
        self._mcp_tool_names: set[str] = set()
        self._mcp_configs = dict(mcp_configs or {})
        self._hook_session_open = False
        if mcp_config_path is not None and mcp_config_path.exists():
            loaded_mcp_configs = load_mcp_servers(mcp_config_path)
            duplicates = self._mcp_configs.keys() & loaded_mcp_configs.keys()
            if duplicates:
                raise ValueError(
                    "duplicate MCP server name(s): " + ", ".join(sorted(duplicates))
                )
            self._mcp_configs.update(loaded_mcp_configs)

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

        await self._fire_session_end("shutdown")
        self._flush_runtime_events()
        if self._mcp_runtime is not None:
            await self._mcp_runtime.close()
            self._mcp_runtime = None
        self._mcp_tool_names.clear()
        await asyncio.gather(
            *(tool.aclose() for tool in self.tools.values()),
            return_exceptions=True,
        )
        await self.provider.aclose()

    async def _fire_session_end(self, reason: str) -> None:
        hooks = self._active_hooks()
        if (
            not self._hook_session_open
            or hooks is None
            or self.current_session is None
        ):
            return
        self._hook_session_open = False
        await hooks.fire_lifecycle(
            "session_end",
            {
                "session_id": self.current_session.session_id,
                "reason": reason,
            },
        )

    def _active_hooks(self) -> "HookRegistry | None":
        if self.permission_policy.mode.value == "dry_run":
            return None
        return self.hooks

    async def _fire_hook_lifecycle(
        self, event: "HookEvent", payload: dict[str, Any]
    ) -> None:
        hooks = self._active_hooks()
        if hooks is not None:
            await hooks.fire_lifecycle(event, payload)

    def _envelope_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = getattr(self, "current_session", None)
        turn_context = getattr(self, "turn_context", None)
        call_id = payload.get("call_id")
        if call_id is None and turn_context is not None:
            call_id = turn_context.get("tool_call_id")
        event = envelope_event(
            payload,
            context=EventContext(
                session_id=session.session_id if session is not None else None,
                turn_id=(
                    turn_context.turn_id if turn_context is not None else None
                ),
                operation_id=str(call_id) if call_id else None,
            ),
        )
        session_id = event.get("session_id")
        event_id = str(event["event_id"])
        if (
            isinstance(session_id, str)
            and session_id
            and event_id not in self._pending_runtime_event_ids
        ):
            self._pending_runtime_events.append(event)
            self._pending_runtime_event_ids.add(event_id)
            if len(self._pending_runtime_events) >= 64:
                self._flush_runtime_events()
        return event

    def _emit_event(self, payload: dict[str, Any]) -> None:
        event = self._envelope_event(payload)
        self.ui.emit_event(event)
        if event["type"] in {"turn.completed", "turn.cancelled", "turn.error"}:
            self._flush_runtime_events()

    def _flush_runtime_events(self) -> None:
        if not self._pending_runtime_events:
            return
        events = self._pending_runtime_events
        self.session_store.save_runtime_events(events)
        self._pending_runtime_events = []
        self._pending_runtime_event_ids.clear()

    # --- session lifecycle ------------------------------------------------

    async def start_session(self, session_id: str | None = None) -> Session:
        """Create a new session or restore one by id."""

        if self._mcp_configs and self._mcp_runtime is None:
            await self._start_mcp_runtime()
        search_tool = self.tools.get("search_tools")
        reset_activations = getattr(search_tool, "reset_activations", None)
        if callable(reset_activations):
            reset_activations()

        if self.current_session is not None and self._hook_session_open:
            reason = (
                "reload" if session_id == self.current_session.session_id else "switch"
            )
            await self._fire_session_end(reason)
            self.current_session = None
        self.system_prompt = self._base_system_prompt

        if session_id is not None:
            self.current_session = self.session_store.load_session(session_id)
            if normalize_project_path(
                self.current_session.project_path
            ) != normalize_project_path(self.project_root):
                self.current_session = None
                raise ValueError("session belongs to a different workspace")
            from core.checkpoints import recover_interrupted_turns

            self.recovery_summary = recover_interrupted_turns(
                self.session_store,
                self.safety_guard,
                session_id,
            )
            self.recovered_turns = self.recovery_summary.interrupted_turns
            if self.recovered_turns:
                self._emit_event(
                    {
                        "type": "session.recovery",
                        **self.recovery_summary.to_dict(),
                    }
                )
            hooks = self._active_hooks()
            self._hook_session_open = hooks is not None
            if hooks is not None:
                await hooks.fire_session_start(
                    {
                        "session_id": self.current_session.session_id,
                        "source": "resume",
                        "project_path": self.current_session.project_path,
                        "model": self.provider.model_name,
                    }
                )
                injected = hooks.get_injected_prompt()
                if injected:
                    self.system_prompt = f"{self.system_prompt}\n\n{injected}"
            await self._start_runtime_tools()
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

        session = self.session_store.create_session(
            str(self.project_root), model=self.provider.model_name
        )
        self.current_session = session
        hooks = self._active_hooks()
        self._hook_session_open = hooks is not None
        if hooks is not None:
            await hooks.fire_session_start(
                {
                    "session_id": session.session_id,
                    "source": "new",
                    "project_path": session.project_path,
                    "model": self.provider.model_name,
                }
            )
            injected = hooks.get_injected_prompt()
            if injected:
                self.system_prompt = f"{self.system_prompt}\n\n{injected}"
        await self._start_runtime_tools()
        return session

    async def _start_runtime_tools(self) -> None:
        for tool in self.tools.values():
            identity = id(tool)
            if identity in self._started_tool_ids:
                continue
            await tool.start()
            self._started_tool_ids.add(identity)

    async def reload_mcp_servers(
        self, configs: dict[str, MCPServerConfig]
    ) -> dict[str, str]:
        if self._mcp_runtime is not None:
            await self._mcp_runtime.close()
            self._mcp_runtime = None
        for name in self._mcp_tool_names:
            self.tools.pop(name, None)
        self._mcp_tool_names.clear()
        self._mcp_configs = dict(configs)
        if self.current_session is not None and self._mcp_configs:
            await self._start_mcp_runtime()
        return dict(self._mcp_runtime.errors) if self._mcp_runtime is not None else {}

    async def reload_plugin_runtime_tools(self, tools: Sequence["BaseTool"]) -> None:
        """Atomically replace executable plugin proxies and stop their old hosts."""

        next_tools = {tool.name: tool for tool in tools}
        if len(next_tools) != len(tools):
            raise ValueError("duplicate executable plugin tool name")
        occupied = self.tools.keys() - self._plugin_tool_names
        duplicates = occupied & next_tools.keys()
        if duplicates:
            await asyncio.gather(
                *(tool.aclose() for tool in tools),
                return_exceptions=True,
            )
            raise ValueError(
                "plugin tool collides with an existing tool: "
                + ", ".join(sorted(duplicates))
            )
        old_tools = [
            self.tools.pop(name)
            for name in self._plugin_tool_names
            if name in self.tools
        ]
        await asyncio.gather(
            *(tool.aclose() for tool in old_tools),
            return_exceptions=True,
        )
        for tool in next_tools.values():
            tool.set_event_sink(self._emit_event)
        self.tools.update(next_tools)
        self._plugin_tool_names = set(next_tools)

    async def _start_mcp_runtime(self) -> None:
        from mcp.runtime import MCPRuntime

        self._mcp_runtime = MCPRuntime(self._mcp_configs, self.safety_guard)
        tools = await self._mcp_runtime.start()
        duplicates = self.tools.keys() & tools.keys()
        if duplicates:
            await self._mcp_runtime.close()
            self._mcp_runtime = None
            raise ValueError(
                "MCP tool collides with an existing tool: "
                + ", ".join(sorted(duplicates))
            )
        for tool in tools.values():
            tool.set_event_sink(self._emit_event)
        self.tools.update(tools)
        self._mcp_tool_names = set(tools)
        for name, error in self._mcp_runtime.errors.items():
            _log.warning("MCP server %s unavailable: %s", name, error)

    # --- the main turn ----------------------------------------------------

    @property
    def is_turn_running(self) -> bool:
        return self._turn_running

    @property
    def last_turn_usage(self) -> dict[str, int | float | str | bool]:
        prompt = self._last_turn_prompt_tokens
        has_estimates = self._last_usage_source in {"estimated", "mixed"}
        return {
            "prompt_tokens": prompt,
            "completion_tokens": self._last_turn_completion_tokens,
            "cache_read_tokens": self._last_cache_read_tokens,
            "cache_write_tokens": self._last_cache_write_tokens,
            "usage_source": self._last_usage_source,
            "estimated_prompt_tokens": self._last_estimated_prompt_tokens,
            "estimated_completion_tokens": self._last_estimated_completion_tokens,
            "has_estimates": has_estimates,
            "cache_hit_rate": (
                self._last_cache_read_tokens / prompt if prompt else 0.0
            ),
            "cost_usd": self._last_turn_cost_usd,
            "estimated_cost_usd": self._last_estimated_cost_usd,
            "cost_is_estimated": self._last_estimated_cost_usd > 0,
        }

    async def run_turn(
        self,
        user_input: str,
        *,
        user_metadata: dict[str, Any] | None = None,
    ) -> str:
        """Run one turn while preventing unsafe concurrent session mutation."""

        if self._turn_running:
            raise RuntimeError("a turn is already running")
        previous_turn_id = self.turn_context.turn_id if self.turn_context else None
        self._turn_running = True
        try:
            return await self._run_turn(user_input, user_metadata=user_metadata)
        except asyncio.CancelledError:
            current_turn_id = self.turn_context.turn_id if self.turn_context else None
            if current_turn_id is not None and current_turn_id != previous_turn_id:
                try:
                    from core.checkpoints import recover_interrupted_turns

                    if self.current_session is None:
                        raise RuntimeError("cancelled turn has no active session")
                    self.recovery_summary = recover_interrupted_turns(
                        self.session_store,
                        self.safety_guard,
                        self.current_session.session_id,
                    )
                    self.recovered_turns = self.recovery_summary.interrupted_turns
                except Exception as exc:  # noqa: BLE001 - preserve cancellation semantics
                    _log.warning(
                        "cancel recovery failed for {}: {}", current_turn_id, exc
                    )
                    self.session_store.interrupt_turn(current_turn_id)
            discarded = len(self._steering_messages)
            self._steering_messages.clear()
            self._emit_event(
                {
                    "type": "turn.cancelled",
                    "discarded_steering": discarded,
                }
            )
            await self._fire_hook_lifecycle(
                "turn_end",
                {
                    "session_id": (
                        self.current_session.session_id
                        if self.current_session is not None
                        else None
                    ),
                    "turn_id": current_turn_id,
                    "status": "cancelled",
                },
            )
            raise
        except Exception as exc:
            current_turn_id = self.turn_context.turn_id if self.turn_context else None
            if current_turn_id is not None and current_turn_id != previous_turn_id:
                self.session_store.interrupt_turn(current_turn_id)
                self._emit_event({"type": "turn.error", "error": redact_text(str(exc))})
            payload = {
                "session_id": (
                    self.current_session.session_id
                    if self.current_session is not None
                    else None
                ),
                "turn_id": current_turn_id,
                "error": redact_text(str(exc)),
            }
            await self._fire_hook_lifecycle("turn_error", payload)
            await self._fire_hook_lifecycle("turn_end", {**payload, "status": "error"})
            raise
        finally:
            self._flush_runtime_events()
            self._turn_running = False

    async def _run_turn(
        self,
        user_input: str,
        *,
        user_metadata: dict[str, Any] | None = None,
    ) -> str:
        """Run a single user turn to completion and return the final text."""

        _log.info("turn started")
        if self.current_session is None:
            await self.start_session()
        if self.current_session is None:
            raise RuntimeError("start_session() returned None")
        session = self.current_session

        from context.turn import TurnContext

        self._turn_modified_paths = set()
        self.turn_context = TurnContext(
            session_id=session.session_id,
            turn_id=str(uuid4()),
        )
        self.session_store.start_turn(
            session.session_id, self.turn_context.turn_id, user_input
        )
        self._emit_event({"type": "turn.started"})
        await self._fire_hook_lifecycle(
            "turn_start",
            {
                "session_id": session.session_id,
                "turn_id": self.turn_context.turn_id,
                "input": redact_text(user_input),
            },
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
                approved = (
                    await self.on_plan_approval(execution)
                    if self.on_plan_approval is not None
                    else self.ui.show_plan(execution)
                )
                if not approved:
                    execution.abort("rejected by user")
                    self.session_store.save_sprint(session.session_id, execution)
                    self.session_store.complete_turn(self.turn_context.turn_id)
                    response = (
                        f"Plan rejected. Sprint {execution.contract.contract_id[:8]} aborted; "
                        "no further actions taken."
                    )
                    self._emit_event(
                        {
                            "type": "turn.completed",
                            "response": response,
                            "model": self.provider.model_name,
                            "context_tokens": self._last_context_tokens,
                            "usage": self.last_turn_usage,
                        }
                    )
                    await self._fire_hook_lifecycle(
                        "turn_end",
                        {
                            "session_id": session.session_id,
                            "turn_id": self.turn_context.turn_id,
                            "status": "completed",
                            "response": response,
                            "usage": self.last_turn_usage,
                        },
                    )
                    return response
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
            metadata=dict(user_metadata or {}),
        )
        persisted_metadata = dict(user_message.metadata)
        persisted_metadata.pop("image_blocks", None)
        self.session_store.save_message(
            session.session_id,
            user_message.model_copy(
                update={
                    "content": redact_text(user_message.content),
                    "metadata": redact_value(persisted_metadata),
                }
            ),
            turn_id=self.turn_context.turn_id,
        )
        # Keep the in-memory session mirror in sync so subsequent
        # _build_messages() calls in the same turn see the history.
        session.messages.append(user_message)

        # 2. Stream/execute loop bounded by max_turn_iterations.
        final_text = ""
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_cache_read_tokens = 0
        total_cache_write_tokens = 0
        total_estimated_prompt_tokens = 0
        total_estimated_completion_tokens = 0
        usage_sources: set[str] = set()
        iteration = 0
        iteration_budget = self.max_turn_iterations
        maximum_iteration_budget = self.max_turn_iterations + self.max_steering_messages
        while iteration < iteration_budget:
            iteration += 1
            self._drain_steering_messages(session)
            # Optionally search semantic memory and inject relevant context.
            self._pending_memory_context = ""
            if self.enable_semantic_memory and self._vector_pipeline is not None:
                hits = await self.semantic_search(user_input, top_k=3)
                if hits:
                    self._pending_memory_context = "\n\n".join(
                        f"// From {hit.file_path}:\n{hit.content[:500]}" for hit in hits
                    )
            messages = self._build_messages(session)
            await self._fire_hook_lifecycle(
                "pre_model",
                {
                    "session_id": session.session_id,
                    "turn_id": self.turn_context.turn_id,
                    "model": self.provider.model_name,
                    "iteration": iteration,
                    "message_count": len(messages),
                    "tool_count": len(self.tools),
                },
            )
            try:
                (
                    assistant_text,
                    tool_calls,
                    turn_prompt_tokens,
                    turn_completion_tokens,
                    turn_cache_read_tokens,
                    turn_cache_write_tokens,
                    turn_usage_source,
                ) = await self._stream_one_completion(messages)
            except asyncio.CancelledError:
                await self._fire_hook_lifecycle(
                    "post_model",
                    {
                        "session_id": session.session_id,
                        "turn_id": self.turn_context.turn_id,
                        "model": self.provider.model_name,
                        "iteration": iteration,
                        "status": "cancelled",
                    },
                )
                raise
            except Exception as exc:
                await self._fire_hook_lifecycle(
                    "post_model",
                    {
                        "session_id": session.session_id,
                        "turn_id": self.turn_context.turn_id,
                        "model": self.provider.model_name,
                        "iteration": iteration,
                        "status": "error",
                        "error": redact_text(str(exc)),
                    },
                )
                raise
            await self._fire_hook_lifecycle(
                "post_model",
                {
                    "session_id": session.session_id,
                    "turn_id": self.turn_context.turn_id,
                    "model": self.provider.model_name,
                    "iteration": iteration,
                    "status": "completed",
                    "response": redact_text(assistant_text),
                    "tool_call_count": len(tool_calls),
                    "prompt_tokens": turn_prompt_tokens,
                    "completion_tokens": turn_completion_tokens,
                    "cache_read_tokens": turn_cache_read_tokens,
                    "cache_write_tokens": turn_cache_write_tokens,
                    "usage_source": turn_usage_source,
                },
            )
            total_prompt_tokens += turn_prompt_tokens
            total_completion_tokens += turn_completion_tokens
            total_cache_read_tokens += turn_cache_read_tokens
            total_cache_write_tokens += turn_cache_write_tokens
            usage_sources.add(turn_usage_source)
            if turn_usage_source == "estimated":
                total_estimated_prompt_tokens += turn_prompt_tokens
                total_estimated_completion_tokens += turn_completion_tokens

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
                turn_id=self.turn_context.turn_id,
            )
            session.messages.append(assistant_message)

            if not tool_calls:
                final_text = assistant_text
                if self._steering_messages:
                    iteration_budget = min(
                        maximum_iteration_budget,
                        iteration_budget + 1,
                    )
                    continue
                if (
                    self.continuous_mode
                    and self._continuous_turns < self.max_continuous_turns
                ):
                    self._continuous_turns += 1
                    follow_up = "Continue the previous task. What is the next step?"
                    self.session_store.complete_turn(self.turn_context.turn_id)
                    await self._fire_hook_lifecycle(
                        "turn_end",
                        {
                            "session_id": session.session_id,
                            "turn_id": self.turn_context.turn_id,
                            "status": "continued",
                            "response": redact_text(assistant_text),
                        },
                    )
                    return await self._run_turn(follow_up)
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
                self.session_store.save_message(
                    session.session_id,
                    tool_message,
                    turn_id=self.turn_context.turn_id,
                )
                session.messages.append(tool_message)

            if self._steering_messages and iteration >= iteration_budget:
                iteration_budget = min(
                    maximum_iteration_budget,
                    iteration_budget + 1,
                )

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
        cache_read = int(total_cache_read_tokens) if total_cache_read_tokens else 0
        cache_write = int(total_cache_write_tokens) if total_cache_write_tokens else 0
        estimated_prompt = int(total_estimated_prompt_tokens)
        estimated_completion = int(total_estimated_completion_tokens)
        if not usage_sources:
            usage_source = "unavailable"
        elif len(usage_sources) == 1:
            usage_source = next(iter(usage_sources))
        else:
            usage_source = "mixed"
        pricing: dict[str, float] = {}
        if self._config is not None:
            pricing = self._config.model_pricing_usd_per_million.get(
                self._config.model, {}
            )
        turn_cost_usd = _calculate_turn_cost(
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            pricing=pricing,
        )
        estimated_cost_usd = _calculate_turn_cost(
            prompt_tokens=estimated_prompt,
            completion_tokens=estimated_completion,
            cache_read_tokens=0,
            cache_write_tokens=0,
            pricing=pricing,
        )
        self._last_turn_prompt_tokens = prompt
        self._last_turn_completion_tokens = completion
        self._last_cache_read_tokens = cache_read
        self._last_cache_write_tokens = cache_write
        self._last_estimated_prompt_tokens = estimated_prompt
        self._last_estimated_completion_tokens = estimated_completion
        self._last_usage_source = usage_source
        self._last_turn_cost_usd = turn_cost_usd
        self._last_estimated_cost_usd = estimated_cost_usd
        usage_payload = self.last_turn_usage
        self.turn_context.set("usage", usage_payload)
        self.session_store.save_turn_usage(self.turn_context.turn_id, usage_payload)
        if prompt > 0 or completion > 0:
            self._emit_event({"type": "turn.usage", **usage_payload})
            self.session_store.save_session_token_stats(
                session.session_id,
                prompt,
                completion,
                turn_cost_usd,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                estimated_prompt_tokens=estimated_prompt,
                estimated_completion_tokens=estimated_completion,
                estimated_cost_usd=estimated_cost_usd,
            )

        if self.auto_commit:
            commit_paths = self.auto_commit_paths or sorted(self._turn_modified_paths)
            if commit_paths:
                commit_result = await auto_commit_turn(
                    self.project_root,
                    message=f"ash: turn complete ({len(final_text)} chars)",
                    paths=commit_paths,
                    safety_guard=self.safety_guard,
                    environment_allowlist=getattr(
                        self._config, "command_env_allowlist", ()
                    ),
                )
                if not commit_result.success and commit_result.error:
                    # Surface commit failures to the user but don't fail the turn.
                    self.ui.console.print(f"auto_commit failed: {commit_result.error}")

        _log.info(f"turn complete, {len(final_text)} chars returned")
        self.session_store.complete_turn(self.turn_context.turn_id)
        self._emit_event(
            {
                "type": "turn.completed",
                "response": final_text,
                "model": self.provider.model_name,
                "context_tokens": self._last_context_tokens,
                "usage": self.last_turn_usage,
            }
        )
        await self._fire_hook_lifecycle(
            "turn_end",
            {
                "session_id": session.session_id,
                "turn_id": self.turn_context.turn_id,
                "status": "completed",
                "response": redact_text(final_text),
                "usage": self.last_turn_usage,
            },
        )
        return final_text

    @property
    def pending_steering_count(self) -> int:
        return len(self._steering_messages)

    def queue_steering(self, message: str) -> int:
        """Queue user guidance for the next safe model-iteration boundary."""

        normalized = message.strip()
        if not normalized:
            raise ValueError("steering message cannot be empty")
        if len(self._steering_messages) >= self.max_steering_messages:
            raise OverflowError(
                f"steering queue is full ({self.max_steering_messages} messages)"
            )
        self._steering_messages.append(normalized)
        self._emit_event(
            {
                "type": "turn.steering.queued",
                "pending": len(self._steering_messages),
            }
        )
        return len(self._steering_messages)

    def _drain_steering_messages(self, session: Session) -> int:
        applied = 0
        while self._steering_messages:
            content = self._steering_messages.popleft()
            message = Message(
                role="user",
                content=content,
                timestamp=_utc_now(),
                metadata={"steering": True},
            )
            self.session_store.save_message(
                session.session_id,
                message.model_copy(update={"content": redact_text(content)}),
                turn_id=self.turn_context.turn_id if self.turn_context else None,
            )
            session.messages.append(message)
            applied += 1
        if applied:
            self._emit_event(
                {
                    "type": "turn.steering.applied",
                    "count": applied,
                    "pending": 0,
                }
            )
        return applied

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
            schema = tool.json_schema() if hasattr(tool, "json_schema") else {}
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

    def _provider_tools(self) -> dict[str, BaseTool]:
        search_tool = self.tools.get("search_tools")
        visible_tools = getattr(search_tool, "visible_tools", None)
        if callable(visible_tools):
            return dict(visible_tools(self.tools))
        return self.tools

    def _estimate_tool_schema_tokens(self) -> int:
        """Estimate tool declaration tokens reserved outside chat messages."""

        payload = self._tool_schema_payload()
        if not payload:
            return 0
        return max(0, int(self.provider.count_tokens(json.dumps(payload, default=str))))

    def _tool_schema_payload(self) -> list[dict[str, Any]]:
        """Return the exact provider-facing representation used for budgeting."""

        provider_tools = self._provider_tools()
        if not provider_tools:
            return []
        if _provider_capabilities(self.provider).native_tools:
            return self._tools_to_openai_format(provider_tools)
        return [
            {"name": tool.name, "description": getattr(tool, "description", "")}
            for tool in provider_tools.values()
        ]

    async def _stream_one_completion(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], int, int, int, int, str]:
        """Stream one completion with normalized token and cache usage."""

        canonical_messages = normalize_messages(messages)
        # Build OpenAI-format tools list for providers that support native tool_calls.
        provider_tools = self._provider_tools()
        openai_tools = (
            self._tools_to_openai_format(provider_tools)
            if provider_tools and _provider_capabilities(self.provider).native_tools
            else None
        )

        parser = StreamingXMLParser()
        text_chunks: list[str] = []
        response_fragments: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        prompt_tokens = 0
        completion_tokens = 0
        cache_read_tokens = 0
        cache_write_tokens = 0
        usage_source = "unavailable"
        native_tool_calls_from_api: list[CanonicalToolCall] = []
        maximum_attempts = int(getattr(self._config, "provider_max_attempts", 3))
        retry_base_delay = float(
            getattr(self._config, "provider_retry_base_delay", 0.5)
        )
        retry_max_delay = float(getattr(self._config, "provider_retry_max_delay", 8.0))

        try:
            self.provider_circuit_breaker.before_request(self._provider_circuit_key)
            with self.ui.begin_turn():
                attempt = 1
                while True:
                    emitted = False
                    try:
                        async for chunk in self.provider.stream_chat(
                            canonical_messages, tools=openai_tools
                        ):
                            emitted = True
                            for fragment in (chunk.content, chunk.tool_call_delta):
                                if not fragment:
                                    continue
                                response_fragments.append(fragment)
                                for event in parser.feed(fragment):
                                    self._handle_event(event, text_chunks, tool_calls)
                            if chunk.native_tool_calls:
                                native_tool_calls_from_api.extend(
                                    chunk.native_tool_calls
                                )
                            if chunk.is_done:
                                prompt_tokens = chunk.prompt_tokens
                                completion_tokens = chunk.completion_tokens
                                cache_read_tokens = chunk.cache_read_tokens
                                cache_write_tokens = chunk.cache_write_tokens
                                usage_source = chunk.usage_source
                                if usage_source == "unavailable" and any(
                                    (
                                        prompt_tokens,
                                        completion_tokens,
                                        cache_read_tokens,
                                        cache_write_tokens,
                                    )
                                ):
                                    usage_source = "provider"
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        failure = classify_provider_failure(exc)
                        if (
                            emitted
                            or not failure.retriable
                            or attempt >= maximum_attempts
                        ):
                            if (
                                failure.retriable
                                and self.provider_circuit_breaker.record_failure(
                                    self._provider_circuit_key
                                )
                            ):
                                snapshot = self.provider_circuit_breaker.snapshot(
                                    self._provider_circuit_key
                                )
                                self._emit_event(
                                    {
                                        "type": "provider.circuit_opened",
                                        "provider": self._provider_circuit_key,
                                        "failures": snapshot["failures"],
                                        "cooldown_seconds": getattr(
                                            self._config,
                                            "provider_circuit_cooldown_seconds",
                                            30.0,
                                        ),
                                    }
                                )
                            raise
                        delay = retry_delay(
                            failure,
                            attempt,
                            base_delay=retry_base_delay,
                            max_delay=retry_max_delay,
                        )
                        safe_reason = redact_text(failure.message)
                        self._emit_event(
                            {
                                "type": "provider.retrying",
                                "attempt": attempt + 1,
                                "max_attempts": maximum_attempts,
                                "delay_seconds": delay,
                                "status_code": failure.status_code,
                                "reason": safe_reason,
                            }
                        )
                        _log.warning(
                            "provider attempt {}/{} failed before output; retrying in {:.2f}s: {}",
                            attempt,
                            maximum_attempts,
                            delay,
                            safe_reason,
                        )
                        await asyncio.sleep(delay)
                        attempt += 1
                self.provider_circuit_breaker.record_success(self._provider_circuit_key)
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

        if usage_source == "unavailable":
            prompt_tokens = self._last_context_tokens or max(
                0,
                int(
                    self.provider.count_tokens(
                        json.dumps(messages, default=str, separators=(",", ":"))
                    )
                ),
            )
            completion_payload = "".join(response_fragments)
            if native_tool_calls_from_api:
                completion_payload += json.dumps(
                    native_tool_calls_from_api, default=str, separators=(",", ":")
                )
            completion_tokens = max(
                0, int(self.provider.count_tokens(completion_payload))
            )
            cache_read_tokens = 0
            cache_write_tokens = 0
            usage_source = "estimated"

        return (
            "".join(text_chunks),
            tool_calls,
            prompt_tokens,
            completion_tokens,
            cache_read_tokens,
            cache_write_tokens,
            usage_source,
        )

    def _handle_event(
        self,
        event: Event,
        text_chunks: list[str],
        tool_calls: list[dict[str, Any]],
    ) -> None:
        kind, payload = event
        if kind == "token" and isinstance(payload, str):
            self._emit_event({"type": "assistant.delta", "text": payload})
            self.ui.print_token(payload)
            text_chunks.append(payload)
        elif kind == "thought" and isinstance(payload, str):
            self._emit_event({"type": "reasoning.delta", "text": payload})
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

    async def _fire_tool_error_hook(
        self,
        session: Session,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        error: str,
    ) -> None:
        await self._fire_hook_lifecycle(
            "tool_error",
            {
                "session_id": session.session_id,
                "turn_id": (
                    self.turn_context.turn_id if self.turn_context is not None else None
                ),
                "call_id": call_id,
                "tool": tool_name,
                "arguments": redact_value(arguments),
                "error": redact_text(error),
            },
        )

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
            self._emit_event({"type": "tool.requested", **event_base})

            decision = self.permission_policy.evaluate(tool_name, arguments)
            if decision.action == PolicyAction.DENY:
                approved = False
            elif self.on_tool_approval is not None:
                approved = await self.on_tool_approval(tool_name, arguments)
            elif self.ui.has_approval_callback:
                approved = self.ui.request_tool_approval(tool_name, arguments)
            elif decision.action == PolicyAction.ALLOW:
                approved = True
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
                        "rule_id": decision.rule_id,
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
                self.session_store.save_tool_call(
                    session.session_id,
                    record,
                    turn_id=self.turn_context.turn_id if self.turn_context else None,
                )
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
                        "rule_id": decision.rule_id,
                    },
                    result=(
                        "BLOCKED_BY_GUARD"
                        if decision.action == PolicyAction.DENY
                        else "DENIED"
                    ),
                )
                self._emit_event(
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

            # Persist approved intent before execution so a fresh process can
            # distinguish an interrupted tool from an unstarted request.
            self.session_store.save_tool_call(
                session.session_id,
                record,
                turn_id=self.turn_context.turn_id if self.turn_context else None,
            )
            tool = self.tools.get(tool_name)
            if tool is None:
                record.error = f"Unknown tool: {tool_name}"
                self.session_store.save_tool_call(
                    session.session_id,
                    record,
                    turn_id=self.turn_context.turn_id if self.turn_context else None,
                )
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
                self._emit_event(
                    {"type": "tool.error", **event_base, "error": record.error}
                )
                await self._fire_tool_error_hook(
                    session,
                    call_id=record.call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    error=record.error,
                )
                results.append(
                    {
                        "success": False,
                        "output": "",
                        "error": f"Unknown tool: {tool_name}",
                    }
                )
                continue

            self._emit_event({"type": "tool.started", **event_base})
            if self.turn_context is not None:
                self.turn_context.set("tool_call_id", record.call_id)
            try:
                hooks = self._active_hooks()
                if hooks is not None:
                    await hooks.fire_pre_tool(tool_name, arguments)
                await self._apply_middlewares_before(tool_name, arguments, tool)
                with tool.event_context(event_base):
                    result_dict = await _execute_with_retry(tool, tool_name, arguments)
                tool_result = ToolResult(
                    success=result_dict["success"],
                    output=result_dict["output"],
                    error=result_dict["error"],
                    truncated=result_dict.get("truncated", False),
                    token_count=result_dict.get("token_count", 0),
                )
                if hooks is not None:
                    await hooks.fire_post_tool(tool_name, arguments, tool_result)
                tool_result = await self._apply_middlewares_after(
                    tool_name, arguments, tool_result
                )
                if self.turn_context is not None:
                    self.turn_context.data.pop("tool_call_id", None)
            except asyncio.CancelledError:
                if self.turn_context is not None:
                    self.turn_context.data.pop("tool_call_id", None)
                raise
            except ToolMiddlewareSkip:
                if self.turn_context is not None:
                    self.turn_context.data.pop("tool_call_id", None)
                tool_result = ToolResult(
                    success=True, output="skipped by middleware", error=None
                )
            except Exception as exc:  # noqa: BLE001 — we want any error captured
                if self.turn_context is not None:
                    self.turn_context.data.pop("tool_call_id", None)
                record.executed = True
                record.error = str(exc)
                self.session_store.save_tool_call(
                    session.session_id,
                    record,
                    turn_id=self.turn_context.turn_id if self.turn_context else None,
                )
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
                self._emit_event(
                    {
                        "type": "tool.error",
                        **event_base,
                        "error": redact_text(str(exc)),
                    }
                )
                await self._fire_tool_error_hook(
                    session,
                    call_id=record.call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    error=str(exc),
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
            self.session_store.save_tool_call(
                session.session_id,
                record,
                turn_id=self.turn_context.turn_id if self.turn_context else None,
            )
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
            self._emit_event(
                {
                    "type": "tool.completed",
                    **event_base,
                    "success": tool_result.success,
                    "output": tool_result.output,
                    "error": tool_result.error,
                    "truncated": tool_result.truncated,
                }
            )
            if not tool_result.success:
                await self._fire_tool_error_hook(
                    session,
                    call_id=record.call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    error=tool_result.error or "tool reported failure",
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
            self._record_repo_map_activity(tool_name, arguments, tool_result)
            self._record_turn_file_mutation(tool_name, arguments, tool_result)
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

    def _remember_repo_file(self, path: Path) -> None:
        """Keep a bounded least-recently-used list of files relevant to context."""

        resolved = path.resolve()
        if resolved in self._repo_map_active_files:
            self._repo_map_active_files.remove(resolved)
        self._repo_map_active_files.append(resolved)
        del self._repo_map_active_files[:-MAX_ACTIVE_REPO_FILES]

    def _record_repo_map_activity(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> None:
        if self.repo_map is None or not result.success:
            return
        paths = self._tool_paths(tool_name, arguments)
        if not paths:
            return

        for path in paths:
            try:
                resolved = self.safety_guard.validate_path(path)
            except Exception:  # noqa: BLE001 - context tracking is best-effort
                continue
            self._remember_repo_file(resolved)
        if tool_name in FILE_WRITE_TOOLS and not bool(arguments.get("dry_run", False)):
            self._repo_map_dirty = True

    def _record_turn_file_mutation(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> None:
        if (
            tool_name not in FILE_WRITE_TOOLS
            or not result.success
            or bool(arguments.get("dry_run", False))
        ):
            return
        for path in self._tool_paths(tool_name, arguments):
            try:
                self._turn_modified_paths.add(self.safety_guard.validate_path(path))
            except Exception:  # noqa: BLE001 - auto-commit capture is best-effort
                continue

    def _tool_paths(self, tool_name: str, arguments: dict[str, Any]) -> set[str]:
        if tool_name == "apply_patch":
            try:
                from tools.patch import extract_patch_paths

                return extract_patch_paths(
                    str(arguments.get("patch", "")), self.safety_guard
                )
            except (TypeError, ValueError):
                return set()
        if tool_name in REPO_MAP_FILE_TOOLS:
            file_path = arguments.get("file_path")
            if isinstance(file_path, str) and file_path:
                return {file_path}
        return set()

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
        try:
            from plugins.skills import ListSkillsTool, render_available_skills

            list_skills_tool = self.tools.get("list_skills")
            if isinstance(list_skills_tool, ListSkillsTool):
                skill_section = render_available_skills(list_skills_tool.catalog)
                if skill_section:
                    system_content = f"{system_content}\n\n{skill_section}"
        except (OSError, UnicodeError, ValueError):
            # Invalid skills are isolated in catalog diagnostics and must not
            # prevent the agent runtime from building a usable prompt.
            pass
        repo_section = ""
        if self.repo_map is not None:
            try:
                if self._repo_map_dirty:
                    try:
                        self.repo_map.refresh()
                    finally:
                        self._repo_map_dirty = False
                ranked = self.repo_map.rank(self._repo_map_active_files)
                repo_section = self.repo_map.render(
                    ranked, top_files=5, symbols_per_file=6
                )
            except Exception as exc:  # noqa: BLE001 — repo map is best-effort
                repo_section = f"(repo map unavailable: {exc})"

        memory_section = ""
        if self._pending_memory_context:
            memory_section = f"## Relevant Context\n{self._pending_memory_context}"

        if self._config is not None:
            from context.history import (
                ContextBudgetAllocator,
                ContextFragmentKind,
                ContextTrust,
                HistoryCompactor,
                context_fragment,
            )

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
            tool_schema_content = json.dumps(
                self._tool_schema_payload(), sort_keys=True, default=str
            )

            system_fit = allocator.fit_text(
                system_content,
                limit=budget_limits["system"],
                count_tokens=self.provider.count_tokens,
            )
            budget_usage["system"] = system_fit.tokens
            if system_fit.truncated:
                truncated.add("system")
            system_parts = [system_fit.text]
            repo_fragment_content = ""
            memory_fragment_content = ""

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
                repo_fragment_content = repo_fit.text
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
                memory_fragment_content = memory_fit.text
            else:
                budget_usage["memory"] = 0

            system_content = "\n\n".join(part for part in system_parts if part)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_content}
            ]
            for message in session.messages:
                msg_dict: dict[str, Any] = {
                    "role": message.role,
                    "content": _canonical_message_content(message),
                }
                if message.role == "assistant" and message.metadata.get("tool_calls"):
                    msg_dict["tool_calls"] = message.metadata["tool_calls"]
                # OpenAI requires tool_call_id on role=tool messages.
                if message.role == "tool" and message.metadata.get("call_id"):
                    msg_dict["tool_call_id"] = message.metadata["call_id"]
                messages.append(msg_dict)

            reserved_message_tokens = (
                budget_usage["system"]
                + budget_usage["repo_map"]
                + budget_usage["memory"]
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
            self._last_context_tokens = result.estimated_tokens + tool_schema_tokens
            budget_usage["history"] = max(
                0, result.estimated_tokens - reserved_message_tokens
            )
            if budget_usage["history"] > budget_limits["history"]:
                truncated.add("history")
            maximum_input = max(
                1, maximum_context - self._config.max_completion_tokens
            )
            self._emit_event(
                {
                    "type": "context.usage",
                    "current": self._last_context_tokens,
                    "maximum": maximum_input,
                }
            )
            self.ui.update_token_count(self._last_context_tokens, maximum_input)
            if result.compacted and result.summary != session.context_summary:
                session.context_summary = redact_text(result.summary)
                self.session_store.save_context_summary(
                    session.session_id,
                    session.context_summary,
                )
            history_content = json.dumps(
                result.messages[1:], sort_keys=True, default=str
            )
            fragments = (
                context_fragment(
                    kind=ContextFragmentKind.SYSTEM,
                    source="assembled_system_prompt",
                    trust=ContextTrust.MIXED,
                    content=system_fit.text,
                    tokens=budget_usage["system"],
                    limit=budget_limits["system"],
                    truncated="system" in truncated,
                ),
                context_fragment(
                    kind=ContextFragmentKind.TOOL_SCHEMA,
                    source="runtime_tool_registry",
                    trust=ContextTrust.MIXED,
                    content=tool_schema_content,
                    tokens=budget_usage["tools"],
                    limit=budget_limits["tools"],
                    truncated="tools" in truncated,
                    metadata={"tool_count": str(len(self.tools))},
                ),
                context_fragment(
                    kind=ContextFragmentKind.HISTORY,
                    source="session_transcript",
                    trust=ContextTrust.SESSION,
                    content=history_content,
                    tokens=budget_usage["history"],
                    limit=budget_limits["history"],
                    truncated="history" in truncated,
                    metadata={
                        "message_count": str(max(0, len(result.messages) - 1)),
                        "compacted": str(result.compacted).lower(),
                    },
                ),
                context_fragment(
                    kind=ContextFragmentKind.REPO_MAP,
                    source="workspace_repository_map",
                    trust=ContextTrust.PROJECT,
                    content=repo_fragment_content,
                    tokens=budget_usage["repo_map"],
                    limit=budget_limits["repo_map"],
                    truncated="repo_map" in truncated,
                ),
                context_fragment(
                    kind=ContextFragmentKind.MEMORY,
                    source="semantic_memory",
                    trust=ContextTrust.MIXED,
                    content=memory_fragment_content,
                    tokens=budget_usage["memory"],
                    limit=budget_limits["memory"],
                    truncated="memory" in truncated,
                ),
            )
            self._last_context_budget = allocator.report(
                limits=budget_limits,
                usage=budget_usage,
                truncated=truncated,
                fragments=fragments,
            )
            if self.turn_context is not None:
                self.turn_context.set("context_budget", self._last_context_budget)
            return result.messages
        messages = [{"role": "system", "content": system_content}]
        if repo_section:
            messages[0]["content"] = f"{messages[0]['content']}\n\n{repo_section}"
        if memory_section:
            messages[0]["content"] = f"{messages[0]['content']}\n\n{memory_section}"
        for message in session.messages:
            msg_dict = {
                "role": message.role,
                "content": _canonical_message_content(message),
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
