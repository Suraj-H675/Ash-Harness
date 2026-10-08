"""Managed Git worktrees for isolated subagent execution."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ash.safe_io import validate_unlinked_directory_path, validate_unlinked_path
from ash.safety.anchored_fs import AnchoredDirectory, AnchoredFilesystemError
from ash.safety.environment import build_scrubbed_environment, resolve_host_executable
from ash.safety.git import (
    managed_worktree_git_args,
    managed_worktree_git_config_probe_args,
    read_only_git_untrusted_config_keys,
)
from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.safety.scoped_io import ScopedIOError
from ash.sandbox.process_utils import (
    ProcessOutputLimitExceeded,
    ProcessTreeError,
    ProcessTreeUnavailable,
    communicate_process,
    prepare_process_tree,
    prepare_scoped_process_launch,
    settle_process_tree_after_cancellation,
    terminate_process_tree,
)


class WorktreeError(RuntimeError):
    """A managed worktree operation could not be completed safely."""

    def __init__(self, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


MAX_WORKTREE_GIT_OUTPUT_BYTES = 100_000
WORKTREE_GIT_OUTPUT_LIMIT_EXIT = -2


@dataclass(frozen=True)
class WorktreeLease:
    agent_id: str
    path: Path
    branch: str
    base_commit: str
    path_identity: tuple[int, int] | None = None


class WorktreeManager:
    """Create, commit, and clean isolated agent branches."""

    def __init__(self, repository: Path, storage_root: Path) -> None:
        self.repository = repository.expanduser().resolve()
        try:
            metadata = os.stat(self.repository)
        except OSError:
            self._repository_identity: tuple[int, int] | None = None
        else:
            self._repository_identity = (metadata.st_dev, metadata.st_ino)
        self.storage_root = self._validate_storage_root(storage_root)
        self._storage_root_identity: tuple[int, int] | None = None

    async def create(self, agent_id: str) -> WorktreeLease:
        safe_id = _safe_agent_id(agent_id)
        root = await self._repository_root()
        if root != self.repository:
            raise WorktreeError(
                f"workspace is not the Git worktree root: {self.repository}"
            )
        status = await self._git(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        if status.stdout:
            raise WorktreeError(
                "isolated agents require a clean lead worktree; commit or stash "
                "current changes, or explicitly choose shared isolation"
            )
        base_commit = (await self._git("rev-parse", "HEAD")).stdout.strip()
        branch = f"ash-agent/{safe_id}"
        branch_check = await self._git(
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        )
        if branch_check.returncode == 0:
            raise WorktreeError(f"agent branch already exists: {branch}")
        self._prepare_storage_root()
        path = self.storage_root / safe_id
        if path.exists() or path.is_symlink():
            raise WorktreeError(f"agent worktree path already exists: {path}")
        # A failed `git worktree add` leaves ownership of both the path and the
        # branch ambiguous: Git may have created either partially, or another
        # process may have claimed the previously-free name before the failure
        # returned. Do not guess ownership and destructively clean up here.
        await self._git_mutation(
            "worktree",
            "add",
            "--lock",
            "--reason",
            f"Ash subagent {safe_id}",
            "-b",
            branch,
            str(path),
            base_commit,
        )
        lease = WorktreeLease(safe_id, path, branch, base_commit)
        identity = self._validate_lease_path(lease)
        return WorktreeLease(
            safe_id,
            path,
            branch,
            base_commit,
            path_identity=identity,
        )

    async def commit_changes(
        self,
        lease: WorktreeLease,
        *,
        message: str,
        baseline_commit: str | None = None,
    ) -> str | None:
        self._validate_lease_path(lease)
        branch = await self._git_at(
            lease.path,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            check=False,
        )
        if branch.returncode != 0 or branch.stdout.strip() != lease.branch:
            raise WorktreeError(
                "agent worktree HEAD is not attached to the managed branch "
                f"{lease.branch}"
            )
        status = await self._git_at(
            lease.path,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        baseline = baseline_commit or lease.base_commit
        if not status.stdout:
            head = (await self._git_at(lease.path, "rev-parse", "HEAD")).stdout.strip()
            return head if head != baseline else None
        await self._git_at_mutation(lease.path, "add", "-A", "--", ".")
        await self._git_at_mutation(
            lease.path,
            "-c",
            "user.name=Ash Agent",
            "-c",
            "user.email=ash-agent@local",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--no-verify",
            "-m",
            message,
        )
        return (await self._git_at(lease.path, "rev-parse", "HEAD")).stdout.strip()

    async def accept_git_artifacts(
        self,
        lease: WorktreeLease,
        artifacts: Sequence[tuple[str, str]],
    ) -> str | None:
        """Merge verified retained agent commits into an isolated worktree."""

        self._validate_lease_path(lease)
        starting_head = (
            await self._git_at(lease.path, "rev-parse", "HEAD")
        ).stdout.strip()
        verified: list[str] = []
        seen: set[str] = set()
        for branch, expected_commit in artifacts:
            _validate_agent_branch(branch)
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected_commit):
                raise WorktreeError(
                    f"artifact for {branch} has an invalid Git commit ID"
                )
            actual = (
                await self._git(
                    "rev-parse",
                    "--verify",
                    f"refs/heads/{branch}^{{commit}}",
                )
            ).stdout.strip()
            if actual != expected_commit:
                raise WorktreeError(
                    f"artifact branch {branch} no longer matches recorded commit"
                )
            if actual not in seen:
                verified.append(actual)
                seen.add(actual)

        accepted = False
        for commit in verified:
            ancestor = await self._git_at(
                lease.path,
                "merge-base",
                "--is-ancestor",
                commit,
                "HEAD",
                check=False,
            )
            if ancestor.returncode == 0:
                accepted = True
                continue
            if ancestor.returncode != 1:
                raise WorktreeError(
                    ancestor.stderr.strip()
                    or f"could not compare artifact commit {commit}"
                )
            result = await self._git_at_mutation(
                lease.path,
                "-c",
                "user.name=Ash Agent",
                "-c",
                "user.email=ash-agent@local",
                "-c",
                "commit.gpgsign=false",
                "merge",
                "--no-ff",
                "--no-edit",
                commit,
                check=False,
            )
            if result.returncode != 0:
                await self._git_at_mutation(
                    lease.path, "merge", "--abort", check=False
                )
                rollback = await self._git_at_mutation(
                    lease.path,
                    "reset",
                    "--hard",
                    starting_head,
                    check=False,
                )
                if rollback.returncode != 0:
                    primary = (
                        result.stderr.strip()
                        or f"artifact commit {commit} conflicts with dependent worktree"
                    )
                    rollback_detail = rollback.stderr.strip() or "unknown rollback failure"
                    raise WorktreeError(
                        f"{primary}; dependency rollback failed: {rollback_detail}"
                    )
                raise WorktreeError(
                    result.stderr.strip()
                    or f"artifact commit {commit} conflicts with dependent worktree"
                )
            accepted = True
        if not accepted:
            return None
        return (await self._git_at(lease.path, "rev-parse", "HEAD")).stdout.strip()

    async def remove(
        self,
        lease: WorktreeLease,
        *,
        keep_branch: bool,
        expected_head: str | None = None,
    ) -> None:
        self._validate_lease_path(lease)
        status = await self._git_at(
            lease.path,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        if status.stdout:
            raise WorktreeError(
                "refusing to remove agent worktree with uncommitted changes"
            )
        if not keep_branch:
            head = (await self._git_at(lease.path, "rev-parse", "HEAD")).stdout.strip()
            disposable_head = expected_head or lease.base_commit
            if head != disposable_head:
                raise WorktreeError(
                    "refusing to remove agent worktree with unretained commits"
                )
        await self._git_mutation("worktree", "unlock", str(lease.path), check=False)
        result = await self._git_mutation(
            "worktree",
            "remove",
            str(lease.path),
            check=False,
        )
        if result.returncode != 0 and lease.path.exists():
            raise WorktreeError(result.stderr.strip() or "git worktree remove failed")
        if not keep_branch:
            branch_head = await self._git(
                "rev-parse",
                "--verify",
                f"refs/heads/{lease.branch}",
                check=False,
            )
            if (
                branch_head.returncode == 0
                and branch_head.stdout.strip() != disposable_head
            ):
                raise WorktreeError(
                    "agent branch changed during cleanup; preserved updated branch"
                )
            deleted = await self._git_mutation(
                "branch", "-D", lease.branch, check=False
            )
            if deleted.returncode != 0:
                raise WorktreeError(
                    deleted.stderr.strip() or f"could not delete agent branch {lease.branch}"
                )
        await self._git_mutation("worktree", "prune", "--expire", "now", check=False)

    async def list_agent_branches(self) -> list[tuple[str, str]]:
        result = await self._git(
            "for-each-ref",
            "--format=%(refname:short)%09%(objectname)",
            "refs/heads/ash-agent/",
        )
        branches: list[tuple[str, str]] = []
        for line in result.stdout.splitlines():
            branch, separator, commit = line.partition("\t")
            if separator and branch and commit:
                branches.append((branch, commit))
        return branches

    async def apply_branch(self, branch: str, *, delete_branch: bool = True) -> str:
        _validate_agent_branch(branch)
        status = await self._git(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        if status.stdout:
            raise WorktreeError("applying agent changes requires a clean lead worktree")
        commit = (await self._git("rev-parse", "--verify", branch)).stdout.strip()
        result = await self._git_mutation(
            "-c",
            "commit.gpgsign=false",
            "merge",
            "--squash",
            "--no-commit",
            branch,
            check=False,
        )
        if result.returncode != 0:
            await self._git_mutation("reset", "--merge", "HEAD", check=False)
            raise WorktreeError(
                result.stderr.strip() or f"agent branch {branch} conflicts with HEAD"
            )
        changed = await self._git("diff", "--cached", "--quiet", check=False)
        if changed.returncode == 1:
            committed = await self._git_mutation(
                "-c",
                "user.name=Ash Agent",
                "-c",
                "user.email=ash-agent@local",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--no-verify",
                "-m",
                f"Apply {branch}",
                check=False,
            )
            if committed.returncode != 0:
                await self._git_mutation("reset", "--merge", "HEAD", check=False)
                raise WorktreeError(
                    committed.stderr.strip()
                    or f"could not commit agent branch {branch}"
                )
        elif changed.returncode != 0:
            await self._git_mutation("reset", "--merge", "HEAD", check=False)
            raise WorktreeError(
                changed.stderr.strip() or f"could not inspect agent branch {branch}"
            )
        if delete_branch:
            await self._git_mutation("branch", "-D", branch)
        return commit

    async def discard_branch(self, branch: str) -> None:
        _validate_agent_branch(branch)
        result = await self._git_mutation("branch", "-D", branch, check=False)
        if result.returncode != 0:
            raise WorktreeError(
                result.stderr.strip() or f"could not delete agent branch {branch}"
            )

    async def _repository_root(self) -> Path:
        result = await self._git("rev-parse", "--show-toplevel")
        return Path(result.stdout.strip()).resolve()

    def _validate_storage_root(self, path: Path) -> Path:
        try:
            return validate_unlinked_directory_path(
                path, label="agent worktree storage"
            )
        except ValueError as exc:
            raise WorktreeError(str(exc)) from exc

    def _prepare_storage_root(self) -> None:
        self._validate_storage_root(self.storage_root)
        try:
            with AnchoredDirectory.open(
                self.storage_root,
                create=True,
                private=True,
                pin_path=True,
            ) as directory:
                directory.validation_path()
                opened = os.fstat(directory.descriptor)
                identity = (opened.st_dev, opened.st_ino)
                if self._storage_root_identity is None:
                    self._storage_root_identity = identity
                elif self._storage_root_identity != identity:
                    raise WorktreeError(
                        "agent worktree storage identity changed; restart the operation"
                    )
        except WorktreeError:
            raise
        except (AnchoredFilesystemError, OSError) as exc:
            raise WorktreeError(
                f"agent worktree storage could not be prepared safely: {exc}"
            ) from exc

    def _validate_lease_path(self, lease: WorktreeLease) -> tuple[int, int]:
        self._prepare_storage_root()
        try:
            validate_unlinked_path(
                lease.path,
                trusted_root=self.storage_root,
                label="agent worktree",
            )
            with AnchoredDirectory.open(
                lease.path,
                create=False,
                private=False,
                pin_path=True,
            ) as directory:
                directory.validation_path()
                opened = os.fstat(directory.descriptor)
                identity = (opened.st_dev, opened.st_ino)
                if lease.path_identity is not None and identity != lease.path_identity:
                    raise WorktreeError(
                        "agent worktree identity changed; refusing replaced lease"
                    )
        except (AnchoredFilesystemError, OSError, ValueError) as exc:
            raise WorktreeError(
                f"worktree is outside managed storage: {lease.path}"
            ) from exc
        return identity

    async def _git(
        self,
        *args: str,
        check: bool = True,
    ) -> "GitResult":
        return await _run_git(
            self.repository,
            args,
            check=check,
            expected_cwd_identity=self._repository_identity,
            hooks_path=self._empty_hooks_path(),
        )

    async def _git_mutation(
        self,
        *args: str,
        check: bool = True,
    ) -> "GitResult":
        return await _run_git(
            self.repository,
            args,
            check=check,
            expected_cwd_identity=self._repository_identity,
            hooks_path=self._empty_hooks_path(),
            mutating=True,
        )

    async def _git_at(
        self,
        cwd: Path,
        *args: str,
        check: bool = True,
    ) -> "GitResult":
        return await _run_git(
            cwd,
            args,
            check=check,
            hooks_path=self._empty_hooks_path(),
        )

    async def _git_at_mutation(
        self,
        cwd: Path,
        *args: str,
        check: bool = True,
    ) -> "GitResult":
        return await _run_git(
            cwd,
            args,
            check=check,
            hooks_path=self._empty_hooks_path(),
            mutating=True,
        )

    def _empty_hooks_path(self) -> Path:
        self._prepare_storage_root()
        hooks = self.storage_root / "empty-hooks"
        try:
            with AnchoredDirectory.open(
                hooks,
                create=True,
                private=True,
                pin_path=True,
            ) as directory:
                return directory.validation_path()
        except (AnchoredFilesystemError, OSError) as exc:
            raise WorktreeError(
                "agent worktree disabled hooks directory could not be prepared safely: "
                f"{exc}"
            ) from exc


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_truncated: bool = False

    @property
    def interrupted(self) -> bool:
        return self.timed_out or self.output_truncated


async def _run_git(
    cwd: Path,
    args: Sequence[str],
    *,
    check: bool,
    expected_cwd_identity: tuple[int, int] | None = None,
    hooks_path: Path | None = None,
    mutating: bool = False,
) -> GitResult:
    if expected_cwd_identity is None:
        try:
            metadata = os.stat(cwd)
        except OSError:
            pass
        else:
            expected_cwd_identity = (metadata.st_dev, metadata.st_ino)
    git = resolve_host_executable("git", workspace_root=cwd, cwd=cwd)
    if git is None:
        result = GitResult(127, "", "git is unavailable outside the workspace")
        if check:
            raise WorktreeError(result.stderr)
        return result
    environment = build_scrubbed_environment(
        ("XDG_CONFIG_HOME",),
        overrides={
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": os.devnull,
            "SSH_ASKPASS": os.devnull,
        },
    )
    probe = await _run_git_process(
        cwd,
        git,
        managed_worktree_git_config_probe_args(),
        environment,
        expected_cwd_identity=expected_cwd_identity,
    )
    if probe.returncode == 0:
        try:
            rejected = read_only_git_untrusted_config_keys(probe.stdout)
        except ValueError as exc:
            result = GitResult(126, "", f"Git config provenance check failed: {exc}")
            if check:
                raise WorktreeError(result.stderr) from exc
            return result
        if rejected:
            result = GitResult(
                126,
                "",
                "refusing untrusted repository Git config for managed worktree: "
                + ", ".join(rejected[:8]),
            )
            if check:
                raise WorktreeError(result.stderr)
            return result
    elif probe.returncode != 1:
        detail = probe.stderr.strip() or probe.stdout.strip() or (
            f"git config exited with status {probe.returncode}"
        )
        result = GitResult(126, "", f"Git config provenance check failed: {detail}")
        if check:
            raise WorktreeError(result.stderr)
        return result
    protected_args = managed_worktree_git_args(
        args,
        hooks_path=hooks_path if hooks_path is not None else Path(os.devnull),
    )
    result = await _run_git_process(
        cwd,
        git,
        protected_args,
        environment,
        expected_cwd_identity=expected_cwd_identity,
    )
    if mutating and result.interrupted:
        operation = _worktree_git_operation(args)
        detail = (
            "timed out after 30 seconds"
            if result.timed_out
            else "was stopped after exceeding the Git output limit"
        )
        cleanup_detail = (
            f" {result.stderr.strip()}"
            if "process-tree cleanup failed:" in result.stderr.casefold()
            else ""
        )
        raise WorktreeError(
            f"{operation} {detail}; its outcome is unknown.{cleanup_detail} "
            "Inspect the worktree and branch before retrying.",
            outcome_unknown=True,
        )
    if check and result.returncode != 0:
        raise WorktreeError(
            result.stderr.strip()
            or f"git {' '.join(args)} failed with exit {result.returncode}"
        )
    return result


async def _run_git_process(
    cwd: Path,
    git: str,
    args: Sequence[str],
    environment: dict[str, str],
    *,
    expected_cwd_identity: tuple[int, int] | None,
) -> GitResult:
    command = [git, *args]
    cwd_guard = SafetyGuard(cwd)
    try:
        with prepare_scoped_process_launch(
            command,
            cwd=cwd,
            guard=cwd_guard,
            expected_cwd_identity=expected_cwd_identity,
        ) as launch:
            try:
                process_tree_plan = prepare_process_tree(
                    workspace_root=cwd_guard.project_root
                )
            except ProcessTreeUnavailable as exc:
                return GitResult(126, "", f"git command was not started: {exc}")
            spawn_options = dict(process_tree_plan.spawn_options)
            if launch.pass_fds:
                spawn_options["pass_fds"] = launch.pass_fds
            process = await asyncio.create_subprocess_exec(
                *launch.argv,
                cwd=launch.cwd,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **spawn_options,
            )
    except (ProcessTreeUnavailable, SafetyViolation, ScopedIOError) as exc:
        result = GitResult(126, "", f"git command was not started: {exc}")
        return result
    try:
        stdout, stderr = await asyncio.wait_for(
            communicate_process(
                process,
                max_output_bytes=MAX_WORKTREE_GIT_OUTPUT_BYTES,
                process_tree_plan=process_tree_plan,
            ),
            timeout=30,
        )
    except ProcessOutputLimitExceeded as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        result = GitResult(
            WORKTREE_GIT_OUTPUT_LIMIT_EXIT,
            exc.stdout.decode("utf-8", errors="replace"),
            detail
            or f"git output exceeded {MAX_WORKTREE_GIT_OUTPUT_BYTES} bytes",
            output_truncated=True,
        )
        if exc.cleanup_error is not None:
            result = GitResult(
                result.returncode,
                result.stdout,
                result.stderr
                + f"; process-tree cleanup failed: {exc.cleanup_error}",
                output_truncated=True,
            )
        return result
    except asyncio.TimeoutError as timeout_error:
        try:
            await terminate_process_tree(process, plan=process_tree_plan)
        except ProcessTreeError as exc:
            timeout_error.add_note(f"Process-tree cleanup failed: {exc}")
            return GitResult(
                125,
                "",
                "git command timed out after 30 seconds; "
                f"process-tree cleanup failed: {exc}",
                timed_out=True,
            )
        return GitResult(
            124,
            "",
            "git command timed out after 30 seconds",
            timed_out=True,
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
    except Exception as primary_error:
        cleanup_error, cleanup_cancelled = (
            await settle_process_tree_after_cancellation(
                process, plan=process_tree_plan
            )
        )
        if cleanup_error is not None:
            primary_error.add_note(f"Process-tree cleanup failed: {cleanup_error}")
        if cleanup_cancelled:
            cleanup_cancellation = asyncio.CancelledError()
            cleanup_cancellation.add_note(
                "worktree Git failed before process-tree cleanup was cancelled"
            )
            if cleanup_error is not None:
                cleanup_cancellation.add_note("Process-tree cleanup also failed")
            raise cleanup_cancellation from primary_error
        raise
    return GitResult(
        process.returncode if process.returncode is not None else -1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _worktree_git_operation(args: Sequence[str]) -> str:
    index = 0
    while index < len(args):
        argument = args[index]
        if argument in {"-c", "-C", "--git-dir", "--work-tree"}:
            index += 2
            continue
        if argument.startswith(("--git-dir=", "--work-tree=")):
            index += 1
            continue
        if argument.startswith("-"):
            index += 1
            continue
        if argument == "worktree" and index + 1 < len(args):
            return f"git worktree {args[index + 1]}"
        return f"git {argument}"
    return "git command"


def _safe_agent_id(agent_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]", "-", agent_id).strip(".-")
    normalized = re.sub(r"-+", "-", normalized)[:64]
    if not normalized or normalized != agent_id:
        raise WorktreeError(
            "agent_id must contain only letters, numbers, dots, underscores, or hyphens"
        )
    return normalized


def _validate_agent_branch(branch: str) -> None:
    if not branch.startswith("ash-agent/"):
        raise WorktreeError("only ash-agent/* branches can be managed")
    _safe_agent_id(branch.removeprefix("ash-agent/"))
