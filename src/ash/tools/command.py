"""Subprocess command execution tool."""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

from ash.core.redaction import StreamingRedactor
from ash.safety.environment import build_scrubbed_environment
from ash.safety.guard import SafetyGuard
from ash.sandbox import (
    SANDBOX_TIER_BWRAP,
    SANDBOX_TIER_SCOPED,
    SandboxBackendUnavailable,
    SandboxInvocation,
    SandboxManager,
    SandboxResult,
)
from ash.sandbox.process_utils import (
    ProcessOutputLimitExceeded,
    ProcessTreeError,
    ProcessTreeUnavailable,
    close_pty_fd,
    communicate_pty_process,
    communicate_process,
    open_process_pty,
    prepare_pty_process_argv,
    prepare_process_tree,
    prepare_scoped_process_launch,
    pty_process_spawn_options,
    settle_process_tree_after_cancellation,
    terminate_process_tree,
)
from ash.tools.base import BaseTool, ToolResult, count_output_tokens


def _directory_identity(path: Path) -> tuple[int, int] | None:
    try:
        metadata = os.stat(path)
    except OSError:
        return None
    return (metadata.st_dev, metadata.st_ino)


DEFAULT_TIMEOUT_SECONDS = 300
MAX_COMMAND_TIMEOUT_SECONDS = 86_400
MAX_COMMAND_INPUT_CHARS = 100_000
MAX_COMMAND_CWD_CHARS = 4_096
MAX_COMMAND_OUTPUT_CHARS = 100_000
OUTPUT_CAPTURE_LIMIT_NOTICE = "Process output capture limit reached."
MAX_DIAGNOSTIC_ITEMS = 50
MAX_SUMMARY_ITEMS = 8


class RunCommandArgs(BaseModel):
    command_line: str = Field(
        ...,
        max_length=MAX_COMMAND_INPUT_CHARS,
        description="The shell command string to execute.",
    )
    cwd: str | None = Field(
        None,
        max_length=MAX_COMMAND_CWD_CHARS,
        description="Directory path context to run the command in.",
    )
    timeout_seconds: int = Field(
        DEFAULT_TIMEOUT_SECONDS,
        ge=1,
        le=MAX_COMMAND_TIMEOUT_SECONDS,
        description="Hard timeout for subprocess execution.",
    )
    pty: bool = Field(
        False,
        description=(
            "Run the command in a POSIX pseudo-terminal. Use this only for "
            "TTY-required CLIs; stdout and stderr are merged as terminal output."
        ),
    )


def _diagnostic_from_line(line: str, *, severity: str) -> dict[str, Any] | None:
    """Extract one bounded diagnostic from common compiler/test/lint formats."""

    stripped = line.strip()
    if not stripped or len(stripped) > 1000:
        return None
    patterns = (
        re.compile(
            r"^(?P<path>[^:\n]+):(?P<line>\d+):(?P<column>\d+):"
            r"\s+(?P<severity>error|warning|note):\s*(?P<message>.+)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?P<path>[^:\n]+):(?P<line>\d+):\s+(?P<severity>error|warning|note):\s*(?P<message>.+)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?P<path>[^:\n]+\.py):(?P<line>\d+):\s+\[(?P<code>[A-Z0-9]+)\]\s+(?P<message>.+)$"
        ),
        re.compile(
            r"^FAILED\s+(?P<path>tests/[^\s:]+)::(?P<symbol>[^\s]+)"
            r"(?:\s+-\s+(?P<message>.+))?$"
        ),
    )
    for pattern in patterns:
        match = pattern.match(stripped)
        if match is None:
            continue
        values = match.groupdict()
        return {
            "path": values.get("path", ""),
            "line": int(values["line"]) if values.get("line") else None,
            "column": int(values["column"]) if values.get("column") else None,
            "symbol": values.get("symbol"),
            "code": values.get("code"),
            "message": values.get("message") or "",
            "severity": severity,
            "source_text": stripped[:500],
        }
    return None


