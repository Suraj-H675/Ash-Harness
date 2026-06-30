"""Stable asynchronous library API for embedding Ash without a terminal UI."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from ash.cli import _build_provider, _build_repo_map, _build_tools
from config import AshConfig
from core.checkpoints import FileCheckpointMiddleware
from core.loop import AshLoop
from core.planner import Planner
from core.redaction import redact_text
from core.secret_middleware import SecretRedactionMiddleware
from core.session import SessionStore, SessionSummary
from providers.base import ProviderABC
from safety.grants import load_permission_rules
from safety.guard import SafetyGuard
from sandbox import SandboxBackendUnavailable, SandboxManager, auto_approve_safety_error
from ui.headless import HeadlessUI


ApprovalCallback = Callable[[str, dict], Awaitable[bool]]


@dataclass(frozen=True)
class AshResult:
    response: str
    session_id: str
    model: str
    context_tokens: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def usage(self) -> dict[str, int | float]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_hit_rate": (
                self.cache_read_tokens / self.prompt_tokens
                if self.prompt_tokens
                else 0.0
            ),
            "cost_usd": self.cost_usd,
        }


@dataclass(frozen=True)
class AshEvent:
    type: str
    data: dict[str, Any]


class AshClient:
    """Own one Ash runtime and its provider, tools, sessions, and subprocesses."""

    def __init__(self, loop: AshLoop, config: AshConfig) -> None:
        self.loop = loop
        self.config = config
        self._started = False
        self._turn_lock = asyncio.Lock()

    @classmethod
    async def create(
        cls,
        *,
        config: AshConfig | None = None,
        workspace: Path | None = None,
        provider: ProviderABC | None = None,
        approval_callback: ApprovalCallback | None = None,
    ) -> "AshClient":
        runtime_config = config or AshConfig.load()
        if workspace is not None:
            runtime_config = runtime_config.model_copy(
                update={"workspace_root": workspace.resolve()}
            )
        sandbox = SandboxManager(workspace_root=runtime_config.workspace_root)
        safety_error = auto_approve_safety_error(
            sandbox,
            allow_unsafe=runtime_config.allow_unsafe_auto_approve,
        )
        if runtime_config.safety_tier == "auto_approve" and safety_error:
            raise SandboxBackendUnavailable(safety_error)
        permission_rules = load_permission_rules(runtime_config.workspace_root)
        store = SessionStore(runtime_config.db_directory / "sessions.db")
        guard = SafetyGuard(
            runtime_config.workspace_root,
            blocklist_commands=runtime_config.command_blocklist,
        )
        active_provider = provider or _build_provider(runtime_config)
        repo_map = _build_repo_map(runtime_config)
        tools = _build_tools(
            guard,
            runtime_config.workspace_root,
            sandbox_manager=sandbox,
            allow_project_extensions=False,
            provider_factory=lambda: _build_provider(runtime_config),
            agent_db_path=runtime_config.db_directory / "agents.db",
            repo_map=repo_map,
        )
        loop = AshLoop(
            session_store=store,
            provider=active_provider,
            safety_guard=guard,
            ui=HeadlessUI(output_format="text", stream=io.StringIO()),
            project_root=runtime_config.workspace_root,
            repo_map=repo_map,
            tools=tools,
            config=runtime_config,
            max_steering_messages=runtime_config.steering_queue_limit,
            planner=(
                Planner(active_provider)
                if runtime_config.enable_sprint_planning
                else None
            ),
            enable_sprint_planning=runtime_config.enable_sprint_planning,
            safety_tier=runtime_config.safety_tier,
            on_tool_approval=approval_callback,
            enable_semantic_memory=runtime_config.memory_backend != "off",
            memory_backend=runtime_config.memory_backend,
            embedding_provider=runtime_config.embedding_provider,
            openai_api_key=runtime_config.openai_api_key,
            onnx_model_path=runtime_config.onnx_model_path,
            chroma_persist_dir=runtime_config.chroma_persist_dir,
        )
        loop.permission_policy.set_persistent_rules(permission_rules)

        def checkpoint_context() -> tuple[str, str] | None:
            if loop.current_session is None or loop.turn_context is None:
                return None
            return loop.current_session.session_id, loop.turn_context.turn_id

        loop.tool_middlewares.extend(
            [
                FileCheckpointMiddleware(store, guard, checkpoint_context),
                SecretRedactionMiddleware(),
            ]
        )
        client = cls(loop, runtime_config)
        await client.start()
        return client

    async def start(self, session_id: str | None = None) -> str:
        async with self._turn_lock:
            return await self._start_unlocked(session_id)

    async def _start_unlocked(self, session_id: str | None = None) -> str:
        if self._started and self.loop.current_session is not None:
            return self.loop.current_session.session_id
        session = await self.loop.start_session(session_id)
        self._started = True
        return session.session_id

    async def prompt(self, text: str) -> AshResult:
        async with self._turn_lock:
            return await self._prompt_unlocked(text)

    async def steer(self, text: str) -> int:
        """Queue guidance for the currently running turn without waiting on it."""

        if not self.loop.is_turn_running:
            raise RuntimeError("no turn is currently running")
        return self.loop.queue_steering(text)

    async def _prompt_unlocked(self, text: str) -> AshResult:
        if not text.strip():
            raise ValueError("prompt cannot be empty")
        if not self._started:
            await self._start_unlocked()
        response = await self.loop.run_turn(text)
        assert self.loop.current_session is not None
        usage = self.loop.last_turn_usage
        return AshResult(
            response=response,
            session_id=self.loop.current_session.session_id,
            model=self.config.model,
            context_tokens=self.loop._last_context_tokens,
            prompt_tokens=int(usage["prompt_tokens"]),
            completion_tokens=int(usage["completion_tokens"]),
            cache_read_tokens=int(usage["cache_read_tokens"]),
            cache_write_tokens=int(usage["cache_write_tokens"]),
            cost_usd=float(usage["cost_usd"]),
        )

    async def stream_prompt(self, text: str) -> AsyncIterator[AshEvent]:
        """Yield real runtime deltas and one terminal completion/error event."""

        if not text.strip():
            raise ValueError("prompt cannot be empty")
        async with self._turn_lock:
            ui = self.loop.ui
            if not isinstance(ui, HeadlessUI):
                raise RuntimeError("stream_prompt requires Ash's headless event UI")
            queue: asyncio.Queue[AshEvent] = asyncio.Queue()

            def receive(payload: dict[str, Any]) -> None:
                event_type = str(payload.pop("type"))
                queue.put_nowait(AshEvent(event_type, payload))

            unsubscribe = ui.subscribe(receive)

            async def run() -> None:
                try:
                    result = await self._prompt_unlocked(text)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    queue.put_nowait(
                        AshEvent("turn.error", {"error": redact_text(str(exc))})
                    )
                    return
                queue.put_nowait(
                    AshEvent(
                        "turn.completed",
                        {
                            "response": result.response,
                            "session_id": result.session_id,
                            "model": result.model,
                            "context_tokens": result.context_tokens,
                            "usage": result.usage,
                        },
                    )
                )

            task = asyncio.create_task(run())
            try:
                yield AshEvent("turn.started", {})
                while True:
                    event = await queue.get()
                    yield event
                    if event.type in {"turn.completed", "turn.error"}:
                        break
            finally:
                unsubscribe()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def sessions(self, *, query: str = "", limit: int = 20) -> list[SessionSummary]:
        return self.loop.session_store.list_sessions(
            project_path=str(self.loop.project_root), query=query, limit=limit
        )

    async def resume(self, session_id: str) -> str:
        async with self._turn_lock:
            session = await self.loop.start_session(session_id)
            self._started = True
            return session.session_id

    async def new_session(self) -> str:
        async with self._turn_lock:
            session = await self.loop.start_session()
            self._started = True
            return session.session_id

    async def close(self) -> None:
        async with self._turn_lock:
            await self.loop.aclose()
            self._started = False

    async def __aenter__(self) -> "AshClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
