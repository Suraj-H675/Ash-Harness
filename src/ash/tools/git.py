"""Git auto-commit tool for Ash.

Provides a single :class:`AutoCommitTool` that stages a list of file
paths and creates a commit. The :func:`auto_commit_turn` helper is a
thin convenience used by the loop to record a per-turn commit after
the model finishes a turn (and any tools ran).
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, Field

from ash.core.redaction import find_secret_candidates
from ash.safe_io import read_bounded_bytes
from ash.safety.environment import build_scrubbed_environment, resolve_host_executable
from ash.safety.git import read_only_git_args, read_only_git_environment
from ash.safety.guard import SafetyGuard
from ash.safety.scoped_io import snapshot_scoped_file
from ash.sandbox import SandboxBackendUnavailable, SandboxManager
from ash.sandbox.process_utils import (
    ProcessOutputLimitExceeded,
    ProcessTreeError,
    ProcessTreeUnavailable,
    communicate_process,
    prepare_process_tree,
    settle_process_tree_after_cancellation,
    terminate_process_tree,
)
from ash.tools.base import BaseTool, ToolResult, count_output_tokens


DEFAULT_COMMIT_AUTHOR = "ash <ash@local>"
DEFAULT_GIT_OUTPUT_LIMIT = 100_000
GIT_OUTPUT_LIMIT_EXIT = -2
MAX_SECRET_FINDINGS = 20
MAX_GIT_PATH_CHARS = 4_096
MAX_COMMIT_MESSAGE_CHARS = 65_536
MAX_COMMIT_MESSAGE_BYTES = MAX_COMMIT_MESSAGE_CHARS * 4
MAX_COMMIT_PATHS = 1_000
MAX_COMMIT_AUTHOR_CHARS = 512
MAX_AUTO_COMMIT_VERIFY_BYTES = 20 * 1024 * 1024
_GitPath = Annotated[str, Field(min_length=1, max_length=MAX_GIT_PATH_CHARS)]
_DIFF_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class GitStatusArgs(BaseModel):
    include_untracked: bool = True


class GitDiffArgs(BaseModel):
    staged: bool = False
    path: str = Field("", max_length=MAX_GIT_PATH_CHARS)


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
        return await _git_result(
            self.safety_guard.project_root,
            command,
            read_only=True,
        )


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
        return await _git_result(
            self.safety_guard.project_root,
            command,
            read_only=True,
        )


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
            read_only=True,
        )


class AutoCommitArgs(BaseModel):
    message: str = Field(
        ...,
        min_length=1,
        max_length=MAX_COMMIT_MESSAGE_CHARS,
        description="Commit message body.",
    )
    paths: list[_GitPath] = Field(
        default_factory=list,
        max_length=MAX_COMMIT_PATHS,
        description="Explicit workspace-relative paths to stage and commit.",
    )
    author: str = Field(
        DEFAULT_COMMIT_AUTHOR,
        max_length=MAX_COMMIT_AUTHOR_CHARS,
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
        sandbox_manager: SandboxManager | None = None,
    ) -> None:
        super().__init__(safety_guard)
        self.environment_allowlist = tuple(environment_allowlist)
        self.sandbox_manager = sandbox_manager

    async def run(self, **kwargs: Any) -> ToolResult:
        args = AutoCommitArgs(**kwargs)
        return await self._commit(args)

    async def run_owned(
        self,
        *,
        message: str,
        paths: list[str],
        expected_sha256: Mapping[str, str],
        author: str = DEFAULT_COMMIT_AUTHOR,
    ) -> ToolResult:
        """Commit only paths that still match Ash's post-edit file digests."""

        args = AutoCommitArgs(message=message, paths=paths, author=author)
        return await self._commit(args, expected_sha256=expected_sha256)

    async def _commit(
        self,
        args: AutoCommitArgs,
        *,
        expected_sha256: Mapping[str, str] | None = None,
    ) -> ToolResult:

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
        staged_before = await _cached_paths(
            workspace_root,
            self.environment_allowlist,
            sandbox_manager=self.sandbox_manager,
        )
        if staged_before is None:
            return ToolResult(
                success=False,
                output="",
                error="git diff --cached failed before staging",
            )
        if staged_before:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "Refused to auto-commit while the Git index already contains "
                    "staged paths; preserve existing user staging first: "
                    + ", ".join(staged_before[:20])
                ),
            )
        stage_cmd = ["add", "--", *resolved_paths]

        stage_code, stage_stdout, stage_stderr = await _run_git(
            workspace_root,
            stage_cmd,
            self.environment_allowlist,
            sandbox_manager=self.sandbox_manager,
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
        staged_after = await _cached_paths(
            workspace_root,
            self.environment_allowlist,
            sandbox_manager=self.sandbox_manager,
        )
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
        if expected_sha256 is not None:
            changed_paths: list[str] = []
            for relative_path in resolved_paths:
                expected = expected_sha256.get(relative_path)
                try:
                    _, snapshot = snapshot_scoped_file(
                        workspace_root / relative_path,
                        self.safety_guard,
                        max_bytes=MAX_AUTO_COMMIT_VERIFY_BYTES,
                    )
                except Exception:  # noqa: BLE001 - ownership check fails closed
                    snapshot = None
                if expected is None or snapshot is None or snapshot.sha256 != expected:
                    changed_paths.append(relative_path)
            if changed_paths:
                return ToolResult(
                    success=False,
                    output="Changes remain staged for inspection.",
                    error=(
                        "Refused to auto-commit paths changed after Ash's last edit: "
                        + ", ".join(changed_paths[:20])
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
            sandbox_manager=self.sandbox_manager,
            read_only=True,
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
            return _secret_scan_failure(secret_findings)
        if expected_sha256 is not None:
            return await self._commit_index_snapshot(
                workspace_root,
                args,
                resolved_paths,
                expected_sha256=expected_sha256,
                run_hooks=False,
            )
        return await self._commit_index_snapshot(
            workspace_root,
            args,
            resolved_paths,
            expected_sha256=None,
            run_hooks=True,
        )

    async def _commit_index_snapshot(
        self,
        workspace_root: Path,
        args: AutoCommitArgs,
        resolved_paths: Sequence[str],
        *,
        expected_sha256: Mapping[str, str] | None,
        run_hooks: bool,
    ) -> ToolResult:
        message_file: Path | None = None
        if run_hooks:
            hook_result, message_file = await self._run_commit_hooks_before_snapshot(
                workspace_root,
                args.message,
            )
            if hook_result is not None:
                return hook_result
            staged_after_hooks = await _cached_paths(
                workspace_root,
                self.environment_allowlist,
                sandbox_manager=self.sandbox_manager,
            )
            if staged_after_hooks is None:
                return ToolResult(
                    success=False,
                    output="",
                    error="git diff --cached failed after commit hooks",
                )
            unrelated_after_hooks = _paths_outside_scope(
                staged_after_hooks, resolved_paths
            )
            if unrelated_after_hooks:
                return ToolResult(
                    success=False,
                    output="Changes remain staged for inspection.",
                    error=(
                        "Refused to commit paths staged outside explicit scope by "
                        "Git hooks or concurrent changes: "
                        + ", ".join(unrelated_after_hooks[:20])
                    ),
                )
            if not staged_after_hooks:
                return ToolResult(
                    success=True,
                    output="No changes to commit.",
                    token_count=0,
                )

        tree_code, tree_stdout, tree_stderr = await _run_git(
            workspace_root,
            ["write-tree"],
            self.environment_allowlist,
            sandbox_manager=self.sandbox_manager,
        )
        if tree_code != 0:
            return ToolResult(
                success=False,
                output=tree_stdout,
                error=_format_git_failure(
                    "git write-tree", tree_code, tree_stdout, tree_stderr
                ),
            )
        tree = tree_stdout.strip()

        parent_code, parent_stdout, parent_stderr = await _run_git(
            workspace_root,
            ["rev-parse", "--verify", "HEAD"],
            self.environment_allowlist,
            sandbox_manager=self.sandbox_manager,
            read_only=True,
        )
        parent = parent_stdout.strip() if parent_code == 0 else None
        if parent is None:
            symbolic_code, _, symbolic_stderr = await _run_git(
                workspace_root,
                ["symbolic-ref", "--quiet", "HEAD"],
                self.environment_allowlist,
                sandbox_manager=self.sandbox_manager,
                read_only=True,
            )
            if symbolic_code != 0:
                return ToolResult(
                    success=False,
                    output="",
                    error=(
                        "git rev-parse HEAD failed before owned commit: "
                        + (parent_stderr.strip() or symbolic_stderr.strip())
                    ),
                )

        if expected_sha256 is not None:
            changed_paths: list[str] = []
            for relative_path in resolved_paths:
                expected = expected_sha256.get(relative_path)
                try:
                    _, snapshot = snapshot_scoped_file(
                        workspace_root / relative_path,
                        self.safety_guard,
                        max_bytes=MAX_AUTO_COMMIT_VERIFY_BYTES,
                    )
                except Exception:  # noqa: BLE001 - ownership check fails closed
                    snapshot = None
                if expected is None or snapshot is None or snapshot.sha256 != expected:
                    changed_paths.append(relative_path)
            if changed_paths:
                return ToolResult(
                    success=False,
                    output="Changes remain staged for inspection.",
                    error=(
                        "Refused to auto-commit paths changed after Ash's last edit: "
                        + ", ".join(changed_paths[:20])
                    ),
                )
            compare_code, compare_stdout, compare_stderr = await _run_git(
                workspace_root,
                ["diff", "--quiet", tree, "--", *resolved_paths],
                self.environment_allowlist,
                sandbox_manager=self.sandbox_manager,
                read_only=True,
            )
            if compare_code == 1:
                return ToolResult(
                    success=False,
                    output="Changes remain staged for inspection.",
                    error=(
                        "Refused to auto-commit because the frozen Git index no longer "
                        "matches Ash's owned working files."
                    ),
                )
            if compare_code != 0:
                return ToolResult(
                    success=False,
                    output=compare_stdout,
                    error=_format_git_failure(
                        "git diff for owned snapshot verification",
                        compare_code,
                        compare_stdout,
                        compare_stderr,
                    ),
                )

        commit_args = [
            "-c",
            f"user.name={_name_from_author(args.author)}",
            "-c",
            f"user.email={_email_from_author(args.author)}",
            "commit-tree",
            tree,
        ]
        if parent is not None:
            commit_args.extend(["-p", parent])
        if message_file is None:
            commit_args.extend(["-m", args.message])
        else:
            commit_args.extend(["-F", str(message_file)])
        commit_code, commit_stdout, commit_stderr = await _run_git(
            workspace_root,
            commit_args,
            self.environment_allowlist,
            sandbox_manager=self.sandbox_manager,
        )
        if commit_code != 0:
            return ToolResult(
                success=False,
                output=commit_stdout,
                error=_format_git_failure(
                    "git commit-tree", commit_code, commit_stdout, commit_stderr
                ),
            )
        commit = commit_stdout.strip()

        scope_code, scope_stdout, scope_stderr = await _run_git(
            workspace_root,
            [
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                commit,
            ],
            self.environment_allowlist,
            sandbox_manager=self.sandbox_manager,
            read_only=True,
        )
        if scope_code != 0:
            return ToolResult(
                success=False,
                output=scope_stdout,
                error=_format_git_failure(
                    "git diff-tree for commit scope verification",
                    scope_code,
                    scope_stdout,
                    scope_stderr,
                ),
            )
        frozen_paths = sorted(path for path in scope_stdout.split("\0") if path)
        unrelated_frozen = _paths_outside_scope(frozen_paths, resolved_paths)
        if unrelated_frozen:
            return ToolResult(
                success=False,
                output="Changes remain staged for inspection.",
                error=(
                    "Refused to commit frozen paths outside explicit scope: "
                    + ", ".join(unrelated_frozen[:20])
                ),
            )

        scan_code, scan_stdout, scan_stderr = await _run_git(
            workspace_root,
            [
                "show",
                "--format=",
                "--no-color",
                "--unified=0",
                commit,
                "--",
                *resolved_paths,
            ],
            self.environment_allowlist,
            sandbox_manager=self.sandbox_manager,
            read_only=True,
        )
        if scan_code != 0:
            return ToolResult(
                success=False,
                output=scan_stdout,
                error=_format_git_failure(
                    "git show for secret scan",
                    scan_code,
                    scan_stdout,
                    scan_stderr,
                ),
            )
        secret_findings = _scan_added_secret_findings(scan_stdout)
        if secret_findings:
            return _secret_scan_failure(secret_findings)

        expected_head = parent or ("0" * len(commit))
        update_code, update_stdout, update_stderr = await _run_git(
            workspace_root,
            ["update-ref", "HEAD", commit, expected_head],
            self.environment_allowlist,
            sandbox_manager=self.sandbox_manager,
        )
        if update_code != 0:
            return ToolResult(
                success=False,
                output=update_stdout,
                error=_format_git_failure(
                    "git update-ref", update_code, update_stdout, update_stderr
                ),
            )
        output = f"Commit {commit} created."
        if run_hooks:
            post_code, post_stdout, post_stderr = await _run_git(
                workspace_root,
                ["hook", "run", "--ignore-missing", "post-commit"],
                self.environment_allowlist,
                sandbox_manager=self.sandbox_manager,
            )
            hook_output = "\n".join(
                part.strip() for part in (post_stdout, post_stderr) if part.strip()
            )
            if hook_output:
                output += f"\n{hook_output}"
            if post_code != 0:
                output += f"\npost-commit hook exited with code {post_code}."
        return ToolResult(success=True, output=output)

    async def _run_commit_hooks_before_snapshot(
        self,
        workspace_root: Path,
        message: str,
    ) -> tuple[ToolResult | None, Path | None]:
        pre_code, pre_stdout, pre_stderr = await _run_git(
            workspace_root,
            ["hook", "run", "--ignore-missing", "pre-commit"],
            self.environment_allowlist,
            sandbox_manager=self.sandbox_manager,
        )
        if pre_code != 0:
            return (
                ToolResult(
                    success=False,
                    output=pre_stdout,
                    error=_format_git_failure(
                        "git commit", pre_code, pre_stdout, pre_stderr
                    ),
                ),
                None,
            )

        path_code, path_stdout, path_stderr = await _run_git(
            workspace_root,
            ["rev-parse", "--git-path", "COMMIT_EDITMSG"],
            self.environment_allowlist,
            sandbox_manager=self.sandbox_manager,
            read_only=True,
        )
        if path_code != 0 or not path_stdout.strip():
            return (
                ToolResult(
                    success=False,
                    output=path_stdout,
                    error=_format_git_failure(
                        "git rev-parse COMMIT_EDITMSG",
                        path_code,
                        path_stdout,
                        path_stderr,
                    ),
                ),
                None,
            )
        raw_message_path = Path(path_stdout.strip())
        message_path = (
            raw_message_path
            if raw_message_path.is_absolute()
            else workspace_root / raw_message_path
        )
        try:
            _write_no_follow(message_path, message.encode("utf-8"))
        except OSError as exc:
            return (
                ToolResult(
                    success=False,
                    output="",
                    error=f"could not prepare Git commit message: {exc}",
                ),
                None,
            )

        for hook_name, hook_args in (
            ("prepare-commit-msg", [str(message_path), "message"]),
            ("commit-msg", [str(message_path)]),
        ):
            hook_code, hook_stdout, hook_stderr = await _run_git(
                workspace_root,
                ["hook", "run", "--ignore-missing", hook_name, "--", *hook_args],
                self.environment_allowlist,
                sandbox_manager=self.sandbox_manager,
            )
            if hook_code != 0:
                return (
                    ToolResult(
                        success=False,
                        output=hook_stdout,
                        error=_format_git_failure(
                            "git commit", hook_code, hook_stdout, hook_stderr
                        ),
                    ),
                    None,
                )
        try:
            read_bounded_bytes(
                message_path,
                MAX_COMMIT_MESSAGE_BYTES,
                label="Git commit message",
            )
        except (OSError, ValueError) as exc:
            return (
                ToolResult(
                    success=False,
                    output="",
                    error=f"Git commit message is unsafe after hooks: {exc}",
                ),
                None,
            )
        return None, message_path


async def _run_git(
    cwd: Path,
    args: Sequence[str],
    environment_allowlist: Iterable[str] = (),
    *,
    sandbox_manager: SandboxManager | None = None,
    read_only: bool = False,
) -> tuple[int, str, str]:
    """Run ``git <args>`` in ``cwd`` and return (exit, stdout, stderr)."""

    git = resolve_host_executable("git", workspace_root=cwd, cwd=cwd)
    if git is None:
        return 127, "", "git is unavailable outside the workspace"
    allowlist = tuple(environment_allowlist)
    git_args = read_only_git_args(args) if read_only else list(args)
    cmd = [git, *git_args]
    environment = (
        read_only_git_environment(allowlist)
        if read_only
        else build_scrubbed_environment(allowlist)
    )
    if sandbox_manager is not None:
        sandbox_command = (
            ["git", *git_args] if sandbox_manager.backend_name == "docker" else cmd
        )
        try:
            result = await sandbox_manager.run(
                sandbox_command,
                cwd=cwd,
                timeout=30,
                env=environment,
                passthrough_env_names=allowlist,
            )
        except SandboxBackendUnavailable as exc:
            return 126, "", f"sandbox unavailable for git command: {exc}"
        if result.output_truncated:
            return (
                GIT_OUTPUT_LIMIT_EXIT,
                result.stdout,
                result.stderr or f"git output exceeded {DEFAULT_GIT_OUTPUT_LIMIT} bytes",
            )
        return result.exit_code, result.stdout, result.stderr
    try:
        process_tree_plan = prepare_process_tree(workspace_root=cwd)
    except ProcessTreeUnavailable as exc:
        return 126, "", f"git command was not started: {exc}"
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_tree_plan.spawn_options,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            communicate_process(
                process,
                max_output_bytes=DEFAULT_GIT_OUTPUT_LIMIT,
                process_tree_plan=process_tree_plan,
            ),
            timeout=30,
        )
    except ProcessOutputLimitExceeded as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        return (
            GIT_OUTPUT_LIMIT_EXIT,
            exc.stdout.decode("utf-8", errors="replace"),
            (
                detail or f"git output exceeded {DEFAULT_GIT_OUTPUT_LIMIT} bytes"
            )
            + (
                f"; process-tree cleanup failed: {exc.cleanup_error}"
                if exc.cleanup_error is not None
                else ""
            ),
        )
    except asyncio.TimeoutError:
        try:
            await terminate_process_tree(process, plan=process_tree_plan)
        except ProcessTreeError as exc:
            return (
                125,
                "",
                "git command timed out after 30 seconds; "
                f"process-tree cleanup failed: {exc}",
            )
        return (
            124,
            "",
            "git command timed out after 30 seconds",
        )
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


async def git_dirty_paths(
    cwd: Path,
    environment_allowlist: Iterable[str] = (),
    *,
    sandbox_manager: SandboxManager | None = None,
) -> set[str] | None:
    """Return tracked/index/untracked dirty paths without executing Git extensions."""

    commands = (
        ["diff", "--name-only", "--no-renames", "-z", "--"],
        ["diff", "--cached", "--name-only", "--no-renames", "-z", "--"],
        ["ls-files", "--others", "--exclude-standard", "-z", "--"],
    )
    dirty: set[str] = set()
    for command in commands:
        code, stdout, _ = await _run_git(
            cwd,
            command,
            environment_allowlist,
            sandbox_manager=sandbox_manager,
            read_only=True,
        )
        if code != 0:
            return None
        dirty.update(path for path in stdout.split("\0") if path)
    return dirty


async def _cached_paths(
    cwd: Path,
    environment_allowlist: Iterable[str] = (),
    *,
    sandbox_manager: SandboxManager | None = None,
) -> list[str] | None:
    code, stdout, _ = await _run_git(
        cwd,
        ["diff", "--cached", "--name-only", "-z"],
        environment_allowlist,
        sandbox_manager=sandbox_manager,
        read_only=True,
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


def _write_no_follow(path: Path, contents: bytes) -> None:
    if path.is_symlink():
        raise OSError(f"refusing to write symlinked Git commit message: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while preparing Git commit message")
            view = view[written:]
    finally:
        os.close(descriptor)


def _secret_scan_failure(
    findings: Sequence[tuple[str, str]],
) -> ToolResult:
    rendered = ", ".join(
        f"{location} ({kind})" for location, kind in findings[:MAX_SECRET_FINDINGS]
    )
    suffix = (
        f", and {len(findings) - MAX_SECRET_FINDINGS} more"
        if len(findings) > MAX_SECRET_FINDINGS
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


async def _git_result(
    cwd: Path,
    args: Sequence[str],
    *,
    read_only: bool = False,
) -> ToolResult:
    code, stdout, stderr = await _run_git(cwd, args, read_only=read_only)
    output = stdout
    truncated = code == GIT_OUTPUT_LIMIT_EXIT or len(output) > DEFAULT_GIT_OUTPUT_LIMIT
    if truncated:
        output = output[:DEFAULT_GIT_OUTPUT_LIMIT] + "\n[output truncated]"
    if code != 0 and code != GIT_OUTPUT_LIMIT_EXIT:
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
    expected_sha256: Mapping[str, str] | None = None,
) -> ToolResult:
    """Convenience wrapper used by the loop to record a per-turn commit."""

    body = (
        message
        or f"ash: turn complete at {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    )
    guard = safety_guard or SafetyGuard(project_root=workspace_root)
    tool = AutoCommitTool(guard, environment_allowlist=environment_allowlist)
    payload_paths = [str(p) for p in paths] if paths else []
    if expected_sha256 is not None:
        return await tool.run_owned(
            message=body,
            paths=payload_paths,
            expected_sha256=expected_sha256,
        )
    return await tool.run(message=body, paths=payload_paths)


# Provide a free function for use outside the tool registry.
async def auto_commit(safety_guard: SafetyGuard, **kwargs: Any) -> ToolResult:
    return await AutoCommitTool(safety_guard).run(**kwargs)
