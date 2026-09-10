"""Managed long-running workspace processes."""

from __future__ import annotations

import asyncio
import platform
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

from ash.safety.guard import SafetyGuard
from ash.sandbox.process_utils import (
    ProcessTreeError,
    ProcessTreePlan,
    ProcessTreeUnavailable,
    prepare_process_tree,
    terminate_process_tree,
)
from ash.sandbox import SANDBOX_TIER_BWRAP, SandboxBackendUnavailable, SandboxManager
from ash.tools.base import BaseTool, ToolResult, count_output_tokens
from ash.tools.command import build_scrubbed_command_env


MAX_BACKGROUND_OUTPUT_CHARS = 100_000
MAX_BACKGROUND_JOBS = 32
MAX_BACKGROUND_COMMAND_CHARS = 100_000
MAX_BACKGROUND_INPUT_CHARS = 1_000_000
MAX_BACKGROUND_JOB_ID_CHARS = 128
MAX_BACKGROUND_CWD_CHARS = 4_096
BACKGROUND_OUTPUT_TRUNCATION_MARKER = (
    "\n[background process output truncated after "
    f"{MAX_BACKGROUND_OUTPUT_CHARS} characters]\n"
)


async def _settle_cleanup(
    awaitable: Awaitable[Any],
) -> tuple[Any, BaseException | None, bool]:
    """Finish cleanup despite caller cancellation, then report its outcome."""

    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    try:
        return task.result(), None, cancelled
    except BaseException as exc:
        return None, exc, cancelled


@dataclass
class Job:
    job_id: str
    command: str
    process: asyncio.subprocess.Process
    process_tree_plan: ProcessTreePlan
    output: list[str] = field(default_factory=list)
    cursor: int = 0
    output_size: int = 0
    output_truncated: bool = False
    readers: list[asyncio.Task[None]] = field(default_factory=list)
    sandbox_backend: str = "scoped"


class BackgroundProcessArgs(BaseModel):
    action: str = Field(..., pattern="^(start|list|poll|write|stop)$")
    command: str = Field("", max_length=MAX_BACKGROUND_COMMAND_CHARS)
    job_id: str = Field("", max_length=MAX_BACKGROUND_JOB_ID_CHARS)
    input: str = Field("", max_length=MAX_BACKGROUND_INPUT_CHARS)
    cwd: str | None = Field(None, max_length=MAX_BACKGROUND_CWD_CHARS)


