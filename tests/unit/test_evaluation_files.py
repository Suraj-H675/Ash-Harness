"""Private source evidence must never be confused with model test freshness."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

_devtools = Path(__file__).resolve().parents[2] / "devtools"
if str(_devtools) not in sys.path:
    sys.path.insert(0, str(_devtools))

import evaluation_files as files  # noqa: E402


def test_edit_evidence_exact_changed_bytes_only(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    (root / "a.py").write_bytes(b"old\r\n")
    (root / "deleted.py").write_text("gone")
    (root / "protected.py").write_text("PROTECTED CONTENT")
    editable = frozenset({"a.py", "deleted.py", "added.py"})
    original = files.snapshot_workspace_files(root)
    before = files.capture_editable_text(root, original, editable)
    (root / "a.py").write_bytes("new \u03bb\r\n".encode())
    (root / "a.py").chmod(0o755)
    (root / "deleted.py").unlink()
    (root / "added.py").write_text("added")
    (root / "protected.py").write_text("tampered PROTECTED CONTENT")
    submitted = files.snapshot_workspace_files(root)
    after = files.capture_editable_text(root, submitted, editable)
    receipt = files.retain_edit_evidence(evidence, before, after, original, submitted)
    artifact = evidence / "editable-changes.json"
    assert artifact.stat().st_mode & 0o777 == 0o600
    raw = artifact.read_bytes()
    assert b"PROTECTED CONTENT" not in raw
    assert receipt["sha256"] == hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw)
    by_path = {e["path"]: e for e in payload["entries"]}
    assert by_path["a.py"]["before"]["text"].encode() == b"old\r\n"
    assert by_path["a.py"]["after"]["text"].encode() == "new \u03bb\r\n".encode()
    assert by_path["a.py"]["after"]["fingerprint"].startswith("FILE:0755:")
    assert by_path["added.py"]["before"]["status"] == "absent"
    assert by_path["deleted.py"]["after"]["status"] == "absent"
    assert files.read_edit_evidence(evidence, receipt) == {
        "paths": ["a.py", "added.py", "deleted.py"], "complete": True,
    }
    artifact.write_bytes(raw + b" ")
    with pytest.raises(ValueError, match="digest"):
        files.read_edit_evidence(evidence, receipt)


@pytest.mark.parametrize("fault", ["secret", "large", "binary", "link", "parent-link", "hardlink",
                                    "stale", "fifo"])
def test_capture_refuses_unsafe_or_sensitive_text(tmp_path, fault):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "sub").mkdir()
    target = root / "sub" / "code.py"
    target.write_text("ordinary")
    external = tmp_path / "external"
    external.write_text("SYNTHETIC_EXTERNAL_CONTENT")
    if fault == "secret":
        target.write_text("token=supersecretvalue")
    elif fault == "large":
        target.write_bytes(b"x" * (files._MAX_EDIT_BYTES + 1))
    elif fault == "binary":
        target.write_bytes(b"abc\x00def")
    original = files.snapshot_workspace_files(root)
    if fault == "link":
        target.unlink()
        target.symlink_to(external)
    elif fault == "parent-link":
        target.unlink()
        target.parent.rmdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "code.py").write_text("ordinary")
        target.parent.symlink_to(outside, target_is_directory=True)
    elif fault == "hardlink":
        target.unlink()
        os.link(external, target)
        original = files.snapshot_workspace_files(root)
    elif fault == "stale":
        target.write_text("changed")
    elif fault == "fifo":
        target.unlink()
        os.mkfifo(target)
    result = files.capture_editable_text(root, original, frozenset({"sub/code.py"}))
    item = result["sub/code.py"]
    assert "text" not in item
    assert item["status"] not in ("retained", "absent")


def test_evidence_requires_private_destination_and_refuses_overwrite(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_text("original")
    original = files.snapshot_workspace_files(root)
    before = files.capture_editable_text(root, original, frozenset({"a.py"}))
    (root / "a.py").write_text("changed")
    final = files.snapshot_workspace_files(root)
    after = files.capture_editable_text(root, final, frozenset({"a.py"}))
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o755)
    receipt = files.retain_edit_evidence(evidence, before, after, original, final)
    assert receipt["status"] == "private_directory_required"
    assert not list(evidence.iterdir())
    evidence.chmod(0o700)
    files.retain_edit_evidence(evidence, before, after, original, final)
    with pytest.raises(FileExistsError):
        files.retain_edit_evidence(evidence, before, after, original, final)


@pytest.mark.parametrize("fault", [None, "before", "during", "unsafe"])
def test_independent_grade_evidence_tracks_submitted_snapshot(tmp_path, fault):
    target = tmp_path / "code.py"
    target.write_text("submission")
    submitted = files.snapshot_workspace_files(tmp_path)
    called = []
    if fault == "before":
        target.write_text("different")
    elif fault == "unsafe":
        target.unlink()
        target.symlink_to("missing")
        submitted = files.snapshot_workspace_files(tmp_path)

    def grade():
        called.append(True)
        if fault == "during":
            target.write_text("different")
        return {"correct": True, "visible_oracle_exit": 0, "hidden_oracle_exit": 0}

    outcome, proof = files.independent_grade_evidence(tmp_path, submitted, grade)
    assert proof["submitted_sha256"] == files.snapshot_digest(submitted)
    assert outcome["correct"] is (fault is None)
    assert called == ([] if fault in ("before", "unsafe") else [True])
    if fault is None:
        assert proof["status"] == "stable_snapshots"
        assert proof["before_sha256"] == proof["after_sha256"] == proof["submitted_sha256"]
    elif fault == "during":
        assert proof["status"] == "changed_during_grading"
        assert proof["after_sha256"] != proof["submitted_sha256"]
    else:
        assert proof["status"] == (
            "changed_before_grading" if fault == "before" else "unsafe_submission"
        )
