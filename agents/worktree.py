"""Managed Git worktrees for isolated subagent execution."""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


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
    )
    stdout, stderr = await process.communicate()
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
