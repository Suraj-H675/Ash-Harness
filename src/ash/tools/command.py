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
    ProcessTreeUnavailable,
    close_pty_fd,
    communicate_pty_process,
    communicate_process,
    open_process_pty,
    prepare_pty_process_argv,
    prepare_process_tree,
    prepare_scoped_process_launch,
    settle_process_tree_after_cancellation,
    spawn_process_tree,
    terminate_process_tree_after_timeout,
)
from ash.tools.base import (
    BaseTool,
    ToolExecutionOutcome,
    ToolResult,
    count_output_tokens,
)


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
MAX_COMMAND_TAIL_CHARS = 12_000
# Preserve a bounded live transcript while letting ordinary verbose test/build
# commands finish. Truly runaway output still terminates the process tree.
MAX_COMMAND_DRAIN_BYTES = 2_000_000
OUTPUT_CAPTURE_LIMIT_NOTICE = "Process output capture limit reached."
MAX_DIAGNOSTIC_ITEMS = 50
MAX_SUMMARY_ITEMS = 8
MAX_SUMMARY_SCAN_LINES = 2_000
MAX_SUMMARY_HEAD_LINES = 64
_RUFF_CODE = r"[A-Z]{1,4}\d{3,4}"
_RUFF_HEADING = re.compile(rf"^(?P<code>{_RUFF_CODE})(?:\s+\[\*\])?\s+(?P<message>.+)$")
_RUST_HEADING = re.compile(
    r"^(?P<severity>error|warning)(?:\[(?P<code>[A-Z]\d{4})\])?: "
    r"(?P<message>.+)$"
)
_COMPILER_LOCATION = re.compile(
    r"^\s+-->\s+(?P<path>[^:\n]+):(?P<line>\d+):(?P<column>\d+)\s*$"
)
_PYTEST_SUMMARY_TIME = re.compile(r"\bin\s+\d+(?:\.\d+)?s\b")
_PYTEST_SUMMARY_COUNT = re.compile(r"\b(\d+)\s+(failed|passed|errors?)\b", re.I)
_MYPY_SUMMARY = re.compile(
    r"^Found\s+(\d+)\s+errors?\s+in\s+\d+\s+files?\s+"
    r"\(checked\s+\d+\s+source\s+files?\)",
    re.I,
)
_RUFF_ERROR_SUMMARY = re.compile(r"^Found\s+(\d+)\s+errors?\.$", re.I)
_RUFF_FIXABLE = re.compile(r"\b(\d+)\s+fixable\s+with\s+the\s+[`']?--fix", re.I)
_PYTEST_FAILURE_HEADER = re.compile(r"^={3,}\s+(?:FAILURES|ERRORS)\s+={3,}$")
_PYTEST_SECTION_END = re.compile(r"^={3,}\s+short test summary info\s+={3,}$", re.I)
_PYTEST_TRACE_LOCATION = re.compile(
    r"^(?P<path>[^\s:]+\.py):(?P<line>\d+):\s+in\s+(?P<symbol>\S+)\s*$"
)
_PYTEST_EXCEPTION_LOCATION = re.compile(
    r"^(?P<path>[^\s:]+\.py):(?P<line>\d+):\s+"
    r"(?P<exception>[\w.]+(?:Error|Exception|Failed))"
    r"(?::\s*(?P<message>.*))?$"
)
_PYTEST_ERROR_DETAIL = re.compile(r"^E\s+(?P<message>[^\n]+)$")
_PYTHON_TRACEBACK_FRAME = re.compile(
    r'^\s+File "(?P<path>[^"\n]+\.py)", line (?P<line>\d+)'
    r'(?:, in (?P<symbol>[^\n]+))?$'
)
_PYTHON_TRACEBACK_EXCEPTION = re.compile(
    r"^(?P<exception>(?:[A-Za-z_]\w*\.)*"
    r"[A-Za-z_]\w*(?:Error|Exception|Exit|Interrupt))"
    r"(?::\s*(?P<message>[^\n]*))?$"
)


