from __future__ import annotations

import sqlite3
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ash.commands.storage import (
    backup_database,
    check_database,
    render_storage_check,
    restore_database,
)
from ash.core.session import SessionStorageError, SessionStore
from ash.cli import main


def test_storage_check_does_not_create_missing_database(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    check = check_database(path)
    assert check.exists is False
    assert check.ok is False
    assert not path.exists()
    assert '"ok": false' in render_storage_check(check, json_output=True)


def test_storage_human_output_sanitizes_database_diagnostics() -> None:
    from ash.commands.storage import StorageCheck

    check = StorageCheck(
        path="/tmp/db\nname\u202ehidden\u202c",
        exists=True,
        ok=False,
        schema_version=1,
        messages=("foreign key violation: bad\x1b[2J",),
    )

    rendered = render_storage_check(check)
    machine = json.loads(render_storage_check(check, json_output=True))

    assert "/tmp/db\\x0aname\\u202ehidden\\u202c" in rendered
    assert "foreign key violation: bad\\x1b[2J" in rendered
    assert "\x1b[2J" not in rendered
    assert "\u202e" not in rendered
    assert machine["path"] == "/tmp/db\nname\u202ehidden\u202c"


def test_storage_cli_honors_database_directory_override(tmp_path: Path, capsys) -> None:
    assert (
        main(
            [
                "--db-directory",
                str(tmp_path),
                "storage",
                "check",
                "--json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == str(tmp_path / "sessions.db")
    assert not (tmp_path / "sessions.db").exists()


def test_storage_check_detects_corrupt_database(tmp_path: Path) -> None:
    path = tmp_path / "broken.db"
    path.write_bytes(b"not sqlite")
    check = check_database(path)
    assert check.exists is True
    assert check.ok is False
    assert check.messages


def test_backup_and_restore_preserve_current_database(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    store = SessionStore(path)
    original = store.create_session("/original")
    backup = backup_database(path, tmp_path / "known-good.db")
    replacement = store.create_session("/replacement")

    restored, preserved = restore_database(path, backup, confirmed=True)

    assert restored == path
    sessions = SessionStore(path).list_sessions(limit=10)
    assert {item.session_id for item in sessions} == {original.session_id}
    assert replacement.session_id not in {item.session_id for item in sessions}
    assert len(preserved) >= 1
    assert all(item.exists() for item in preserved)


def test_restore_cleans_temporary_sqlite_sidecars(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    store = SessionStore(path)
    store.create_session("/original")
    backup = backup_database(path, tmp_path / "known-good.db")

    restore_database(path, backup, confirmed=True)

    assert not list(tmp_path.glob(".sessions.db.restore-*.tmp*"))


def test_restore_uses_validated_backup_bytes_when_backup_path_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sessions.db"
    current_store = SessionStore(path)
    current_store.create_session("/current")
    backup = tmp_path / "known-good.db"
    good_store = SessionStore(backup)
    good_session = good_store.create_session("/good")
    replacement = tmp_path / "replacement.db"
    replacement_store = SessionStore(replacement)
    replacement_session = replacement_store.create_session("/replacement")
    real_copy2 = shutil.copy2
    swapped = False

    def swap_backup_before_copy(source, destination, *args, **kwargs):
        nonlocal swapped
        if Path(source) == backup and not swapped:
            swapped = True
            backup.unlink()
            try:
                backup.symlink_to(replacement)
            except OSError as exc:
                pytest.skip(f"symlinks are unavailable: {exc}")
        return real_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", swap_backup_before_copy)

    restore_database(path, backup, confirmed=True)

    assert swapped is False
    restored_ids = {
        item.session_id for item in SessionStore(path).list_sessions(limit=10)
    }
    assert good_session.session_id in restored_ids
    assert replacement_session.session_id not in restored_ids


def test_restore_does_not_follow_pre_restore_snapshot_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.commands.storage as storage_module

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 2, 3, 4, 5, 678901, tzinfo=tz or timezone.utc)

    path = tmp_path / "sessions.db"
    SessionStore(path).create_session("/current")
    backup = backup_database(path, tmp_path / "known-good.db")
    victim = tmp_path / "victim.txt"
    victim.write_text("do-not-touch\n", encoding="utf-8")
    timestamp = "20260102T030405678901Z"
    preserved_path = tmp_path / f"sessions.db.pre-restore.{timestamp}.raw"
    try:
        preserved_path.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    monkeypatch.setattr(storage_module, "datetime", FixedDateTime)

    with pytest.raises(SessionStorageError, match="symlink or junction"):
        restore_database(path, backup, confirmed=True)

    assert victim.read_text(encoding="utf-8") == "do-not-touch\n"


def test_restore_rejects_database_directory_swapped_before_sidecar_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.commands.storage as storage_module

    db_dir = tmp_path / "db"
    db_dir.mkdir()
    database = db_dir / "sessions.db"
    SessionStore(database).create_session("/current")
    backup = tmp_path / "backup.db"
    SessionStore(backup).create_session("/backup")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_wal = outside / "sessions.db-wal"
    outside_shm = outside / "sessions.db-shm"
    outside_wal.write_bytes(b"DO NOT DELETE WAL\n")
    outside_shm.write_bytes(b"DO NOT DELETE SHM\n")
    real_open = storage_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == db_dir and not swapped:
            swapped = True
            db_dir.rename(tmp_path / "db-real")
            try:
                db_dir.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                directory.close()
                pytest.skip(f"symlink creation is unavailable: {exc}")
        return directory

    monkeypatch.setattr(
        storage_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises(SessionStorageError, match="no longer identifies the held directory"):
        restore_database(database, backup, confirmed=True)

    assert swapped is True
    assert outside_wal.read_bytes() == b"DO NOT DELETE WAL\n"
    assert outside_shm.read_bytes() == b"DO NOT DELETE SHM\n"
    assert (tmp_path / "db-real" / "sessions.db").exists()


def test_restore_rejects_database_replaced_after_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.commands.storage as storage_module

    db_dir = tmp_path / "db"
    db_dir.mkdir()
    database = db_dir / "sessions.db"
    SessionStore(database).create_session("/current")
    backup = tmp_path / "backup.db"
    SessionStore(backup).create_session("/backup")
    replacement = b"UNRELATED REPLACEMENT\n"
    real_validation_path = storage_module.AnchoredDirectory.validation_path
    validation_calls = 0
    replaced = False

    def validate_then_replace(directory):
        nonlocal validation_calls, replaced
        validation_calls += 1
        if validation_calls == 3 and not replaced:
            replaced = True
            database.unlink()
            database.write_bytes(replacement)
        return real_validation_path(directory)

    monkeypatch.setattr(
        storage_module.AnchoredDirectory,
        "validation_path",
        validate_then_replace,
    )

    with pytest.raises(SessionStorageError, match="changed before restore publication"):
        restore_database(database, backup, confirmed=True)

    assert replaced is True
    assert database.read_bytes() == replacement


def test_restore_rejects_sidecar_created_after_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.commands.storage as storage_module

    db_dir = tmp_path / "db"
    db_dir.mkdir()
    database = db_dir / "sessions.db"
    SessionStore(database).create_session("/current")
    backup = tmp_path / "backup.db"
    SessionStore(backup).create_session("/backup")
    wal = db_dir / "sessions.db-wal"
    sentinel = b"NEW CONCURRENT WAL\n"
    real_validation_path = storage_module.AnchoredDirectory.validation_path
    validation_calls = 0
    created = False

    def validate_then_create(directory):
        nonlocal validation_calls, created
        validation_calls += 1
        if validation_calls == 3 and not created:
            created = True
            wal.write_bytes(sentinel)
        return real_validation_path(directory)

    monkeypatch.setattr(
        storage_module.AnchoredDirectory,
        "validation_path",
        validate_then_create,
    )

    with pytest.raises(SessionStorageError, match="appeared after snapshot"):
        restore_database(database, backup, confirmed=True)

    assert created is True
    assert wal.read_bytes() == sentinel


def test_restore_quiesces_concurrent_session_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.commands.storage as storage_module

    database = tmp_path / "sessions.db"
    store = SessionStore(database)
    original = store.create_session("/original")
    backup = backup_database(database, tmp_path / "known-good.db")
    store.create_session("/after-backup")

    replace_entered = threading.Event()
    writer_done = threading.Event()
    writer_started = threading.Event()
    writer_session_id: list[str] = []
    writer_errors: list[BaseException] = []
    completed_before_replace: list[bool] = []
    real_rename = storage_module.AnchoredDirectory.rename

    def writer() -> None:
        assert replace_entered.wait(5)
        writer_started.set()
        try:
            session = store.create_session("/during-restore")
            writer_session_id.append(session.session_id)
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            writer_done.set()

    def observe_before_replace(directory, source, destination, **kwargs):
        if destination == database.name:
            replace_entered.set()
            assert writer_started.wait(5)
            completed_before_replace.append(writer_done.wait(1))
        return real_rename(directory, source, destination, **kwargs)

    monkeypatch.setattr(
        storage_module.AnchoredDirectory,
        "rename",
        observe_before_replace,
    )
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        restore_database(database, backup, confirmed=True)
        thread.join(5)
    finally:
        replace_entered.set()
        thread.join(5)

    assert not thread.is_alive()
    assert completed_before_replace == [False]
    assert writer_errors == []
    assert len(writer_session_id) == 1
    session_ids = {
        item.session_id for item in SessionStore(database).list_sessions(limit=10)
    }
    assert original.session_id in session_ids
    assert writer_session_id[0] in session_ids


@pytest.mark.skipif(os.name != "posix", reason="cross-process flock is POSIX-only")
def test_restore_quiesces_cross_process_session_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.commands.storage as storage_module

    database = tmp_path / "sessions.db"
    store = SessionStore(database)
    original = store.create_session("/original")
    backup = backup_database(database, tmp_path / "known-good.db")
    store.create_session("/after-backup")
    started = tmp_path / "writer-started"
    result = tmp_path / "writer-result"
    child: subprocess.Popen[str] | None = None
    completed_before_replace: list[bool] = []
    real_rename = storage_module.AnchoredDirectory.rename

    child_code = """
from pathlib import Path
import sys
from ash.core.session import SessionStore

database = Path(sys.argv[1])
started = Path(sys.argv[2])
result = Path(sys.argv[3])
started.write_text("started", encoding="utf-8")
try:
    session = SessionStore(database).create_session("/cross-process-during-restore")
except BaseException as exc:
    result.write_text("error:" + repr(exc), encoding="utf-8")
    raise
else:
    result.write_text("ok:" + session.session_id, encoding="utf-8")
"""

    def observe_before_replace(directory, source, destination, **kwargs):
        nonlocal child
        if destination != database.name:
            return real_rename(directory, source, destination, **kwargs)
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                child_code,
                str(database),
                str(started),
                str(result),
            ],
            text=True,
        )
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        time.sleep(0.25)
        completed_before_replace.append(result.exists())
        return real_rename(directory, source, destination, **kwargs)

    monkeypatch.setattr(
        storage_module.AnchoredDirectory,
        "rename",
        observe_before_replace,
    )
    try:
        restore_database(database, backup, confirmed=True)
        assert child is not None
        assert child.wait(timeout=5) == 0
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    assert completed_before_replace == [False]
    payload = result.read_text(encoding="utf-8")
    assert payload.startswith("ok:")
    child_session_id = payload.removeprefix("ok:")
    session_ids = {
        item.session_id for item in SessionStore(database).list_sessions(limit=10)
    }
    assert original.session_id in session_ids
    assert child_session_id in session_ids


def test_backup_rejects_symlinked_destination(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    SessionStore(path).create_session("/workspace")
    victim = tmp_path / "victim.db"
    linked = tmp_path / "backup.db"
    try:
        linked.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(SessionStorageError, match="symlink or junction"):
        backup_database(path, linked)

    assert not victim.exists()


def test_restore_refuses_unconfirmed_or_invalid_backup(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    SessionStore(path)
    backup = tmp_path / "broken.db"
    backup.write_bytes(b"broken")
    with pytest.raises(SessionStorageError, match="confirmation"):
        restore_database(path, backup, confirmed=False)
    with pytest.raises(SessionStorageError, match="unhealthy backup"):
        restore_database(path, backup, confirmed=True)


def test_restore_rejects_linked_database_destination(tmp_path: Path) -> None:
    target = tmp_path / "target.db"
    target_store = SessionStore(target)
    current = target_store.create_session("/current")
    backup = tmp_path / "backup.db"
    backup_store = SessionStore(backup)
    replacement = backup_store.create_session("/backup")
    linked = tmp_path / "sessions.db"
    try:
        linked.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(SessionStorageError, match="symlink or junction"):
        restore_database(linked, backup, confirmed=True)

    session_ids = {
        item.session_id for item in SessionStore(target).list_sessions(limit=10)
    }
    assert current.session_id in session_ids
    assert replacement.session_id not in session_ids


def test_debug_bundle_is_bounded_json_and_restricted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.commands.storage import create_debug_bundle
    from ash.config import AshConfig

    workspace = tmp_path / "repo"
    workspace.mkdir()
    db_dir = tmp_path / "db"
    config = AshConfig(
        model="anthropic/claude-sonnet-4-6",
        workspace_root=workspace,
        db_directory=db_dir,
        memory_backend="off",
    )
    monkeypatch.chdir(workspace)
    destination = tmp_path / "bundle.json"

    created = create_debug_bundle(config, destination)

    payload = json.loads(created.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["ash"]["model"] == "anthropic/claude-sonnet-4-6"
    assert payload["storage"]["path"] == str(db_dir / "sessions.db")
    assert payload["runtime"]["workspace"] == str(workspace.resolve())
    assert oct(created.stat().st_mode & 0o777) in {"0o600", "0o644"}


def test_debug_bundle_rejects_symlinked_destination(tmp_path: Path) -> None:
    from ash.commands.storage import create_debug_bundle
    from ash.config import AshConfig

    workspace = tmp_path / "repo"
    workspace.mkdir()
    config = AshConfig(
        model="anthropic/claude-sonnet-4-6",
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    victim = tmp_path / "victim.json"
    victim.write_text("keep", encoding="utf-8")
    linked = tmp_path / "bundle.json"
    try:
        linked.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(SessionStorageError, match="symlink or junction"):
        create_debug_bundle(config, linked)

    assert victim.read_text(encoding="utf-8") == "keep"


def test_debug_bundle_rejects_plain_parent_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.commands.storage as storage_module
    from ash.commands.storage import create_debug_bundle
    from ash.config import AshConfig

    workspace = tmp_path / "repo"
    target_directory = tmp_path / "target"
    saved_directory = tmp_path / "target-original"
    replacement_directory = tmp_path / "replacement"
    workspace.mkdir()
    target_directory.mkdir()
    replacement_directory.mkdir()
    victim = replacement_directory / "bundle.json"
    victim.write_text("DO NOT REPLACE\n", encoding="utf-8")
    config = AshConfig(
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    real_open = storage_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == target_directory and not swapped:
            swapped = True
            target_directory.rename(saved_directory)
            replacement_directory.rename(target_directory)
        return directory

    monkeypatch.setattr(
        storage_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises(SessionStorageError):
        create_debug_bundle(config, target_directory / "bundle.json")

    assert swapped is True
    assert (target_directory / "bundle.json").read_text(encoding="utf-8") == (
        "DO NOT REPLACE\n"
    )
    assert not (saved_directory / "bundle.json").exists()


def test_restore_database_rejects_plain_parent_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.commands.storage as storage_module
    from ash.commands.storage import restore_database

    database_directory = tmp_path / "db"
    saved_directory = tmp_path / "db-original"
    replacement_directory = tmp_path / "replacement"
    backup_directory = tmp_path / "backup"
    database_directory.mkdir()
    replacement_directory.mkdir()
    backup_directory.mkdir()

    original_store = SessionStore(database_directory / "sessions.db")
    original = original_store.create_session(str(tmp_path / "original-project"))
    backup = original_store.backup(backup_directory / "sessions.backup.db")

    replacement_store = SessionStore(replacement_directory / "sessions.db")
    victim = replacement_store.create_session(str(tmp_path / "victim-project"))

    real_open = storage_module.AnchoredDirectory.open
    swapped = False

    def open_then_swap(path, **kwargs):
        nonlocal swapped
        directory = real_open(path, **kwargs)
        if Path(path) == database_directory and not swapped:
            swapped = True
            database_directory.rename(saved_directory)
            replacement_directory.rename(database_directory)
        return directory

    monkeypatch.setattr(
        storage_module.AnchoredDirectory,
        "open",
        staticmethod(open_then_swap),
    )

    with pytest.raises(SessionStorageError):
        restore_database(database_directory / "sessions.db", backup, confirmed=True)

    assert swapped is True
    visible_ids = {
        item.session_id
        for item in SessionStore(database_directory / "sessions.db").list_sessions(
            limit=20
        )
    }
    original_ids = {
        item.session_id
        for item in SessionStore(saved_directory / "sessions.db").list_sessions(limit=20)
    }
    assert victim.session_id in visible_ids
    assert original.session_id in original_ids


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative flock is POSIX-only")
def test_restore_coordination_lock_is_anchored_across_parent_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.core.session as session_module

    database_directory = tmp_path / "db"
    saved_directory = tmp_path / "db-original"
    replacement_directory = tmp_path / "replacement"
    database_directory.mkdir()
    replacement_directory.mkdir()
    database = database_directory / "sessions.db"
    SessionStore(database).create_session("/current")
    backup = backup_database(database, tmp_path / "known-good.db")
    SessionStore(replacement_directory / "sessions.db").create_session("/replacement")

    original_lock = database_directory / ".sessions.db.ash-lock"
    replacement_lock = replacement_directory / ".sessions.db.ash-lock"
    original_identity = (original_lock.stat().st_dev, original_lock.stat().st_ino)
    replacement_identity = (
        replacement_lock.stat().st_dev,
        replacement_lock.stat().st_ino,
    )
    assert original_identity != replacement_identity

    real_open = session_module._open_database_coordination_file
    observed: list[tuple[int, int]] = []
    swapped = False

    def open_during_aba(db_path: str, *, parent_descriptor: int | None = None):
        nonlocal swapped
        if parent_descriptor is None or swapped:
            return real_open(db_path, parent_descriptor=parent_descriptor)
        swapped = True
        database_directory.rename(saved_directory)
        replacement_directory.rename(database_directory)
        try:
            descriptor = real_open(
                db_path,
                parent_descriptor=parent_descriptor,
            )
            assert descriptor is not None
            metadata = os.fstat(descriptor)
            observed.append((metadata.st_dev, metadata.st_ino))
            return descriptor
        finally:
            database_directory.rename(replacement_directory)
            saved_directory.rename(database_directory)

    monkeypatch.setattr(
        session_module,
        "_open_database_coordination_file",
        open_during_aba,
    )

    restore_database(database, backup, confirmed=True)

    assert swapped is True
    assert observed == [original_identity]


def test_debug_bundle_refuses_workspace_swap_for_git_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.commands.storage as storage_module
    from ash.commands.storage import create_debug_bundle
    from ash.config import AshConfig

    workspace = tmp_path / "workspace"
    replacement = tmp_path / "replacement"
    workspace.mkdir()
    replacement.mkdir()

    def initialize_repo(path: Path, marker: str) -> str:
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(
            ["git", "config", "user.email", "ash@example.test"],
            cwd=path,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Ash Test"],
            cwd=path,
            check=True,
        )
        (path / "marker.txt").write_text(marker, encoding="utf-8")
        subprocess.run(["git", "add", "marker.txt"], cwd=path, check=True)
        subprocess.run(
            ["git", "commit", "-qm", marker], cwd=path, check=True
        )
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    original_revision = initialize_repo(workspace, "original")
    replacement_revision = initialize_repo(replacement, "replacement")
    config = AshConfig(
        workspace_root=workspace,
        db_directory=tmp_path / "db",
        memory_backend="off",
    )
    configured_workspace = str(config.workspace_root)
    real_resolve = storage_module.resolve_host_executable
    swapped = False

    def resolve_and_swap(name: str, *, workspace_root: Path, cwd: Path):
        nonlocal swapped
        resolved = real_resolve(name, workspace_root=workspace_root, cwd=cwd)
        if not swapped:
            swapped = True
            workspace.rename(tmp_path / "moved-original")
            workspace.symlink_to(replacement, target_is_directory=True)
        return resolved

    monkeypatch.setattr(
        storage_module,
        "resolve_host_executable",
        resolve_and_swap,
    )

    created = create_debug_bundle(config, tmp_path / "bundle.json")
    payload = json.loads(created.read_text(encoding="utf-8"))

    assert original_revision != replacement_revision
    assert payload["runtime"]["workspace"] == configured_workspace
    assert payload["runtime"]["git_revision"] == ""


def test_metrics_cli_reports_local_only_aggregate(
    tmp_path: Path,
    capsys,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.save_session_token_stats(
        session.session_id,
        10,
        5,
        0.01,
        cache_read_tokens=2,
        cache_write_tokens=1,
        estimated_prompt_tokens=3,
        estimated_completion_tokens=2,
        estimated_cost_usd=0.002,
    )

    assert main(["--db-directory", str(tmp_path), "metrics"]) == 0
    output = capsys.readouterr().out
    assert "15 tokens" in output
    assert "$0.010000" in output

    assert main(["--db-directory", str(tmp_path), "metrics", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["telemetry"] == "local_only"
    assert payload["metrics"]["session_count"] == 1
    assert payload["metrics"]["total_tokens"] == 15
    assert payload["metrics"]["cost_usd"] == pytest.approx(0.01)


@pytest.mark.parametrize(
    "command",
    [
        ("metrics",),
        ("sessions",),
        ("plans", "list"),
        ("audit", "list", "--session", "missing"),
    ],
    ids=["metrics", "sessions", "plans", "audit"],
)
def test_session_cli_reports_corrupt_database_without_traceback(
    tmp_path: Path,
    capsys,
    command: tuple[str, ...],
) -> None:
    (tmp_path / "sessions.db").write_bytes(b"not sqlite")

    assert main(["--db-directory", str(tmp_path), *command]) == 1
    human = capsys.readouterr()
    assert human.out == ""
    assert "Error [storage]:" in human.err
    assert "Run `ash storage check`" in human.err
    assert "Traceback" not in human.err

    assert main(["--db-directory", str(tmp_path), *command, "--json"]) == 1
    structured = capsys.readouterr()
    assert structured.err == ""
    payload = json.loads(structured.out)
    assert payload["error"]["category"] == "storage"
    assert payload["error"]["exit_code"] == 1
    assert "file is not a database" in payload["error"]["message"]


def test_storage_cli_classifies_malformed_user_config_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    ash_dir = tmp_path / ".ash"
    ash_dir.mkdir()
    (ash_dir / "ash.toml").write_text("model = [", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert main(["storage", "check", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["error"]["category"] == "config"
    assert "Traceback" not in captured.out


def test_storage_cli_redacts_secret_like_config_validation_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    secret = "sk-proj-" + "a" * 24
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ASH_MAX_CONTEXT_TOKENS", secret)

    assert main(["storage", "check", "--json"]) == 2
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    payload = json.loads(captured.out)
    assert payload["error"]["category"] == "config"
    assert "[REDACTED]" in payload["error"]["message"]


def test_metrics_cli_classifies_store_operation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    database = tmp_path / "sessions.db"
    SessionStore(database)

    def fail_metrics(self: SessionStore) -> dict:
        raise SessionStorageError("database read failed")

    monkeypatch.setattr(SessionStore, "local_metrics_summary", fail_metrics)

    assert main(["--db-directory", str(tmp_path), "metrics", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "error": {
            "category": "storage",
            "exit_code": 1,
            "message": "database read failed",
            "remedy": (
                "Run `ash storage check`; if needed, create a backup and restore "
                "a known-good sessions database."
            ),
            "retriable": False,
        }
    }


def test_sessions_cli_classifies_corrupt_persisted_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    monkeypatch.chdir(tmp_path)
    database = tmp_path / "sessions.db"
    session = SessionStore(database).create_session(str(tmp_path))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE sessions SET created_at = 'not-a-timestamp' "
            "WHERE session_id = ?",
            (session.session_id,),
        )

    assert main(["--db-directory", str(tmp_path), "sessions", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["error"]["category"] == "storage"
    assert "stored data is invalid" in payload["error"]["message"]


def test_plans_cli_classifies_corrupt_persisted_contract(
    tmp_path: Path,
    capsys,
) -> None:
    from ash.core.sprint import ChecklistItem, SprintContract, SprintExecution

    database = tmp_path / "sessions.db"
    store = SessionStore(database)
    session = store.create_session(str(tmp_path))
    execution = SprintExecution(contract=SprintContract(goal="corruptible"))
    execution.set_items([ChecklistItem(idx=1, section="test", description="item")])
    store.save_sprint(session.session_id, execution)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE sprints SET contract_json = 'not-json' WHERE sprint_id = ?",
            (execution.contract.contract_id,),
        )

    assert (
        main(
            [
                "--db-directory",
                str(tmp_path),
                "plans",
                "show",
                execution.contract.contract_id,
                "--json",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["error"]["category"] == "storage"
    assert "stored data is invalid" in payload["error"]["message"]


def test_session_cli_classifies_structurally_incomplete_sqlite(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES (11, 'now');
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                project_path TEXT NOT NULL,
                title TEXT,
                created_at TEXT,
                updated_at TEXT,
                model TEXT
            );
            """
        )

    assert main(["--db-directory", str(tmp_path), "sessions", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["error"]["category"] == "storage"


def test_session_cli_classifies_malformed_schema_metadata(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES ('bad', 'now');
            """
        )

    assert main(["--db-directory", str(tmp_path), "metrics", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["error"]["category"] == "storage"


def test_startup_storage_error_uses_headless_event_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    database = tmp_path / "sessions.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES ('bad', 'now');
            """
        )
    monkeypatch.setenv("ASH_MODEL", "ollama/test-model")
    monkeypatch.setenv("ASH_WORKSPACE_ROOT", str(tmp_path))

    assert (
        main(
            [
                "--db-directory",
                str(tmp_path),
                "--prompt",
                "hello",
                "--output-format",
                "stream-json",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["type"] == "error"
    assert payload["schema_version"] == 1
    assert payload["event_id"]
    assert payload["timestamp"]
    assert payload["source"] == {"type": "runtime", "id": "ash"}
    assert payload["error"]["category"] == "storage"


def test_storage_check_reports_newer_schema_as_unsupported(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL
            );
            INSERT INTO schema_migrations VALUES (999, CURRENT_TIMESTAMP);
            """
        )
    check = check_database(path)
    assert check.ok is False
    assert check.schema_version == 999
    assert "newer" in " ".join(check.messages)
