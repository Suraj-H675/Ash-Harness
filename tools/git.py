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

from safety.guard import SafetyGuard
from tools.base import BaseTool, ToolResult, count_output_tokens


DEFAULT_COMMIT_AUTHOR = "ash <ash@local>"
DEFAULT_GIT_OUTPUT_LIMIT = 100_000


class GitStatusArgs(BaseModel):
    include_untracked: bool = True


class GitDiffArgs(BaseModel):
    staged: bool = False
    path: str = ""


class GitLogArgs(BaseModel):
    limit: int = Field(20, ge=1, le=100)


class GitStatusTool(BaseTool):
    name = "git_status"
    description = "Show machine-readable Git worktree and branch status."
    args_schema = GitStatusArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = GitStatusArgs(**kwargs)
        command = ["status", "--short", "--branch"]
        if not args.include_untracked:
            command.append("--untracked-files=no")
        return await _git_result(self.safety_guard.project_root, command)


class GitDiffTool(BaseTool):
    name = "git_diff"
    description = "Show a unified Git diff for the workspace or one path."
    args_schema = GitDiffArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = GitDiffArgs(**kwargs)
        command = ["diff"]
        if args.staged:
            command.append("--cached")
        command.extend(["--no-ext-diff", "--"])
        if args.path:
            path = self.safety_guard.validate_path(args.path)
            command.append(str(path.relative_to(self.safety_guard.project_root)))
        return await _git_result(self.safety_guard.project_root, command)


class GitLogTool(BaseTool):
    name = "git_log"
    description = "Show recent commits without invoking a pager."
    args_schema = GitLogArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = GitLogArgs(**kwargs)
        return await _git_result(
            self.safety_guard.project_root,
            [
                "log",
                f"-{args.limit}",
                "--date=iso-strict",
                "--pretty=format:%h%x09%ad%x09%an%x09%s",
            ],
        )


class AutoCommitArgs(BaseModel):
    message: str = Field(..., description="Commit message body.")
    paths: list[str] = Field(
        default_factory=list,
        description="Explicit workspace-relative paths to stage and commit.",
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

        if not args.paths:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "Refused to auto-commit without an explicit path scope; "
                    "pass paths=[...] to avoid committing unrelated work."
                ),
            )

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
            try:
                relative = resolved.relative_to(workspace_root)
            except ValueError:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Refused to stage path outside Git workspace: {raw!r}",
                )
            resolved_paths.append(relative.as_posix())
        staged_before = await _cached_paths(workspace_root)
        if staged_before is None:
            return ToolResult(
                success=False,
                output="",
                error="git diff --cached failed before staging",
            )
        unrelated_before = _paths_outside_scope(staged_before, resolved_paths)
        if unrelated_before:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "Refused to commit pre-staged paths outside explicit scope: "
                    + ", ".join(unrelated_before[:20])
                ),
            )
        stage_cmd = ["add", "--", *resolved_paths]

        stage_code, stage_stdout, stage_stderr = await _run_git(
            workspace_root, stage_cmd
        )
        if stage_code != 0:
            return ToolResult(
                success=False,
                output=stage_stdout,
                error=_format_git_failure(
                    "git add", stage_code, stage_stdout, stage_stderr
                ),
            )

        # Skip commit if there's nothing staged.
        staged_after = await _cached_paths(workspace_root)
        if staged_after is None:
            return ToolResult(
                success=False,
                output="",
                error="git diff --cached failed after staging",
            )
        unrelated_after = _paths_outside_scope(staged_after, resolved_paths)
        if unrelated_after:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "Refused to commit staged paths outside explicit scope: "
                    + ", ".join(unrelated_after[:20])
                ),
            )
        if not staged_after:
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
                error=_format_git_failure(
                    "git commit", commit_code, commit_stdout, commit_stderr
                ),
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


async def _cached_paths(cwd: Path) -> list[str] | None:
    code, stdout, _ = await _run_git(cwd, ["diff", "--cached", "--name-only", "-z"])
    if code != 0:
        return None
    return sorted(path for path in stdout.split("\0") if path)


def _paths_outside_scope(paths: Sequence[str], scopes: Sequence[str]) -> list[str]:
    return [
        path
        for path in paths
        if not any(_path_in_scope(path, scope) for scope in scopes)
    ]


def _path_in_scope(path: str, scope: str) -> bool:
    normalized_scope = scope.strip("/") or "."
    if normalized_scope == ".":
        return True
    return path == normalized_scope or path.startswith(f"{normalized_scope}/")


def _format_git_failure(
    operation: str,
    code: int,
    stdout: str,
    stderr: str,
) -> str:
    parts = [f"{operation} failed with exit code {code}."]
    if stderr.strip():
        parts.append(f"stderr:\n{stderr.strip()}")
    if stdout.strip():
        parts.append(f"stdout:\n{stdout.strip()}")
    return "\n".join(parts)


async def _git_result(cwd: Path, args: Sequence[str]) -> ToolResult:
    code, stdout, stderr = await _run_git(cwd, args)
    output = stdout
    truncated = len(output) > DEFAULT_GIT_OUTPUT_LIMIT
    if truncated:
        output = output[:DEFAULT_GIT_OUTPUT_LIMIT] + "\n[output truncated]"
    if code != 0:
        return ToolResult(success=False, output=output, error=stderr.strip())
    return ToolResult(
        success=True,
        output=output.rstrip(),
        token_count=count_output_tokens(output),
        truncated=truncated,
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