class RunCommandArgs(BaseModel):
    command_line: str = Field(
        ...,
        max_length=MAX_COMMAND_INPUT_CHARS,
        description=(
            "Shell command to execute. If the user specified an exact command, "
            "send it verbatim; do not prefix it with cd or other shell commands. "
            "Exact command grants apply to this command_line string."
        ),
    )
    cwd: str | None = Field(
        None,
        max_length=MAX_COMMAND_CWD_CHARS,
        description=(
            "Optional directory within the project workspace. If omitted, "
            "the command runs from the project root (Current Project Path). "
            "Omit for a user-specified exact command unless told otherwise."
        ),
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
            rf"^(?P<path>[^:\n]+):(?P<line>\d+):(?P<column>\d+):\s+"
            rf"(?P<code>{_RUFF_CODE})\s+(?P<message>.+)$"
        ),
        re.compile(
            r"^(?P<path>[^:\n]+\.go):(?P<line>\d+):(?P<column>\d+):\s+"
            r"(?P<message>.+)$"
        ),
        re.compile(
            r"^(?P<pytest_kind>FAILED|ERROR)\s+"
            r"(?P<path>[^\s:]+\.py)(?:::(?P<symbol>[^\s]+))?"
            r"(?:\s+-\s+(?P<message>.+))?$"
        ),
    )
    for pattern in patterns:
        match = pattern.match(stripped)
        if match is None:
            continue
        values = match.groupdict()
        explicit_severity = values.get("severity")
        if explicit_severity:
            item_severity = explicit_severity.lower()
        elif (
            values.get("code")
            or values.get("symbol")
            or values.get("pytest_kind")
            or (values.get("column") and values["path"].endswith(".go"))
        ):
            item_severity = "error"
        else:
            item_severity = severity
        return {
            "path": values.get("path", ""),
            "line": int(values["line"]) if values.get("line") else None,
            "column": int(values["column"]) if values.get("column") else None,
            "symbol": values.get("symbol"),
            "code": values.get("code"),
            "message": values.get("message") or "",
            "severity": item_severity,
            "source_text": stripped[:500],
        }
    return None


def _diagnostic_from_compiler_pair(header: str, location: str) -> dict[str, Any] | None:
    match = _COMPILER_LOCATION.match(location)
    if match is None:
        return None
    heading = _RUFF_HEADING.fullmatch(header.strip())
    if heading is not None:
        severity = "error"
    else:
        heading = _RUST_HEADING.fullmatch(header.strip())
        if heading is None:
            return None
        severity = heading["severity"]
    path = match["path"]
    line = int(match["line"])
    column = int(match["column"])
    code = heading["code"]
    message = heading["message"]
    return {
        "path": path,
        "line": line,
        "column": column,
        "symbol": None,
        "code": code,
        "message": message,
        "severity": severity,
        "source_text": f"{path}:{line}:{column}: {code or severity} {message}"[:500],
    }


