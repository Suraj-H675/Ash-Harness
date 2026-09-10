"""Policy-gated unified patch application."""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ash.safety.environment import resolve_host_executable
from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.sandbox.process_utils import (
    ProcessOutputLimitExceeded,
    ProcessTreeError,
    ProcessTreeUnavailable,
    communicate_process,
    prepare_process_tree,
    settle_process_tree_after_cancellation,
    terminate_process_tree,
)
from ash.tools.base import BaseTool, ToolResult


class ApplyPatchArgs(BaseModel):
    patch: str = Field(..., min_length=1, description="Unified diff to apply.")
    dry_run: bool = False


MAX_PATCH_BYTES = 8 * 1024 * 1024
MAX_PATCH_OUTPUT_BYTES = 100_000
PATCH_OUTPUT_LIMIT_EXIT = -2


class ApplyPatchTool(BaseTool):
    name = "apply_patch"
    description = "Validate and atomically apply a unified multi-file patch."
    args_schema = ApplyPatchArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ApplyPatchArgs(**kwargs)
        patch_bytes = args.patch.encode("utf-8")
        if len(patch_bytes) > MAX_PATCH_BYTES:
            return ToolResult(
                success=False,
                output="",
                error=f"Patch exceeds {MAX_PATCH_BYTES} bytes",
            )
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
        try:
            for path in paths:
                self.safety_guard.validate_mutation_path(path)
        except SafetyViolation as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Patch path changed after validation: {exc}",
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
                guard.validate_mutation_path(normalized)
                paths.add(normalized)
            continue
        if not candidate or candidate == "/dev/null":
            continue
        normalized = _normalize_patch_path(candidate)
        guard.validate_mutation_path(normalized)
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
    git = resolve_host_executable("git", workspace_root=cwd, cwd=cwd)
    if git is None:
        return 127, "", "git is unavailable outside the workspace"
    try:
        process_tree_plan = prepare_process_tree(workspace_root=cwd)
    except ProcessTreeUnavailable as exc:
        return 126, "", f"git apply was not started: {exc}"
    command = [git, "apply", "--whitespace=nowarn"]
    if check:
        command.append("--check")
    command.append("-")
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_tree_plan.spawn_options,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            communicate_process(
                process,
                input_data=patch.encode("utf-8"),
                max_output_bytes=MAX_PATCH_OUTPUT_BYTES,
                process_tree_plan=process_tree_plan,
            ),
            timeout=30,
        )
    except ProcessOutputLimitExceeded as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        return (
            PATCH_OUTPUT_LIMIT_EXIT,
            exc.stdout.decode("utf-8", errors="replace"),
            (
                detail or f"git apply output exceeded {MAX_PATCH_OUTPUT_BYTES} bytes"
            )
            + (
                f"; process-tree cleanup failed: {exc.cleanup_error}"
                if exc.cleanup_error is not None
                else ""
            ),
        )
    except asyncio.TimeoutError as timeout_error:
        try:
            await terminate_process_tree(process, plan=process_tree_plan)
        except ProcessTreeError as exc:
            timeout_error.add_note(f"Process-tree cleanup failed: {exc}")
        raise
    except asyncio.CancelledError as cancellation:
        cleanup_error, cleanup_cancelled = (
            await settle_process_tree_after_cancellation(
                process, plan=process_tree_plan
            )
        )
        if cleanup_error is not None:
            cancellation.add_note(f"Process-tree cleanup failed: {cleanup_error}")
        if cleanup_cancelled:
            cancellation.add_note("Process-tree cleanup was cancelled")
        raise
    return (
        process.returncode if process.returncode is not None else -1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )
