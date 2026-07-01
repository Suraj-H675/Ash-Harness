"""Subprocess command execution tool."""

from __future__ import annotations

import asyncio
import platform
import re
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

from core.redaction import StreamingRedactor
from safety.environment import build_scrubbed_environment
from safety.guard import SafetyGuard, SafetyViolation
from sandbox._base import SANDBOX_TIER_BWRAP, SandboxBackendUnavailable
from sandbox.manager import SandboxManager, SandboxResult
from sandbox.process_utils import communicate_process
from tools.base import BaseTool, ToolResult, count_output_tokens
from sandbox.process_utils import process_group_options, terminate_process_tree


DEFAULT_TIMEOUT_SECONDS = 300
MAX_COMMAND_OUTPUT_CHARS = 100_000
POWERSHELL_FILE_CMDLETS = (
    "get-content",
    "set-content",
    "add-content",
    "copy-item",
    "move-item",
    "remove-item",
    "rename-item",
    "new-item",
)


class RunCommandArgs(BaseModel):
    command_line: str = Field(..., description="The shell command string to execute.")
    cwd: str | None = Field(
        None, description="Directory path context to run the command in."
    )
    timeout_seconds: int = Field(
        DEFAULT_TIMEOUT_SECONDS,
        ge=1,
        description="Hard timeout for subprocess execution.",
    )


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
        self.sandbox_manager = sandbox_manager
        self.environment_allowlist = tuple(environment_allowlist)

    async def run(self, **kwargs: Any) -> ToolResult:
        args = RunCommandArgs(**kwargs)
        self.safety_guard.validate_command(args.command_line)
        self._validate_powershell_literal_paths(args.command_line)

        cwd = None
        if args.cwd is not None:
            cwd_path = self.safety_guard.validate_path(args.cwd)
            if not cwd_path.is_dir():
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Error: cwd is not a directory: {args.cwd}",
                )
            cwd = str(cwd_path)
        elif self.project_root is not None:
            cwd = str(self.project_root)

        # Tier 2+ (bwrap / docker) wants a real argv so the sandbox
        # binary can exec it directly. Tier 1 (scoped) keeps the
        # original shell semantics via create_subprocess_shell.
        sandboxed = (
            self.sandbox_manager is not None
            and self.sandbox_manager.tier >= SANDBOX_TIER_BWRAP
        )

        streamer = _CommandEventStreamer(self.emit_event)
        try:
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
                )

            return await self._run_scoped(
                args.command_line,
                args.timeout_seconds,
                cwd,
                env=build_scrubbed_command_env(
                    self.project_root, self.environment_allowlist
                ),
                stream_callback=streamer,
            )
        finally:
            streamer.finish()

    async def _run_sandboxed(
        self,
        argv: list[str],
        timeout_seconds: int,
        cwd: str | None,
        *,
        env: dict[str, str],
        passthrough_env_names: tuple[str, ...],
        stream_callback: "_CommandEventStreamer",
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
            )
        except SandboxBackendUnavailable as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Sandbox unavailable; command was not run: {exc}",
            )
        output, truncated = _truncate_command_output(result.stdout)
        error, error_truncated = _truncate_command_output(result.stderr)
        if error_truncated:
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
        )

    async def _run_scoped(
        self,
        command_line: str,
        timeout_seconds: int,
        cwd: str | None,
        *,
        env: dict[str, str],
        stream_callback: "_CommandEventStreamer",
    ) -> ToolResult:
        try:
            if platform.system() == "Windows":
                process = await asyncio.create_subprocess_exec(
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command_line,
                    cwd=cwd,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **process_group_options(),
                )
            else:
                process = await asyncio.create_subprocess_shell(
                    command_line,
                    cwd=cwd,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **process_group_options(),
                )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                communicate_process(process, stream_callback=stream_callback),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            if "process" in locals():
                await terminate_process_tree(process)
            return ToolResult(
                success=False,
                output="",
                error=f"Error: Command timed out after {timeout_seconds} seconds.",
            )
        except asyncio.CancelledError:
            if "process" in locals():
                await terminate_process_tree(process)
            raise

        stdout = decode_stream(stdout_bytes)
        stderr = decode_stream(stderr_bytes)
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
        )

    def _validate_powershell_literal_paths(self, command_line: str) -> None:
        if platform.system() != "Windows":
            return

        lowered = command_line.casefold()
        if contains_forbidden_windows_chain(command_line):
            raise SafetyViolation(
                "Windows command chains are forbidden for this command."
            )
        if not any(cmdlet in lowered for cmdlet in POWERSHELL_FILE_CMDLETS):
            return
        if "-literalpath" not in lowered:
            raise SafetyViolation(
                "PowerShell file cmdlets must use -LiteralPath for path arguments."
            )


async def run_command(safety_guard: SafetyGuard, **kwargs: Any) -> ToolResult:
    return await RunCommandTool(safety_guard).run(**kwargs)


def decode_stream(raw_bytes: bytes) -> str:
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return raw_bytes.decode("cp1252", errors="replace")


def quote_powershell_literal_path(path: str) -> str:
    escaped = path.replace("'", "''")
    return f"-LiteralPath '{escaped}'"


def contains_forbidden_windows_chain(command_line: str) -> bool:
    allowed_compiler_chain = re.compile(
        r"\b(cargo|npm|pnpm|yarn|uv|python|pytest|go|dotnet)\b.*(&&|\|\|)",
        re.IGNORECASE,
    )
    if allowed_compiler_chain.search(command_line):
        return False
    return any(chain in command_line for chain in (";", "&&", "||"))


def build_scrubbed_command_env(
    project_root: Path | None = None,
    environment_allowlist: Iterable[str] = (),
) -> dict[str, str]:
    env = build_scrubbed_environment(environment_allowlist)
    if project_root is not None:
        env["ASH_WORKSPACE_ROOT"] = str(project_root)
    return env


def _truncate_command_output(output: str) -> tuple[str, bool]:
    if len(output) <= MAX_COMMAND_OUTPUT_CHARS:
        return output, False
    return (
        output[:MAX_COMMAND_OUTPUT_CHARS]
        + "\n[Warning: Output truncated. Command output exceeded 100000 characters.]",
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
