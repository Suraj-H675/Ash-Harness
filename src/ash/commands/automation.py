"""CLI helpers for durable unattended Ash automations."""

from __future__ import annotations

import asyncio
import json
import signal
from collections.abc import Callable
from typing import Any

from ash.automation.models import (
    AutomationJob,
    AutomationRun,
    AutomationWorkerSummary,
)
from ash.automation.schedules import build_schedule, render_schedule
from ash.automation.store import (
    AutomationRestartRequired,
    AutomationStore,
)
from ash.automation.worker import AutomationWorkerService
from ash.config import AshConfig


def automation_store(config: AshConfig) -> AutomationStore:
    return AutomationStore(config.db_directory / "automation.db")


def automation_config_loader(config: AshConfig) -> Callable[[], AshConfig]:
    cli_overrides = {
        field: getattr(config, field)
        for field in type(config).model_fields
        if (config.config_source(field) or (None,))[0] == "cli"
    }
    cli_overrides["workspace_root"] = config.workspace_root
    startup_database = config.db_directory.expanduser().resolve()

    def load() -> AshConfig:
        refreshed = AshConfig.load(
            _override_source="cli",
            _override_detail="automation worker startup options",
            **cli_overrides,
        )
        if refreshed.db_directory.expanduser().resolve() != startup_database:
            raise AutomationRestartRequired(
                "automation db_directory changed; restart the worker to bind the "
                "new persistence path"
            )
        return refreshed

    return load


def job_payload(job: AutomationJob, *, include_prompt: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "job_id": job.job_id,
        "name": job.name,
        "workspace": job.workspace,
        "enabled": job.enabled,
        "schedule": {
            "kind": job.schedule.kind,
            "value": job.schedule.value,
            "timezone": job.schedule.timezone,
            "anchor_at": (
                job.schedule.anchor_at.isoformat()
                if job.schedule.anchor_at is not None
                else None
            ),
            "display": render_schedule(job.schedule),
        },
        "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
        "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
        "last_run_status": job.last_run_status,
        "last_error": job.last_error,
        "consecutive_failures": job.consecutive_failures,
        "misfire_grace_seconds": job.misfire_grace_seconds,
        "timeout_seconds": job.timeout_seconds,
        "token_budget": job.token_budget,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }
    if include_prompt:
        payload["prompt"] = job.prompt
    return payload


