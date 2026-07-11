"""Provider-backed subagent execution tool."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from pydantic import BaseModel, Field

from agents.shared_state import SharedState
from agents.tasks import AgentTaskBudgetExceeded, AgentTaskError
from agents.subprocess_agent import AGENT_ROLES, AgentReport, SubprocessAgent
from agents.worktree import WorktreeError, WorktreeLease, WorktreeManager
from core.loop import AshLoop
from core.session import SessionStore
from providers.base import ProviderABC
from safety.guard import SafetyGuard
from sandbox import SandboxManager
from tools.base import BaseTool, ToolResult, count_output_tokens
from ui.headless import HeadlessUI

if TYPE_CHECKING:
    from config import AshConfig
    from plugins.agents import AgentDefinition


class SpawnAgentArgs(BaseModel):
    role: str = Field(
        "general",
        description="A built-in role or discovered custom agent name.",
    )
    task: str = Field(..., min_length=1, max_length=20_000)
    agent_id: str | None = Field(
        None,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    background: bool = False
    isolation: str = Field("auto", pattern="^(auto|shared|worktree)$")
    parent_task_id: str | None = Field(
        None,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        description="Optional durable parent task for continuation lineage.",
    )


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
        config: "AshConfig | None" = None,
        max_turn_iterations: int = 12,
        custom_agents: dict[str, "AgentDefinition"] | None = None,
    ) -> None:
        super().__init__(safety_guard)
        self._shared_state = shared_state
        self._provider_factory = provider_factory
        self._max_return_chars = max_return_chars
        self._config = config
        self._max_turn_iterations = max_turn_iterations
        self._max_concurrency = config.max_concurrent_agents if config else 4
        self._task_token_budget = config.agent_token_budget if config else 4000
        self._task_time_budget = config.agent_time_budget_seconds if config else 900.0
        self._task_lease_seconds = config.agent_lease_seconds if config else 30.0
        self._custom_agents = dict(custom_agents or {})
        if self._custom_agents:
            self._update_description()
        self._tasks: dict[str, asyncio.Task[AgentReport]] = {}

    def set_custom_agents(self, agents: dict[str, "AgentDefinition"]) -> None:
        self._custom_agents = dict(agents)
        self._update_description()

    def _update_description(self) -> None:
        roles = ", ".join((*AGENT_ROLES, *sorted(self._custom_agents)))
        self.description = f"Run a bounded worker on a focused subtask. Roles: {roles}."

    async def run(self, **kwargs: Any) -> ToolResult:
        args = SpawnAgentArgs(**kwargs)
        agent_definition = self._custom_agents.get(args.role)
        if args.role not in AGENT_ROLES and agent_definition is None:
            expected = (*AGENT_ROLES, *sorted(self._custom_agents))
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown role {args.role!r}; expected one of {expected}",
            )
        execution_role = (
            agent_definition.base_role if agent_definition is not None else args.role
        )

        agent_id = args.agent_id or f"spawned-{uuid.uuid4().hex[:8]}"
        existing = self._shared_state.get_status(agent_id)
        if existing is not None and existing.status in {"idle", "working"}:
            return ToolResult(
                success=False,
                output="",
                error=f"Subagent {agent_id!r} is already running.",
            )

        try:
            durable_task = self._shared_state.tasks.create_task(
                args.task,
                role=args.role,
                task_id=f"agent-task-{uuid.uuid4()}",
                parent_task_id=args.parent_task_id,
                token_budget=self._task_token_budget,
                time_budget_seconds=self._task_time_budget,
                metadata={
                    "agent_id": agent_id,
                    "background": args.background,
                    "workspace": str(Path(self.safety_guard.project_root).resolve()),
                },
            )
        except (AgentTaskError, ValueError) as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Could not create durable subagent task: {exc}",
            )
        durable_lease = self._shared_state.tasks.claim_task(
            agent_id,
            task_id=durable_task.task_id,
            lease_seconds=self._task_lease_seconds,
            max_active=self._max_concurrency,
        )
        if durable_lease is None:
            self._shared_state.tasks.cancel_task(
                durable_task.task_id,
                reason="subagent concurrency limit reached",
            )
            return ToolResult(
                success=False,
                output="",
                error=(
                    "Subagent concurrency limit reached; wait for a running agent "
                    "to finish before spawning another."
                ),
            )
        self._shared_state.tasks.start_task(durable_task.task_id, durable_lease.token)

        isolation = args.isolation
        if isolation == "auto":
            isolation = (
                "worktree" if execution_role in {"coder", "tester"} else "shared"
            )
        worker_workspace = Path(self.safety_guard.project_root)
        worktree_manager: WorktreeManager | None = None
        lease: WorktreeLease | None = None
        if isolation == "worktree":
            digest = hashlib.sha256(str(worker_workspace).encode()).hexdigest()[:12]
            worktree_manager = WorktreeManager(
                worker_workspace,
                Path(self._shared_state.db_path).parent / "worktrees" / digest,
            )
            try:
                lease = await worktree_manager.create(agent_id)
            except asyncio.CancelledError:
                self._shared_state.tasks.cancel_task(
                    durable_task.task_id,
                    reason="subagent spawn cancelled during worktree creation",
                )
                raise
            except WorktreeError as exc:
                self._shared_state.tasks.fail_task(
                    durable_task.task_id,
                    durable_lease.token,
                    f"worktree creation failed: {exc}",
                )
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Could not create isolated subagent worktree: {exc}",
                )
            worker_workspace = lease.path

        branch_state: dict[str, str | None] = {"commit": None}
        cleanup_state = {"done": False}

        async def provider_runner(context: dict[str, Any]) -> AgentReport:
            started = datetime.now(timezone.utc)
            artifacts: dict[str, Any] = {
                "isolation": isolation,
                "workspace": str(worker_workspace),
            }
            try:
                summary, completion_tokens = await self._run_worker_loop(
                    role=context["role"],
                    execution_role=execution_role,
                    agent_definition=agent_definition,
                    task=context["task"],
                    workspace=worker_workspace,
                    agent_id=context["agent_id"],
                    durable_task_id=durable_task.task_id,
                    durable_lease_token=durable_lease.token,
                )
                artifacts["completion_tokens"] = completion_tokens
                try:
                    self._shared_state.tasks.record_tokens(
                        durable_task.task_id,
                        durable_lease.token,
                        completion_tokens,
                    )
                except AgentTaskBudgetExceeded as exc:
                    summary = f"{summary}\n{exc}"
                    success = False
                else:
                    success = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                summary = f"Subagent failed: {exc}"
                success = False
            if lease is not None and worktree_manager is not None:
                try:
                    commit = await worktree_manager.commit_changes(
                        lease,
                        message=f"ash agent {agent_id}: {args.task[:120]}",
                    )
                except WorktreeError as exc:
                    summary = f"{summary}\nWorktree commit failed: {exc}"
                    success = False
                else:
                    branch_state["commit"] = commit
                    if commit is not None:
                        artifacts.update({"branch": lease.branch, "commit": commit})
                try:
                    await worktree_manager.remove(
                        lease,
                        keep_branch=branch_state["commit"] is not None,
                    )
                except WorktreeError as exc:
                    summary = f"{summary}\nWorktree cleanup failed: {exc}"
                    success = False
                else:
                    cleanup_state["done"] = True
            finished = datetime.now(timezone.utc)
            return AgentReport(
                agent_id=context["agent_id"],
                role=context["role"],
                task=context["task"],
                success=success,
                summary=summary[: self._max_return_chars],
                artifacts=artifacts,
                started_at=started,
                finished_at=finished,
            )

        agent = SubprocessAgent(
            agent_id=agent_id,
            role=args.role,
            task=args.task,
            shared_state=self._shared_state,
            runner=provider_runner,
            tool_allowlist=(),
            return_budget=self._max_return_chars,
            metadata={
                "isolation": isolation,
                "workspace": str(worker_workspace),
                "base_role": execution_role,
                "durable_task_id": durable_task.task_id,
                **({"branch": lease.branch} if lease is not None else {}),
            },
            workspace_root=worker_workspace,
            allow_custom_role=agent_definition is not None,
        )

        async def execute_agent() -> AgentReport:
            try:
                report = await agent.run_in_process()
                current = self._shared_state.tasks.get_task(durable_task.task_id)
                if current is not None and current.state in {"leased", "running"}:
                    if report.success:
                        self._shared_state.tasks.complete_task(
                            durable_task.task_id,
                            durable_lease.token,
                            agent.report_to_payload(report),
                        )
                    else:
                        self._shared_state.tasks.fail_task(
                            durable_task.task_id,
                            durable_lease.token,
                            report.summary,
                        )
                branch = report.artifacts.get("branch")
                commit = report.artifacts.get("commit")
                if isinstance(branch, str) and isinstance(commit, str):
                    self._shared_state.tasks.add_artifact(
                        durable_task.task_id,
                        kind="git-commit",
                        uri=branch,
                        metadata={"commit": commit},
                    )
                return report
            except asyncio.CancelledError:
                self._shared_state.tasks.cancel_task(
                    durable_task.task_id,
                    reason="subagent execution cancelled",
                )
                self._shared_state.update_status(
                    agent_id,
                    "failed",
                    current_task="subagent execution cancelled",
                )
                raise
            except Exception as exc:
                current = self._shared_state.tasks.get_task(durable_task.task_id)
                if current is not None and current.state in {"leased", "running"}:
                    try:
                        self._shared_state.tasks.fail_task(
                            durable_task.task_id,
                            durable_lease.token,
                            f"subagent execution failed: {exc}",
                        )
                    except AgentTaskError:
                        pass
                raise
            finally:
                if (
                    lease is not None
                    and worktree_manager is not None
                    and not cleanup_state["done"]
                ):
                    try:
                        await worktree_manager.remove(
                            lease,
                            keep_branch=branch_state["commit"] is not None,
                        )
                    except WorktreeError:
                        # Preserve the original worker failure/cancellation. The
                        # locked worktree remains visible to `git worktree list`.
                        pass

        if args.background:
            task = asyncio.create_task(execute_agent())
            self._tasks[agent_id] = task

            def finish_background(completed: asyncio.Task[AgentReport]) -> None:
                self._finish_background_task(agent_id, completed)

            task.add_done_callback(finish_background)
            return ToolResult(
                success=True,
                output=(
                    f"Started subagent {agent_id} in background "
                    f"(task {durable_task.task_id})."
                ),
            )
        report = await execute_agent()
        output = report.summary
        branch = report.artifacts.get("branch")
        commit = report.artifacts.get("commit")
        if branch and commit:
            output += f"\nIsolated changes: branch={branch} commit={commit}"
        return ToolResult(
            success=report.success,
            output=output,
            token_count=count_output_tokens(output),
            error=None if report.success else report.summary,
        )

    async def _run_worker_loop(
        self,
        *,
        role: str,
        execution_role: str,
        agent_definition: "AgentDefinition | None",
        task: str,
        workspace: Path,
        agent_id: str,
        durable_task_id: str,
        durable_lease_token: str,
    ) -> tuple[str, int]:
        provider = self._provider_factory()
        guard = SafetyGuard(workspace)
        sandbox = SandboxManager(
            workspace_root=workspace,
            network=False,
            backend_preference=(
                self._config.sandbox_backend if self._config is not None else "auto"
            ),
            docker_image=(
                self._config.sandbox_docker_image
                if self._config is not None
                else "ash-sandbox:latest"
            ),
        )
        tools = _worker_tools(execution_role, guard, sandbox)
        if agent_definition is not None and agent_definition.allowed_tools:
            unknown_tools = sorted(set(agent_definition.allowed_tools) - tools.keys())
            if unknown_tools:
                raise ValueError(
                    f"custom agent {role!r} requests unavailable tools: "
                    + ", ".join(unknown_tools)
                )
            tools = {
                name: tool
                for name, tool in tools.items()
                if name in agent_definition.allowed_tools
            }
        worker_store = SessionStore(
            Path(self._shared_state.db_path).with_name("agent-sessions.db")
        )
        worker_config = (
            self._config.model_copy(
                update={
                    "workspace_root": workspace,
                    "safety_tier": "auto_approve",
                    "max_completion_tokens": min(
                        self._config.max_completion_tokens,
                        self._task_token_budget,
                    ),
                    "enable_sprint_planning": False,
                    "memory_backend": "off",
                }
            )
            if self._config is not None
            else None
        )
        instructions = (
            f"You are Ash subagent {agent_id}, acting only as {role}. "
            f"Your workspace is {workspace}. Use the available tools to inspect and "
            "complete the focused task. Do not spawn other agents. Return concise "
            "findings with file paths, commands, and test evidence."
        )
        if agent_definition is not None:
            instructions = (
                f"{instructions}\n\nCustom agent instructions:\n"
                f"{agent_definition.instructions}"
            )
        loop = AshLoop(
            session_store=worker_store,
            provider=provider,
            safety_guard=guard,
            ui=HeadlessUI(output_format="text", stream=io.StringIO()),
            project_root=workspace,
            tools=tools,
            safety_tier="auto_approve",
            system_prompt=instructions,
            max_turn_iterations=self._max_turn_iterations,
            config=worker_config,
            enable_semantic_memory=False,
        )
        try:
            await loop.start_session()
            turn = asyncio.create_task(loop.run_turn(task))
            inbox = asyncio.create_task(
                self._consume_worker_messages(
                    loop,
                    agent_id,
                    turn,
                    durable_task_id=durable_task_id,
                    durable_lease_token=durable_lease_token,
                )
            )
            try:
                response = await asyncio.wait_for(
                    asyncio.shield(turn),
                    timeout=self._task_time_budget,
                )
                usage = loop.last_turn_usage
                completion_tokens = int(usage["completion_tokens"])
                if completion_tokens == 0:
                    completion_tokens = int(usage["estimated_completion_tokens"])
                return response[: self._max_return_chars], completion_tokens
            except asyncio.TimeoutError as exc:
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)
                raise TimeoutError(
                    f"subagent exceeded {self._task_time_budget:g}s time budget"
                ) from exc
            finally:
                inbox.cancel()
                await asyncio.gather(inbox, return_exceptions=True)
        finally:
            await loop.aclose()

    async def _consume_worker_messages(
        self,
        loop: AshLoop,
        agent_id: str,
        turn: asyncio.Task[str],
        *,
        durable_task_id: str,
        durable_lease_token: str,
    ) -> None:
        next_renewal = 0.0
        while not turn.done():
            now = asyncio.get_running_loop().time()
            if now >= next_renewal:
                try:
                    self._shared_state.tasks.renew_lease(
                        durable_task_id,
                        durable_lease_token,
                        lease_seconds=self._task_lease_seconds,
                    )
                except AgentTaskError:
                    turn.cancel()
                    return
                next_renewal = now + max(1.0, self._task_lease_seconds / 3)
            messages = self._shared_state.fetch_messages(
                agent_id,
                undelivered_only=True,
                limit=25,
            )
            delivered: list[int] = []
            for message in messages:
                if message.message_type == "stop":
                    self._shared_state.update_status(
                        agent_id,
                        "failed",
                        current_task="stopped by persisted message",
                    )
                    delivered.append(message.message_id)
                    turn.cancel()
                    break
                if message.message_type not in {"steer", "message"}:
                    continue
                content = message.content.get("content", message.content)
                steering = (
                    content
                    if isinstance(content, str)
                    else json.dumps(content, ensure_ascii=False, sort_keys=True)
                )
                try:
                    loop.queue_steering(steering)
                except (OverflowError, ValueError):
                    continue
                delivered.append(message.message_id)
            if delivered:
                self._shared_state.mark_delivered(delivered)
            await asyncio.sleep(0.1)

    async def aclose(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._shared_state.close()

    def _finish_background_task(
        self,
        agent_id: str,
        task: asyncio.Task[AgentReport],
    ) -> None:
        self._tasks.pop(agent_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._shared_state.update_status(
                agent_id,
                "failed",
                current_task=f"background worker failed: {error}",
            )

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

    async def resume(self, agent_id: str) -> ToolResult:
        status = self._shared_state.get_status(agent_id)
        if status is None:
            return ToolResult(
                success=False,
                output="",
                error=f"Unknown subagent: {agent_id}",
            )
        if status.status in {"idle", "working"}:
            return ToolResult(
                success=False,
                output="",
                error=f"Subagent {agent_id} is still running.",
            )
        report = next(
            (
                message.content
                for message in reversed(
                    self._shared_state.fetch_messages(
                        "lead",
                        undelivered_only=False,
                        limit=1000,
                    )
                )
                if message.message_type == "agent_report"
                and message.content.get("agent_id") == agent_id
            ),
            None,
        )
        if report is None:
            return ToolResult(
                success=False,
                output="",
                error=f"No persisted report exists for subagent {agent_id}.",
            )
        branch = report.get("artifacts", {}).get("branch")
        if branch:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Apply isolated branch {branch} before resuming so the new "
                    "worker sees the previous changes."
                ),
            )
        original_task = str(status.metadata.get("task") or report.get("task") or "")
        prior_summary = str(report.get("summary") or "")
        continuation = (
            f"Continue this prior subtask:\n{original_task}\n\n"
            f"Prior worker report:\n{prior_summary}"
        )[:20_000]
        resumed_id = f"{agent_id[:50]}-r-{uuid.uuid4().hex[:6]}"
        result = await self.run(
            role=status.role,
            task=continuation,
            agent_id=resumed_id,
            background=True,
            isolation=str(status.metadata.get("isolation") or "shared"),
            parent_task_id=status.metadata.get("durable_task_id"),
        )
        if result.success:
            result.output += f" Continued from {agent_id}."
        return result

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


def _worker_tools(
    role: str,
    guard: SafetyGuard,
    sandbox: SandboxManager,
) -> dict[str, BaseTool]:
    from tools.filesystem import (
        ReadFileTool,
        ReplaceFileContentTool,
        ReplaceFileEditsTool,
        WholeEditTool,
        WriteFileTool,
    )
    from tools.git import GitDiffTool, GitLogTool, GitStatusTool
    from tools.patch import ApplyPatchTool
    from tools.search import GlobFilesTool, ListDirectoryTool, SearchTextTool

    tools: list[BaseTool] = [
        ReadFileTool(guard),
        ListDirectoryTool(guard),
        GlobFilesTool(guard),
        SearchTextTool(guard),
        GitStatusTool(guard),
        GitDiffTool(guard),
        GitLogTool(guard),
    ]
    if role == "coder":
        tools.extend(
            [
                WriteFileTool(guard),
                ReplaceFileContentTool(guard),
                ReplaceFileEditsTool(guard),
                WholeEditTool(guard),
                ApplyPatchTool(guard),
            ]
        )
    if role == "tester" and sandbox.is_fully_isolated():
        from tools.command import RunCommandTool

        tools.append(
            RunCommandTool(
                guard,
                project_root=guard.project_root,
                sandbox_manager=sandbox,
            )
        )
    return {tool.name: tool for tool in tools}