def extract_diagnostics(output: str, error: str) -> list[dict[str, Any]]:
    """Bound structured diagnostics to the most actionable stream lines."""

    diagnostics: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source, severity in ((error, "error"), (output, "warning")):
        for line in source.splitlines():
            diagnostic = _diagnostic_from_line(line, severity=severity)
            if diagnostic is None:
                continue
            key = diagnostic["source_text"]
            if key in seen:
                continue
            seen.add(key)
            diagnostics.append(diagnostic)
            if len(diagnostics) >= MAX_DIAGNOSTIC_ITEMS:
                return diagnostics
    return diagnostics


def extract_diagnostic_summary(output: str, error: str) -> dict[str, int]:
    """Aggregate common framework result counts from concise summary lines."""

    combined = "\n".join((error, output))
    counts: dict[str, int] = {}
    patterns: tuple[tuple[str, re.Pattern[str]], ...] = (
        (
            "pytest_failed",
            re.compile(r"^=+\s+(?:FAILED|\d+ failed)", re.IGNORECASE),
        ),
        ("pytest_passed", re.compile(r"(\d+)\s+passed")),
        ("pytest_errors", re.compile(r"(\d+)\s+errors?")),
        ("ruff_fixable", re.compile(r"(\d+)\s+fixable")),
        (
            "mypy_error_count",
            re.compile(r"Found\s+(\d+)\s+error", re.IGNORECASE),
        ),
    )
    for line in combined.splitlines()[:2000]:
        stripped = line.strip()
        if not stripped:
            continue
        if "failed" in stripped.casefold():
            failed_matches = re.findall(r"(\d+)\s+failed", stripped, re.IGNORECASE)
            if failed_matches:
                counts["pytest_failed"] = sum(int(value) for value in failed_matches)
            elif re.search(r"^=+\s+FAILED\b|^\s*FAILED\b", stripped, re.IGNORECASE):
                counts["pytest_failed"] = max(counts.get("pytest_failed", 0), 1)
        for name, pattern in patterns[1:]:
            match = pattern.search(stripped)
            if match:
                value = int(match.group(1))
                counts[name] = max(counts.get(name, 0), value)
        if len(counts) >= MAX_SUMMARY_ITEMS:
            break
    return {name: value for name, value in sorted(counts.items())}