def run_payload(run: AutomationRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "job_id": run.job_id,
        "trigger": run.trigger,
        "scheduled_for": run.scheduled_for.isoformat(),
        "status": run.status,
        "worker_id": run.worker_id,
        "cancel_requested": run.cancel_requested,
        "session_id": run.session_id,
        "response": run.response,
        "error": run.error,
        "prompt_tokens": run.prompt_tokens,
        "completion_tokens": run.completion_tokens,
        "cache_read_tokens": run.cache_read_tokens,
        "cache_write_tokens": run.cache_write_tokens,
        "cost_usd": run.cost_usd,
        "usage_source": run.usage_source,
        "estimated_prompt_tokens": run.estimated_prompt_tokens,
        "estimated_completion_tokens": run.estimated_completion_tokens,
        "estimated_cost_usd": run.estimated_cost_usd,
        "cost_is_estimated": run.estimated_cost_usd > 0,
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def render_jobs(jobs: list[AutomationJob], *, json_output: bool = False) -> str:
    if json_output:
        return json.dumps([job_payload(job) for job in jobs], sort_keys=True)
    if not jobs:
        return "No automations configured for this workspace."
    lines: list[str] = []
    for job in jobs:
        state = "enabled" if job.enabled else "paused"
        next_run = job.next_run_at.isoformat() if job.next_run_at else "none"
        lines.append(
            f"{job.job_id}  {job.name}  [{state}]\n"
            f"  {render_schedule(job.schedule)}  next={next_run}"
        )
        if job.last_run_status:
            lines.append(
                f"  last={job.last_run_status} failures={job.consecutive_failures}"
            )
    return "\n".join(lines)


def render_job(job: AutomationJob, *, json_output: bool = False) -> str:
    payload = job_payload(job, include_prompt=True)
    if json_output:
        return json.dumps(payload, sort_keys=True)
    lines = [
        f"Automation: {job.name} ({job.job_id})",
        f"State: {'enabled' if job.enabled else 'paused'}",
        f"Schedule: {render_schedule(job.schedule)}",
        f"Next run: {payload['next_run_at'] or 'none'}",
        f"Workspace: {job.workspace}",
        f"Timeout: {job.timeout_seconds:g}s",
        f"Token budget: {job.token_budget}",
        "Prompt:",
        job.prompt,
    ]
    if job.last_run_status:
        lines.append(f"Last run: {job.last_run_status} at {payload['last_run_at']}")
    if job.last_error:
        lines.append(f"Last error: {job.last_error}")
    return "\n".join(lines)


def render_runs(runs: list[AutomationRun], *, json_output: bool = False) -> str:
    if json_output:
        return json.dumps([run_payload(run) for run in runs], sort_keys=True)
    if not runs:
        return "No automation runs found."
    lines = []
    for run in runs:
        usage = run.prompt_tokens + run.completion_tokens
        approximate = "~" if run.usage_source in {"estimated", "mixed"} else ""
        lines.append(
            f"{run.run_id}  {run.status}  job={run.job_id} "
            f"scheduled={run.scheduled_for.isoformat()} tokens={usage} "
            f"cost={approximate}${run.cost_usd:.6f} usage={run.usage_source}"
        )
        if run.error:
            lines.append(f"  error: {run.error}")
    return "\n".join(lines)


def render_status(config: AshConfig, *, json_output: bool = False) -> str:
    with automation_store(config) as store:
        job_count, active_job_count = store.job_counts(config.workspace_root)
        workers = store.list_workers(
            config.workspace_root,
            stale_after_seconds=max(config.automation_poll_seconds * 4, 10),
        )
        running_count = store.count_running_runs(config.workspace_root)
    payload = {
        "enabled": config.automation_enabled,
        "workspace": str(config.workspace_root.resolve()),
        "jobs": job_count,
        "active_jobs": active_job_count,
        "running": running_count,
        "workers": [
            {
                "worker_id": worker.worker_id,
                "pid": worker.pid,
                "heartbeat_at": worker.heartbeat_at.isoformat(),
                "max_concurrent_runs": worker.max_concurrent_runs,
            }
            for worker in workers
        ],
    }
    if json_output:
        return json.dumps(payload, sort_keys=True)
    lines = [
        f"Automation: {'enabled' if config.automation_enabled else 'disabled'}",
        f"Workspace: {payload['workspace']}",
        f"Jobs: {payload['active_jobs']} active / {payload['jobs']} total",
        f"Runs: {payload['running']} running",
        f"Workers: {len(workers)} active",
    ]
    for worker in workers:
        lines.append(
            f"  {worker.worker_id} pid={worker.pid} "
            f"heartbeat={worker.heartbeat_at.isoformat()}"
        )
    if config.automation_enabled and active_job_count and not workers:
        lines.append(
            "Warning: no worker is active; run `ash cron worker` for schedules to fire."
        )
    return "\n".join(lines)


async def run_worker(
    config: AshConfig,
    *,
    once: bool = False,
    on_run_finished: Callable[[AutomationRun], None] | None = None,
) -> AutomationWorkerSummary:
    if not config.automation_enabled:
        raise ValueError("automation is disabled by configuration")
    with automation_store(config) as store:
        service = AutomationWorkerService(
            store,
            config.workspace_root,
            max_concurrent_runs=config.automation_max_concurrent_runs,
            poll_seconds=config.automation_poll_seconds,
            lease_seconds=config.automation_lease_seconds,
            config_loader=automation_config_loader(config),
            on_run_finished=on_run_finished,
            run_retention_days=config.automation_run_retention_days,
            session_retention_days=config.session_retention_days,
            session_store_path=config.db_directory / "sessions.db",
        )
        loop = asyncio.get_running_loop()
        installed_signals: list[signal.Signals] = []
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, service.request_stop)
            except (NotImplementedError, RuntimeError):
                continue
            installed_signals.append(signum)
        try:
            return await service.run_forever(once=once)
        finally:
            for signum in installed_signals:
                loop.remove_signal_handler(signum)


async def run_manual(config: AshConfig, reference: str) -> AutomationRun:
    if not config.automation_enabled:
        raise ValueError("automation is disabled by configuration")
    with automation_store(config) as store:
        service = AutomationWorkerService(
            store,
            config.workspace_root,
            max_concurrent_runs=1,
            lease_seconds=config.automation_lease_seconds,
            config_loader=automation_config_loader(config),
        )
        return await service.run_manual(reference)


def create_job_from_cli(
    config: AshConfig,
    *,
    name: str,
    prompt: str,
    at: str | None,
    every: str | None,
    cron: str | None,
    timezone_name: str,
    misfire_grace_seconds: int,
    timeout_seconds: float,
    token_budget: int,
) -> AutomationJob:
    schedule = build_schedule(
        at=at,
        every=every,
        cron=cron,
        timezone_name=timezone_name,
    )
    with automation_store(config) as store:
        return store.create_job(
            name=name,
            prompt=prompt,
            workspace=config.workspace_root,
            schedule=schedule,
            misfire_grace_seconds=misfire_grace_seconds,
            timeout_seconds=timeout_seconds,
            token_budget=token_budget,
        )
