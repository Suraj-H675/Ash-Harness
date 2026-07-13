"""Public data contracts for durable Ash automations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


ScheduleKind = Literal["at", "every", "cron"]
AutomationRunStatus = Literal[
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
    "skipped",
]
UsageSource = Literal["unavailable", "provider", "estimated", "mixed"]


@dataclass(frozen=True)
class ScheduleSpec:
    """One normalized time trigger."""

    kind: ScheduleKind
    value: str
    timezone: str = "UTC"
    anchor_at: datetime | None = None


@dataclass(frozen=True)
class AutomationJob:
    job_id: str
    name: str
    prompt: str
    workspace: str
    schedule: ScheduleSpec
    enabled: bool
    next_run_at: datetime | None
    misfire_grace_seconds: int
    timeout_seconds: float
    token_budget: int
    created_at: datetime
    updated_at: datetime
    last_run_at: datetime | None = None
    last_run_status: AutomationRunStatus | None = None
    last_error: str | None = None
    consecutive_failures: int = 0


@dataclass(frozen=True)
class AutomationRun:
    run_id: str
    job_id: str
    scheduled_for: datetime
    status: AutomationRunStatus
    attempt: int
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    cancel_requested: bool = False
    session_id: str | None = None
    response: str | None = None
    error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    usage_source: UsageSource = "unavailable"
    estimated_prompt_tokens: int = 0
    estimated_completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    trigger: Literal["scheduled", "manual"] = "scheduled"


@dataclass(frozen=True)
class AutomationRunLease:
    job: AutomationJob
    run: AutomationRun
    token: str


@dataclass(frozen=True)
class AutomationWorker:
    worker_id: str
    workspace: str
    pid: int
    started_at: datetime
    heartbeat_at: datetime
    max_concurrent_runs: int


@dataclass
class AutomationWorkerSummary:
    """Terminal outcomes observed by one worker invocation."""

    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    interrupted: int = 0
    skipped: int = 0
    stopped: bool = False

    def record(self, run: AutomationRun) -> None:
        if run.status not in {
            "succeeded",
            "failed",
            "cancelled",
            "interrupted",
            "skipped",
        }:
            return
        self.completed += 1
        setattr(self, run.status, getattr(self, run.status) + 1)

    @property
    def ok(self) -> bool:
        return not self.stopped and self.failed == 0 and self.interrupted == 0

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "completed": self.completed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "interrupted": self.interrupted,
            "skipped": self.skipped,
            "stopped": self.stopped,
            "ok": self.ok,
        }
