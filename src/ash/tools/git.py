"""Git auto-commit tool for Ash.

Provides a single :class:`AutoCommitTool` that stages a list of file
paths and creates a commit. The :func:`auto_commit_turn` helper is a
thin convenience used by the loop to record a per-turn commit after
the model finishes a turn (and any tools ran).
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from pydantic import BaseModel, Field

from ash.core.redaction import find_secret_candidates
from ash.safety.environment import build_scrubbed_environment
from ash.safety.guard import SafetyGuard
from ash.sandbox.process_utils import (
    communicate_process,
    process_group_options,
    terminate_process_tree,
)
from ash.tools.base import BaseTool, ToolResult, count_output_tokens


DEFAULT_COMMIT_AUTHOR = "ash <ash@local>"
DEFAULT_GIT_OUTPUT_LIMIT = 100_000
MAX_SECRET_FINDINGS = 20
_DIFF_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


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

    def __init__(
        self,
        safety_guard: SafetyGuard,
        *,
        environment_allowlist: Iterable[str] = (),
    ) -> None:
        super().__init__(safety_guard)
        self.environment_allowlist = tuple(environment_allowlist)

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
        staged_before = await _cached_paths(workspace_root, self.environment_allowlist)
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
            workspace_root, stage_cmd, self.environment_allowlist
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
        staged_after = await _cached_paths(workspace_root, self.environment_allowlist)
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
        scan_code, scan_stdout, scan_stderr = await _run_git(
            workspace_root,
            [
                "diff",
                "--cached",
                "--no-ext-diff",
                "--no-color",
                "--unified=0",
                "--",
                *resolved_paths,
            ],
            self.environment_allowlist,
        )
        if scan_code != 0:
            return ToolResult(
                success=False,
                output=scan_stdout,
                error=_format_git_failure(
                    "git diff for secret scan",
                    scan_code,
                    scan_stdout,
                    scan_stderr,
                ),
            )
        secret_findings = _scan_added_secret_findings(scan_stdout)
        if secret_findings:
            rendered = ", ".join(
                f"{location} ({kind})"
                for location, kind in secret_findings[:MAX_SECRET_FINDINGS]
            )
            suffix = (
                f", and {len(secret_findings) - MAX_SECRET_FINDINGS} more"
                if len(secret_findings) > MAX_SECRET_FINDINGS
                else ""
            )
            return ToolResult(
                success=False,
                output="Changes remain staged for inspection.",
                error=(
                    "Potential secret detected in staged additions; commit refused: "
                    f"{rendered}{suffix}"
                ),
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
            self.environment_allowlist,
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


async def _run_git(
    cwd: Path,
    args: Sequence[str],
    environment_allowlist: Iterable[str] = (),
) -> tuple[int, str, str]:
    """Run ``git <args>`` in ``cwd`` and return (exit, stdout, stderr)."""

    cmd = ["git", *args]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=build_scrubbed_environment(environment_allowlist),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_group_options(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            communicate_process(process), timeout=30
        )
    except asyncio.TimeoutError:
        await terminate_process_tree(process)
        return (
            124,
            "",
            "git command timed out after 30 seconds",
        )
    return (
        process.returncode if process.returncode is not None else -1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def _cached_paths(
    cwd: Path, environment_allowlist: Iterable[str] = ()
) -> list[str] | None:
    code, stdout, _ = await _run_git(
        cwd,
        ["diff", "--cached", "--name-only", "-z"],
        environment_allowlist,
    )
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


def _scan_added_secret_findings(diff: str) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    path = "(unknown path)"
    new_line = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("+++ "):
            raw_path = line[4:]
            path = raw_path[2:] if raw_path.startswith("b/") else raw_path
            continue
        hunk = _DIFF_HUNK.match(line)
        if hunk is not None:
            new_line = int(hunk.group(1))
            in_hunk = True
            continue
        if not in_hunk or line.startswith("\\"):
            continue
        if line.startswith("+"):
            for finding in find_secret_candidates(line[1:]):
                findings.append((f"{path}:{new_line}", finding.kind))
            new_line += 1
        elif not line.startswith("-"):
            new_line += 1
    return findings


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
    environment_allowlist: Iterable[str] = (),
) -> ToolResult:
    """Convenience wrapper used by the loop to record a per-turn commit."""

    body = (
        message
        or f"ash: turn complete at {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    )
    guard = safety_guard or SafetyGuard(project_root=workspace_root)
    tool = AutoCommitTool(guard, environment_allowlist=environment_allowlist)
    payload: dict[str, Any] = {"message": body}
    if paths:
        payload["paths"] = [str(p) for p in paths]
    return await tool.run(**payload)


# Provide a free function for use outside the tool registry.
async def auto_commit(safety_guard: SafetyGuard, **kwargs: Any) -> ToolResult:
    return await AutoCommitTool(safety_guard).run(**kwargs)
