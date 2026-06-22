"""Provider-backed subagent execution tool."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from agents.shared_state import SharedState
from agents.subprocess_agent import AGENT_ROLES, AgentReport, SubprocessAgent
from providers.base import ProviderABC
from safety.guard import SafetyGuard
from tools.base import BaseTool, ToolResult, count_output_tokens


class SpawnAgentArgs(BaseModel):
    role: str = Field("general", description=f"One of: {', '.join(AGENT_ROLES)}")
    task: str = Field(..., min_length=1, max_length=20_000)
    agent_id: str | None = None
    background: bool = False


class SpawnAgentTool(BaseTool):
    name = "spawn_agent"
    description = "Run a bounded provider-backed worker on a focused subtask."
    args_schema = SpawnAgentArgs

    def __init__(
        self,
        safety_guard: SafetyGuard,
        shared_state: SharedState,
        provider_factory: Callable[[], ProviderABC],
        *,
        max_return_chars: int = 20_000,
    ) -> None:
        super().__init__(safety_guard)
        self._shared_state = shared_state
        self._provider_factory = provider_factory
        self._max_return_chars = max_return_chars
        self._tasks: dict[str, asyncio.Task[AgentReport]] = {}

    async def run(self, **kwargs: Any) -> ToolResult:
        args = SpawnAgentArgs(**kwargs)
        if args.role not in AGENT_ROLES:
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown role {args.role!r}; expected one of {AGENT_ROLES}",
            )

        agent_id = args.agent_id or f"spawned-{uuid.uuid4().hex[:8]}"

        async def provider_runner(context: dict[str, Any]) -> str:
            provider = self._provider_factory()
            chunks: list[str] = []
            try:
                messages = [
                    {
                        "role": "system",
                        "content": (
                            f"You are a focused {context['role']} subagent. "
                            "Return concise findings with concrete evidence. "
                            "You cannot modify files or call tools in this worker."
                        ),
                    },
                    {"role": "user", "content": context["task"]},
                ]
                async for chunk in provider.stream_chat(messages, tools=None):
                    if chunk.content:
                        chunks.append(chunk.content)
                    if sum(map(len, chunks)) >= self._max_return_chars:
                        break
            finally:
                await provider.aclose()
            return "".join(chunks)[: self._max_return_chars]

        agent = SubprocessAgent(
            agent_id=agent_id,
            role=args.role,
            task=args.task,
            shared_state=self._shared_state,
            runner=provider_runner,
            tool_allowlist=(),
            return_budget=self._max_return_chars,
            workspace_root=Path(self.safety_guard.project_root),
        )
        if args.background:
            task = asyncio.create_task(agent.run_in_process())
            self._tasks[agent_id] = task
            task.add_done_callback(lambda _task: self._tasks.pop(agent_id, None))
            return ToolResult(
                success=True,
                output=f"Started subagent {agent_id} in background.",
            )
        report = await agent.run_in_process()
        return ToolResult(
            success=report.success,
            output=report.summary,
            token_count=count_output_tokens(report.summary),
            error=None if report.success else report.summary,
        )

    async def aclose(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._shared_state.close()

    async def stop(self, agent_id: str) -> bool:
        task = self._tasks.get(agent_id)
        if task is None:
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._shared_state.update_status(
            agent_id, "failed", current_task="stopped by user"
        )
        return True

    def statuses(self) -> list[dict[str, str]]:
        return [
            {
                "agent_id": status.agent_id,
                "role": status.role,
                "status": status.status,
                "task": status.current_task,
            }
            for status in self._shared_state.list_agents()
        ]