class RunCommandTool(BaseTool):
    name = "run_command"
    description = "Execute a shell command after safety validation."
    args_schema = RunCommandArgs

    def __init__(
        self,
        safety_guard: SafetyGuard,
        *,
        project_root: Path | None = None,
        sandbox_manager: SandboxManager | None = None,
        environment_allowlist: Iterable[str] = (),
    ) -> None:
        super().__init__(safety_guard)
        self.project_root = (
            project_root if project_root is not None else safety_guard.project_root
        )
        self._project_root_identity = (
            _directory_identity(Path(self.project_root))
            if self.project_root is not None
            else None
        )
        self.sandbox_manager = sandbox_manager
        self.environment_allowlist = tuple(environment_allowlist)

    async def run(self, **kwargs: Any) -> ToolResult:
        args = RunCommandArgs(**kwargs)
        self.safety_guard.validate_command(args.command_line)

        if self.project_root is not None and self._project_root_identity is not None:
            if _directory_identity(Path(self.project_root)) != self._project_root_identity:
                return ToolResult(
                    success=False,
                    output="",
                    error="Error: command was not started: working directory identity changed",
                )

        cwd = None
        expected_cwd_identity: tuple[int, int] | None = None
        if args.cwd is not None:
            cwd_path = self.safety_guard.validate_path(args.cwd)
            if not cwd_path.is_dir():
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Error: cwd is not a directory: {args.cwd}",
                )
            cwd = str(cwd_path)
            expected_cwd_identity = _directory_identity(cwd_path)
        elif self.project_root is not None:
            cwd = str(self.project_root)
            expected_cwd_identity = self._project_root_identity

        # Tier 2+ (bwrap / docker) wants a real argv so the sandbox
        # binary can exec it directly. Tier 1 (scoped) keeps the
        # original shell semantics via create_subprocess_shell.
        sandboxed = (
            self.sandbox_manager is not None
            and self.sandbox_manager.tier >= SANDBOX_TIER_BWRAP
        )

        streamer = _CommandEventStreamer(self.emit_event)
        try:
            if args.pty:
                return await self._run_pty(
                    ["/bin/sh", "-c", args.command_line],
                    args.timeout_seconds,
                    cwd,
                    env=build_scrubbed_command_env(
                        self.project_root, self.environment_allowlist
                    ),
                    passthrough_env_names=self.environment_allowlist,
                    stream_callback=streamer,
                    expected_cwd_identity=expected_cwd_identity,
                )
            if sandboxed:
                assert self.sandbox_manager is not None
                argv = ["/bin/sh", "-c", args.command_line]
                return await self._run_sandboxed(
                    argv,
                    args.timeout_seconds,
                    cwd,
                    env=build_scrubbed_command_env(
                        self.project_root, self.environment_allowlist
                    ),
                    passthrough_env_names=self.environment_allowlist,
                    stream_callback=streamer,
                    expected_cwd_identity=expected_cwd_identity,
                )

            return await self._run_scoped(
                args.command_line,
                args.timeout_seconds,
                cwd,
                env=build_scrubbed_command_env(
                    self.project_root, self.environment_allowlist
                ),
                stream_callback=streamer,
                expected_cwd_identity=expected_cwd_identity,
            )
        finally:
            streamer.finish()

    async def _run_pty(
        self,
        argv: list[str],
        timeout_seconds: int,
        cwd: str | None,
        *,
        env: dict[str, str],
        passthrough_env_names: tuple[str, ...],
        stream_callback: "_CommandEventStreamer",
        expected_cwd_identity: tuple[int, int] | None,
    ) -> ToolResult:
        if os.name != "posix":
            return ToolResult(
                success=False,
                output="",
                error="Error: PTY execution is supported only on Linux/macOS hosts",
            )
        workspace = self.project_root or (Path(cwd) if cwd is not None else Path.cwd())
        cwd_path = Path(cwd) if cwd is not None else workspace
        try:
            process_tree_plan = prepare_process_tree(workspace_root=workspace)
        except ProcessTreeUnavailable as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Error: command was not started: {exc}",
            )

        invocation_context: AbstractContextManager[SandboxInvocation]
        if self.sandbox_manager is None:
            invocation_context = nullcontext(
                SandboxInvocation(
                    tuple(argv),
                    cwd_path,
                    SANDBOX_TIER_SCOPED,
                    "scoped",
                )
            )
        else:
            invocation_context = self.sandbox_manager.prepare_launch(
                argv,
                cwd=cwd_path,
                passthrough_env_names=passthrough_env_names,
                pty=True,
            )

        master_fd: int | None = None
        slave_fd: int | None = None
        try:
            with invocation_context as invocation:
                with prepare_scoped_process_launch(
                    invocation.argv,
                    cwd=invocation.cwd,
                    guard=self.safety_guard,
                    search_path=env.get("PATH"),
                    expected_cwd_identity=expected_cwd_identity,
                ) as launch:
                    master_fd, slave_fd = open_process_pty()
                    inherited_fds = tuple(
                        dict.fromkeys((*invocation.pass_fds, *launch.pass_fds))
                    )
                    spawn_options = pty_process_spawn_options(process_tree_plan)
                    pty_argv = (
                        tuple(launch.argv)
                        if invocation.pty_claimed_in_backend
                        else prepare_pty_process_argv(
                            launch.argv,
                            plan=process_tree_plan,
                            search_path=env.get("PATH"),
                        )
                    )
                    if inherited_fds:
                        spawn_options["pass_fds"] = inherited_fds
                    process = await asyncio.create_subprocess_exec(
                        *pty_argv,
                        cwd=launch.cwd,
                        env=env,
                        stdin=slave_fd,
                        stdout=slave_fd,
                        stderr=slave_fd,
                        **spawn_options,
                    )
                    close_pty_fd(slave_fd)
                    slave_fd = None
                captured = await asyncio.wait_for(
                    communicate_pty_process(
                        process,
                        master_fd,
                        stream_callback=stream_callback,
                        max_output_bytes=MAX_COMMAND_OUTPUT_CHARS,
                        process_tree_plan=process_tree_plan,
                    ),
                    timeout=timeout_seconds,
                )
        except asyncio.TimeoutError:
            if "process" in locals():
                try:
                    await terminate_process_tree(process, plan=process_tree_plan)
                except ProcessTreeError as exc:
                    cleanup = f" Process-tree cleanup failed: {exc}."
                else:
                    cleanup = ""
            else:
                cleanup = ""
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Error: Command timed out after {timeout_seconds} seconds."
                    f"{cleanup}"
                ),
            )
        except asyncio.CancelledError as cancellation:
            if "process" in locals():
                cleanup_error, cleanup_cancelled = (
                    await settle_process_tree_after_cancellation(
                        process, plan=process_tree_plan
                    )
                )
                if cleanup_error is not None:
                    cancellation.add_note(
                        f"Process-tree cleanup failed: {cleanup_error}"
                    )
                if cleanup_cancelled:
                    cancellation.add_note("Process-tree cleanup was cancelled")
            raise
        except ProcessOutputLimitExceeded as exc:
            output = _redact_captured_output(decode_stream(exc.stdout))
            output, _ = _truncate_command_output(
                output,
                force=True,
                notice=OUTPUT_CAPTURE_LIMIT_NOTICE,
            )
            error = (
                f"Process-tree cleanup failed: {exc.cleanup_error}"
                if exc.cleanup_error is not None
                else None
            )
            return ToolResult(
                success=process.returncode == 0 and exc.cleanup_error is None,
                output=output,
                error=error,
                token_count=count_output_tokens(output),
                truncated=True,
                diagnostics=extract_diagnostics("", output),
                diagnostic_summary=extract_diagnostic_summary(output, ""),
            )
        except (SandboxBackendUnavailable, ProcessTreeUnavailable) as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Error: command was not started: {exc}",
            )
        except Exception as primary_error:
            if "process" in locals():
                cleanup_error, cleanup_cancelled = (
                    await settle_process_tree_after_cancellation(
                        process, plan=process_tree_plan
                    )
                )
                if cleanup_error is not None:
                    primary_error.add_note(
                        f"Process-tree cleanup failed: {cleanup_error}"
                    )
                if cleanup_cancelled:
                    raise asyncio.CancelledError from primary_error
            raise
        finally:
            close_pty_fd(slave_fd)
            close_pty_fd(master_fd)

        terminal_output = _redact_captured_output(decode_stream(captured))
        output, truncated = _truncate_command_output(terminal_output)
        if not invocation.fallback_used and invocation.tier >= SANDBOX_TIER_BWRAP:
            annotation = f"[sandbox tier={invocation.tier} backend={invocation.backend_name}]"
            output = f"{annotation}\n{output}" if output else annotation
        return ToolResult(
            success=process.returncode == 0,
            output=output,
            error=None,
            token_count=count_output_tokens(output),
            truncated=truncated,
            diagnostics=extract_diagnostics("", terminal_output),
            diagnostic_summary=extract_diagnostic_summary(terminal_output, ""),
        )

    async def _run_sandboxed(
        self,
        argv: list[str],
        timeout_seconds: int,
        cwd: str | None,
        *,
        env: dict[str, str],
        passthrough_env_names: tuple[str, ...],
        stream_callback: "_CommandEventStreamer",
        expected_cwd_identity: tuple[int, int] | None,
    ) -> ToolResult:
        assert self.sandbox_manager is not None
        from pathlib import Path

        cwd_path = Path(cwd) if cwd is not None else None
        try:
            result: SandboxResult = await self.sandbox_manager.run(
                argv,
                cwd=cwd_path,
                timeout=timeout_seconds,
                env=env,
                passthrough_env_names=passthrough_env_names,
                stream_callback=stream_callback,
                expected_cwd_identity=expected_cwd_identity,
            )
        except SandboxBackendUnavailable as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Sandbox unavailable; command was not run: {exc}",
            )
        redacted_stdout = _redact_captured_output(result.stdout)
        redacted_stderr = _redact_captured_output(result.stderr)
        output, truncated = _truncate_command_output(
            redacted_stdout,
            force=result.output_truncated and bool(redacted_stdout),
            notice=OUTPUT_CAPTURE_LIMIT_NOTICE,
        )
        error, error_truncated = _truncate_command_output(redacted_stderr)
        if error_truncated:
            truncated = True
        if result.output_truncated and not result.stdout:
            error, _ = _truncate_command_output(
                result.stderr,
                force=True,
                notice=OUTPUT_CAPTURE_LIMIT_NOTICE,
            )
            truncated = True
        elif result.output_truncated:
            truncated = True
        if not result.fallback_used and result.tier >= SANDBOX_TIER_BWRAP:
            annotation = f"[sandbox tier={result.tier} backend={result.backend_name}]"
            output = f"{annotation}\n{output}" if output else annotation
        return ToolResult(
            success=result.exit_code == 0,
            output=output,
            error=error or None,
            token_count=count_output_tokens(output),
            truncated=truncated,
            diagnostics=extract_diagnostics(result.stdout, result.stderr),
            diagnostic_summary=extract_diagnostic_summary(
                result.stdout,
                result.stderr,
            ),
        )

    async def _run_scoped(
        self,
        command_line: str,
        timeout_seconds: int,
        cwd: str | None,
        *,
        env: dict[str, str],
        stream_callback: "_CommandEventStreamer",
        expected_cwd_identity: tuple[int, int] | None,
    ) -> ToolResult:
        try:
            workspace = self.project_root or (
                Path(cwd) if cwd is not None else Path.cwd()
            )
            try:
                process_tree_plan = prepare_process_tree(workspace_root=workspace)
            except ProcessTreeUnavailable as exc:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Error: command was not started: {exc}",
                )
            cwd_target = Path(cwd) if cwd is not None else workspace
            try:
                with prepare_scoped_process_launch(
                    ["/bin/sh", "-c", command_line],
                    cwd=cwd_target,
                    guard=self.safety_guard,
                    search_path=env.get("PATH"),
                    expected_cwd_identity=expected_cwd_identity,
                ) as launch:
                    process = await asyncio.create_subprocess_exec(
                        *launch.argv,
                        cwd=launch.cwd,
                        env=env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        pass_fds=launch.pass_fds,
                        **process_tree_plan.spawn_options,
                    )
            except ProcessTreeUnavailable as exc:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Error: command was not started: {exc}",
                )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                communicate_process(
                    process,
                    stream_callback=stream_callback,
                    max_output_bytes=MAX_COMMAND_OUTPUT_CHARS,
                    process_tree_plan=process_tree_plan,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            if "process" in locals():
                try:
                    await terminate_process_tree(process, plan=process_tree_plan)
                except ProcessTreeError as exc:
                    cleanup = f" Process-tree cleanup failed: {exc}."
                else:
                    cleanup = ""
            else:
                cleanup = ""
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Error: Command timed out after {timeout_seconds} seconds."
                    f"{cleanup}"
                ),
            )
        except asyncio.CancelledError as cancellation:
            if "process" in locals():
                cleanup_error, cleanup_cancelled = (
                    await settle_process_tree_after_cancellation(
                        process, plan=process_tree_plan
                    )
                )
                if cleanup_error is not None:
                    cancellation.add_note(
                        f"Process-tree cleanup failed: {cleanup_error}"
                    )
                if cleanup_cancelled:
                    cancellation.add_note("Process-tree cleanup was cancelled")
            raise
        except ProcessOutputLimitExceeded as exc:
            stdout = _redact_captured_output(decode_stream(exc.stdout))
            stderr = _redact_captured_output(decode_stream(exc.stderr))
            output, _ = _truncate_command_output(
                stdout,
                force=True,
                notice=OUTPUT_CAPTURE_LIMIT_NOTICE,
            )
            error = _truncate_command_output(stderr)[0] if stderr else None
            if exc.cleanup_error is not None:
                cleanup = f"Process-tree cleanup failed: {exc.cleanup_error}"
                error = f"{error}; {cleanup}" if error else cleanup
            return ToolResult(
                success=process.returncode == 0 and exc.cleanup_error is None,
                output=output,
                error=error,
                token_count=count_output_tokens(output),
                truncated=True,
                diagnostics=extract_diagnostics(stdout, stderr),
                diagnostic_summary=extract_diagnostic_summary(stdout, stderr),
            )
        except Exception as primary_error:
            if "process" in locals():
                cleanup_error, cleanup_cancelled = (
                    await settle_process_tree_after_cancellation(
                        process, plan=process_tree_plan
                    )
                )
                if cleanup_error is not None:
                    primary_error.add_note(
                        f"Process-tree cleanup failed: {cleanup_error}"
                    )
                if cleanup_cancelled:
                    cleanup_cancellation = asyncio.CancelledError()
                    cleanup_cancellation.add_note(
                        "command failed before process-tree cleanup was cancelled"
                    )
                    if cleanup_error is not None:
                        cleanup_cancellation.add_note(
                            "Process-tree cleanup also failed"
                        )
                    raise cleanup_cancellation from primary_error
            raise

        stdout = _redact_captured_output(decode_stream(stdout_bytes))
        stderr = _redact_captured_output(decode_stream(stderr_bytes))
        output, truncated = _truncate_command_output(stdout)
        error, error_truncated = _truncate_command_output(stderr)

        if error_truncated:
            truncated = True

        return ToolResult(
            success=process.returncode == 0,
            output=output,
            error=error or None,
            token_count=count_output_tokens(output),
            truncated=truncated,
            diagnostics=extract_diagnostics(stdout, stderr),
            diagnostic_summary=extract_diagnostic_summary(stdout, stderr),
        )