def _pytest_failure_locations(output: str) -> list[dict[str, Any]]:
    """Extract bounded pytest traceback sites only inside failure sections.

    Pytest's short tracebacks use `path.py:line: in test_name` followed by
    `E   assert ...`, not the `path:line: error:` shape that ordinary compiler
    parsers recognize. Never classify arbitrary passing command logs this way.
    """

    locations: list[dict[str, Any]] = []
    in_failure = False
    pending: dict[str, Any] | None = None

    def retain_pending() -> None:
        nonlocal pending
        if pending is not None and len(locations) < MAX_DIAGNOSTIC_ITEMS:
            pending["source_text"] = (
                f"{pending['path']}:{pending['line']}: {pending['message']}"
            )[:500]
            locations.append(pending)
        pending = None

    for line in output.splitlines():
        if len(locations) >= MAX_DIAGNOSTIC_ITEMS:
            break
        stripped = line.strip()
        if _PYTEST_FAILURE_HEADER.fullmatch(stripped):
            retain_pending()
            in_failure = True
            continue
        if _PYTEST_SECTION_END.fullmatch(stripped):
            retain_pending()
            in_failure = False
        if not in_failure or len(stripped) > 1000:
            continue
        location = _PYTEST_TRACE_LOCATION.fullmatch(stripped)
        if location is not None:
            retain_pending()
            pending = {
                "path": location["path"],
                "line": int(location["line"]),
                "column": None,
                "symbol": location["symbol"],
                "code": None,
                "message": "pytest traceback location",
                "severity": "error",
            }
            continue
        exception = _PYTEST_EXCEPTION_LOCATION.fullmatch(stripped)
        if exception is not None:
            retain_pending()
            message = exception["message"] or exception["exception"]
            pending = {
                "path": exception["path"],
                "line": int(exception["line"]),
                "column": None,
                "symbol": None,
                "code": exception["exception"],
                "message": message[:500],
                "severity": "error",
            }
            continue
        detail = _PYTEST_ERROR_DETAIL.fullmatch(stripped)
        if detail is not None and pending is not None and (
            pending["message"] == "pytest traceback location"
        ):
            message = detail["message"]
            if not message.startswith("+"):
                pending["message"] = message[:500]
                exception_type = _PYTHON_TRACEBACK_EXCEPTION.fullmatch(message)
                pending["code"] = (
                    "AssertionError"
                    if message.startswith("assert ")
                    else exception_type["exception"] if exception_type else None
                )
    retain_pending()
    return locations


def _python_traceback_locations(output: str) -> list[dict[str, Any]]:
    """Find the innermost Python exception frame from real traceback blocks.

    A frame-shaped line alone is insufficient: require a traceback header and
    a subsequent terminal exception type. No file is opened or executed.
    """

    locations: list[dict[str, Any]] = []
    traceback_active = False
    last_frame: re.Match[str] | None = None
    for line in output.splitlines():
        if len(locations) >= MAX_DIAGNOSTIC_ITEMS:
            break
        stripped = line.strip()
        if stripped == "Traceback (most recent call last):":
            traceback_active = True
            last_frame = None
            continue
        if not traceback_active or len(stripped) > 1000:
            continue
        frame = _PYTHON_TRACEBACK_FRAME.fullmatch(line)
        if frame is not None:
            last_frame = frame
            continue
        exception = _PYTHON_TRACEBACK_EXCEPTION.fullmatch(stripped)
        if exception is None or last_frame is None:
            continue
        path = last_frame["path"]
        line_number = int(last_frame["line"])
        exception_type = exception["exception"]
        message = (exception["message"] or exception_type)[:500]
        locations.append({
            "path": path,
            "line": line_number,
            "column": None,
            "symbol": last_frame["symbol"],
            "code": exception_type,
            "message": message,
            "severity": "error",
            "source_text": f"{path}:{line_number}: {exception_type}: {message}"[:500],
        })
        traceback_active = False
        last_frame = None
    return locations


def extract_diagnostics(output: str, error: str) -> list[dict[str, Any]]:
    """Bound structured diagnostics to the most actionable stream lines."""

    diagnostics: list[dict[str, Any]] = []
    seen: set[str] = set()
    sources = ((error, "error"), (output, "warning"))
    # Structured terminal traceback evidence must not disappear simply
    # because stderr contains fifty earlier generic compiler warnings.
    for source, _ in sources:
        for structured in (
            *_pytest_failure_locations(source),
            *_python_traceback_locations(source),
        ):
            key = structured["source_text"]
            if key in seen:
                continue
            seen.add(key)
            diagnostics.append(structured)
            if len(diagnostics) >= MAX_DIAGNOSTIC_ITEMS:
                return diagnostics
    for source, severity in sources:
        previous_line = ""
        for line in source.splitlines():
            diagnostic = _diagnostic_from_line(line, severity=severity)
            if diagnostic is None:
                diagnostic = _diagnostic_from_compiler_pair(previous_line, line)
            previous_line = line
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


