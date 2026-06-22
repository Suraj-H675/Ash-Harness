"""Stable asynchronous library API for embedding Ash without a terminal UI."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from ash.cli import _build_provider, _build_tools
from config import AshConfig
from core.checkpoints import FileCheckpointMiddleware
from core.loop import AshLoop
from core.secret_middleware import SecretRedactionMiddleware
from core.session import SessionStore, SessionSummary
from providers.base import ProviderABC
from safety.grants import load_tool_grants
from safety.guard import SafetyGuard
from sandbox import SandboxManager
from ui.headless import HeadlessUI


ApprovalCallback = Callable[[str, dict], Awaitable[bool]]


@dataclass(frozen=True)
class AshResult:
    response: str
    session_id: str
    model: str
    context_tokens: int


class AshClient:
    """Own one Ash runtime and its provider, tools, sessions, and subprocesses."""

    def __init__(self, loop: AshLoop, config: AshConfig) -> None:
        self.loop = loop
        self.config = config
        self._started = False

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
        store = SessionStore(runtime_config.db_directory / "sessions.db")
        guard = SafetyGuard(
            runtime_config.workspace_root,
            blocklist_commands=runtime_config.command_blocklist,
        )
        active_provider = provider or _build_provider(runtime_config)
        sandbox = SandboxManager(workspace_root=runtime_config.workspace_root)
        tools = _build_tools(
            guard,
            runtime_config.workspace_root,
            sandbox_manager=sandbox,
            allow_project_extensions=False,
            provider_factory=lambda: _build_provider(runtime_config),
            agent_db_path=runtime_config.db_directory / "agents.db",
        )
        loop = AshLoop(
            session_store=store,
            provider=active_provider,
            safety_guard=guard,
            ui=HeadlessUI(output_format="text", stream=io.StringIO()),
            project_root=runtime_config.workspace_root,
            tools=tools,
            config=runtime_config,
            safety_tier=runtime_config.safety_tier,
            on_tool_approval=approval_callback,
            enable_semantic_memory=runtime_config.memory_backend != "off",
            memory_backend=runtime_config.memory_backend,
            embedding_provider=runtime_config.embedding_provider,
            openai_api_key=runtime_config.openai_api_key,
            onnx_model_path=runtime_config.onnx_model_path,
            chroma_persist_dir=runtime_config.chroma_persist_dir,
        )
        loop.permission_policy.persistent_tool_grants = load_tool_grants(
            runtime_config.workspace_root
        )

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
        if self._started and self.loop.current_session is not None:
            return self.loop.current_session.session_id
        session = await self.loop.start_session(session_id)
        self._started = True
        return session.session_id

    async def prompt(self, text: str) -> AshResult:
        if not text.strip():
            raise ValueError("prompt cannot be empty")
        if not self._started:
            await self.start()
        response = await self.loop.run_turn(text)
        assert self.loop.current_session is not None
        return AshResult(
            response=response,
            session_id=self.loop.current_session.session_id,
            model=self.config.model,
            context_tokens=self.loop._last_context_tokens,
        )

    def sessions(self, *, query: str = "", limit: int = 20) -> list[SessionSummary]:
        return self.loop.session_store.list_sessions(
            project_path=str(self.loop.project_root), query=query, limit=limit
        )

    async def resume(self, session_id: str) -> str:
        session = await self.loop.start_session(session_id)
        self._started = True
        return session.session_id

    async def new_session(self) -> str:
        session = await self.loop.start_session()
        self._started = True
        return session.session_id

    async def close(self) -> None:
        await self.loop.aclose()
        self._started = False

    async def __aenter__(self) -> "AshClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