async def run_command(safety_guard: SafetyGuard, **kwargs: Any) -> ToolResult:
    return await RunCommandTool(safety_guard).run(**kwargs)


def decode_stream(raw_bytes: bytes) -> str:
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return raw_bytes.decode("cp1252", errors="replace")


def build_scrubbed_command_env(
    project_root: Path | None = None,
    environment_allowlist: Iterable[str] = (),
) -> dict[str, str]:
    allowlist = tuple(environment_allowlist)
    env = build_scrubbed_environment(allowlist)
    if project_root is not None:
        env["ASH_WORKSPACE_ROOT"] = str(project_root)
        if not any(name.casefold() == "path" for name in allowlist):
            env["PATH"] = _sanitize_command_path(
                env.get("PATH", os.defpath),
                project_root,
            )
    return env


def _sanitize_command_path(path_value: str, project_root: Path) -> str:
    """Drop implicit PATH entries controlled by the active workspace."""

    root = project_root.expanduser().resolve()
    retained: list[str] = []
    for raw_entry in path_value.split(os.pathsep):
        if not raw_entry:
            continue
        entry = Path(raw_entry).expanduser()
        if not entry.is_absolute():
            continue
        try:
            resolved = entry.resolve()
        except OSError:
            continue
        try:
            resolved.relative_to(root)
        except ValueError:
            retained.append(str(resolved))
    return os.pathsep.join(dict.fromkeys(retained)) or os.defpath


