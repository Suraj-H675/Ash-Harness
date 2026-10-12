"""Policy-routed tools for inspecting and managing durable automations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ash.automation.diagnostics import automation_error_excerpt
from ash.automation.delivery import webhook_display_target
from ash.automation.schedules import build_schedule, render_schedule
from ash.automation.store import AutomationError, AutomationStore
from ash.safety.guard import SafetyGuard
from ash.safety.trust import is_workspace_trusted
from ash.tools.base import BaseTool, ToolResult, count_output_tokens


class ListAutomationsArgs(BaseModel):
    include_disabled: bool = False
    job: str | None = Field(
        None,
        min_length=1,
        max_length=256,
        description="Optional workspace-local job ID or name to inspect.",
    )
    history_limit: int | None = Field(
        None,
        ge=1,
        le=20,
        strict=True,
        description="Recent run and delivery records per stream; requires job; default 5.",
    )

    @model_validator(mode="after")
    def validate_history_selector(self) -> "ListAutomationsArgs":
        if self.history_limit is not None and self.job is None:
            raise ValueError("history_limit requires a job")
        return self


class ListAutomationsTool(BaseTool):
    name = "list_automations"
    description = (
        "List durable schedules for the current workspace, or inspect one by its "
        "job ID or name. For inspection, pass job and optional history_limit "
        "(1–20; default 5). A targeted row includes run and webhook delivery "
        "histories separately, with more_runs and more_deliveries flags. Removed "
        "schedules can be inspected by ID. This is a read-only view."
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
            history_limit = args.history_limit or (5 if args.job is not None else 0)
            runs = []
            deliveries = []
            if not self._store_path.is_file():
                if args.job is not None:
                    raise AutomationError(f"automation not found: {args.job}")
                jobs = []
            else:
                with AutomationStore(self._store_path) as store:
                    workspace = self.safety_guard.project_root
                    if args.job is None:
                        jobs = store.list_jobs(
                            workspace,
                            include_disabled=args.include_disabled,
                        )
                    else:
                        job = store.get_job(
                            args.job,
                            workspace=workspace,
                            include_deleted=True,
                        )
                        if job is None:
                            raise AutomationError(
                                f"automation not found: {args.job}"
                            )
                        jobs = [job]
                        runs = store.list_runs(
                            workspace=workspace,
                            job_id=job.job_id,
                            limit=history_limit + 1,
                        )
                        deliveries = store.list_deliveries(
                            workspace=workspace,
                            job_id=job.job_id,
                            limit=history_limit + 1,
                        )
        except (AutomationError, OSError, ValueError) as exc:
            return ToolResult(success=False, output="", error=str(exc))

        payload = []
        for job in jobs:
            row = {
                "job_id": job.job_id,
                "name": job.name,
                "enabled": job.enabled,
                "schedule": render_schedule(job.schedule),
                "next_run_at": (
                    job.next_run_at.isoformat() if job.next_run_at else None
                ),
                "last_run_status": job.last_run_status,
                "last_error": automation_error_excerpt(
                    job.last_error,
                    job=job,
                    max_characters=240,
                ),
            }
            if args.job is not None:
                row.update(
                    {
                        "last_run_at": (
                            job.last_run_at.isoformat() if job.last_run_at else None
                        ),
                        "consecutive_failures": job.consecutive_failures,
                        "webhook_configured": job.webhook_url is not None,
                        "history": {
                            "runs": [
                                {
                                    "run_id": run.run_id,
                                    "status": run.status,
                                    "trigger": run.trigger,
                                    "attempt": run.attempt,
                                    "scheduled_for": run.scheduled_for.isoformat(),
                                    "started_at": (
                                        run.started_at.isoformat()
                                        if run.started_at
                                        else None
                                    ),
                                    "finished_at": (
                                        run.finished_at.isoformat()
                                        if run.finished_at
                                        else None
                                    ),
                                    "error": automation_error_excerpt(
                                        run.error,
                                        job=job,
                                        max_characters=500,
                                    ),
                                }
                                for run in runs[:history_limit]
                            ],
                            "deliveries": [
                                {
                                    "delivery_id": delivery.delivery_id,
                                    "run_id": delivery.run_id,
                                    "status": delivery.status,
                                    "attempt": delivery.attempt,
                                    "response_status": delivery.response_status,
                                    "created_at": delivery.created_at.isoformat(),
                                    "finished_at": (
                                        delivery.finished_at.isoformat()
                                        if delivery.finished_at
                                        else None
                                    ),
                                    "recovery_safe": delivery.recovery_safe,
                                    "last_error": automation_error_excerpt(
                                        delivery.last_error,
                                        job=job,
                                        max_characters=500,
                                    ),
                                }
                                for delivery in deliveries[:history_limit]
                            ],
                            "more_runs": len(runs) > history_limit,
                            "more_deliveries": len(deliveries) > history_limit,
                        },
                    }
                )
            payload.append(row)
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
    webhook_url: str | None = Field(None, max_length=2048)
    webhook_secret_env: str | None = Field(None, max_length=256)

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
                        webhook_url=args.webhook_url,
                        webhook_secret_env=args.webhook_secret_env,
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
                    "webhook_target": (
                        webhook_display_target(job.webhook_url)
                        if job.webhook_url
                        else None
                    ),
                    "webhook_secret_env": job.webhook_secret_env,
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
