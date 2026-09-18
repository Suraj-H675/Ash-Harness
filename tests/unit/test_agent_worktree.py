from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from ash.agents.worktree import WorktreeError, WorktreeManager


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "file.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "file.txt")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "initial",
    )
    return root


def test_worktree_agent_commits_branch_without_mutating_lead(
    repository: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def run():
        lease = await manager.create("coder-1")
        (lease.path / "file.txt").write_text("worker\n", encoding="utf-8")
        commit = await manager.commit_changes(lease, message="agent change")
        await manager.remove(lease, keep_branch=True)
        return lease, commit

    lease, commit = asyncio.run(run())

    assert commit
    assert repository.joinpath("file.txt").read_text(encoding="utf-8") == "base\n"
    assert not lease.path.exists()
    assert _git(repository, "show", f"{lease.branch}:file.txt") == "worker"


def test_worktree_agent_branch_can_be_applied_and_removed(
    repository: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def run():
        lease = await manager.create("coder-apply")
        (lease.path / "file.txt").write_text("applied\n", encoding="utf-8")
        await manager.commit_changes(lease, message="agent change")
        await manager.remove(lease, keep_branch=True)
        branches = await manager.list_agent_branches()
        commit = await manager.apply_branch(lease.branch)
        return lease, branches, commit

    lease, branches, commit = asyncio.run(run())

    assert (lease.branch, commit) in branches
    assert repository.joinpath("file.txt").read_text(encoding="utf-8") == "applied\n"
    result = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{lease.branch}"],
        cwd=repository,
        check=False,
    )
    assert result.returncode != 0


def test_worktree_without_changes_removes_branch(
    repository: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def run():
        lease = await manager.create("reviewer-1")
        assert await manager.commit_changes(lease, message="unused") is None
        await manager.remove(lease, keep_branch=False)
        return lease

    lease = asyncio.run(run())
    result = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{lease.branch}"],
        cwd=repository,
        check=False,
    )
    assert result.returncode != 0


def test_worktree_remove_preserves_uncommitted_changes(
    repository: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def run():
        lease = await manager.create("dirty-cleanup")
        (lease.path / "file.txt").write_text("valuable work\n", encoding="utf-8")
        with pytest.raises(WorktreeError, match="uncommitted changes"):
            await manager.remove(lease, keep_branch=False)
        return lease

    lease = asyncio.run(run())

    assert lease.path.joinpath("file.txt").read_text(encoding="utf-8") == (
        "valuable work\n"
    )
    assert _git(repository, "rev-parse", lease.branch) == lease.base_commit


def test_worktree_remove_preserves_unretained_commits(
    repository: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def run():
        lease = await manager.create("committed-cleanup")
        (lease.path / "file.txt").write_text("worker commit\n", encoding="utf-8")
        _git(lease.path, "add", "file.txt")
        _git(
            lease.path,
            "-c",
            "user.name=Worker",
            "-c",
            "user.email=worker@example.com",
            "commit",
            "-qm",
            "worker commit",
        )
        worker_commit = _git(lease.path, "rev-parse", "HEAD")
        with pytest.raises(WorktreeError, match="unretained commits"):
            await manager.remove(lease, keep_branch=False)
        return lease, worker_commit

    lease, worker_commit = asyncio.run(run())

    assert lease.path.exists()
    assert _git(repository, "rev-parse", lease.branch) == worker_commit


def test_commit_changes_returns_preexisting_worker_commit(
    repository: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def run():
        lease = await manager.create("self-commit")
        (lease.path / "file.txt").write_text("worker commit\n", encoding="utf-8")
        _git(lease.path, "add", "file.txt")
        _git(
            lease.path,
            "-c",
            "user.name=Worker",
            "-c",
            "user.email=worker@example.com",
            "commit",
            "-qm",
            "worker commit",
        )
        worker_commit = _git(lease.path, "rev-parse", "HEAD")
        captured = await manager.commit_changes(
            lease,
            message="ash capture",
            baseline_commit=lease.base_commit,
        )
        await manager.remove(lease, keep_branch=True)
        return lease, worker_commit, captured

    lease, worker_commit, captured = asyncio.run(run())

    assert captured == worker_commit
    assert _git(repository, "rev-parse", lease.branch) == worker_commit


def test_commit_changes_rejects_detached_worker_head(
    repository: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def run():
        lease = await manager.create("detached-worker")
        _git(lease.path, "checkout", "--detach", "-q")
        (lease.path / "file.txt").write_text("detached work\n", encoding="utf-8")
        _git(lease.path, "add", "file.txt")
        _git(
            lease.path,
            "-c",
            "user.name=Worker",
            "-c",
            "user.email=worker@example.com",
            "commit",
            "-qm",
            "detached worker commit",
        )
        detached_commit = _git(lease.path, "rev-parse", "HEAD")
        with pytest.raises(WorktreeError, match="managed branch"):
            await manager.commit_changes(
                lease,
                message="ash capture",
                baseline_commit=lease.base_commit,
            )
        return lease, detached_commit

    lease, detached_commit = asyncio.run(run())

    assert lease.path.exists()
    assert _git(lease.path, "rev-parse", "HEAD") == detached_commit


def test_dependent_worktree_accepts_verified_agent_commit(
    repository: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def run():
        producer = await manager.create("producer")
        (producer.path / "file.txt").write_text("producer\n", encoding="utf-8")
        commit = await manager.commit_changes(producer, message="produce")
        assert commit is not None
        await manager.remove(producer, keep_branch=True)
        consumer = await manager.create("consumer")
        accepted = await manager.accept_git_artifacts(
            consumer, [(producer.branch, commit)]
        )
        content = (consumer.path / "file.txt").read_text(encoding="utf-8")
        await manager.remove(consumer, keep_branch=True)
        return producer, consumer, accepted, content

    producer, consumer, accepted, content = asyncio.run(run())

    assert accepted is not None
    assert content == "producer\n"
    assert repository.joinpath("file.txt").read_text(encoding="utf-8") == "base\n"
    assert (
        _git(repository, "merge-base", "--is-ancestor", producer.branch, accepted) == ""
    )
    assert _git(repository, "rev-parse", consumer.branch) == accepted


def test_accepted_dependency_commit_is_disposable_baseline(
    repository: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def run():
        producer = await manager.create("baseline-producer")
        (producer.path / "producer.txt").write_text("producer\n", encoding="utf-8")
        producer_commit = await manager.commit_changes(producer, message="produce")
        assert producer_commit is not None
        await manager.remove(producer, keep_branch=True)

        consumer = await manager.create("baseline-consumer")
        accepted = await manager.accept_git_artifacts(
            consumer, [(producer.branch, producer_commit)]
        )
        assert accepted is not None
        captured = await manager.commit_changes(
            consumer,
            message="no worker changes",
            baseline_commit=accepted,
        )
        await manager.remove(
            consumer,
            keep_branch=False,
            expected_head=accepted,
        )
        return consumer, captured

    consumer, captured = asyncio.run(run())

    assert captured is None
    result = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{consumer.branch}"],
        cwd=repository,
        check=False,
    )
    assert result.returncode != 0


def test_dependent_worktree_rejects_stale_artifact_commit(
    repository: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def run():
        producer = await manager.create("producer-stale")
        (producer.path / "file.txt").write_text("producer\n", encoding="utf-8")
        commit = await manager.commit_changes(producer, message="produce")
        assert commit is not None
        await manager.remove(producer, keep_branch=True)
        consumer = await manager.create("consumer-stale")
        try:
            with pytest.raises(WorktreeError, match="no longer matches"):
                await manager.accept_git_artifacts(
                    consumer, [(producer.branch, "0" * len(commit))]
                )
        finally:
            await manager.remove(consumer, keep_branch=False)

    asyncio.run(run())


def test_dependent_worktree_aborts_conflicting_artifact_merge(
    repository: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def produce(agent_id: str, content: str):
        lease = await manager.create(agent_id)
        (lease.path / "file.txt").write_text(content, encoding="utf-8")
        commit = await manager.commit_changes(lease, message=agent_id)
        assert commit is not None
        await manager.remove(lease, keep_branch=True)
        return lease, commit

    async def run():
        first, first_commit = await produce("conflict-one", "one\n")
        second, second_commit = await produce("conflict-two", "two\n")
        consumer = await manager.create("conflict-consumer")
        try:
            with pytest.raises(WorktreeError):
                await manager.accept_git_artifacts(
                    consumer,
                    [
                        (first.branch, first_commit),
                        (second.branch, second_commit),
                    ],
                )
            status = _git(consumer.path, "status", "--porcelain=v1")
            assert status == ""
            assert _git(consumer.path, "rev-parse", "HEAD") == consumer.base_commit
        finally:
            await manager.remove(consumer, keep_branch=False)

    asyncio.run(run())


def test_apply_dependent_branch_squashes_inherited_and_new_changes(
    repository: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def run():
        producer = await manager.create("squash-producer")
        (producer.path / "producer.txt").write_text("producer\n", encoding="utf-8")
        producer_commit = await manager.commit_changes(producer, message="producer")
        assert producer_commit is not None
        await manager.remove(producer, keep_branch=True)

        consumer = await manager.create("squash-consumer")
        await manager.accept_git_artifacts(
            consumer, [(producer.branch, producer_commit)]
        )
        (consumer.path / "consumer.txt").write_text("consumer\n", encoding="utf-8")
        consumer_commit = await manager.commit_changes(consumer, message="consumer")
        assert consumer_commit is not None
        await manager.remove(consumer, keep_branch=True)
        applied = await manager.apply_branch(consumer.branch)
        return consumer_commit, applied

    consumer_commit, applied = asyncio.run(run())

    assert applied == consumer_commit
    assert (repository / "producer.txt").read_text(encoding="utf-8") == "producer\n"
    assert (repository / "consumer.txt").read_text(encoding="utf-8") == "consumer\n"
    assert _git(repository, "show", "--format=%s", "--no-patch", "HEAD") == (
        "Apply ash-agent/squash-consumer"
    )


def test_worktree_rejects_dirty_lead(repository: Path, tmp_path: Path) -> None:
    (repository / "file.txt").write_text("dirty\n", encoding="utf-8")
    manager = WorktreeManager(repository, tmp_path / "agents")

    with pytest.raises(WorktreeError, match="clean lead worktree"):
        asyncio.run(manager.create("coder-1"))


def test_worktree_rejects_unsafe_agent_id(repository: Path, tmp_path: Path) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    with pytest.raises(WorktreeError, match="agent_id"):
        asyncio.run(manager.create("../escape"))


def test_worktree_rejects_symlinked_storage_parent(
    repository: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "worktrees"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(WorktreeError, match="agent worktree storage.*symlink or junction"):
        WorktreeManager(repository, linked_parent / "project")

    assert list(outside.iterdir()) == []


def test_worktree_revalidates_storage_before_create(
    repository: Path,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "agents"
    manager = WorktreeManager(repository, storage_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        storage_root.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(WorktreeError, match="agent worktree storage.*symlink or junction"):
        asyncio.run(manager.create("coder-swap"))

    assert list(outside.iterdir()) == []
    result = subprocess.run(
        ["git", "show-ref", "--verify", "refs/heads/ash-agent/coder-swap"],
        cwd=repository,
        check=False,
    )
    assert result.returncode != 0


def test_worktree_add_failure_does_not_delete_replacement_directory(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "agents"
    manager = WorktreeManager(repository, storage_root)
    victim = tmp_path / "victim"
    victim.mkdir()
    sentinel = victim / "sentinel.txt"
    sentinel.write_text("DO NOT DELETE\n", encoding="utf-8")
    real_git = manager._git

    async def fail_after_replacement(*args: str, check: bool = True):
        if args[:2] == ("worktree", "add"):
            path = storage_root / "coder-replaced"
            path.mkdir()
            (path / "partial.txt").write_text("partial\n", encoding="utf-8")
            path.rename(storage_root / "partial-saved")
            victim.rename(path)
            raise WorktreeError("simulated worktree add failure")
        return await real_git(*args, check=check)

    monkeypatch.setattr(manager, "_git", fail_after_replacement)

    with pytest.raises(WorktreeError, match="simulated worktree add failure"):
        asyncio.run(manager.create("coder-replaced"))

    replacement = storage_root / "coder-replaced"
    assert (replacement / "sentinel.txt").read_text(encoding="utf-8") == (
        "DO NOT DELETE\n"
    )
    assert (storage_root / "partial-saved" / "partial.txt").read_text(
        encoding="utf-8"
    ) == "partial\n"


def test_worktree_add_failure_does_not_delete_replacement_branch(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")
    real_git = manager._git
    replacement_branch = "ash-agent/coder-branch-replaced"

    async def fail_after_branch_replacement(*args: str, check: bool = True):
        if args[:2] == ("worktree", "add"):
            _git(repository, "branch", replacement_branch, "HEAD")
            raise WorktreeError("simulated worktree add failure")
        return await real_git(*args, check=check)

    monkeypatch.setattr(manager, "_git", fail_after_branch_replacement)

    with pytest.raises(WorktreeError, match="simulated worktree add failure"):
        asyncio.run(manager.create("coder-branch-replaced"))

    assert _git(
        repository,
        "show-ref",
        "--verify",
        f"refs/heads/{replacement_branch}",
    )


def test_worktree_apply_rejects_dirty_lead(repository: Path, tmp_path: Path) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def prepare():
        lease = await manager.create("coder-dirty")
        (lease.path / "file.txt").write_text("worker\n", encoding="utf-8")
        await manager.commit_changes(lease, message="agent change")
        await manager.remove(lease, keep_branch=True)
        return lease

    lease = asyncio.run(prepare())
    (repository / "local.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(WorktreeError, match="clean lead worktree"):
        asyncio.run(manager.apply_branch(lease.branch))