def _redact_captured_output(output: str) -> str:
    """Redact captured process output without unbounded whole-buffer regex work."""

    redactor = StreamingRedactor()
    chunks = (
        redactor.feed(output[index : index + 4096])
        for index in range(0, len(output), 4096)
    )
    return "".join((*chunks, redactor.finish()))


def _truncate_command_output(
    output: str,
    *,
    force: bool = False,
    notice: str = "Output truncated. Command output exceeded 100000 characters.",
) -> tuple[str, bool]:
    if not force and len(output) <= MAX_COMMAND_OUTPUT_CHARS:
        return output, False
    return (
        output[:MAX_COMMAND_OUTPUT_CHARS]
        + f"\n[Warning: {notice}]",
        True,
    )


class _CommandEventStreamer:
    """Convert subprocess chunks into bounded, redacted typed events."""

    def __init__(self, emit: Any) -> None:
        self._emit = emit
        self._emitted_characters = 0
        self._truncated = False
        self._redactors = {
            "stdout": StreamingRedactor(),
            "stderr": StreamingRedactor(),
        }

    def __call__(self, stream: str, text: str) -> None:
        redactor = self._redactors[stream]
        delta = redactor.feed(text)
        if delta:
            self._send(stream, delta)

    def finish(self) -> None:
        for stream, redactor in self._redactors.items():
            delta = redactor.finish()
            if delta:
                self._send(stream, delta)

    def _send(self, stream: str, delta: str) -> None:
        if self._truncated:
            return
        remaining = MAX_COMMAND_OUTPUT_CHARS - self._emitted_characters
        if len(delta) > remaining:
            warning = (
                "\n[Warning: Live command output truncated after 100000 characters.]"
            )
            delta = delta[:remaining] + warning
            self._truncated = True
        self._emitted_characters += min(len(delta), remaining)
        if delta:
            try:
                self._emit({"type": "tool.output", "stream": stream, "delta": delta})
            except Exception:
                pass