def _bounded_summary_lines(stream: str) -> list[str]:
    """Preserve terminal summaries from verbose stdout and stderr separately.

    Output has already been captured and redacted under command-tool limits.
    Retain a small header window for formatter identification (e.g. Ruff)
    and the final result window; never scan only the first N log lines.
    """

    lines = stream.splitlines()
    if len(lines) <= MAX_SUMMARY_SCAN_LINES:
        return lines
    tail_lines = MAX_SUMMARY_SCAN_LINES - MAX_SUMMARY_HEAD_LINES
    return lines[:MAX_SUMMARY_HEAD_LINES] + lines[-tail_lines:]


def extract_diagnostic_summary(output: str, error: str) -> dict[str, int]:
    """Aggregate common framework result counts from concise summary lines."""

    counts: dict[str, int] = {}
    # Both streams may contain thousands of lines. A large stderr prelude
    # cannot hide a final pytest result on stdout, or vice versa.
    lines = _bounded_summary_lines(error) + _bounded_summary_lines(output)
    has_ruff_codes = any(
        _RUFF_HEADING.fullmatch(line.strip())
        or re.match(rf"^[^:\n]+:\d+:\d+:\s+{_RUFF_CODE}\s+", line)
        for line in lines
    )
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("FAILED tests/"):
            counts["pytest_failed"] = max(counts.get("pytest_failed", 0), 1)
        if _PYTEST_SUMMARY_TIME.search(stripped):
            for match in _PYTEST_SUMMARY_COUNT.finditer(stripped):
                label = match[2].casefold()
                name = {
                    "failed": "pytest_failed",
                    "passed": "pytest_passed",
                }.get(label, "pytest_errors")
                counts[name] = max(counts.get(name, 0), int(match[1]))
        mypy_match = _MYPY_SUMMARY.match(stripped)
        if mypy_match:
            counts["mypy_error_count"] = max(
                counts.get("mypy_error_count", 0), int(mypy_match[1])
            )
        ruff_match = _RUFF_ERROR_SUMMARY.match(stripped)
        if has_ruff_codes and ruff_match:
            counts["ruff_error_count"] = max(
                counts.get("ruff_error_count", 0), int(ruff_match[1])
            )
        fixable_match = _RUFF_FIXABLE.search(stripped)
        if fixable_match:
            counts["ruff_fixable"] = max(
                counts.get("ruff_fixable", 0), int(fixable_match[1])
            )
        if len(counts) >= MAX_SUMMARY_ITEMS:
            break
    return {name: value for name, value in sorted(counts.items())}


