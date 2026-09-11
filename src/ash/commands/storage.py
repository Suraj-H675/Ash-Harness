"""Session database integrity, backup, and non-destructive restore operations."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ash.core.session import CURRENT_SCHEMA_VERSION, SessionStorageError, SessionStore
from ash.core.redaction import redact_text
from ash.safe_io import validate_unlinked_file_path
from ash.safety.environment import resolve_host_executable


@dataclass(frozen=True)
class StorageCheck:
    path: str
    exists: bool
    ok: bool
    schema_version: int | None
    messages: tuple[str, ...]

    def as_dict(self) -> dict:
        return asdict(self)


def check_database(path: str | Path) -> StorageCheck:
    """Inspect a database without creating or migrating it."""

    database = Path(path).expanduser().resolve()
    if not database.is_file():
        return StorageCheck(str(database), False, False, None, ("database not found",))
    messages: list[str] = []
    version: int | None = None
    try:
        uri = f"{database.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            messages.extend(str(row[0]) for row in integrity_rows if row[0] != "ok")
            foreign_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
            messages.extend(
                "foreign key violation: " + ", ".join(str(value) for value in row)
                for row in foreign_rows
            )
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='schema_migrations'"
            ).fetchone()
            if table is None:
                version = 0
            else:
                version = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                    ).fetchone()[0]
                )
                if version > CURRENT_SCHEMA_VERSION:
                    messages.append(
                        f"schema {version} is newer than supported schema "
                        f"{CURRENT_SCHEMA_VERSION}"
                    )
    except (OSError, sqlite3.DatabaseError) as exc:
        messages.append(str(exc))
    return StorageCheck(
        path=str(database),
        exists=True,
        ok=not messages,
        schema_version=version,
        messages=tuple(messages or ("ok",)),
    )


def backup_database(path: str | Path, destination: str | Path | None = None) -> Path:
    check = check_database(path)
    if not check.ok:
        raise SessionStorageError(
            "Refusing to back up an unhealthy database: " + "; ".join(check.messages)
        )
    return SessionStore(path).backup(destination)


def restore_database(
    path: str | Path,
    backup: str | Path,
    *,
    confirmed: bool,
) -> tuple[Path, tuple[Path, ...]]:
    """Validate a backup, preserve current files, then atomically restore it."""

    if not confirmed:
        raise SessionStorageError("Restore requires explicit confirmation")
    try:
        database = validate_unlinked_file_path(path, label="session database")
        backup_path = validate_unlinked_file_path(backup, label="session backup")
    except ValueError as exc:
        raise SessionStorageError(str(exc)) from exc
    if database == backup_path:
        raise SessionStorageError("Backup and destination must differ")
    check = check_database(backup_path)
    if not check.ok:
        raise SessionStorageError(
            "Refusing to restore an unhealthy backup: " + "; ".join(check.messages)
        )

    database.parent.mkdir(parents=True, exist_ok=True)
    try:
        database = validate_unlinked_file_path(database, label="session database")
        backup_path = validate_unlinked_file_path(backup_path, label="session backup")
    except ValueError as exc:
        raise SessionStorageError(str(exc)) from exc
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    preserved: list[Path] = []
    for current in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
        if current.exists():
            preserved_path = database.with_name(
                f"{current.name}.pre-restore.{timestamp}.raw"
            )
            shutil.copy2(current, preserved_path)
            _restrict(preserved_path)
            preserved.append(preserved_path)

    temporary = database.with_name(f".{database.name}.restore-{uuid4().hex}.tmp")
    try:
        try:
            validate_unlinked_file_path(database, label="session database")
            validate_unlinked_file_path(backup_path, label="session backup")
        except ValueError as exc:
            raise SessionStorageError(str(exc)) from exc
        shutil.copy2(backup_path, temporary)
        _restrict(temporary)
        temporary_check = check_database(temporary)
        if not temporary_check.ok:
            raise SessionStorageError(
                "Copied backup failed verification: "
                + "; ".join(temporary_check.messages)
            )
        for sidecar in (Path(f"{database}-wal"), Path(f"{database}-shm")):
            sidecar.unlink(missing_ok=True)
        try:
            validate_unlinked_file_path(database, label="session database")
        except ValueError as exc:
            raise SessionStorageError(str(exc)) from exc
        os.replace(temporary, database)
        _restrict(database)
    finally:
        for artifact in (
            temporary,
            Path(f"{temporary}-wal"),
            Path(f"{temporary}-shm"),
        ):
            artifact.unlink(missing_ok=True)
    return database, tuple(preserved)


def render_storage_check(check: StorageCheck, *, json_output: bool = False) -> str:
    if json_output:
        return json.dumps(check.as_dict(), sort_keys=True)
    state = "ok" if check.ok else "failed"
    version = "unknown" if check.schema_version is None else str(check.schema_version)
    return f"Storage check {state}: {check.path} (schema {version})\n" + "\n".join(
        check.messages
    )


def create_debug_bundle(config, destination: str | Path | None = None) -> Path:
    """Create a bounded, redacted JSON diagnostics bundle."""

    database = config.db_directory / "sessions.db"
    check = check_database(database)
    try:
        git = resolve_host_executable(
            "git", workspace_root=config.workspace_root, cwd=config.workspace_root
        )
        git_revision = redact_text(
            subprocess.run(
                [git, "rev-parse", "--short", "HEAD"],
                cwd=config.workspace_root,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout.strip()
            if git is not None
            else ""
        )
    except (OSError, subprocess.TimeoutExpired):
        git_revision = ""

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ash": {
            "config_schema_version": config.config_schema_version,
            "model": config.model,
            "safety_tier": config.safety_tier,
            "memory_backend": config.memory_backend,
            "session_retention_days": config.session_retention_days,
        },
        "runtime": {
            "platform": platform.system(),
            "platform_release": platform.release(),
            "python_version": platform.python_version(),
            "workspace": str(Path(config.workspace_root).resolve()),
            "git_revision": git_revision,
        },
        "storage": {
            **check.as_dict(),
            "path": str(database),
        },
    }
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    raw_destination = (
        Path(destination).expanduser()
        if destination is not None
        else config.db_directory
        / f"debug-bundle-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    )
    try:
        destination_path = validate_unlinked_file_path(
            raw_destination, label="debug bundle"
        )
    except ValueError as exc:
        raise SessionStorageError(str(exc)) from exc
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination_path = validate_unlinked_file_path(
            destination_path, label="debug bundle"
        )
    except ValueError as exc:
        raise SessionStorageError(str(exc)) from exc
    temporary = destination_path.with_name(
        f".{destination_path.name}.{uuid4().hex}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(serialized + "\n")
        _restrict(temporary)
        try:
            destination_path = validate_unlinked_file_path(
                destination_path, label="debug bundle"
            )
        except ValueError as exc:
            raise SessionStorageError(str(exc)) from exc
        os.replace(temporary, destination_path)
    finally:
        temporary.unlink(missing_ok=True)
    return destination_path


def render_local_metrics(summary: dict, *, json_output: bool = False) -> str:
    """Render aggregate local-only model usage metrics."""

    if json_output:
        return json.dumps(
            {"telemetry": "local_only", "metrics": summary},
            sort_keys=True,
        )
    return (
        f"Local model usage ({summary['session_count']} sessions): "
        f"{int(summary['total_tokens'])} tokens "
        f"({int(summary['prompt_tokens'])} prompt / "
        f"{int(summary['completion_tokens'])} completion), "
        f"cache {int(summary['cache_read_tokens'])}/"
        f"{int(summary['cache_write_tokens'])}, "
        f"cost ${float(summary['cost_usd']):.6f}"
    )


def _restrict(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
