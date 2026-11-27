"""Fail-closed source-tree snapshots for isolated coding evaluations."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Callable

from ash.core.redaction import redact_text


UNSAFE_ENTRIES = frozenset({"SYMLINK", "SPECIAL", "UNREADABLE"})

# Version 1 disqualifies observed stale verification, without claiming that
# absence of an observed edit proves the final source was tested.
VERIFICATION_ORDER_VERSION = 1


def verification_freshness(observed_late_edit: bool | None) -> str:
    """Classify only what the trace proves, never infer source-hash freshness."""
    return "observed_stale" if observed_late_edit is True else "unknown"


def content_identity(fingerprint: str | None) -> str | None:
    """Ignore permission bits when proving that required source bytes changed."""

    if fingerprint is not None and fingerprint.startswith("FILE:"):
        return fingerprint.rsplit(":", 1)[-1]
    return fingerprint


def snapshot_workspace_files(
    root: Path, *, include_git: bool = False, include_directories: bool = False,
) -> dict[str, str]:
    """Capture regular-file content and mode, plus every non-directory hazard.

    File descriptors are opened without following links and checked again
    after open; nonblocking opens cannot hang on a FIFO substituted at read time.
    An unreadable or unsupported entry must not disappear from scoring.
    """

    entries: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if not include_git and relative.parts[0] == ".git":
            continue
        name = relative.as_posix()
        try:
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                if include_directories:
                    entries[name] = f"DIR:{stat.S_IMODE(mode):04o}"
                continue
            if stat.S_ISLNK(mode):
                entries[name] = "SYMLINK"
                continue
            if not stat.S_ISREG(mode):
                entries[name] = "SPECIAL"
                continue
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(path, flags), "rb") as source:
                opened = os.fstat(source.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    entries[name] = "SPECIAL"
                    continue
                digest = hashlib.sha256()
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            entries[name] = f"FILE:{stat.S_IMODE(opened.st_mode):04o}:{digest.hexdigest()}"
        except OSError:
            entries[name] = "UNREADABLE"
    return entries


# Retain only bounded text from the task's editable allowlist. The public
# upstream is trusted by the operator; candidate-written bytes are not.
_MAX_EDIT_BYTES = 1024 * 1024
_MAX_RETAINED_BYTES = 4 * 1024 * 1024
_MAX_EVIDENCE_BYTES = 16 * 1024 * 1024


def snapshot_digest(files: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _read_editable(root: Path, name: str, expected: str) -> bytes:
    """Read a bounded regular file without following any path component."""
    relative = Path(name)
    if (
        relative.is_absolute() or not relative.parts or ".." in relative.parts
        or relative.parts[0] == ".git"
    ):
        raise ValueError("unsafe editable path")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    directory = os.open(root, flags)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size > _MAX_EDIT_BYTES
            ):
                raise ValueError("unsupported editable file")
            data = stream.read(_MAX_EDIT_BYTES + 1)
            if len(data) > _MAX_EDIT_BYTES:
                raise ValueError("editable file exceeds limit")
            actual = f"FILE:{stat.S_IMODE(info.st_mode):04o}:{hashlib.sha256(data).hexdigest()}"
            if actual != expected:
                raise ValueError("editable file changed during capture")
            return data
    finally:
        os.close(directory)


def capture_editable_text(
    root: Path, files: dict[str, str], editable: frozenset[str],
) -> dict[str, dict[str, Any]]:
    """Keep hashes even when exact text cannot safely be retained."""
    result: dict[str, dict[str, Any]] = {}
    retained = 0
    for name in sorted(editable):
        fingerprint = files.get(name)
        item: dict[str, Any] = {"fingerprint": fingerprint, "status": "absent"}
        if fingerprint is not None:
            item["status"] = "unavailable"
            if fingerprint.startswith("FILE:"):
                try:
                    data = _read_editable(root, name, fingerprint)
                    text = data.decode("utf-8")
                    if "\x00" in text:
                        item["status"] = "binary"
                    elif redact_text(text) != text:
                        item["status"] = "sensitive"
                    elif retained + len(data) > _MAX_RETAINED_BYTES:
                        item["status"] = "limit"
                    else:
                        retained += len(data)
                        item.update(status="retained", text=text)
                except (OSError, ValueError):
                    pass
        result[name] = item
    return result


def retain_edit_evidence(
    evidence: Path, before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]], original: dict[str, str],
    updated: dict[str, str],
) -> dict[str, Any]:
    """Store exact allowed text pairs, including deletions/additions and mode changes.

    Never copy protected files, arbitrary paths, Git metadata or raw tool logs.
    Redaction is conservative and not a guarantee of detecting all secrets.
    """
    entries = [
        {"path": path, "before": dict(before[path]), "after": dict(after[path])}
        for path in sorted(before)
        if original.get(path) != updated.get(path)
    ]
    complete = all(
        side["status"] in ("retained", "absent")
        for entry in entries for side in (entry["before"], entry["after"])
    )
    payload = {
        "version": 1, "scope": "editable-paths-only",
        "original_sha256": snapshot_digest(original),
        "submitted_sha256": snapshot_digest(updated),
        "entries": entries, "complete": complete,
    }
    data = (json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n").encode()
    if len(data) > _MAX_EVIDENCE_BYTES:
        for entry in entries:
            for side in (entry["before"], entry["after"]):
                if "text" in side:
                    del side["text"]
                    side["status"] = "limit"
        complete = payload["complete"] = False
        data = (json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n").encode()
    if len(data) > _MAX_EVIDENCE_BYTES:
        raise ValueError("editable evidence manifest exceeds limit")
    if evidence.is_symlink() or not evidence.is_dir():
        raise RuntimeError("unsafe private evidence directory")
    info = evidence.stat()
    resolved = evidence.resolve()
    if (
        info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
        or any((p / ".git").exists() for p in (resolved, *resolved.parents))
    ):
        # Programmatic callers must also use a private evidence destination.
        return {"version": 1, "status": "private_directory_required"}
    descriptor = os.open(
        evidence / "editable-changes.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
    return {
        "version": 1, "status": "retained", "complete": complete,
        "sha256": hashlib.sha256(data).hexdigest(),
        "original_sha256": payload["original_sha256"],
        "submitted_sha256": payload["submitted_sha256"],
    }


def read_edit_evidence(evidence: Path, expected: dict[str, Any]) -> dict[str, Any]:
    """Validate a bounded private artifact against its recorded digest.

    Source text is used only to check fingerprints; callers must not echo it.
    """
    if evidence.is_symlink() or not evidence.is_dir():
        raise ValueError("unsafe private evidence directory")
    directory = evidence.stat()
    if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) & 0o077:
        raise ValueError("unsafe private evidence directory")
    descriptor = os.open(
        evidence / "editable-changes.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    )
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or info.st_size > _MAX_EVIDENCE_BYTES
        ):
            raise ValueError("unsafe editable evidence artifact")
        data = stream.read(_MAX_EVIDENCE_BYTES + 1)
    if len(data) > _MAX_EVIDENCE_BYTES or hashlib.sha256(data).hexdigest() != expected.get("sha256"):
        raise ValueError("editable evidence digest mismatch")
    payload = json.loads(data)
    if (
        not isinstance(payload, dict) or payload.get("version") != 1
        or payload.get("scope") != "editable-paths-only"
        or any(payload.get(k) != expected.get(k)
               for k in ("original_sha256", "submitted_sha256", "complete"))
        or not isinstance(payload.get("entries"), list)
        or type(payload.get("complete")) is not bool
    ):
        raise ValueError("invalid editable evidence manifest")
    paths = []
    complete = True
    for entry in payload["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("invalid editable evidence entry")
        paths.append(entry["path"])
        fingerprints = []
        for key in ("before", "after"):
            side = entry.get(key)
            if not isinstance(side, dict):
                raise ValueError("invalid editable evidence side")
            fp = side.get("fingerprint")
            status = side.get("status")
            fingerprints.append(fp)
            if status == "retained":
                text = side.get("text")
                if (
                    not isinstance(text, str) or not isinstance(fp, str)
                    or not fp.startswith("FILE:")
                    or content_identity(fp) != hashlib.sha256(text.encode()).hexdigest()
                    or len(text.encode()) > _MAX_EDIT_BYTES
                ):
                    raise ValueError("editable evidence content mismatch")
            elif status == "absent":
                if fp is not None or "text" in side:
                    raise ValueError("invalid absent editable evidence")
            elif status in ("unavailable", "binary", "sensitive", "limit"):
                if not isinstance(fp, str) or "text" in side:
                    raise ValueError("invalid omitted editable evidence")
                complete = False
            else:
                raise ValueError("invalid editable evidence status")
        if fingerprints[0] == fingerprints[1]:
            raise ValueError("unchanged editable evidence entry")
    if paths != sorted(set(paths)) or complete is not payload["complete"]:
        raise ValueError("inconsistent editable evidence")
    return {"paths": paths, "complete": complete}


def independent_grade_evidence(
    workspace: Path, submitted: dict[str, str], grade: Callable[[], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind sealed oracle results to observed pre/post source snapshots.

    This is not an attestation of the agent's own earlier test command, nor
    proof against a same-user host process modifying and restoring bytes
    during grading. The evaluator runs read-only oracles after agent exit.
    """
    before = snapshot_workspace_files(workspace)
    if before != submitted or UNSAFE_ENTRIES.intersection(before.values()):
        status = "changed_before_grading" if before != submitted else "unsafe_submission"
        return (
            {"correct": False, "visible_oracle_exit": None, "hidden_oracle_exit": None},
            {"version": 1, "scope": "independent-oracles", "status": status,
             "submitted_sha256": snapshot_digest(submitted),
             "before_sha256": snapshot_digest(before)},
        )
    result = grade()
    after = snapshot_workspace_files(workspace)
    stable = before == after
    proof = {
        "version": 1, "scope": "independent-oracles",
        "status": "stable_snapshots" if stable else "changed_during_grading",
        "submitted_sha256": snapshot_digest(submitted),
        "before_sha256": snapshot_digest(before),
        "after_sha256": snapshot_digest(after),
    }
    if not stable:
        result = {**result, "correct": False}
    return result, proof
