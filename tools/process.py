"""Managed long-running workspace processes."""

from __future__ import annotations

import asyncio
import platform
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from safety.guard import SafetyGuard
from sandbox.process_utils import process_group_options, terminate_process_tree
from tools.base import BaseTool, ToolResult, count_output_tokens


@dataclass
class Job:
    job_id: str
    command: str
    process: asyncio.subprocess.Process
    output: list[str] = field(default_factory=list)
    cursor: int = 0
    readers: list[asyncio.Task[None]] = field(default_factory=list)


class BackgroundProcessArgs(BaseModel):
    action: str = Field(..., pattern="^(start|list|poll|write|stop)$")
    command: str = ""
    job_id: str = ""
    input: str = ""
    cwd: str | None = None


class BackgroundProcessTool(BaseTool):
    name = "background_process"
    description = "Start, list, poll, write to, or stop a managed background process."
    args_schema = BackgroundProcessArgs

    def __init__(self, safety_guard: SafetyGuard) -> None:
        super().__init__(safety_guard)
        self.jobs: dict[str, Job] = {}

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
            return self._result(f"{self._status(job)}\n{output}".rstrip())
        if args.action == "write":
            if job.process.stdin is None or job.process.returncode is not None:
                return ToolResult(
                    success=False, output="", error="Job stdin is unavailable"
                )
            job.process.stdin.write(args.input.encode())
            await job.process.stdin.drain()
            return self._result(f"Wrote {len(args.input)} bytes to {job.job_id}.")
        await terminate_process_tree(job.process)
        await asyncio.gather(*job.readers, return_exceptions=True)
        return self._result(f"Stopped {job.job_id}.")

    async def _start(self, args: BackgroundProcessArgs) -> ToolResult:
        if not args.command:
            return ToolResult(success=False, output="", error="start requires command")
        self.safety_guard.validate_command(args.command)
        cwd = self.safety_guard.validate_path(
            args.cwd or self.safety_guard.project_root
        )
        if platform.system() == "Windows":
            argv = ["powershell.exe", "-NoProfile", "-Command", args.command]
        else:
            argv = ["/bin/sh", "-c", args.command]
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_group_options(),
        )
        job = Job(uuid.uuid4().hex[:12], args.command, process)
        assert process.stdout is not None and process.stderr is not None
        job.readers = [
            asyncio.create_task(self._read(process.stdout, job, "")),
            asyncio.create_task(self._read(process.stderr, job, "[stderr] ")),
        ]
        self.jobs[job.job_id] = job
        return self._result(f"Started {job.job_id} (pid {process.pid}).")

    async def _read(self, stream: asyncio.StreamReader, job: Job, prefix: str) -> None:
        while line := await stream.readline():
            job.output.append(prefix + line.decode("utf-8", errors="replace"))

    @staticmethod
    def _status(job: Job) -> str:
        state = (
            "running"
            if job.process.returncode is None
            else f"exited({job.process.returncode})"
        )
        return f"{job.job_id} {state}: {job.command}"

    @staticmethod
    def _result(output: str) -> ToolResult:
        return ToolResult(
            success=True, output=output, token_count=count_output_tokens(output)
        )

    async def aclose(self) -> None:
        await asyncio.gather(
            *(terminate_process_tree(job.process) for job in self.jobs.values()),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(reader for job in self.jobs.values() for reader in job.readers),
            return_exceptions=True,
        )
