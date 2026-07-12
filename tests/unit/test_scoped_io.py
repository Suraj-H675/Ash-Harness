from __future__ import annotations

import hashlib
from contextlib import nullcontext
from unittest.mock import patch

import pytest

from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.safety.scoped_io import (
    ScopedFileChanged,
    ScopedIOError,
    atomic_write_scoped_text,
    list_scoped_directory,
    read_scoped_bytes,
)


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
