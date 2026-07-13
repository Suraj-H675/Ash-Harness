"""Policy-routed tools for inspecting and managing durable automations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ash.automation.schedules import build_schedule, render_schedule
from ash.automation.store import AutomationError, AutomationStore
from ash.safety.guard import SafetyGuard
from ash.safety.trust import is_workspace_trusted
from ash.tools.base import BaseTool, ToolResult, count_output_tokens


class ListAutomationsArgs(BaseModel):
    include_disabled: bool = False


class ListAutomationsTool(BaseTool):
    name = "list_automations"
    description = (
        "List durable schedules for the current workspace without running them."
    )
    args_schema = ListAutomationsArgs

    def __init__(
        self,
        safety_guard: SafetyGuard,
        store_path: Path,
    ) -> None:
        super().__init__(safety_guard)
        self._store_path = store_path

    async def run(self, **kwargs) -> ToolResult:
        try:
            args = ListAutomationsArgs(**kwargs)
            if not self._store_path.is_file():
                jobs = []
            else:
                with AutomationStore(self._store_path) as store:
                    jobs = store.list_jobs(
                        self.safety_guard.project_root,
                        include_disabled=args.include_disabled,
                    )
        except (AutomationError, OSError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
        payload = [
            {
                "job_id": job.job_id,
                "name": job.name,
                "enabled": job.enabled,
                "schedule": render_schedule(job.schedule),
                "next_run_at": (
                    job.next_run_at.isoformat() if job.next_run_at else None
                ),
                "last_run_status": job.last_run_status,
            }
            for job in jobs
        ]
        output = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
        )


class ManageAutomationArgs(BaseModel):
    action: Literal["create", "pause", "resume", "remove"]
    job: str | None = Field(None, max_length=256)
    name: str | None = Field(None, max_length=256)
    prompt: str | None = Field(None, max_length=65_536)
    at: str | None = Field(None, max_length=128)
    every: str | None = Field(None, max_length=64)
    cron: str | None = Field(None, max_length=256)
    timezone: str = Field("UTC", max_length=128)
    misfire_grace_seconds: int = Field(86_400, ge=0, le=2_592_000)
    timeout_seconds: float = Field(1800, ge=1, le=86_400)
    token_budget: int = Field(100_000, ge=1, le=10_000_000)

    @model_validator(mode="after")
    def validate_action_fields(self) -> "ManageAutomationArgs":
        if self.action == "create":
            if not self.name or not self.prompt:
                raise ValueError("create requires name and prompt")
            if (
                sum(value is not None for value in (self.at, self.every, self.cron))
                != 1
            ):
                raise ValueError("create requires exactly one of at, every, or cron")
        elif not self.job:
            raise ValueError(f"{self.action} requires job")
        return self


class ManageAutomationTool(BaseTool):
    name = "manage_automation"
    description = (
        "Create, pause, resume, or remove a durable unattended Ash schedule. "
        "Changes require ordinary tool approval; creation and resume also require "
        "a trusted workspace."
    )
    args_schema = ManageAutomationArgs

    def __init__(
        self,
        safety_guard: SafetyGuard,
        store_path: Path,
    ) -> None:
        super().__init__(safety_guard)
        self._store_path = store_path

    async def run(self, **kwargs) -> ToolResult:
        try:
            args = ManageAutomationArgs(**kwargs)
            workspace = self.safety_guard.project_root
            if args.action in {"create", "resume"} and not is_workspace_trusted(
                workspace
            ):
                raise AutomationError(
                    f"workspace must be trusted before automation can {args.action}"
                )
            with AutomationStore(self._store_path) as store:
                if args.action == "create":
                    schedule = build_schedule(
                        at=args.at,
                        every=args.every,
                        cron=args.cron,
                        timezone_name=args.timezone,
                    )
                    job = store.create_job(
                        name=args.name or "",
                        prompt=args.prompt or "",
                        workspace=workspace,
                        schedule=schedule,
                        misfire_grace_seconds=args.misfire_grace_seconds,
                        timeout_seconds=args.timeout_seconds,
                        token_budget=args.token_budget,
                    )
                elif args.action == "pause":
                    job = store.set_enabled(
                        args.job or "", workspace=workspace, enabled=False
                    )
                elif args.action == "resume":
                    job = store.set_enabled(
                        args.job or "", workspace=workspace, enabled=True
                    )
                else:
                    job = store.remove_job(args.job or "", workspace=workspace)
            output = json.dumps(
                {
                    "job_id": job.job_id,
                    "name": job.name,
                    "action": args.action,
                    "enabled": job.enabled if args.action != "remove" else False,
                    "schedule": render_schedule(job.schedule),
                    "next_run_at": (
                        job.next_run_at.isoformat() if job.next_run_at else None
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            return ToolResult(
                success=True,
                output=output,
                token_count=count_output_tokens(output),
            )
        except (AutomationError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
