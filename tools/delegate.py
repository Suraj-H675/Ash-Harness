"""Atomic durable DAG submission for provider-backed agents."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agents.shared_state import SharedState
from agents.tasks import AgentTaskCreate, AgentTaskError
from config import AshConfig
from core.redaction import redact_text
from safety.guard import SafetyGuard
from tools.agent import SpawnAgentTool
from tools.base import BaseTool, ToolResult, count_output_tokens


class DelegatedTaskSpec(BaseModel):
    key: str = Field(
        ...,
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="Stable key used by other tasks in depends_on.",
    )
    role: str = Field("general", min_length=1, max_length=128)
    task: str = Field(..., min_length=1, max_length=20_000)
    depends_on: list[str] = Field(default_factory=list, max_length=32)
    isolation: str = Field(
        "auto",
        pattern="^(auto|shared|worktree)$",
        description=(
            "Use shared when a dependent must see predecessor file changes; "
            "worktree commits require explicit acceptance."
        ),
    )
    max_attempts: int = Field(1, ge=1, le=10)
    token_budget: int | None = Field(None, ge=1)
    time_budget_seconds: float | None = Field(None, ge=0.1, le=86_400)
    accept_git_artifacts: bool = Field(
        True,
        description="Merge verified predecessor agent branches into this worktree.",
    )


class DelegateAgentsArgs(BaseModel):
    goal: str = Field(..., min_length=1, max_length=20_000)
    tasks: list[DelegatedTaskSpec] = Field(..., min_length=1, max_length=32)
    background: bool = False


class DelegateAgentsTool(BaseTool):
    name = "delegate_agents"
    description = (
        "Submit an atomic dependency DAG of bounded provider-backed agent tasks; "
        "ready tasks run automatically and independent tasks run in parallel."
    )
    args_schema = DelegateAgentsArgs

    def __init__(
        self,
        safety_guard: SafetyGuard,
        shared_state: SharedState,
        spawn_tool: SpawnAgentTool,
        config: AshConfig,
    ) -> None:
        super().__init__(safety_guard)
        self._shared_state = shared_state
        self._spawn_tool = spawn_tool
        self._config = config

    async def run(self, **kwargs: Any) -> ToolResult:
        args = DelegateAgentsArgs(**kwargs)
        keys = [spec.key for spec in args.tasks]
        if len(set(keys)) != len(keys):
            return _error("Task keys must be unique within a delegated graph.")
        known_keys = set(keys)
        for spec in args.tasks:
            if not self._spawn_tool.supports_role(spec.role):
                return _error(f"Unknown delegated agent role: {spec.role!r}.")
            unknown = sorted(set(spec.depends_on) - known_keys)
            if unknown:
                return _error(
                    f"Task {spec.key!r} has unknown dependencies: "
                    + ", ".join(unknown)
                )
            if spec.key in spec.depends_on:
                return _error(f"Task {spec.key!r} cannot depend on itself.")
            if (
                spec.token_budget is not None
                and spec.token_budget > self._config.agent_token_budget
            ):
                return _error(
                    f"Task {spec.key!r} token budget exceeds the configured maximum "
                    f"of {self._config.agent_token_budget}."
                )
            if (
                spec.time_budget_seconds is not None
                and spec.time_budget_seconds
                > self._config.agent_time_budget_seconds
            ):
                return _error(
                    f"Task {spec.key!r} time budget exceeds the configured maximum "
                    f"of {self._config.agent_time_budget_seconds:g}s."
                )

        graph_id = f"graph-{uuid.uuid4().hex[:12]}"
        task_ids = {key: f"{graph_id}-{key}" for key in keys}
        workspace = str(Path(self.safety_guard.project_root).resolve())
        definitions = [
            AgentTaskCreate(
                description=spec.task,
                role=spec.role,
                task_id=task_ids[spec.key],
                dependencies=tuple(task_ids[key] for key in spec.depends_on),
                max_attempts=spec.max_attempts,
                token_budget=spec.token_budget or self._config.agent_token_budget,
                time_budget_seconds=(
                    spec.time_budget_seconds
                    or self._config.agent_time_budget_seconds
                ),
                metadata={
                    "agent_id": f"worker-{graph_id[6:]}-{spec.key}",
                    "accept_git_artifacts": spec.accept_git_artifacts,
                    "dispatchable": True,
                    "graph_id": graph_id,
                    "goal": redact_text(args.goal),
                    "isolation": spec.isolation,
                    "task_key": spec.key,
                    "workspace": workspace,
                },
            )
            for spec in args.tasks
        ]
        try:
            created = self._shared_state.tasks.create_tasks(definitions)
        except (AgentTaskError, TypeError, ValueError) as exc:
            return _error(f"Could not create delegated task graph: {exc}")

        self.emit_event(
            {
                "type": "agent.graph.created",
                "graph_id": graph_id,
                "goal": redact_text(args.goal),
                "task_ids": [task.task_id for task in created],
            }
        )
        self._spawn_tool.ensure_dispatcher()
        terminal = (
            await self._spawn_tool.wait_for_tasks([task.task_id for task in created])
            if not args.background
            else []
        )
        terminal_by_id = {task.task_id: task for task in terminal}
        if terminal:
            self.emit_event(
                {
                    "type": "agent.graph.completed",
                    "graph_id": graph_id,
                    "status": (
                        "succeeded"
                        if all(task.state == "succeeded" for task in terminal)
                        else "failed"
                    ),
                    "task_ids": [task.task_id for task in terminal],
                }
            )
        output = json.dumps(
            {
                "graph_id": graph_id,
                "tasks": [
                    {
                        "key": spec.key,
                        "task_id": task_ids[spec.key],
                        "role": spec.role,
                        "depends_on": spec.depends_on,
                        **(
                            {
                                "state": terminal_by_id[task_ids[spec.key]].state,
                                "result": terminal_by_id[task_ids[spec.key]].result,
                                "error": terminal_by_id[task_ids[spec.key]].error,
                            }
                            if task_ids[spec.key] in terminal_by_id
                            else {"state": "queued"}
                        ),
                    }
                    for spec in args.tasks
                ],
            },
            sort_keys=True,
        )
        failed = [task for task in terminal if task.state != "succeeded"]
        return ToolResult(
            success=not failed,
            output=output,
            error=(
                "Delegated graph finished with non-successful tasks: "
                + ", ".join(f"{task.task_id}={task.state}" for task in failed)
                if failed
                else None
            ),
            token_count=count_output_tokens(output),
        )

    async def aclose(self) -> None:
        self._shared_state.close()


def _error(message: str) -> ToolResult:
    return ToolResult(success=False, output="", error=message)
