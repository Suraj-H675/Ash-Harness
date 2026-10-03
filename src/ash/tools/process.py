"""Managed long-running workspace processes."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

from ash.core.redaction import (
    LONG_TOKEN_WITHHELD_MARKER,
    StreamingRedactor,
    redact_text,
)
from ash.safety.guard import SafetyGuard
from ash.sandbox.process_utils import (
    ProcessTreeError,
    ProcessTreePlan,
    ProcessTreeUnavailable,
    close_pty_fd,
    communicate_pty_process,
    open_process_pty,
    prepare_pty_process_argv,
    prepare_process_tree,
    prepare_scoped_process_launch,
    pty_process_spawn_options,
    terminate_process_tree,
    write_pty_input,
)
from ash.sandbox import (
    SANDBOX_TIER_SCOPED,
    SandboxBackendUnavailable,
    SandboxInvocation,
    SandboxManager,
)
from ash.tools.base import BaseTool, ToolResult, count_output_tokens
from ash.tools.command import build_scrubbed_command_env


def _directory_identity(path: Path) -> tuple[int, int] | None:
    try:
        metadata = os.stat(path)
    except OSError:
        return None
    return (metadata.st_dev, metadata.st_ino)


MAX_BACKGROUND_OUTPUT_CHARS = 100_000
MAX_BACKGROUND_JOBS = 32
MAX_BACKGROUND_HISTORY_JOBS = 32
MAX_BACKGROUND_COMMAND_CHARS = 100_000
MAX_BACKGROUND_INPUT_CHARS = 1_000_000
MAX_BACKGROUND_JOB_ID_CHARS = 128
MAX_BACKGROUND_CWD_CHARS = 4_096
BACKGROUND_CLOSE_REAP_GRACE_SECONDS = 0.5
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


async def _allow_natural_process_exit(
    process: asyncio.subprocess.Process,
    *,
    timeout: float = BACKGROUND_CLOSE_REAP_GRACE_SECONDS,
) -> None:
    """Give native exit notifications a bounded chance to update ``returncode``."""

    if process.returncode is not None:
        return
    deadline = asyncio.get_running_loop().time() + timeout
    while process.returncode is None and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)


async def _terminate_background_jobs(jobs: tuple["Job", ...]) -> list[BaseException]:
    """Terminate background jobs while avoiding concurrent Windows taskkill races."""

    failures: list[BaseException] = []
    windows_jobs = tuple(job for job in jobs if job.process_tree_plan.is_windows)
    other_jobs = tuple(job for job in jobs if not job.process_tree_plan.is_windows)
    if other_jobs:
        results = await asyncio.gather(
            *(
                terminate_process_tree(job.process, plan=job.process_tree_plan)
                for job in other_jobs
            ),
            return_exceptions=True,
        )
        failures.extend(
            result for result in results if isinstance(result, BaseException)
        )
    for job in windows_jobs:
        try:
            await terminate_process_tree(job.process, plan=job.process_tree_plan)
        except BaseException as exc:
            failures.append(exc)
    return failures


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
    stdout_redactor: StreamingRedactor = field(default_factory=StreamingRedactor)
    stderr_redactor: StreamingRedactor = field(default_factory=StreamingRedactor)
    pty_master_fd: int | None = None
    pty_write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BackgroundProcessArgs(BaseModel):
    action: str = Field(..., pattern="^(start|list|poll|write|stop)$")
    command: str = Field("", max_length=MAX_BACKGROUND_COMMAND_CHARS)
    job_id: str = Field("", max_length=MAX_BACKGROUND_JOB_ID_CHARS)
    input: str = Field("", max_length=MAX_BACKGROUND_INPUT_CHARS)
    cwd: str | None = Field(None, max_length=MAX_BACKGROUND_CWD_CHARS)
    pty: bool = Field(
        False,
        description=(
            "Allocate a POSIX pseudo-terminal for a started process. PTY output "
            "merges stdout/stderr and write sends input to the terminal."
        ),
    )


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
        self._project_root_identity = _directory_identity(
            self.safety_guard.project_root
        )
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
            if job.process.returncode is not None:
                return ToolResult(
                    success=False, output="", error="Job stdin is unavailable"
                )
            if job.pty_master_fd is not None:
                try:
                    async with job.pty_write_lock:
                        if job.pty_master_fd is None:
                            raise OSError("PTY input closed")
                        await write_pty_input(
                            job.pty_master_fd,
                            args.input.encode(),
                        )
                except (OSError, asyncio.TimeoutError):
                    return ToolResult(
                        success=False,
                        output="",
                        error="Job PTY input is unavailable",
                    )
            else:
                if job.process.stdin is None:
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
        running_jobs = sum(
            1 for job in self.jobs.values() if job.process.returncode is None
        )
        if running_jobs >= MAX_BACKGROUND_JOBS:
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Maximum of {MAX_BACKGROUND_JOBS} running background jobs reached; "
                    "stop or finish an existing job before starting another."
                ),
            )
        self._prune_terminal_history()
        self.safety_guard.validate_command(args.command)
        if (
            self._project_root_identity is not None
            and _directory_identity(self.safety_guard.project_root)
            != self._project_root_identity
        ):
            return ToolResult(
                success=False,
                output="",
                error="Command was not started: working directory identity changed",
            )
        cwd = self.safety_guard.validate_path(
            args.cwd or self.safety_guard.project_root
        )
        expected_cwd_identity = _directory_identity(cwd)
        environment = build_scrubbed_command_env(
            self.safety_guard.project_root, self.environment_allowlist
        )
        argv = ["/bin/sh", "-c", args.command]
        backend_name = "scoped"
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
        try:
            invocation_context: AbstractContextManager[SandboxInvocation]
            if self.sandbox_manager is None:
                invocation_context = nullcontext(
                    SandboxInvocation(
                        tuple(argv),
                        Path(cwd),
                        SANDBOX_TIER_SCOPED,
                        "scoped",
                    )
                )
            else:
                invocation_context = self.sandbox_manager.prepare_launch(
                    argv,
                    cwd=Path(cwd),
                    passthrough_env_names=self.environment_allowlist,
                    pty=args.pty,
                )
            with invocation_context as invocation:
                backend_name = invocation.backend_name
                with prepare_scoped_process_launch(
                    invocation.argv,
                    cwd=invocation.cwd,
                    guard=self.safety_guard,
                    search_path=environment.get("PATH"),
                    expected_cwd_identity=expected_cwd_identity,
                ) as launch:
                    inherited_fds = tuple(
                        dict.fromkeys((*invocation.pass_fds, *launch.pass_fds))
                    )
                    spawn_options = dict(process_tree_plan.spawn_options)
                    master_fd: int | None = None
                    slave_fd: int | None = None
                    if args.pty:
                        spawn_options = pty_process_spawn_options(process_tree_plan)
                        launch_argv = (
                            tuple(launch.argv)
                            if invocation.pty_claimed_in_backend
                            else prepare_pty_process_argv(
                                launch.argv,
                                plan=process_tree_plan,
                                search_path=environment.get("PATH"),
                            )
                        )
                        master_fd, slave_fd = open_process_pty()
                    else:
                        launch_argv = launch.argv
                    if inherited_fds:
                        spawn_options["pass_fds"] = inherited_fds
                    try:
                        process = await asyncio.create_subprocess_exec(
                            *launch_argv,
                            cwd=launch.cwd,
                            env=environment,
                            stdin=(slave_fd if args.pty else asyncio.subprocess.PIPE),
                            stdout=(slave_fd if args.pty else asyncio.subprocess.PIPE),
                            stderr=(slave_fd if args.pty else asyncio.subprocess.PIPE),
                            **spawn_options,
                        )
                    except BaseException:
                        close_pty_fd(master_fd)
                        close_pty_fd(slave_fd)
                        raise
                    close_pty_fd(slave_fd)
        except SandboxBackendUnavailable as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Sandbox unavailable; command was not started: {exc}",
            )
        except ProcessTreeUnavailable as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Command was not started: {exc}",
            )
        job = Job(
            uuid.uuid4().hex[:12],
            args.command,
            process,
            process_tree_plan,
            sandbox_backend=backend_name,
            pty_master_fd=master_fd if args.pty else None,
        )
        if args.pty:
            assert job.pty_master_fd is not None
            job.readers = [asyncio.create_task(self._read_pty(job))]
        else:
            assert process.stdout is not None and process.stderr is not None
            job.readers = [
                asyncio.create_task(
                    self._read(process.stdout, job, "", job.stdout_redactor)
                ),
                asyncio.create_task(
                    self._read(
                        process.stderr,
                        job,
                        "[stderr] ",
                        job.stderr_redactor,
                    )
                ),
            ]
        self.jobs[job.job_id] = job
        return self._result(f"Started {job.job_id} (pid {process.pid}).")

    async def _read_pty(self, job: Job) -> None:
        assert job.pty_master_fd is not None

        def receive(_stream: str, text: str) -> None:
            delta = job.stdout_redactor.feed(text)
            if delta:
                self._append_redacted_output(job, delta)

        try:
            await communicate_pty_process(
                job.process,
                job.pty_master_fd,
                stream_callback=receive,
                process_tree_plan=job.process_tree_plan,
                capture_output=False,
            )
        finally:
            async with job.pty_write_lock:
                master_fd = job.pty_master_fd
                job.pty_master_fd = None
                close_pty_fd(master_fd)
            delta = job.stdout_redactor.finish()
            if delta:
                self._append_redacted_output(job, delta)

    def _prune_terminal_history(self) -> None:
        """Bound retained terminal-job history without consuming live capacity."""

        terminal_ids = [
            job_id
            for job_id, job in self.jobs.items()
            if job.process.returncode is not None
        ]
        excess = len(terminal_ids) - MAX_BACKGROUND_HISTORY_JOBS + 1
        for job_id in terminal_ids[: max(0, excess)]:
            self.jobs.pop(job_id, None)

    async def _read(
        self,
        stream: asyncio.StreamReader,
        job: Job,
        prefix: str,
        redactor: StreamingRedactor,
    ) -> None:
        try:
            while chunk := await stream.read(4096):
                delta = redactor.feed(chunk.decode("utf-8", errors="replace"))
                if delta:
                    self._append_redacted_output(job, prefix + delta)
        finally:
            delta = redactor.finish()
            if delta:
                self._append_redacted_output(job, prefix + delta)

    @staticmethod
    def _append_redacted_output(job: Job, text: str) -> None:
        BackgroundProcessTool._append_output(job, text)
        if LONG_TOKEN_WITHHELD_MARKER in text:
            job.output_truncated = True

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
        return (
            f"{job.job_id} {state} [{job.sandbox_backend}"
            f"{'/pty' if job.pty_master_fd is not None else ''}]: "
            f"{redact_text(job.command)}"
        )

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
        _, grace_error, grace_cancelled = await _settle_cleanup(
            asyncio.gather(
                *(
                    _allow_natural_process_exit(job.process)
                    for job in jobs
                    if job.process.returncode is None
                ),
                return_exceptions=True,
            )
        )
        cleanup_jobs = tuple(
            job
            for job in jobs
            if job.process.returncode is None
            or (
                not job.process_tree_plan.is_windows
                and any(not reader.done() for reader in job.readers)
            )
        )
        cleanup_results, cleanup_error, cleanup_cancelled = await _settle_cleanup(
            _terminate_background_jobs(cleanup_jobs)
        )
        failures: list[BaseException] = []
        if cleanup_error is not None:
            failures.append(cleanup_error)
        elif isinstance(cleanup_results, list):
            failures.extend(cleanup_results)
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
        if grace_cancelled or cleanup_cancelled or reader_cancelled:
            cancellation = asyncio.CancelledError()
            if grace_error is not None:
                cancellation.add_note(f"natural-exit grace failed: {grace_error}")
            for failure in failures:
                cancellation.add_note(f"Process-tree cleanup failed: {failure}")
            if reader_error is not None:
                cancellation.add_note(f"reader cleanup failed: {reader_error}")
            raise cancellation
        if failures:
            raise ProcessTreeError(
                f"background process cleanup failed: {failures[0]}"
            ) from failures[0]
