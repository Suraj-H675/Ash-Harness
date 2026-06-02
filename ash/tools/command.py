"""Subprocess command execution tool."""

from __future__ import annotations

import asyncio
import platform
import re
from typing import Any

from pydantic import BaseModel, Field

from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.tools.base import BaseTool, ToolResult, count_output_tokens


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
    cwd: str | None = Field(None, description="Directory path context to run the command in.")
    timeout_seconds: int = Field(
        DEFAULT_TIMEOUT_SECONDS,
        ge=1,
        description="Hard timeout for subprocess execution.",
    )


class RunCommandTool(BaseTool):
    name = "run_command"
    description = "Execute a shell command after safety validation."
    args_schema = RunCommandArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = RunCommandArgs(**kwargs)
        self.safety_guard.validate_command(args.command_line)
        self._validate_powershell_literal_paths(args.command_line)

        cwd = None
        if args.cwd is not None:
            cwd_path = self.safety_guard.validate_path(args.cwd)
            if not cwd_path.is_dir():
                return ToolResult(success=False, output="", error=f"Error: cwd is not a directory: {args.cwd}")
            cwd = str(cwd_path)

        try:
            if platform.system() == "Windows":
                process = await asyncio.create_subprocess_exec(
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    args.command_line,
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                process = await asyncio.create_subprocess_shell(
                    args.command_line,
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=args.timeout_seconds,
            )
        except asyncio.TimeoutError:
            if "process" in locals():
                process.kill()
                await process.wait()
            return ToolResult(
                success=False,
                output="",
                error=f"Error: Command timed out after {args.timeout_seconds} seconds.",
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
            raise SafetyViolation("Windows command chains are forbidden for this command.")
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
