from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ash.agents.worktree import WorktreeError, WorktreeManager, _run_git


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX Git process timeout regression")
def test_worktree_commit_timeout_preserves_unknown_outcome(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.agents.worktree as worktree_module

    manager = WorktreeManager(repository, tmp_path / "agents")
    lease = asyncio.run(manager.create("coder-timeout"))
    (lease.path / "file.txt").write_text("worker\n", encoding="utf-8")

    real_git = shutil.which("git")
    assert real_git is not None
    git_wrapper = tmp_path / "git-wrapper"
    git_wrapper.write_text(
        "#!/bin/sh\n"
        "is_commit=0\n"
        "for arg in \"$@\"; do\n"
        "  [ \"$arg\" = commit ] && is_commit=1\n"
        "done\n"
        f"'{real_git}' \"$@\"\n"
        "status=$?\n"
        "if [ \"$status\" -eq 0 ] && [ \"$is_commit\" -eq 1 ]; then sleep 3; fi\n"
        "exit \"$status\"\n",
        encoding="utf-8",
    )
    git_wrapper.chmod(0o755)
    monkeypatch.setattr(
        worktree_module,
        "resolve_host_executable",
        lambda *_args, **_kwargs: str(git_wrapper),
    )
    real_wait_for = asyncio.wait_for

    async def shortened_wait_for(awaitable, *, timeout):
        if (
            getattr(awaitable, "cr_code", None)
            is worktree_module.communicate_process.__code__
        ):
            timeout = 1.0
        return await real_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(worktree_module.asyncio, "wait_for", shortened_wait_for)

    with pytest.raises(WorktreeError) as commit_error:
        asyncio.run(manager.commit_changes(lease, message="worker commit"))

    assert commit_error.value.outcome_unknown is True
    assert "outcome is unknown" in str(commit_error.value)
    assert _git(lease.path, "log", "-1", "--format=%s") == "worker commit"


@pytest.mark.skipif(os.name == "nt", reason="descriptor-anchored POSIX regression")
def test_worktree_storage_creation_does_not_follow_parent_swap(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.safety.anchored_fs as anchored_fs

    state_root = tmp_path / "state"
    state_root.mkdir()
    saved_root = tmp_path / "state-saved"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    storage = state_root / "worktrees" / "repo-id"
    manager = WorktreeManager(repository, storage)
    real_open_or_create = anchored_fs._open_or_create_directory
    swapped = False

    def open_or_create_then_swap(
        parent_descriptor,
        name,
        *,
        create,
        expected=None,
    ):
        nonlocal swapped
        if name == "worktrees" and not swapped:
            state_root.rename(saved_root)
            state_root.symlink_to(replacement, target_is_directory=True)
            swapped = True
        return real_open_or_create(
            parent_descriptor,
            name,
            create=create,
            expected=expected,
        )

    monkeypatch.setattr(
        anchored_fs,
        "_open_or_create_directory",
        open_or_create_then_swap,
    )

    with pytest.raises(WorktreeError, match="could not be prepared safely"):
        manager._prepare_storage_root()

    assert swapped is True
    assert not (replacement / "worktrees").exists()
    assert (saved_root / "worktrees" / "repo-id").is_dir()


@pytest.mark.skipif(os.name == "nt", reason="descriptor-anchored POSIX regression")
@pytest.mark.asyncio
async def test_worktree_create_refuses_lease_when_storage_changes_before_git_add(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.agents.worktree as worktree_module

    state_root = tmp_path / "state"
    state_root.mkdir()
    storage = state_root / "agents"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    saved_root = tmp_path / "state-saved"
    manager = WorktreeManager(repository, storage)
    real_run_git = worktree_module._run_git
    swapped = False

    async def run_git_then_swap(cwd, args, **kwargs):
        nonlocal swapped
        if len(args) >= 2 and args[0] == "worktree" and args[1] == "add" and not swapped:
            state_root.rename(saved_root)
            state_root.symlink_to(replacement, target_is_directory=True)
            swapped = True
        return await real_run_git(cwd, args, **kwargs)

    monkeypatch.setattr(worktree_module, "_run_git", run_git_then_swap)

    with pytest.raises(
        WorktreeError,
        match="agent worktree storage.*symlink or junction|storage identity changed",
    ):
        await manager.create("coder-race")

    assert swapped is True
    assert (replacement / "agents" / "coder-race").is_dir()
    assert not (saved_root / "agents" / "coder-race").exists()


@pytest.mark.asyncio
async def test_worktree_lease_rejects_replaced_worktree_directory(
    repository: Path,
    tmp_path: Path,
) -> None:
    manager = WorktreeManager(repository, tmp_path / "agents")
    lease = await manager.create("coder-identity")
    original = lease.path.with_name(f"{lease.path.name}-original")
    lease.path.rename(original)
    lease.path.mkdir()

    with pytest.raises(WorktreeError, match="worktree identity changed"):
        await manager.commit_changes(lease, message="must not run")


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable hook fixture")
def test_worktree_create_does_not_run_repository_post_checkout_hook(
    repository: Path,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "post-checkout-ran"
    hook = repository / ".git" / "hooks" / "post-checkout"
    hook.write_text(
        f"#!/bin/sh\nprintf ran >> {marker}\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def run() -> None:
        lease = await manager.create("hook-create")
        await manager.remove(lease, keep_branch=False)

    asyncio.run(run())

    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable hook fixture")
def test_worktree_commit_does_not_run_repository_post_commit_hook(
    repository: Path,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "post-commit-ran"
    hook = repository / ".git" / "hooks" / "post-commit"
    hook.write_text(
        f"#!/bin/sh\nprintf ran >> {marker}\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    manager = WorktreeManager(repository, tmp_path / "agents")

    async def run() -> None:
        lease = await manager.create("hook-commit")
        (lease.path / "file.txt").write_text("worker\n", encoding="utf-8")
        assert await manager.commit_changes(lease, message="agent change") is not None
        await manager.remove(lease, keep_branch=True)

    asyncio.run(run())

    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable filter fixture")
def test_worktree_refuses_repository_configured_executable_filter(
    repository: Path,
    tmp_path: Path,
) -> None:
    (repository / ".gitattributes").write_text(
        "file.txt filter=leak\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".gitattributes")
    _git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "attributes",
    )
    marker = tmp_path / "filter-ran"
    helper = tmp_path / "filter.sh"
    helper.write_text(
        f"#!/bin/sh\nprintf ran >> {marker}\ncat\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    _git(repository, "config", "filter.leak.clean", str(helper))
    manager = WorktreeManager(repository, tmp_path / "agents")

    with pytest.raises(WorktreeError, match="untrusted repository Git config"):
        asyncio.run(manager.create("filter-config"))

    assert not marker.exists()


def test_worktree_refuses_repository_configured_merge_driver(
    repository: Path,
    tmp_path: Path,
) -> None:
    _git(repository, "config", "merge.untrusted.driver", "false")
    manager = WorktreeManager(repository, tmp_path / "agents")

    with pytest.raises(WorktreeError, match="untrusted repository Git config"):
        asyncio.run(manager.create("merge-driver"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_worktree_manager_refuses_replaced_repository_before_branch_delete(
    repository: Path,
    tmp_path: Path,
) -> None:
    saved = tmp_path / "repository-saved"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    _git(replacement, "init", "-q")
    (replacement / "file.txt").write_text("replacement\n", encoding="utf-8")
    _git(replacement, "add", "file.txt")
    _git(
        replacement,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "replacement",
    )
    _git(replacement, "branch", "ash-agent/victim")
    manager = WorktreeManager(repository, tmp_path / "agents")
    repository.rename(saved)
    replacement.rename(repository)

    with pytest.raises(WorktreeError, match="working directory identity changed"):
        await manager.discard_branch("ash-agent/victim")

    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/ash-agent/victim"],
            cwd=repository,
            check=False,
        ).returncode
        == 0
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_worktree_git_refuses_swap_during_executable_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.agents.worktree as worktree_module

    repository = tmp_path / "repository"
    saved = tmp_path / "repository-saved"
    replacement = tmp_path / "replacement"
    repository.mkdir()
    replacement.mkdir()
    _git(repository, "init", "-q")
    _git(replacement, "init", "-q")
    real_resolve = worktree_module.resolve_host_executable
    swapped = False

    def resolve_then_swap(name: str, *, workspace_root: Path, cwd: Path):
        nonlocal swapped
        resolved = real_resolve(name, workspace_root=workspace_root, cwd=cwd)
        if not swapped:
            swapped = True
            repository.rename(saved)
            replacement.rename(repository)
        return resolved

    monkeypatch.setattr(
        worktree_module,
        "resolve_host_executable",
        resolve_then_swap,
    )

    result = await _run_git(
        repository,
        ("rev-parse", "--show-toplevel"),
        check=False,
    )

    assert swapped is True
    assert result.returncode == 126
    assert "working directory identity changed" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX cwd race regression")
@pytest.mark.asyncio
async def test_worktree_git_cwd_swap_cannot_escape_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.sandbox import process_utils as process_utils_module

    repository = tmp_path / "repository"
    outside = tmp_path / "outside"
    saved = tmp_path / "repository-saved"
    repository.mkdir()
    outside.mkdir()
    _git(repository, "init", "-q")
    _git(outside, "init", "-q")
    real_prepare = process_utils_module.prepare_process_tree
    swapped = False

    def prepare_then_swap(*args, **kwargs):
        nonlocal swapped
        plan = real_prepare(*args, **kwargs)
        if not swapped:
            swapped = True
            repository.rename(saved)
            try:
                repository.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return plan

    monkeypatch.setattr("ash.agents.worktree.prepare_process_tree", prepare_then_swap)

    result = await _run_git(
        repository,
        ("rev-parse", "--show-toplevel"),
        check=False,
    )

    assert swapped is True
    assert result.returncode == 126
    assert result.stdout == ""
    assert "working directory identity changed" in result.stderr


@pytest.mark.asyncio
async def test_worktree_git_fails_closed_without_stable_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from ash.sandbox.process_utils import ProcessTreeUnavailable

    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")

    def unavailable(*args, **kwargs):
        raise ProcessTreeUnavailable("stable cwd unavailable")

    create = AsyncMock(side_effect=AssertionError("worktree Git must not launch"))
    monkeypatch.setattr(
        "ash.agents.worktree.prepare_scoped_process_launch", unavailable
    )
    monkeypatch.setattr(
        "ash.agents.worktree.asyncio.create_subprocess_exec", create
    )

    result = await _run_git(repository, ("status",), check=False)

    assert result.returncode == 126
    assert "stable cwd unavailable" in result.stderr
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_worktree_git_cleans_process_tree_after_unexpected_io_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    import ash.agents.worktree as worktree_module

    class FakeProcess:
        returncode = None

    process = FakeProcess()
    cleaned = False

    @contextmanager
    def launch_context():
        yield SimpleNamespace(argv=("git", "status"), pass_fds=(), cwd=str(tmp_path))

    async def spawn(*args, **kwargs):
        del args, kwargs
        return process

    async def fail_communicate(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("worktree git stream failed")

    async def cleanup(target, *, plan=None, grace_seconds=1.0):
        nonlocal cleaned
        del plan, grace_seconds
        assert target is process
        cleaned = True
        return None, False

    monkeypatch.setattr(
        worktree_module,
        "prepare_scoped_process_launch",
        lambda *args, **kwargs: launch_context(),
    )
    monkeypatch.setattr(
        worktree_module,
        "prepare_process_tree",
        lambda *args, **kwargs: SimpleNamespace(spawn_options={}),
    )
    monkeypatch.setattr(worktree_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(worktree_module, "communicate_process", fail_communicate)
    monkeypatch.setattr(
        worktree_module,
        "settle_process_tree_after_cancellation",
        cleanup,
    )

    with pytest.raises(RuntimeError, match="worktree git stream failed"):
        await worktree_module._run_git_process(
            tmp_path,
            "git",
            ("status",),
            {},
            expected_cwd_identity=None,
        )

    assert cleaned is True


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX Git process timeout regression")
def test_apply_branch_does_not_reset_after_merge_timeout(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.agents.worktree as worktree_module

    manager = WorktreeManager(repository, tmp_path / "agents")
    lease = asyncio.run(manager.create("coder-apply-timeout"))
    (lease.path / "file.txt").write_text("applied\n", encoding="utf-8")
    asyncio.run(manager.commit_changes(lease, message="agent change"))
    asyncio.run(manager.remove(lease, keep_branch=True))
    lead_head = _git(repository, "rev-parse", "HEAD")

    real_git = shutil.which("git")
    assert real_git is not None
    reset_marker = tmp_path / "reset-ran"
    git_wrapper = tmp_path / "git-wrapper"
    git_wrapper.write_text(
        "#!/bin/sh\n"
        "is_merge=0\n"
        "is_reset=0\n"
        "for arg in \"$@\"; do\n"
        "  [ \"$arg\" = merge ] && is_merge=1\n"
        "  [ \"$arg\" = reset ] && is_reset=1\n"
        "done\n"
        f"if [ \"$is_reset\" -eq 1 ]; then printf reset >> '{reset_marker}'; fi\n"
        f"'{real_git}' \"$@\"\n"
        "status=$?\n"
        "if [ \"$status\" -eq 0 ] && [ \"$is_merge\" -eq 1 ]; then sleep 3; fi\n"
        "exit \"$status\"\n",
        encoding="utf-8",
    )
    git_wrapper.chmod(0o755)
    monkeypatch.setattr(
        worktree_module,
        "resolve_host_executable",
        lambda *_args, **_kwargs: str(git_wrapper),
    )
    real_wait_for = asyncio.wait_for

    async def shortened_wait_for(awaitable, *, timeout):
        if (
            getattr(awaitable, "cr_code", None)
            is worktree_module.communicate_process.__code__
        ):
            timeout = 1.0
        return await real_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(worktree_module.asyncio, "wait_for", shortened_wait_for)

    with pytest.raises(WorktreeError) as merge_error:
        asyncio.run(manager.apply_branch(lease.branch))

    assert merge_error.value.outcome_unknown is True
    assert "git merge timed out" in str(merge_error.value)
    assert _git(repository, "rev-parse", "HEAD") == lead_head
    assert not reset_marker.exists()
    staged = subprocess.run(
        [real_git, "diff", "--cached", "--quiet"],
        cwd=repository,
        check=False,
    )
    assert staged.returncode == 1


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
    real_git = manager._git_mutation

    async def fail_after_replacement(*args: str, check: bool = True):
        if args[:2] == ("worktree", "add"):
            path = storage_root / "coder-replaced"
            path.mkdir()
            (path / "partial.txt").write_text("partial\n", encoding="utf-8")
            path.rename(storage_root / "partial-saved")
            victim.rename(path)
            raise WorktreeError("simulated worktree add failure")
        return await real_git(*args, check=check)

    monkeypatch.setattr(manager, "_git_mutation", fail_after_replacement)

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
    real_git = manager._git_mutation
    replacement_branch = "ash-agent/coder-branch-replaced"

    async def fail_after_branch_replacement(*args: str, check: bool = True):
        if args[:2] == ("worktree", "add"):
            _git(repository, "branch", replacement_branch, "HEAD")
            raise WorktreeError("simulated worktree add failure")
        return await real_git(*args, check=check)

    monkeypatch.setattr(manager, "_git_mutation", fail_after_branch_replacement)

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
