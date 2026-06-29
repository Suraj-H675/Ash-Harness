"""Policy-gated unified patch application."""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from safety.guard import SafetyGuard, SafetyViolation
from sandbox.process_utils import process_group_options, terminate_process_tree
from tools.base import BaseTool, ToolResult


class ApplyPatchArgs(BaseModel):
    patch: str = Field(..., min_length=1, description="Unified diff to apply.")
    dry_run: bool = False


class ApplyPatchTool(BaseTool):
    name = "apply_patch"
    description = "Validate and atomically apply a unified multi-file patch."
    args_schema = ApplyPatchArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ApplyPatchArgs(**kwargs)
        try:
            paths = extract_patch_paths(args.patch, self.safety_guard)
        except (ValueError, SafetyViolation) as exc:
            return ToolResult(success=False, output="", error=f"Invalid patch: {exc}")
        check = await _git_apply(self.safety_guard.project_root, args.patch, check=True)
        if check[0] != 0:
            return ToolResult(
                success=False,
                output=check[1],
                error=f"Patch check failed: {check[2].strip()}",
            )
        if args.dry_run:
            return ToolResult(
                success=True,
                output=f"Patch is valid for {len(paths)} file(s); no files changed.",
            )
        applied = await _git_apply(
            self.safety_guard.project_root, args.patch, check=False
        )
        if applied[0] != 0:
            return ToolResult(
                success=False,
                output=applied[1],
                error=f"Patch apply failed: {applied[2].strip()}",
            )
        return ToolResult(
            success=True,
            output="Applied patch to: " + ", ".join(sorted(paths)),
        )


def extract_patch_paths(patch: str, guard: SafetyGuard) -> set[str]:
    paths: set[str] = set()
    for line in patch.splitlines():
        candidate = ""
        if line.startswith(("--- ", "+++ ")):
            candidate = line[4:].split("\t", 1)[0].strip()
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            candidate = line.split(" ", 2)[2].strip()
        elif line.startswith("diff --git "):
            parts = shlex.split(line)
            if len(parts) != 4:
                raise ValueError("unsupported diff header")
            for item in parts[2:]:
                normalized = _normalize_patch_path(item)
                guard.validate_path(normalized)
                paths.add(normalized)
            continue
        if not candidate or candidate == "/dev/null":
            continue
        normalized = _normalize_patch_path(candidate)
        guard.validate_path(normalized)
        paths.add(normalized)
    if not paths:
        raise ValueError("no file paths were found")
    return paths


def _normalize_patch_path(path: str) -> str:
    if path.startswith(("a/", "b/")):
        path = path[2:]
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"out-of-scope path: {path}")
    return candidate.as_posix()


async def _git_apply(cwd: Path, patch: str, *, check: bool) -> tuple[int, str, str]:
    command = ["git", "apply", "--whitespace=nowarn"]
    if check:
        command.append("--check")
    command.append("-")
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_group_options(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(patch.encode("utf-8")), timeout=30
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        await terminate_process_tree(process)
        raise
    return (
        process.returncode if process.returncode is not None else -1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )
