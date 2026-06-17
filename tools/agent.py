"""Agent spawning tool for dynamic subagent creation."""

import uuid
from typing import Any

from pydantic import BaseModel

from ash.agents.shared_state import SharedState
from ash.agents.subprocess_agent import SubprocessAgent, make_simple_text_task
from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult


class SpawnAgentArgs(BaseModel):
    role: str
    task: str
    agent_id: str | None = None


class SpawnAgentTool(BaseTool):
    name = "spawn_agent"
    description = "Spawn a new subagent to handle a subtask."
    args_schema: type[BaseModel] = SpawnAgentArgs

    def __init__(self, safety_guard: SafetyGuard, shared_state: "SharedState") -> None:
        super().__init__(safety_guard)
        self._shared_state = shared_state

    async def run(self, **kwargs: Any) -> ToolResult:
        args = self.args_schema(**kwargs)
        if self._shared_state is None:
            return ToolResult(
                success=False,
                output="",
                error="spawn_agent tool: shared_state not initialized (SubprocessAgent requires a SharedState instance)",
            )
        agent = SubprocessAgent(
            agent_id=args.agent_id or f"spawned-{uuid.uuid4().hex[:8]}",
            role=args.role,
            task=args.task,
            shared_state=self._shared_state,
            runner=make_simple_text_task("done"),
        )
        report = await agent.run_in_process()
        return ToolResult(success=report.success, output=report.summary)
