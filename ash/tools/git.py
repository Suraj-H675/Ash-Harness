"""Git auto-commit tool for Ash.

Provides a single :class:`AutoCommitTool` that stages a list of file
paths and creates a commit. The :func:`auto_commit_turn` helper is a
thin convenience used by the loop to record a per-turn commit after
the model finishes a turn (and any tools ran).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, Field

from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult


DEFAULT_COMMIT_AUTHOR = "ash <ash@local>"


class AutoCommitArgs(BaseModel):
    message: str = Field(..., description="Commit message body.")
    paths: list[str] = Field(
        default_factory=list,
        description="Workspace-relative paths to stage. Empty means stage everything tracked.",
    )
    author: str = Field(
        DEFAULT_COMMIT_AUTHOR,
        description="Commit author in 'Name <email>' form.",
    )


class AutoCommitTool(BaseTool):
    name = "auto_commit"
    description = (
        "Stage paths and create a git commit capturing the current turn's changes."
    )
    args_schema = AutoCommitArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = AutoCommitArgs(**kwargs)

        try:
            workspace_root = self.safety_guard.project_root
        except AttributeError as exc:  # pragma: no cover - safety_guard required
            return ToolResult(
                success=False,
                output="",
                error=f"SafetyGuard missing project_root: {exc}",
            )

        # Stage the requested paths (or all tracked files when none given).
        if args.paths:
            resolved_paths: list[str] = []
            for raw in args.paths:
                try:
                    resolved = self.safety_guard.validate_path(raw)
                except Exception as exc:  # SafetyViolation or ValueError
                    return ToolResult(
                        success=False,
                        output="",
                        error=f"Refused to stage out-of-scope path {raw!r}: {exc}",
                    )
                resolved_paths.append(str(resolved))
            stage_cmd = ["add", "--", *resolved_paths]
        else:
            # No explicit paths: stage all updates + new files in the workspace.
            stage_cmd = ["add", "-A", "--", "."]

        stage_code, stage_stdout, stage_stderr = await _run_git(
            workspace_root, stage_cmd
        )
        if stage_code != 0:
            return ToolResult(
                success=False,
                output=stage_stdout,
                error=f"git add failed: {stage_stderr.strip()}",
            )

        # Skip commit if there's nothing staged.
        diff_code, diff_stdout, _ = await _run_git(
            workspace_root, ["diff", "--cached", "--name-only"]
        )
        if diff_code != 0 or not diff_stdout.strip():
            return ToolResult(
                success=True,
                output="No changes to commit.",
                token_count=0,
            )

        commit_code, commit_stdout, commit_stderr = await _run_git(
            workspace_root,
            [
                "-c",
                f"user.name={_name_from_author(args.author)}",
                "-c",
                f"user.email={_email_from_author(args.author)}",
                "commit",
                "-m",
                args.message,
            ],
        )
        if commit_code != 0:
            return ToolResult(
                success=False,
                output=commit_stdout,
                error=f"git commit failed: {commit_stderr.strip()}",
            )

        return ToolResult(
            success=True,
            output=commit_stdout.strip() or "Commit created.",
        )


async def _run_git(cwd: Path, args: Sequence[str]) -> tuple[int, str, str]:
    """Run ``git <args>`` in ``cwd`` and return (exit, stdout, stderr)."""

    cmd = ["git", *args]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await process.communicate()
    return (
        process.returncode if process.returncode is not None else -1,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


def _name_from_author(author: str) -> str:
    if "<" in author:
        return author.split("<", 1)[0].strip()
    return author


def _email_from_author(author: str) -> str:
    if "<" in author and ">" in author:
        return author.split("<", 1)[1].split(">", 1)[0].strip()
    return f"{author}@local"


async def auto_commit_turn(
    workspace_root: Path,
    *,
    message: str | None = None,
    paths: list[Path] | None = None,
    safety_guard: SafetyGuard | None = None,
) -> ToolResult:
    """Convenience wrapper used by the loop to record a per-turn commit."""

    body = (
        message
        or f"ash: turn complete at {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    )
    guard = safety_guard or SafetyGuard(project_root=workspace_root)
    tool = AutoCommitTool(guard)
    payload: dict[str, Any] = {"message": body}
    if paths:
        payload["paths"] = [str(p) for p in paths]
    return await tool.run(**payload)


# Provide a free function for use outside the tool registry.
async def auto_commit(safety_guard: SafetyGuard, **kwargs: Any) -> ToolResult:
    return await AutoCommitTool(safety_guard).run(**kwargs)
