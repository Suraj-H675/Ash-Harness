from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.safety.anchored_fs import AnchoredDirectory
from ash.safety.scoped_io import (
    ScopedFileChanged,
    ScopedIOError,
    atomic_write_scoped_bytes,
    atomic_write_scoped_text,
    atomic_write_scoped_text_chunks,
    list_scoped_directory,
    open_scoped_directory,
    remove_scoped_file,
    read_scoped_bytes,
    restore_scoped_file,
    snapshot_scoped_file,
    stat_scoped_path,
    workspace_mutation_lock,
)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory descriptor")
def test_anchored_directory_listing_uses_fresh_cursor_and_original_inode(
    tmp_path: Path,
) -> None:
    """Enumerating a held directory must not inherit another reader's offset."""
    original = tmp_path / "source"
    original.mkdir()
    (original / "plugin.json").write_text("{}", encoding="utf-8")
    with AnchoredDirectory.open(original, create=False, private=False) as held:
        opened = os.fstat(held.descriptor)
        actual_listdir = os.listdir
        new_cursor_reads = 0

        def stale_cursor_listdir(descriptor: int) -> list[str]:
            nonlocal new_cursor_reads
            if descriptor == held.descriptor:
                return []  # An exhausted shared offset must not hide entries.
            current = os.fstat(descriptor)
            assert (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)
            new_cursor_reads += 1
            return actual_listdir(descriptor)

        # SEEK_END on directory descriptors is unsupported on some filesystems.
        with patch("ash.safety.anchored_fs.os.listdir", side_effect=stale_cursor_listdir):
            assert held.list_names() == ["plugin.json"]

            displaced = tmp_path / "displaced"
            original.rename(displaced)
            original.mkdir()
            (original / "hostile.json").write_text("{}", encoding="utf-8")
            assert held.list_names() == ["plugin.json"]
        assert new_cursor_reads == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory descriptor")
