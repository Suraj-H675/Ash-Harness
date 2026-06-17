"""Subprocess command execution tool."""

from __future__ import annotations

import asyncio
import platform
import re
import shlex
from typing import Any

from pydantic import BaseModel, Field

from safety.guard import SafetyGuard, SafetyViolation
from sandbox._base import SANDBOX_TIER_BWRAP
from sandbox.manager import SandboxManager, SandboxResult
from tools.base import BaseTool, ToolResult, count_output_tokens


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
    ) -> None:
        super().__init__(safety_guard)
        self.project_root = project_root if project_root is not None else safety_guard.project_root
        self.sandbox_manager = sandbox_manager

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

        if sandboxed:
            assert self.sandbox_manager is not None
            try:
                argv = (
                    shlex.split(args.command_line)
                    if not platform.system() == "Windows"
                    else None
                )
            except ValueError as exc:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Error: failed to tokenize command for sandbox: {exc}",
                )
            if not argv:
                argv = [args.command_line]
            return await self._run_sandboxed(argv, args.timeout_seconds, cwd)

        return await self._run_scoped(args.command_line, args.timeout_seconds, cwd)

    async def _run_sandboxed(
        self, argv: list[str], timeout_seconds: int, cwd: str | None
    ) -> ToolResult:
        assert self.sandbox_manager is not None
        from pathlib import Path

        cwd_path = Path(cwd) if cwd is not None else None
        result: SandboxResult = await self.sandbox_manager.run(
            argv, cwd=cwd_path, timeout=timeout_seconds
        )
        output, truncated = _truncate_command_output(result.stdout)
        error, error_truncated = _truncate_command_output(result.stderr)
        if error_truncated:
            truncated = True
        if not result.fallback_used and result.tier >= SANDBOX_TIER_BWRAP:
            annotation = f"[sandbox tier={result.tier} backend={result.backend_name}]"
            if error:
                error = f"{annotation} {error}"
            else:
                output = f"{annotation}\n{output}" if output else annotation
        return ToolResult(
            success=result.exit_code == 0,
            output=output,
            error=error or None,
            token_count=count_output_tokens(output),
            truncated=truncated,
        )

    async def _run_scoped(
        self, command_line: str, timeout_seconds: int, cwd: str | None
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
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                process = await asyncio.create_subprocess_shell(
                    command_line,
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            if "process" in locals():
                process.kill()
                await process.wait()
            return ToolResult(
                success=False,
                output="",
                error=f"Error: Command timed out after {timeout_seconds} seconds.",
            )

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


def _truncate_command_output(output: str) -> tuple[str, bool]:
    if len(output) <= MAX_COMMAND_OUTPUT_CHARS:
        return output, False
    return (
        output[:MAX_COMMAND_OUTPUT_CHARS]
        + "\n[Warning: Output truncated. Command output exceeded 100000 characters.]",
        True,
    )