class RunCommandTool(BaseTool):
    name = "run_command"
    description = (
        "Run visible tests (such as pytest) to verify a patch or fix, or "
        "execute another shell command after safety validation, starting in the "
        "current project root by default. Command authorization is checked "
        "against the supplied arguments before execution."
    )
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
                result = await self._run_pty(
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
            elif sandboxed:
                assert self.sandbox_manager is not None
                argv = ["/bin/sh", "-c", args.command_line]
                result = await self._run_sandboxed(
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

            else:
                result = await self._run_scoped(
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
        return _successful_command_stderr_as_output(
            _preserve_command_tail(result, streamer)
        )

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
                    pty_argv = prepare_pty_process_argv(
                        launch.argv,
                        plan=process_tree_plan,
                        search_path=env.get("PATH"),
                    )
                    process = await spawn_process_tree(
                        process_tree_plan,
                        *pty_argv,
                        cwd=launch.cwd,
                        env=env,
                        stdin=slave_fd,
                        stdout=slave_fd,
                        stderr=slave_fd,
                        pass_fds=inherited_fds or None,
                    )
                    close_pty_fd(slave_fd)
                    slave_fd = None
                captured = await asyncio.wait_for(
                    communicate_pty_process(
                        process,
                        master_fd,
                        stream_callback=stream_callback,
                        max_output_bytes=MAX_COMMAND_OUTPUT_CHARS,
                        max_drain_bytes=MAX_COMMAND_DRAIN_BYTES,
                        process_tree_plan=process_tree_plan,
                    ),
                    timeout=timeout_seconds,
                )
        except asyncio.TimeoutError:
            process_started = "process" in locals()
            if process_started:
                cleanup_error = await terminate_process_tree_after_timeout(
                    process, plan=process_tree_plan
                )
                cleanup = (
                    f" Process-tree cleanup failed: {cleanup_error}."
                    if cleanup_error is not None
                    else ""
                )
            else:
                cleanup = ""
            return ToolResult(
                success=False,
                output="",
                outcome=(
                    ToolExecutionOutcome.UNKNOWN
                    if process_started
                    else ToolExecutionOutcome.COMPLETED
                ),
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
                exit_code=process.returncode if not exc.interrupted else None,
                outcome=(
                    ToolExecutionOutcome.UNKNOWN
                    if exc.interrupted
                    else ToolExecutionOutcome.COMPLETED
                ),
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
            exit_code=process.returncode,
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
                max_drain_bytes=MAX_COMMAND_DRAIN_BYTES,
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
                redacted_stderr,
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
            exit_code=(
                result.exit_code
                if not result.timed_out and not result.outcome_unknown
                else None
            ),
            outcome=(
                ToolExecutionOutcome.UNKNOWN
                if result.timed_out or result.outcome_unknown
                else ToolExecutionOutcome.COMPLETED
            ),
            token_count=count_output_tokens(output),
            truncated=truncated,
            diagnostics=extract_diagnostics(redacted_stdout, redacted_stderr),
            diagnostic_summary=extract_diagnostic_summary(
                redacted_stdout,
                redacted_stderr,
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
                    process = await spawn_process_tree(
                        process_tree_plan,
                        *launch.argv,
                        cwd=launch.cwd,
                        env=env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        pass_fds=launch.pass_fds or None,
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
                    max_drain_bytes=MAX_COMMAND_DRAIN_BYTES,
                    process_tree_plan=process_tree_plan,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            process_started = "process" in locals()
            if process_started:
                cleanup_error = await terminate_process_tree_after_timeout(
                    process, plan=process_tree_plan
                )
                cleanup = (
                    f" Process-tree cleanup failed: {cleanup_error}."
                    if cleanup_error is not None
                    else ""
                )
            else:
                cleanup = ""
            return ToolResult(
                success=False,
                output="",
                outcome=(
                    ToolExecutionOutcome.UNKNOWN
                    if process_started
                    else ToolExecutionOutcome.COMPLETED
                ),
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
                exit_code=process.returncode if not exc.interrupted else None,
                outcome=(
                    ToolExecutionOutcome.UNKNOWN
                    if exc.interrupted
                    else ToolExecutionOutcome.COMPLETED
                ),
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
            exit_code=process.returncode,
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


def _successful_command_stderr_as_output(result: ToolResult) -> ToolResult:
    """Retain nonfatal stderr without recording a false command failure.

    Verification gates use the durable error field as an execution-failure
    signal. A *known completed* zero-exit command may legitimately write
    warnings to stderr. Preserve those already-redacted bytes in model-visible
    output, but never reclassify a failed or ambiguously completed process.
    """

    if (
        not result.success
        or result.exit_code != 0
        or result.outcome is not ToolExecutionOutcome.COMPLETED
        or not result.error
    ):
        return result

    label = "\n[stderr]\n"
    stderr = result.error
    truncated = result.truncated
    if len(label) + len(stderr) >= MAX_COMMAND_OUTPUT_CHARS:
        warning = "[Warning: earlier successful-command stderr omitted]\n"
        available = max(0, MAX_COMMAND_OUTPUT_CHARS - len(label) - len(warning))
        output = label + warning + (stderr[-available:] if available else "")
        truncated = True
    else:
        available = MAX_COMMAND_OUTPUT_CHARS - len(label) - len(stderr)
        stdout = result.output
        if len(stdout) > available:
            marker = "\n[Warning: stdout middle omitted to retain stderr]\n"
            if available > len(marker):
                retained = available - len(marker)
                head = retained // 2
                stdout = stdout[:head] + marker + stdout[-(retained - head):]
            else:
                stdout = stdout[:available]
            truncated = True
        output = stdout + label + stderr
    return ToolResult(
        **{
            **result.model_dump(),
            "output": output,
            "error": None,
            "truncated": truncated,
            "token_count": count_output_tokens(output),
        }
    )


def _preserve_command_tail(
    result: ToolResult, streamer: "_CommandEventStreamer"
) -> ToolResult:
    """Keep redacted terminal test diagnostics without expanding the text budget."""

    if not result.truncated or result.outcome is ToolExecutionOutcome.UNKNOWN:
        return result
    recent_stdout = streamer.tail("stdout")
    recent_stderr = streamer.tail("stderr")
    original_output = result.output
    original_error = result.error or ""
    tail_stdout = recent_stdout if recent_stdout not in original_output else ""
    tail_stderr = recent_stderr if recent_stderr not in original_error else ""
    if not tail_stdout and not tail_stderr:
        return result

    notice = (
        "\n[Warning: Process output capture limit reached; middle omitted; "
        "final output follows]\n"
    )
    # Small capture budgets used by embedded hosts and tests must not be
    # inflated by the retained tail or its explanation.
    tail_budget = min(
        MAX_COMMAND_TAIL_CHARS,
        max(0, (MAX_COMMAND_OUTPUT_CHARS - 2 * len(notice)) // 2),
    )
    if tail_budget == 0:
        return result
    tail_stdout = tail_stdout[-tail_budget:]
    tail_stderr = tail_stderr[-tail_budget:]
    extra = sum(
        len(notice) + len(tail)
        for tail in (tail_stdout, tail_stderr)
        if tail
    )
    remaining = max(0, MAX_COMMAND_OUTPUT_CHARS - extra)
    # Favor a small early stderr excerpt while keeping the beginning of stdout
    # (including any sandbox annotation) and the independently redacted tails.
    initial_error = original_error[: min(len(original_error), remaining, 8_192)]
    remaining -= len(initial_error)
    initial_output = original_output[:remaining]
    remaining -= len(initial_output)
    if remaining:
        initial_error += original_error[len(initial_error) : len(initial_error) + remaining]
    output = initial_output + (notice + tail_stdout if tail_stdout else "")
    error = initial_error + (notice + tail_stderr if tail_stderr else "")

    recent_diagnostics = extract_diagnostics(recent_stdout, recent_stderr)
    diagnostics = recent_diagnostics + [
        item for item in result.diagnostics if item not in recent_diagnostics
    ]
    summary = dict(result.diagnostic_summary)
    summary.update(extract_diagnostic_summary(recent_stdout, recent_stderr))
    return ToolResult(
        **{
            **result.model_dump(),
            "output": output,
            "error": error or None,
            "token_count": count_output_tokens(output),
            "diagnostics": diagnostics[:MAX_DIAGNOSTIC_ITEMS],
            "diagnostic_summary": summary,
        }
    )


class _CommandEventStreamer:
    """Convert subprocess chunks into bounded, redacted typed events."""

    def __init__(self, emit: Any) -> None:
        self._emit = emit
        self._emitted_characters = 0
        self._truncated = False
        self._tails = {"stdout": "", "stderr": ""}
        self._redactors = {
            "stdout": StreamingRedactor(),
            "stderr": StreamingRedactor(),
        }

    def __call__(self, stream: str, text: str) -> None:
        redactor = self._redactors[stream]
        delta = redactor.feed(text)
        if delta:
            self._retain_tail(stream, delta)
            self._send(stream, delta)

    def finish(self) -> None:
        for stream, redactor in self._redactors.items():
            delta = redactor.finish()
            if delta:
                self._retain_tail(stream, delta)
                self._send(stream, delta)

    def _retain_tail(self, stream: str, delta: str) -> None:
        self._tails[stream] = (self._tails[stream] + delta)[-MAX_COMMAND_TAIL_CHARS:]

    def tail(self, stream: str) -> str:
        return self._tails[stream]

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