def test_open_scoped_directory_holds_original_inode_after_rename(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    directory = workspace / "work"
    directory.mkdir()
    saved = workspace / "work-saved"
    expected = directory.stat()

    with open_scoped_directory("work", SafetyGuard(workspace)) as (resolved, fd):
        directory.rename(saved)
        held = os.fstat(fd)

    assert resolved == directory
    assert (held.st_dev, held.st_ino) == (expected.st_dev, expected.st_ino)
    assert (saved.stat().st_dev, saved.stat().st_ino) == (held.st_dev, held.st_ino)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory descriptor")
def test_open_scoped_directory_accepts_internal_symlink_alias(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target"
    target.mkdir()
    alias = workspace / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")

    with open_scoped_directory("alias", SafetyGuard(workspace)) as (resolved, fd):
        held = os.fstat(fd)

    expected = target.stat()
    assert resolved == target
    assert (held.st_dev, held.st_ino) == (expected.st_dev, expected.st_ino)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory descriptor")
def test_open_scoped_directory_rejects_external_symlink_alias(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = workspace / "alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")

    with pytest.raises(SafetyViolation, match="outside project scope"):
        with open_scoped_directory("alias", SafetyGuard(workspace)):
            pytest.fail("external alias must not be opened")


@pytest.mark.parametrize("fallback", [False, True])
def test_scoped_read_enforces_byte_limit(tmp_path, fallback: bool) -> None:
    target = tmp_path / "target.txt"
    target.write_bytes(b"12345")
    guard = SafetyGuard(tmp_path)

    mode = (
        patch("ash.safety.scoped_io._supports_anchored_io", return_value=False)
        if fallback
        else nullcontext()
    )
    with mode:
        with pytest.raises(ScopedIOError, match="exceeds 4 bytes"):
            read_scoped_bytes(target, guard, max_bytes=4)
        _, content = read_scoped_bytes(target, guard, max_bytes=5)

    assert content == b"12345"


def test_scoped_read_rejects_in_scope_symlink(tmp_path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("content", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")

    with pytest.raises(SafetyViolation, match="symlink or junction"):
        read_scoped_bytes(link, SafetyGuard(tmp_path))


@pytest.mark.parametrize("fallback", [False, True])
def test_scoped_snapshot_can_reject_hardlinks_without_changing_default(
    tmp_path,
    fallback: bool,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("evidence", encoding="utf-8")
    alias = tmp_path / "alias.txt"
    try:
        os.link(target, alias)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Hard-link creation is unavailable: {exc}")

    mode = (
        patch("ash.safety.scoped_io._supports_anchored_io", return_value=False)
        if fallback
        else nullcontext()
    )
    with mode:
        _, ordinary_snapshot = snapshot_scoped_file(target, SafetyGuard(tmp_path))
        assert ordinary_snapshot.content == b"evidence"
        with pytest.raises(ScopedIOError, match="exactly one hard link"):
            snapshot_scoped_file(
                target,
                SafetyGuard(tmp_path),
                require_single_link=True,
            )


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory descriptor")
def test_scoped_read_rejects_workspace_root_swapped_after_validation(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    saved = tmp_path / "workspace-original"
    replacement = tmp_path / "replacement"
    workspace.mkdir()
    replacement.mkdir()
    (workspace / "target.txt").write_text("safe", encoding="utf-8")
    (replacement / "target.txt").write_text("replacement-secret", encoding="utf-8")
    guard = SafetyGuard(workspace)
    real_validate = guard.validate_mutation_path
    calls = 0

    def validate_then_swap(path):
        nonlocal calls
        calls += 1
        validated = real_validate(path)
        if calls == 2:
            workspace.rename(saved)
            replacement.rename(workspace)
        return validated

    guard.validate_mutation_path = validate_then_swap  # type: ignore[method-assign]

    with pytest.raises(SafetyViolation, match="project root identity changed"):
        read_scoped_bytes("target.txt", guard)

    assert calls == 2


def test_scoped_atomic_write_detects_in_place_change_before_replace(tmp_path) -> None:
    from ash.safety import scoped_io

    target = tmp_path / "target.txt"
    target.write_text("original", encoding="utf-8")
    guard = SafetyGuard(tmp_path)
    expected = hashlib.sha256(b"original").hexdigest()
    real_write = scoped_io._write_all

    def mutate_target(fd, payload):
        real_write(fd, payload)
        target.write_text("concurrent update", encoding="utf-8")

    with patch("ash.safety.scoped_io._write_all", side_effect=mutate_target):
        with pytest.raises(ScopedFileChanged, match="changed before replace"):
            atomic_write_scoped_text(
                target,
                "replacement",
                guard,
                overwrite=True,
                expected_sha256=expected,
            )

    assert target.read_text(encoding="utf-8") == "concurrent update"
    assert list(tmp_path.glob(".target.txt.*.tmp")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX parent-directory fsync")
@pytest.mark.parametrize("overwrite", [False, True])
def test_scoped_atomic_write_reports_uncertain_after_publication(
    tmp_path, overwrite: bool,
) -> None:
    """A failed durability sync cannot conceal an already-visible write."""
    from ash.safety import scoped_io

    target = tmp_path / "target.txt"
    if overwrite:
        target.write_bytes(b"previous")
    real_sync = scoped_io.os.fsync

    def fault_after_publish(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected parent-directory sync failure")
        real_sync(fd)

    with patch("ash.safety.scoped_io.os.fsync", side_effect=fault_after_publish):
        with pytest.raises(ScopedIOError, match="may have changed"):
            atomic_write_scoped_bytes(
                target,
                b"published",
                SafetyGuard(tmp_path),
                overwrite=overwrite,
            )

    assert target.read_bytes() == b"published"
    assert list(tmp_path.glob(".target.txt.*.tmp")) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX file-descriptor sync")
def test_scoped_atomic_write_failing_before_publication_keeps_original(
    tmp_path,
) -> None:
    from ash.safety import scoped_io

    target = tmp_path / "target.txt"
    target.write_bytes(b"original")
    real_sync = scoped_io.os.fsync

    def fail_file_sync(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("injected staged-file sync failure")
        real_sync(fd)

    with patch("ash.safety.scoped_io.os.fsync", side_effect=fail_file_sync):
        with pytest.raises(OSError, match="staged-file sync failure") as error:
            atomic_write_scoped_bytes(
                target, b"replacement", SafetyGuard(tmp_path), overwrite=True
            )

    assert not isinstance(error.value, ScopedIOError)
    assert target.read_bytes() == b"original"
    assert list(tmp_path.glob(".target.txt.*.tmp")) == []


def test_fallback_create_does_not_hide_postpublication_cleanup_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even fallback link publication must not turn a cleanup error into a retry."""
    target = tmp_path / "created.txt"
    original_unlink = Path.unlink

    def fail_staged_cleanup(path, *args, **kwargs):
        if path.name.endswith(".tmp") and path.name.startswith(".created.txt."):
            raise OSError("injected staged-file cleanup failure")
        return original_unlink(path, *args, **kwargs)

    with patch("ash.safety.scoped_io._supports_anchored_io", return_value=False), monkeypatch.context() as scoped_fault:
        scoped_fault.setattr(Path, "unlink", fail_staged_cleanup)
        with pytest.raises(ScopedIOError, match="may have changed"):
            atomic_write_scoped_bytes(
                target, b"published", SafetyGuard(tmp_path), overwrite=False
            )

    assert target.read_bytes() == b"published"


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor-anchored write")
def test_anchored_create_does_not_hide_postpublication_cleanup_failure(tmp_path) -> None:
    """An unlink failure after successful link publication stays ambiguous."""
    from ash.safety import scoped_io

    target = tmp_path / "created.txt"
    original_unlink = scoped_io.os.unlink

    def fail_staged_cleanup(path, *args, **kwargs):
        if str(path).startswith(".created.txt.") and str(path).endswith(".tmp"):
            raise OSError("injected anchored cleanup failure")
        return original_unlink(path, *args, **kwargs)

    # Open the Ash workspace lease before patching the POSIX unlink function.
    with workspace_mutation_lock():
        with patch("ash.safety.scoped_io.os.unlink", side_effect=fail_staged_cleanup):
            with pytest.raises(ScopedIOError, match="may have changed"):
                atomic_write_scoped_bytes(
                    target, b"published", SafetyGuard(tmp_path), overwrite=False
                )

    assert target.read_bytes() == b"published"


def test_revalidated_fallback_write_and_read(tmp_path) -> None:
    target = tmp_path / "nested" / "file.txt"
    guard = SafetyGuard(tmp_path)

    with patch("ash.safety.scoped_io._supports_anchored_io", return_value=False):
        atomic_write_scoped_text(target, "hello", guard, overwrite=False)
        resolved, content = read_scoped_bytes(target, guard)

    assert resolved == target
    assert content == b"hello"


def test_fallback_no_overwrite_is_atomic(tmp_path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("existing", encoding="utf-8")

    with (
        patch("ash.safety.scoped_io._supports_anchored_io", return_value=False),
        pytest.raises(FileExistsError),
    ):
        atomic_write_scoped_text(
            target,
            "replacement",
            SafetyGuard(tmp_path),
            overwrite=False,
        )

    assert target.read_text(encoding="utf-8") == "existing"


@pytest.mark.parametrize("fallback", [False, True])
def test_streaming_atomic_write_cleans_up_on_iterator_failure(
    tmp_path,
    fallback: bool,
) -> None:
    target = tmp_path / "streamed.txt"
    target.write_text("original", encoding="utf-8")
    guard = SafetyGuard(tmp_path)

    def chunks():
        yield "first chunk"
        raise RuntimeError("synthetic export failure")

    mode = (
        patch("ash.safety.scoped_io._supports_anchored_io", return_value=False)
        if fallback
        else nullcontext()
    )
    with mode:
        with pytest.raises(RuntimeError, match="synthetic export failure"):
            atomic_write_scoped_text_chunks(
                target,
                chunks(),
                guard,
                overwrite=True,
            )

    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob(".streamed.txt.*.tmp")) == []


@pytest.mark.parametrize("fallback", [False, True])
def test_scoped_restore_and_remove_check_state_and_mode(tmp_path, fallback: bool) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("current", encoding="utf-8")
    target.chmod(0o600)
    created = tmp_path / "nested" / "created.txt"
    guard = SafetyGuard(tmp_path)

    mode = (
        patch("ash.safety.scoped_io._supports_anchored_io", return_value=False)
        if fallback
        else nullcontext()
    )
    with mode:
        restore_scoped_file(
            target,
            b"restored",
            guard,
            expected_sha256=hashlib.sha256(b"current").hexdigest(),
            expected_mode=0o600,
            mode=0o640,
        )
        assert target.read_bytes() == b"restored"
        assert target.stat().st_mode & 0o777 == 0o640

        with pytest.raises(ScopedFileChanged):
            restore_scoped_file(
                target,
                b"must-not-write",
                guard,
                expected_sha256=hashlib.sha256(b"current").hexdigest(),
                mode=0o600,
            )

        target.chmod(0o600)
        with pytest.raises(ScopedFileChanged, match="permissions changed"):
            restore_scoped_file(
                target,
                b"must-not-write",
                guard,
                expected_sha256=hashlib.sha256(b"restored").hexdigest(),
                expected_mode=0o640,
                mode=0o640,
            )
        assert target.read_bytes() == b"restored"
        assert target.stat().st_mode & 0o777 == 0o600

        restore_scoped_file(
            created,
            b"created",
            guard,
            expected_sha256="missing",
            mode=0o640,
        )
        remove_scoped_file(
            target,
            guard,
            expected_sha256=hashlib.sha256(b"restored").hexdigest(),
        )
        remove_scoped_file(
            created,
            guard,
            expected_sha256=hashlib.sha256(b"created").hexdigest(),
        )

    assert not target.exists()
    assert not created.exists()


@pytest.mark.asyncio
async def test_workspace_mutation_lock_is_reentrant_but_rejects_other_task(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def owner() -> None:
        with workspace_mutation_lock():
            with workspace_mutation_lock():
                entered.set()
                await release.wait()

    async def contender() -> None:
        await entered.wait()
        with pytest.raises(ScopedIOError, match="another Ash workspace mutation"):
            with workspace_mutation_lock():
                pass
        release.set()

    await asyncio.gather(owner(), contender())


@pytest.mark.parametrize("fallback", [False, True])
def test_missing_restore_and_remove_preserve_new_file(
    tmp_path,
    fallback: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.safety import scoped_io

    target = tmp_path / "created.txt"
    guard = SafetyGuard(tmp_path)
    mode = (
        patch("ash.safety.scoped_io._supports_anchored_io", return_value=False)
        if fallback
        else nullcontext()
    )
    with mode:
        if fallback:
            real_link = os.link

            def create_before_link(source, destination, *args, **kwargs):
                target.write_bytes(b"external")
                return real_link(source, destination, *args, **kwargs)

            monkeypatch.setattr(os, "link", create_before_link)
        else:
            real_link = scoped_io.os.link

            def create_before_link(source, destination, *args, **kwargs):
                target.write_bytes(b"external")
                return real_link(source, destination, *args, **kwargs)

            monkeypatch.setattr(scoped_io.os, "link", create_before_link)

        with pytest.raises(ScopedFileChanged, match="appeared"):
            restore_scoped_file(
                target,
                b"restored",
                guard,
                expected_sha256="missing",
                mode=0o600,
            )
        assert target.read_bytes() == b"external"

    target.unlink()
    original_digest = scoped_io._entry_digest

    def appear_after_missing(
        parent_fd, name, path, *, max_bytes, expected_mode
    ):
        digest = original_digest(
            parent_fd,
            name,
            path,
            max_bytes=max_bytes,
            expected_mode=expected_mode,
        )
        if digest == "missing":
            target.write_bytes(b"external")
        return digest

    if fallback:
        original_fallback_digest = scoped_io._fallback_entry_digest

        def fallback_appear_after_missing(
            path, file_guard, *, max_bytes, expected_mode
        ):
            digest = original_fallback_digest(
                path,
                file_guard,
                max_bytes=max_bytes,
                expected_mode=expected_mode,
            )
            if digest == "missing":
                target.write_bytes(b"external")
            return digest

        monkeypatch.setattr(
            scoped_io,
            "_fallback_entry_digest",
            fallback_appear_after_missing,
        )
        with patch("ash.safety.scoped_io._supports_anchored_io", return_value=False):
            remove_scoped_file(target, guard, expected_sha256="missing")
    else:
        monkeypatch.setattr(scoped_io, "_entry_digest", appear_after_missing)
        remove_scoped_file(target, guard, expected_sha256="missing")

    assert target.read_bytes() == b"external"


def test_scoped_directory_listing_does_not_follow_child_links(tmp_path) -> None:
    (tmp_path / "folder").mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    link = tmp_path / "linked-folder"
    try:
        link.symlink_to(tmp_path / "folder", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")

    _, entries = list_scoped_directory(tmp_path, SafetyGuard(tmp_path))

    assert ("folder", True) in entries
    assert ("file.txt", False) in entries
    assert ("linked-folder", False) in entries


@pytest.mark.parametrize("fallback", [False, True])
def test_scoped_stat_reports_regular_files_and_directories(
    tmp_path, fallback: bool
) -> None:
    directory = tmp_path / "folder"
    directory.mkdir()
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    guard = SafetyGuard(tmp_path)

    mode = (
        patch("ash.safety.scoped_io._supports_anchored_io", return_value=False)
        if fallback
        else nullcontext()
    )
    with mode:
        _, directory_metadata = stat_scoped_path(directory, guard)
        _, file_metadata = stat_scoped_path(file_path, guard)

    assert stat.S_ISDIR(directory_metadata.st_mode)
    assert stat.S_ISREG(file_metadata.st_mode)


def test_atomic_write_can_refuse_parent_creation_and_hardlink_replacement(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    guard = SafetyGuard(workspace)
    missing_parent = workspace / "not-created" / "target.txt"
    with pytest.raises((FileNotFoundError, ScopedIOError)):
        atomic_write_scoped_bytes(
            missing_parent,
            b"content",
            guard,
            overwrite=False,
            create_parents=False,
        )
    assert not missing_parent.parent.exists()

    target = workspace / "target.txt"
    alias = workspace / "alias.txt"
    target.write_bytes(b"old")
    try:
        os.link(target, alias)
    except (NotImplementedError, OSError):
        pytest.skip("hard links are unavailable")
    with pytest.raises(ScopedIOError, match="hard link"):
        atomic_write_scoped_bytes(
            target,
            b"new",
            guard,
            overwrite=True,
            expected_sha256=hashlib.sha256(b"old").hexdigest(),
            require_single_link=True,
        )
    assert target.read_bytes() == b"old"