class BackgroundProcessTool(BaseTool):
    name = "background_process"
    description = "Start, list, poll, write to, or stop a managed background process."
    args_schema = BackgroundProcessArgs

    def __init__(
        self,
        safety_guard: SafetyGuard,
        *,
        sandbox_manager: SandboxManager | None = None,
        environment_allowlist: Iterable[str] = (),
    ) -> None:
        super().__init__(safety_guard)
        self.jobs: dict[str, Job] = {}
        self.sandbox_manager = sandbox_manager
        self.environment_allowlist = tuple(environment_allowlist)

    async def run(self, **kwargs: Any) -> ToolResult:
        args = BackgroundProcessArgs(**kwargs)
        if args.action == "start":
            return await self._start(args)
        if args.action == "list":
            lines = [self._status(job) for job in self.jobs.values()]
            return self._result("\n".join(lines) or "No background jobs.")
        job = self.jobs.get(args.job_id)
        if job is None:
            return ToolResult(
                success=False, output="", error=f"Unknown job: {args.job_id}"
            )
        if args.action == "poll":
            output = "".join(job.output[job.cursor :])
            job.cursor = len(job.output)
            return self._result(
                f"{self._status(job)}\n{output}".rstrip(),
                truncated=job.output_truncated,
            )
        if args.action == "write":
            if job.process.stdin is None or job.process.returncode is not None:
                return ToolResult(
                    success=False, output="", error="Job stdin is unavailable"
                )
            job.process.stdin.write(args.input.encode())
            await job.process.stdin.drain()
            return self._result(f"Wrote {len(args.input)} bytes to {job.job_id}.")
        _, cleanup_error, cancelled = await _settle_cleanup(
            terminate_process_tree(job.process, plan=job.process_tree_plan)
        )
        if cancelled:
            cancellation = asyncio.CancelledError()
            if cleanup_error is not None:
                cancellation.add_note(f"Process-tree cleanup failed: {cleanup_error}")
            raise cancellation from cleanup_error
        if cleanup_error is not None:
            for reader in job.readers:
                if not reader.done():
                    reader.cancel()
            _, reader_error, reader_cancelled = await _settle_cleanup(
                asyncio.gather(*job.readers, return_exceptions=True)
            )
            if reader_cancelled:
                cancellation = asyncio.CancelledError()
                cancellation.add_note(f"Process-tree cleanup failed: {cleanup_error}")
                if reader_error is not None:
                    cancellation.add_note(f"reader cleanup failed: {reader_error}")
                raise cancellation from cleanup_error
            return ToolResult(
                success=False,
                output="",
                error=f"Could not stop {job.job_id}: {cleanup_error}",
            )
        _, reader_error, reader_cancelled = await _settle_cleanup(
            asyncio.gather(*job.readers, return_exceptions=True)
        )
        if reader_cancelled:
            raise asyncio.CancelledError from reader_error
        return self._result(f"Stopped {job.job_id}.")

    async def _start(self, args: BackgroundProcessArgs) -> ToolResult:
        if not args.command:
            return ToolResult(success=False, output="", error="start requires command")
        if len(self.jobs) >= MAX_BACKGROUND_JOBS:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Maximum of {MAX_BACKGROUND_JOBS} background jobs reached; "
                    "stop or finish existing jobs before starting another."
                ),
            )
        self.safety_guard.validate_command(args.command)
        cwd = self.safety_guard.validate_path(
            args.cwd or self.safety_guard.project_root
        )
        isolated = (
            self.sandbox_manager is not None
            and self.sandbox_manager.tier >= SANDBOX_TIER_BWRAP
        )
        if isolated or platform.system() != "Windows":
            argv = ["/bin/sh", "-c", args.command]
        else:
            argv = ["powershell.exe", "-NoProfile", "-Command", args.command]
        backend_name = "scoped"
        if self.sandbox_manager is not None:
            try:
                invocation = self.sandbox_manager.prepare(
                    argv,
                    cwd=Path(cwd),
                    passthrough_env_names=self.environment_allowlist,
                )
            except SandboxBackendUnavailable as exc:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Sandbox unavailable; command was not started: {exc}",
                )
            argv = list(invocation.argv)
            backend_name = invocation.backend_name
        try:
            process_tree_plan = prepare_process_tree(
                workspace_root=self.safety_guard.project_root
            )
        except ProcessTreeUnavailable as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Command was not started: {exc}",
            )
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=build_scrubbed_command_env(
                self.safety_guard.project_root, self.environment_allowlist
            ),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_tree_plan.spawn_options,
        )
        job = Job(
            uuid.uuid4().hex[:12],
            args.command,
            process,
            process_tree_plan,
            sandbox_backend=backend_name,
        )
        assert process.stdout is not None and process.stderr is not None
        job.readers = [
            asyncio.create_task(self._read(process.stdout, job, "")),
            asyncio.create_task(self._read(process.stderr, job, "[stderr] ")),
        ]
        self.jobs[job.job_id] = job
        return self._result(f"Started {job.job_id} (pid {process.pid}).")

    async def _read(self, stream: asyncio.StreamReader, job: Job, prefix: str) -> None:
        while chunk := await stream.read(4096):
            self._append_output(
                job,
                prefix + chunk.decode("utf-8", errors="replace"),
            )

    @staticmethod
    def _append_output(job: Job, text: str) -> None:
        """Retain a bounded preview while continuing to drain the child pipe."""

        if not text or job.output_truncated:
            return
        remaining = MAX_BACKGROUND_OUTPUT_CHARS - job.output_size
        if len(text) <= remaining:
            job.output.append(text)
            job.output_size += len(text)
            return
        if remaining:
            job.output.append(text[:remaining])
            job.output_size += remaining
        job.output.append(BACKGROUND_OUTPUT_TRUNCATION_MARKER)
        job.output_truncated = True

    @staticmethod
    def _status(job: Job) -> str:
        state = (
            "running"
            if job.process.returncode is None
            else f"exited({job.process.returncode})"
        )
        return f"{job.job_id} {state} [{job.sandbox_backend}]: {job.command}"

    @staticmethod
    def _result(output: str, *, truncated: bool = False) -> ToolResult:
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
            truncated=truncated,
        )

    async def aclose(self) -> None:
        jobs = tuple(self.jobs.values())
        cleanup_results, cleanup_error, cleanup_cancelled = await _settle_cleanup(
            asyncio.gather(
                *(
                    terminate_process_tree(
                        job.process,
                        plan=job.process_tree_plan,
                    )
                    for job in jobs
                ),
                return_exceptions=True,
            )
        )
        failures: list[BaseException] = []
        if cleanup_error is not None:
            failures.append(cleanup_error)
        elif isinstance(cleanup_results, list):
            failures.extend(
                result
                for result in cleanup_results
                if isinstance(result, BaseException)
            )
        if failures:
            for job in jobs:
                for reader in job.readers:
                    if not reader.done():
                        reader.cancel()
        _, reader_error, reader_cancelled = await _settle_cleanup(
            asyncio.gather(
                *(reader for job in jobs for reader in job.readers),
                return_exceptions=True,
            )
        )
        if cleanup_cancelled or reader_cancelled:
            cancellation = asyncio.CancelledError()
            for failure in failures:
                cancellation.add_note(f"Process-tree cleanup failed: {failure}")
            if reader_error is not None:
                cancellation.add_note(f"reader cleanup failed: {reader_error}")
            raise cancellation
        if failures:
            raise ProcessTreeError(
                f"background process cleanup failed: {failures[0]}"
            ) from failures[0]
