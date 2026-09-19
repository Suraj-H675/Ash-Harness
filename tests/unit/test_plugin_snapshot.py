from __future__ import annotations

import io
import stat
import tarfile
from pathlib import Path

import pytest

from ash.plugins.anchored_fs import AnchoredDirectory, supports_anchored_mutation
from ash.plugins.snapshot import PluginSnapshot


def test_snapshot_tar_stream_preserves_bytes_modes_and_requested_owner(
    tmp_path: Path,
) -> None:
    if not supports_anchored_mutation():
        pytest.skip("descriptor-anchored plugin snapshots are unavailable")
    root = tmp_path / "plugin"
    nested = root / "nested"
    nested.mkdir(parents=True)
    script = root / "runtime.py"
    script.write_bytes(b"print('hello')\n")
    script.chmod(0o750)
    payload = nested / "payload.bin"
    payload.write_bytes(b"\x00payload\xff")
    payload.chmod(0o640)

    with AnchoredDirectory.open(root, create=False, private=False) as source:
        snapshot = PluginSnapshot.capture(
            source,
            max_files=10,
            max_bytes=1024,
            max_entries=20,
            max_depth=5,
        )
    try:
        archive_bytes = io.BytesIO()
        snapshot.write_tar(archive_bytes, uid=123, gid=456)
        archive_bytes.seek(0)
        with tarfile.open(fileobj=archive_bytes, mode="r:*") as archive:
            members = {member.name: member for member in archive.getmembers()}
            assert set(members) == {
                "workspace",
                "workspace/nested",
                "workspace/nested/payload.bin",
                "workspace/runtime.py",
            }
            runtime = members["workspace/runtime.py"]
            assert stat.S_IMODE(runtime.mode) == 0o750
            assert (runtime.uid, runtime.gid) == (123, 456)
            runtime_file = archive.extractfile(runtime)
            assert runtime_file is not None
            assert runtime_file.read() == b"print('hello')\n"
            archived_payload = members["workspace/nested/payload.bin"]
            assert stat.S_IMODE(archived_payload.mode) == 0o640
            payload_file = archive.extractfile(archived_payload)
            assert payload_file is not None
            assert payload_file.read() == b"\x00payload\xff"
    finally:
        snapshot.close()


def test_snapshot_tar_stream_rejects_unsafe_root_name(tmp_path: Path) -> None:
    if not supports_anchored_mutation():
        pytest.skip("descriptor-anchored plugin snapshots are unavailable")
    root = tmp_path / "plugin"
    root.mkdir()
    with AnchoredDirectory.open(root, create=False, private=False) as source:
        snapshot = PluginSnapshot.capture(
            source,
            max_files=1,
            max_bytes=1,
            max_entries=2,
            max_depth=1,
        )
    try:
        with pytest.raises(ValueError, match="root_name"):
            snapshot.write_tar(io.BytesIO(), root_name="../workspace")
        with pytest.raises(ValueError, match="non-negative"):
            snapshot.write_tar(io.BytesIO(), uid=-1)
    finally:
        snapshot.close()
