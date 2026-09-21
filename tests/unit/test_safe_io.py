from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from ash.safe_io import (
    atomic_write_unlinked_bytes,
    create_anchored_regular_file,
    create_unlinked_regular_file,
    ensure_anchored_directory,
    read_bounded_open_file,
    read_bounded_text,
    remove_anchored_directory_tree,
    replace_anchored_open_file,
    replace_open_file,
    strict_json_loads,
    validate_unlinked_directory_path,
    validate_unlinked_file_path,
    validate_unlinked_path,
    verify_open_file_identity,
)


def test_strict_json_loads_accepts_standard_json() -> None:
    assert strict_json_loads(b'{"name":"ash","enabled":true}') == {
        "name": "ash",
        "enabled": True,
    }


@pytest.mark.parametrize("payload", ['{"a":1,"a":2}', '{"a":NaN}'])
def test_strict_json_loads_rejects_ambiguous_or_nonstandard_json(payload: str) -> None:
    with pytest.raises(ValueError):
        strict_json_loads(payload)


def test_bounded_text_reads_text_streams_and_preserves_utf8() -> None:
    assert read_bounded_text(io.StringIO("café"), 5, label="prompt") == "café"


def test_bounded_text_rejects_text_streams_over_byte_limit() -> None:
    with pytest.raises(ValueError, match="prompt exceeds 4 bytes"):
        read_bounded_text(io.StringIO("café"), 4, label="prompt")


def test_bounded_text_reads_binary_buffer_streams() -> None:
    class TextWrapper:
        def __init__(self, data: bytes) -> None:
            self.buffer = io.BytesIO(data)

    assert read_bounded_text(TextWrapper(b"hello"), 5, label="request") == "hello"


def test_bounded_text_rejects_invalid_utf8_from_binary_stream() -> None:
    class TextWrapper:
        def __init__(self) -> None:
            self.buffer = io.BytesIO(b"\xff")

    with pytest.raises(ValueError, match="request is not valid UTF-8"):
        read_bounded_text(TextWrapper(), 10, label="request")


def test_validate_unlinked_path_rejects_link_component(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(ValueError, match="symlink or junction"):
        validate_unlinked_path(
            linked / "state.json",
            trusted_root=tmp_path,
            label="test state",
        )


def test_validate_unlinked_file_path_rejects_leaf_and_parent_links(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.db"
    target.touch()
    linked_file = tmp_path / "linked.db"
    linked_parent = tmp_path / "linked-parent"
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        linked_file.symlink_to(target)
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    for path in (linked_file, linked_parent / "state.db"):
        with pytest.raises(ValueError, match="symlink or junction"):
            validate_unlinked_file_path(path, label="test database")


def test_validate_unlinked_directory_path_rejects_directory_and_parent_links(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_directory = tmp_path / "linked-directory"
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_directory.symlink_to(outside, target_is_directory=True)
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    for path in (linked_directory, linked_parent / "state"):
        with pytest.raises(ValueError, match="symlink or junction"):
            validate_unlinked_directory_path(path, label="test state directory")


def test_created_file_identity_check_rejects_replaced_visible_entry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    with create_unlinked_regular_file(path, label="test state") as descriptor:
        path.unlink()
        path.write_bytes(b"attacker replacement")

        with pytest.raises(ValueError, match="changed after opening"):
            verify_open_file_identity(path, descriptor, label="test state")

    assert path.read_bytes() == b"attacker replacement"


def test_replace_open_file_refuses_source_replacement(tmp_path: Path) -> None:
    source = tmp_path / "restore.tmp"
    destination = tmp_path / "sessions.db"
    destination.write_bytes(b"current database")
    with create_unlinked_regular_file(source, label="restore temp") as descriptor:
        source.unlink()
        source.write_bytes(b"attacker replacement")

        with pytest.raises(ValueError, match="changed after opening"):
            replace_open_file(
                source,
                destination,
                descriptor,
                label="restore temp",
            )

    assert destination.read_bytes() == b"current database"
    assert source.read_bytes() == b"attacker replacement"


def test_atomic_write_replaces_existing_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")

    written = atomic_write_unlinked_bytes(
        target,
        b"new",
        label="test state",
    )

    assert written == target
    assert target.read_bytes() == b"new"
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_anchored_io_rejects_intermediate_symlink(tmp_path: Path) -> None:
    trusted_root = tmp_path / "home"
    trusted_root.mkdir()
    outside = tmp_path / "outside"
    nested_outside = outside / "profiles" / "work"
    nested_outside.mkdir(parents=True)
    victim = nested_outside / ".env"
    victim.write_bytes(b"DUMMY=attacker\n")
    try:
        (trusted_root / ".ash").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    target = trusted_root / ".ash" / "profiles" / "work" / ".env"

    with pytest.raises(ValueError, match="symlink or junction"):
        read_bounded_open_file(
            target,
            1024,
            label="profile dotenv",
            trusted_root=trusted_root,
        )
    with pytest.raises(ValueError, match="symlink or junction"):
        atomic_write_unlinked_bytes(
            target,
            b"DUMMY=secret\n",
            label="profile dotenv",
            trusted_root=trusted_root,
        )

    assert victim.read_bytes() == b"DUMMY=attacker\n"


def test_anchored_directory_creation_builds_nested_state_tree(tmp_path: Path) -> None:
    trusted_root = tmp_path / "home"
    trusted_root.mkdir()
    target = trusted_root / ".ash" / "profiles" / "work"

    created = ensure_anchored_directory(
        target,
        trusted_root=trusted_root,
        label="profile state",
        mode=0o700,
    )

    assert created == target
    assert target.is_dir()


def test_anchored_directory_removal_does_not_follow_nested_symlink(tmp_path: Path) -> None:
    trusted_root = tmp_path / "home"
    profile = trusted_root / ".ash" / "profiles" / "work"
    profile.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    try:
        (profile / "linked-outside").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    (profile / "local.txt").write_text("delete\n", encoding="utf-8")

    remove_anchored_directory_tree(
        profile,
        trusted_root=trusted_root,
        label="profile directory",
    )

    assert not profile.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_anchored_regular_file_creation_rejects_intermediate_symlink(
    tmp_path: Path,
) -> None:
    trusted_root = tmp_path / "home"
    trusted_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (trusted_root / ".ash").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    target = trusted_root / ".ash" / "restore.tmp"

    with pytest.raises(ValueError, match="symlink or junction"):
        with create_anchored_regular_file(
            target,
            trusted_root=trusted_root,
            label="restore temporary",
        ):
            pass

    assert not (outside / "restore.tmp").exists()


def test_anchored_replace_rejects_parent_swap_and_preserves_outside_destination(
    tmp_path: Path,
) -> None:
    trusted_root = tmp_path / "home"
    directory = trusted_root / "db"
    directory.mkdir(parents=True)
    source = directory / "restore.tmp"
    destination = directory / "sessions.db"
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "sessions.db"
    victim.write_bytes(b"DO NOT REPLACE\n")

    with pytest.raises(ValueError, match="symlink or junction"):
        with create_anchored_regular_file(
            source,
            trusted_root=trusted_root,
            label="restore temporary",
        ) as descriptor:
            os.write(descriptor, b"replacement")
            directory.rename(trusted_root / "db-real")
            try:
                directory.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
            replace_anchored_open_file(
                source,
                destination,
                descriptor,
                trusted_root=trusted_root,
                label="restore temporary",
            )

    assert victim.read_bytes() == b"DO NOT REPLACE\n"
