"""Managed Git worktrees for isolated subagent execution."""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from sandbox.process_utils import (
    communicate_process,
    process_group_options,
    terminate_process_tree,
)


class WorktreeError(RuntimeError):
    """A managed worktree operation could not be completed safely."""


@dataclass(frozen=True)
class WorktreeLease:
    agent_id: str
    path: Path
    branch: str
    base_commit: str


class WorktreeManager:
    """Create, commit, and clean isolated agent branches."""

    def __init__(self, repository: Path, storage_root: Path) -> None:
        self.repository = repository.expanduser().resolve()
        self.storage_root = storage_root.expanduser().resolve()

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
        self.storage_root.mkdir(parents=True, exist_ok=True)
        path = self.storage_root / safe_id
        if path.exists() or path.is_symlink():
            raise WorktreeError(f"agent worktree path already exists: {path}")
        try:
            await self._git(
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
        except Exception:
            await self._git("branch", "-D", branch, check=False)
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
            raise
        return WorktreeLease(safe_id, path, branch, base_commit)

    async def commit_changes(
        self,
        lease: WorktreeLease,
        *,
        message: str,
    ) -> str | None:
        self._validate_lease_path(lease)
        status = await self._git_at(
            lease.path,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        if not status.stdout:
            return None
        await self._git_at(lease.path, "add", "-A", "--", ".")
        await self._git_at(
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
        hooks = self.storage_root / "empty-hooks"
        hooks.mkdir(parents=True, exist_ok=True)
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
            result = await self._git_at(
                lease.path,
                "-c",
                f"core.hooksPath={hooks}",
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
                await self._git_at(lease.path, "merge", "--abort", check=False)
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
    ) -> None:
        self._validate_lease_path(lease)
        await self._git("worktree", "unlock", str(lease.path), check=False)
        result = await self._git(
            "worktree",
            "remove",
            "--force",
            str(lease.path),
            check=False,
        )
        if result.returncode != 0 and lease.path.exists():
            raise WorktreeError(result.stderr.strip() or "git worktree remove failed")
        if not keep_branch:
            await self._git("branch", "-D", lease.branch, check=False)
        await self._git("worktree", "prune", "--expire", "now", check=False)

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
        hooks = self.storage_root / "empty-hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        result = await self._git(
            "-c",
            f"core.hooksPath={hooks}",
            "-c",
            "commit.gpgsign=false",
            "merge",
            "--squash",
            "--no-commit",
            branch,
            check=False,
        )
        if result.returncode != 0:
            await self._git("reset", "--merge", "HEAD", check=False)
            raise WorktreeError(
                result.stderr.strip() or f"agent branch {branch} conflicts with HEAD"
            )
        changed = await self._git("diff", "--cached", "--quiet", check=False)
        if changed.returncode == 1:
            committed = await self._git(
                "-c",
                f"core.hooksPath={hooks}",
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
                await self._git("reset", "--merge", "HEAD", check=False)
                raise WorktreeError(
                    committed.stderr.strip() or f"could not commit agent branch {branch}"
                )
        elif changed.returncode != 0:
            await self._git("reset", "--merge", "HEAD", check=False)
            raise WorktreeError(
                changed.stderr.strip() or f"could not inspect agent branch {branch}"
            )
        if delete_branch:
            await self._git("branch", "-D", branch)
        return commit

    async def discard_branch(self, branch: str) -> None:
        _validate_agent_branch(branch)
        result = await self._git("branch", "-D", branch, check=False)
        if result.returncode != 0:
            raise WorktreeError(
                result.stderr.strip() or f"could not delete agent branch {branch}"
            )

    async def _repository_root(self) -> Path:
        result = await self._git("rev-parse", "--show-toplevel")
        return Path(result.stdout.strip()).resolve()

    def _validate_lease_path(self, lease: WorktreeLease) -> None:
        try:
            lease.path.resolve().relative_to(self.storage_root)
        except ValueError as exc:
            raise WorktreeError(
                f"worktree is outside managed storage: {lease.path}"
            ) from exc

    async def _git(
        self,
        *args: str,
        check: bool = True,
    ) -> "GitResult":
        return await _run_git(self.repository, args, check=check)

    async def _git_at(
        self,
        cwd: Path,
        *args: str,
        check: bool = True,
    ) -> "GitResult":
        return await _run_git(cwd, args, check=check)


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


async def _run_git(
    cwd: Path,
    args: Sequence[str],
    *,
    check: bool,
) -> GitResult:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_group_options(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            communicate_process(process), timeout=30
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        await terminate_process_tree(process)
        raise
    result = GitResult(
        process.returncode if process.returncode is not None else -1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )
    if check and result.returncode != 0:
        raise WorktreeError(
            result.stderr.strip()
            or f"git {' '.join(args)} failed with exit {result.returncode}"
        )
    return result


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
