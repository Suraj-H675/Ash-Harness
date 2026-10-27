"""Owner-bound coworker identities, sessions, and one fixed offline worker seam.

General provider runtimes remain unavailable: provider credentials, ChatGPT
authentication, and extension discovery still have process/global-home readers
that are not safe for concurrent coworker execution. The only turn admitted here
uses a fixed offline provider inside the supervised Docker worker.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from ash.config import AshConfig
from ash.core.session import (
    CoworkerSessionBinding,
    Session,
    SessionStorageError,
    SessionStore,
    get_db_connection,
)
from ash.profiles import (
    DEFAULT_PROFILE,
    profile_directory,
    profile_exists,
    validate_profile_name,
)
from ash.coworker_worker import (
    WORKER_MAX_REQUEST_BYTES,
    WORKER_MAX_PROMPT_BYTES,
    WORKER_MODEL,
    WORKER_RESPONSE,
)
from ash.sandbox.docker import COWORKER_IMAGE, DEFAULT_IMAGE
from ash.sandbox.manager import SandboxBackendUnavailable, SandboxManager


_COWORKER_DIRECTORY_SCHEMA_VERSION = 3
_MAX_OWNER_ID_CHARS = 128
_MAX_COWORKER_NAME_CHARS = 128
_MAX_SYNTAX_FILES = 32
_MAX_SYNTAX_FILE_BYTES = 1_048_576
_MAX_SYNTAX_TOTAL_BYTES = 5_242_880
_SYNTAX_WORKER_TIMEOUT_SECONDS = 30
_OFFLINE_TURN_TIMEOUT_SECONDS = 45
_OFFLINE_TURN_MAX_RESPONSE_BYTES = 8_192
_SECRET_NAME_PARTS = (
    "credential",
    "secret",
    "password",
    "token",
    "private",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    ".env",
    ".pem",
    ".key",
    ".netrc",
    ".npmrc",
    ".pypirc",
)
_SECRET_CONTENT_PATTERNS = (
    re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC |DSA |PGP )?PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?im)(?<![A-Za-z0-9])(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
        r"client[_-]?secret|credential(?:s)?|password|secret(?:[_-]?(?:access[_-]?)?key)?|"
        r"private[_-]?key)"
        r"\s*[:=]\s*(['\"])[^'\"\r\n]{16,}\1"
    ),
    re.compile(
        r"(?im)(?<![A-Za-z0-9])(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
        r"client[_-]?secret|credential(?:s)?|password|secret(?:[_-]?(?:access[_-]?)?key)?|"
        r"private[_-]?key)"
        r"\s*[:=]\s*[A-Za-z0-9_./+=:-]{24,}"
    ),
)
_COWORKER_SYNTAX_WORKER = """import ast, json, pathlib, sys
names = sys.argv[1:]
root = pathlib.Path.cwd()
if set(names) != {path.name for path in root.iterdir()} or len(names) != len(set(names)):
    raise SystemExit(70)
files = []
for name in names:
    if pathlib.Path(name).name != name or not name.endswith('.py'):
        raise SystemExit(71)
    try:
        ast.parse((root / name).read_text(encoding='utf-8'), filename=name)
    except SyntaxError as error:
        files.append({'name': name, 'ok': False, 'line': error.lineno, 'column': error.offset})
    except (OSError, UnicodeError):
        raise SystemExit(72)
    else:
        files.append({'name': name, 'ok': True, 'line': None, 'column': None})
print(json.dumps({'files': files}, separators=(',', ':')))
"""


class CoworkerNotFound(KeyError):
    """No coworker is available to the supplied owner identity."""


class CoworkerDirectoryError(RuntimeError):
    """The persisted coworker directory is invalid or cannot be used safely."""


class CoworkerRuntimeUnavailable(RuntimeError):
    """Provider execution is not admitted by this identity/session proof."""


@dataclass(frozen=True)
class Coworker:
    """Persisted identity; only ``display_name`` may change after creation."""

    coworker_id: str
    owner_id: str
    owner_uid: int | None
    display_name: str
    profile_name: str
    workspace: Path
    canonical_session_id: str
    state: Literal["provisioning", "ready"]


@dataclass(frozen=True)
class CoworkerSessionRef:
    """An owner-checked canonical or background session association."""

    coworker_id: str
    session_id: str
    canonical_session_id: str
    run_id: str | None


@dataclass(frozen=True)
class CoworkerPythonSyntaxFile:
    """Result for one explicitly staged Python source file."""

    name: str
    valid: bool
    line: int | None
    column: int | None


@dataclass(frozen=True)
class CoworkerPythonSyntaxReport:
    """Sanitized output from the fixed, non-executing syntax worker."""

    session_ref: CoworkerSessionRef
    files: tuple[CoworkerPythonSyntaxFile, ...]

    @property
    def valid(self) -> bool:
        return all(file.valid for file in self.files)


@dataclass(frozen=True)
class CoworkerTurnResult:
    """One fixed offline AshLoop turn and its owner-bound background session."""

    session_ref: CoworkerSessionRef
    request_id: str
    response: str


@dataclass(frozen=True)
class CoworkerProcessEnvironment:
    """Per-run default paths and an allowlisted environment for one child."""

    root: Path
    home: Path
    working_directory: Path
    temporary_directory: Path
    config_directory: Path
    data_directory: Path
    cache_directory: Path
    state_directory: Path
    runtime_directory: Path

    def as_environ(self) -> dict[str, str]:
        """Build a fresh allowlist environment for a child process."""

        home = str(self.home)
        environment = {
            "HOME": home,
            "XDG_CONFIG_HOME": str(self.config_directory),
            "XDG_DATA_HOME": str(self.data_directory),
            "XDG_CACHE_HOME": str(self.cache_directory),
            "XDG_STATE_HOME": str(self.state_directory),
            "XDG_RUNTIME_DIR": str(self.runtime_directory),
            "TMPDIR": str(self.temporary_directory),
            "TMP": str(self.temporary_directory),
            "TEMP": str(self.temporary_directory),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if os.name == "nt":
            environment.update(
                {
                    "USERPROFILE": home,
                    "APPDATA": str(self.home / "AppData" / "Roaming"),
                    "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
                }
            )
            for name in ("SYSTEMROOT", "WINDIR"):
                value = os.environ.get(name)
                if value:
                    environment[name] = value
        return environment


def _normalize_owner_id(owner_id: str) -> str:
    if not isinstance(owner_id, str):
        raise ValueError("owner_id must be a string")
    normalized = owner_id.strip()
    if (
        not normalized
        or len(normalized) > _MAX_OWNER_ID_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ValueError("owner_id must be non-empty, bounded, and printable")
    return normalized


def _normalize_display_name(display_name: str) -> str:
    if not isinstance(display_name, str):
        raise ValueError("display_name must be a string")
    normalized = display_name.strip()
    if (
        not normalized
        or len(normalized) > _MAX_COWORKER_NAME_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ValueError("display_name must be non-empty, bounded, and printable")
    return normalized


def _normalize_coworker_id(coworker_id: str) -> str:
    if not isinstance(coworker_id, str):
        raise ValueError("coworker_id must be a UUID string")
    try:
        return str(UUID(coworker_id))
    except (ValueError, AttributeError) as exc:
        raise ValueError("coworker_id must be a UUID string") from exc


def _normalize_workspace(workspace: str | Path) -> Path:
    if not isinstance(workspace, (str, Path)):
        raise ValueError("workspace must be a filesystem path")
    path = Path(workspace).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError("workspace must be an existing directory")
    return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_from_row(row: sqlite3.Row) -> Coworker:
    return Coworker(
        coworker_id=str(row["coworker_id"]),
        owner_id=str(row["owner_id"]),
        owner_uid=(int(row["owner_uid"]) if row["owner_uid"] is not None else None),
        display_name=str(row["display_name"]),
        profile_name=str(row["profile_name"]),
        workspace=Path(str(row["workspace_path"])),
        canonical_session_id=str(row["canonical_session_id"]),
        state=str(row["state"]),  # type: ignore[arg-type]
    )


def _current_owner_uid() -> int | None:
    if os.name != "posix" or not hasattr(os, "geteuid"):
        return None
    try:
        uid = os.geteuid()
    except OSError:
        return None
    return uid if uid >= 0 else None


def _validated_python_names(permitted_files: Sequence[str]) -> tuple[str, ...]:
    if isinstance(permitted_files, (str, bytes)):
        raise ValueError("permitted_files must be a sequence of exact filenames")
    names = tuple(permitted_files)
    if not names or len(names) > _MAX_SYNTAX_FILES:
        raise ValueError("permitted_files must contain 1 to 32 unique filenames")
    for name in names:
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 128
            or name.startswith(".")
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
            or "/" in name
            or "\\" in name
            or Path(name).name != name
            or name in {".", ".."}
            or not name.endswith(".py")
            or any(part in name.casefold() for part in _SECRET_NAME_PARTS)
        ):
            raise ValueError("permitted_files contains an unsafe Python filename")
    if len(set(names)) != len(names):
        raise ValueError("permitted_files must contain 1 to 32 unique filenames")
    return tuple(sorted(names))


def _linux_mountpoints() -> frozenset[Path]:
    if not sys.platform.startswith("linux"):
        raise CoworkerRuntimeUnavailable(
            "supervised workspace staging is unavailable on this platform"
        )
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CoworkerDirectoryError(
            "could not verify workspace mount boundaries"
        ) from exc

    def decode_mount_path(value: str) -> str:
        return re.sub(
            r"\\([0-7]{3})",
            lambda match: chr(int(match.group(1), 8)),
            value,
        )

    mountpoints: set[Path] = set()
    try:
        for line in lines:
            fields = line.split()
            if len(fields) < 6:
                raise ValueError("mountinfo row is incomplete")
            mountpoints.add(Path(decode_mount_path(fields[4])))
    except (IndexError, ValueError) as exc:
        raise CoworkerDirectoryError(
            "could not verify workspace mount boundaries"
        ) from exc
    return frozenset(mountpoints)


def _stage_python_files(
    workspace: Path,
    destination: Path,
    names: tuple[str, ...],
) -> None:
    if not sys.platform.startswith("linux"):
        raise CoworkerRuntimeUnavailable(
            "supervised workspace staging is unavailable on this platform"
        )
    workspace_path = workspace.resolve(strict=True)
    if workspace_path != workspace or not workspace_path.is_dir():
        raise CoworkerDirectoryError("coworker workspace changed before staging")
    mountpoints = _linux_mountpoints()
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root_descriptor = -1
    total_size = 0
    try:
        root_descriptor = os.open(workspace_path, directory_flags)
        root_metadata = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise CoworkerDirectoryError("coworker workspace is not a regular directory")
        for name in names:
            source = workspace_path / name
            if source in mountpoints:
                raise CoworkerDirectoryError(
                    "permitted Python file crosses a nested mount"
                )
            try:
                metadata = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
            except OSError as exc:
                raise CoworkerDirectoryError(
                    "permitted Python file could not be inspected"
                ) from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_dev != root_metadata.st_dev
            ):
                raise CoworkerDirectoryError(
                    "permitted Python file is not a regular single-link workspace file"
                )
            file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            file_descriptor = -1
            try:
                file_descriptor = os.open(name, file_flags, dir_fd=root_descriptor)
                opened = os.fstat(file_descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or opened.st_dev != root_metadata.st_dev
                    or (opened.st_dev, opened.st_ino)
                    != (metadata.st_dev, metadata.st_ino)
                ):
                    raise CoworkerDirectoryError(
                        "permitted Python file changed while staging"
                    )
                chunks: list[bytes] = []
                file_size = 0
                while True:
                    chunk = os.read(
                        file_descriptor,
                        min(65_536, _MAX_SYNTAX_FILE_BYTES + 1 - file_size),
                    )
                    if not chunk:
                        break
                    file_size += len(chunk)
                    total_size += len(chunk)
                    if (
                        file_size > _MAX_SYNTAX_FILE_BYTES
                        or total_size > _MAX_SYNTAX_TOTAL_BYTES
                    ):
                        raise CoworkerDirectoryError(
                            "permitted Python files exceed the staging size limit"
                        )
                    chunks.append(chunk)
                final_metadata = os.fstat(file_descriptor)
                if (
                    final_metadata.st_nlink != 1
                    or (final_metadata.st_dev, final_metadata.st_ino)
                    != (opened.st_dev, opened.st_ino)
                ):
                    raise CoworkerDirectoryError(
                        "permitted Python file changed while staging"
                    )
            except OSError as exc:
                raise CoworkerDirectoryError(
                    "permitted Python file could not be staged"
                ) from exc
            finally:
                if file_descriptor >= 0:
                    os.close(file_descriptor)

            content = b"".join(chunks)
            try:
                source_text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CoworkerDirectoryError(
                    "permitted Python file is not UTF-8 text"
                ) from exc
            if b"\0" in content or any(
                pattern.search(source_text) for pattern in _SECRET_CONTENT_PATTERNS
            ):
                raise CoworkerDirectoryError(
                    "permitted Python file contains a recognized secret marker"
                )

            target_descriptor = -1
            target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            target_flags |= os.O_CLOEXEC
            try:
                target_descriptor = os.open(
                    destination / name,
                    target_flags,
                    0o400,
                )
                view = memoryview(content)
                while view:
                    written = os.write(target_descriptor, view)
                    if written <= 0:
                        raise OSError("short write while staging Python source")
                    view = view[written:]
            except OSError as exc:
                raise CoworkerDirectoryError(
                    "private Python staging failed"
                ) from exc
            finally:
                if target_descriptor >= 0:
                    os.close(target_descriptor)
    except OSError as exc:
        raise CoworkerDirectoryError("coworker workspace could not be staged") from exc
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)


def _stage_offline_worker_request(
    destination: Path,
    *,
    request_id: str,
    session_id: str,
    prompt: str,
) -> None:
    if not sys.platform.startswith("linux"):
        raise CoworkerRuntimeUnavailable(
            "supervised coworker turns require Linux Docker staging"
        )
    payload = json.dumps(
        {
            "version": 1,
            "request_id": request_id,
            "session_id": session_id,
            "prompt": prompt,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > WORKER_MAX_REQUEST_BYTES:
        raise CoworkerDirectoryError("offline coworker request exceeds its limit")
    directory_descriptor = -1
    file_descriptor = -1
    try:
        directory_descriptor = os.open(
            destination,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        if os.listdir(directory_descriptor):
            raise CoworkerDirectoryError(
                "offline coworker request staging directory is not empty"
            )
        file_descriptor = os.open(
            "request.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o400,
            dir_fd=directory_descriptor,
        )
        view = memoryview(payload)
        while view:
            written = os.write(file_descriptor, view)
            if written <= 0:
                raise OSError("short write while staging offline coworker request")
            view = view[written:]
    except OSError as exc:
        raise CoworkerDirectoryError(
            "offline coworker request staging failed"
        ) from exc
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _unique_protocol_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate worker response field")
        result[key] = value
    return result


def _parse_offline_worker_response(
    stdout: str,
    *,
    request_id: str,
    session_id: str,
) -> str:
    try:
        encoded = stdout.encode("utf-8", errors="strict")
        if len(encoded) > _OFFLINE_TURN_MAX_RESPONSE_BYTES:
            raise ValueError("worker response exceeds its limit")
        payload = json.loads(stdout, object_pairs_hook=_unique_protocol_object)
    except (UnicodeEncodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CoworkerRuntimeUnavailable(
            "supervised coworker worker returned an invalid result"
        ) from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "request_id", "session_id", "response"}
        or type(payload["version"]) is not int
        or payload["version"] != 1
        or payload["request_id"] != request_id
        or payload["session_id"] != session_id
        or payload["response"] != WORKER_RESPONSE
    ):
        raise CoworkerRuntimeUnavailable(
            "supervised coworker worker returned an invalid result"
        )
    return WORKER_RESPONSE


def _parse_python_syntax_report(
    stdout: str,
    expected_names: tuple[str, ...],
    session_ref: CoworkerSessionRef,
) -> CoworkerPythonSyntaxReport:
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise CoworkerRuntimeUnavailable(
            "supervised syntax worker returned an invalid result"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {"files"}:
        raise CoworkerRuntimeUnavailable(
            "supervised syntax worker returned an invalid result"
        )
    rows = payload["files"]
    if not isinstance(rows, list) or len(rows) != len(expected_names):
        raise CoworkerRuntimeUnavailable(
            "supervised syntax worker returned an invalid result"
        )
    files: list[CoworkerPythonSyntaxFile] = []
    for row, expected_name in zip(rows, expected_names, strict=True):
        if not isinstance(row, dict) or set(row) != {"name", "ok", "line", "column"}:
            raise CoworkerRuntimeUnavailable(
                "supervised syntax worker returned an invalid result"
            )
        name, valid, line, column = (
            row["name"],
            row["ok"],
            row["line"],
            row["column"],
        )
        if (
            name != expected_name
            or not isinstance(valid, bool)
            or (line is not None and (type(line) is not int or line < 1))
            or (column is not None and (type(column) is not int or column < 1))
            or (line is None and column is not None)
            or (valid and (line is not None or column is not None))
        ):
            raise CoworkerRuntimeUnavailable(
                "supervised syntax worker returned an invalid result"
            )
        files.append(
            CoworkerPythonSyntaxFile(
                name=name,
                valid=valid,
                line=line,
                column=column,
            )
        )
    return CoworkerPythonSyntaxReport(session_ref=session_ref, files=tuple(files))


def _session_ref(
    binding: CoworkerSessionBinding,
) -> CoworkerSessionRef:
    return CoworkerSessionRef(
        coworker_id=binding.coworker_id,
        session_id=binding.session_id,
        canonical_session_id=binding.canonical_session_id,
        run_id=binding.run_id,
    )


class CoworkerDirectory:
    """Versioned local index of owner/profile-bound coworker identities."""

    def __init__(self, ash_dir: Path) -> None:
        root = Path(ash_dir).expanduser().absolute()
        root_is_junction = getattr(root, "is_junction", lambda: False)()
        if root.is_symlink() or root_is_junction or not root.is_dir():
            raise CoworkerDirectoryError(
                f"Ash state directory is not a regular directory: {root}"
            )
        self.ash_dir = root
        self.db_path = root / "coworkers.sqlite3"
        self._initialize()

    @classmethod
    def open(cls, ash_dir: Path | None = None) -> "CoworkerDirectory":
        """Open the local directory under the explicit or default Ash state root."""

        root = ash_dir or (Path.home() / ".ash")
        return cls(root)

    def _connect(self) -> sqlite3.Connection:
        return get_db_connection(self.db_path)

    def _initialize(self) -> None:
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                tables = {
                    str(row["name"])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if not tables:
                    conn.execute(
                        "CREATE TABLE coworker_directory_meta "
                        "(schema_version INTEGER NOT NULL)"
                    )
                    conn.execute(
                        "INSERT INTO coworker_directory_meta(schema_version) VALUES (?)",
                        (_COWORKER_DIRECTORY_SCHEMA_VERSION,),
                    )
                    conn.execute(
                        """
                        CREATE TABLE coworkers (
                            coworker_id TEXT PRIMARY KEY,
                            owner_id TEXT NOT NULL,
                            owner_uid INTEGER,
                            display_name TEXT NOT NULL,
                            profile_name TEXT NOT NULL UNIQUE,
                            workspace_path TEXT NOT NULL,
                            canonical_session_id TEXT NOT NULL UNIQUE,
                            state TEXT NOT NULL
                                CHECK(state IN ('provisioning', 'ready')),
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        )
                        """
                    )
                elif not {
                    "coworker_directory_meta",
                    "coworkers",
                }.issubset(tables):
                    raise CoworkerDirectoryError(
                        "coworker directory database has an unrecognized schema"
                    )
                row = conn.execute(
                    "SELECT schema_version FROM coworker_directory_meta"
                ).fetchone()
                if row is None:
                    raise CoworkerDirectoryError(
                        "coworker directory schema version is missing"
                    )
                version = int(row["schema_version"])
                if version == 1:
                    columns = {
                        str(column["name"])
                        for column in conn.execute("PRAGMA table_info(coworkers)")
                    }
                    if "owner_uid" not in columns:
                        conn.execute(
                            "ALTER TABLE coworkers ADD COLUMN owner_uid INTEGER"
                        )
                    conn.execute("UPDATE coworkers SET owner_uid = NULL")
                    conn.execute(
                        "UPDATE coworker_directory_meta SET schema_version = ?",
                        (_COWORKER_DIRECTORY_SCHEMA_VERSION,),
                    )
                    version = _COWORKER_DIRECTORY_SCHEMA_VERSION
                if version == 2:
                    conn.execute(
                        "UPDATE coworker_directory_meta SET schema_version = ?",
                        (_COWORKER_DIRECTORY_SCHEMA_VERSION,),
                    )
                    version = _COWORKER_DIRECTORY_SCHEMA_VERSION
                if version != _COWORKER_DIRECTORY_SCHEMA_VERSION:
                    raise CoworkerDirectoryError(
                        "coworker directory schema version is unsupported: "
                        f"{version}"
                    )
                columns = {
                    str(column["name"])
                    for column in conn.execute("PRAGMA table_info(coworkers)")
                }
                if "owner_uid" not in columns:
                    raise CoworkerDirectoryError(
                        "coworker directory owner binding schema is incomplete"
                    )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS coworker_turn_dispatches (
                        owner_id TEXT NOT NULL,
                        coworker_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        request_id TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL CHECK(status IN ('claimed', 'completed')),
                        created_at TEXT NOT NULL,
                        completed_at TEXT,
                        PRIMARY KEY(owner_id, coworker_id, run_id)
                    )
                    """
                )
                dispatch_columns = {
                    str(column["name"])
                    for column in conn.execute(
                        "PRAGMA table_info(coworker_turn_dispatches)"
                    )
                }
                if dispatch_columns != {
                    "owner_id",
                    "coworker_id",
                    "run_id",
                    "session_id",
                    "request_id",
                    "status",
                    "created_at",
                    "completed_at",
                }:
                    raise CoworkerDirectoryError(
                        "coworker dispatch journal schema is incomplete"
                    )
        except (sqlite3.DatabaseError, OSError, SessionStorageError) as exc:
            raise CoworkerDirectoryError(
                f"could not open coworker directory {self.db_path}: {exc}"
            ) from exc

    def _get_owned(self, owner_id: str, coworker_id: str) -> Coworker:
        owner = _normalize_owner_id(owner_id)
        identity = _normalize_coworker_id(coworker_id)
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM coworkers WHERE owner_id = ? AND coworker_id = ?",
                (owner, identity),
            ).fetchone()
        if row is None:
            raise CoworkerNotFound("coworker not found")
        return _record_from_row(row)

    def _claim_offline_turn(
        self,
        *,
        handle: "CoworkerHandle",
        run_id: str,
        session_id: str,
        request_id: str,
    ) -> None:
        """Durably consume one run key before any worker process is dispatched."""

        identity = handle.identity
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM coworkers WHERE owner_id = ? AND coworker_id = ?",
                    (identity.owner_id, identity.coworker_id),
                ).fetchone()
                current = _record_from_row(row) if row is not None else None
                owner_uid = _current_owner_uid()
                if (
                    current is None
                    or current.owner_id != identity.owner_id
                    or current.coworker_id != identity.coworker_id
                    or current.owner_uid != identity.owner_uid
                    or current.profile_name != identity.profile_name
                    or current.workspace != identity.workspace
                    or current.canonical_session_id != identity.canonical_session_id
                    or current.owner_uid is None
                    or current.owner_uid != owner_uid
                    or current.state != "ready"
                ):
                    raise CoworkerRuntimeUnavailable(
                        "local coworker owner identity could not be verified"
                    )
                prior = conn.execute(
                    "SELECT status FROM coworker_turn_dispatches "
                    "WHERE owner_id = ? AND coworker_id = ? AND run_id = ?",
                    (identity.owner_id, identity.coworker_id, run_id),
                ).fetchone()
                if prior is not None:
                    raise CoworkerRuntimeUnavailable(
                        "coworker run was already claimed; refusing replay"
                    )
                conn.execute(
                    "INSERT INTO coworker_turn_dispatches "
                    "(owner_id, coworker_id, run_id, session_id, request_id, "
                    "status, created_at, completed_at) "
                    "VALUES (?, ?, ?, ?, ?, 'claimed', ?, NULL)",
                    (
                        identity.owner_id,
                        identity.coworker_id,
                        run_id,
                        session_id,
                        request_id,
                        _now(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise CoworkerRuntimeUnavailable(
                "coworker run was already claimed; refusing replay"
            ) from exc

    def _complete_offline_turn(
        self,
        *,
        handle: "CoworkerHandle",
        run_id: str,
        session_id: str,
        request_id: str,
    ) -> None:
        """Mark a claimed dispatch complete only after validating its transcript."""

        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            identity = handle.identity
            row = conn.execute(
                "SELECT * FROM coworkers WHERE owner_id = ? AND coworker_id = ?",
                (identity.owner_id, identity.coworker_id),
            ).fetchone()
            current = _record_from_row(row) if row is not None else None
            if (
                current is None
                or current.owner_uid != identity.owner_uid
                or current.profile_name != identity.profile_name
                or current.workspace != identity.workspace
                or current.canonical_session_id != identity.canonical_session_id
                or current.state != "ready"
                or current.owner_uid != _current_owner_uid()
            ):
                raise CoworkerDirectoryError(
                    "coworker owner binding changed before dispatch completion"
                )
            cursor = conn.execute(
                "UPDATE coworker_turn_dispatches SET status = 'completed', "
                "completed_at = ? WHERE owner_id = ? AND coworker_id = ? "
                "AND run_id = ? AND session_id = ? AND request_id = ? "
                "AND status = 'claimed'",
                (
                    _now(),
                    handle.identity.owner_id,
                    handle.identity.coworker_id,
                    run_id,
                    session_id,
                    request_id,
                ),
            )
            if cursor.rowcount != 1:
                raise CoworkerDirectoryError(
                    "offline coworker dispatch journal changed"
                )

    def _require_local_coworker_owner(self, handle: "CoworkerHandle") -> Coworker:
        current = self._get_owned(
            owner_id=handle.identity.owner_id,
            coworker_id=handle.identity.coworker_id,
        )
        if (
            current.owner_id != handle.identity.owner_id
            or current.owner_uid != handle.identity.owner_uid
            or current.profile_name != handle.identity.profile_name
            or current.workspace != handle.identity.workspace
            or current.canonical_session_id != handle.identity.canonical_session_id
            or current.state != "ready"
        ):
            raise CoworkerDirectoryError("coworker process binding changed")
        owner_uid = _current_owner_uid()
        if owner_uid is None or current.owner_uid is None or current.owner_uid != owner_uid:
            raise CoworkerRuntimeUnavailable(
                "local coworker owner identity could not be verified"
            )
        return current

    def _get_by_profile(self, profile_name: str) -> Coworker | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM coworkers WHERE profile_name = ?",
                (profile_name,),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def _effective_config(self, record: Coworker) -> AshConfig:
        if not profile_exists(record.profile_name, ash_dir=self.ash_dir):
            raise CoworkerDirectoryError("bound Ash profile is missing or unsafe")
        return AshConfig._load_for_profile(
            record.profile_name,
            ash_dir=self.ash_dir,
            workspace_root=record.workspace,
        )

    def _session_store(self, record: Coworker, config: AshConfig) -> SessionStore:
        expected_db = self._profile_database_directory(record)
        if config.db_directory != expected_db:
            raise CoworkerDirectoryError("bound coworker database path changed")
        return SessionStore(config.db_directory / "sessions.db")

    def _profile_database_directory(self, record: Coworker) -> Path:
        if record.profile_name == DEFAULT_PROFILE or not profile_exists(
            record.profile_name,
            ash_dir=self.ash_dir,
        ):
            raise CoworkerDirectoryError("bound coworker profile is missing or unsafe")
        return Path(
            os.path.abspath(
                profile_directory(record.profile_name, ash_dir=self.ash_dir) / "db"
            )
        )

    def _session_store_for_record(self, record: Coworker) -> SessionStore:
        """Open only the owner-bound profile DB without reading provider config."""

        return SessionStore(
            self._profile_database_directory(record) / "sessions.db"
        )

    def create(
        self,
        *,
        owner_id: str,
        display_name: str,
        profile_name: str,
        workspace: str | Path,
    ) -> Coworker:
        """Bind a new immutable identity to one empty named profile."""

        owner = _normalize_owner_id(owner_id)
        name = _normalize_display_name(display_name)
        owner_uid = _current_owner_uid()
        profile = validate_profile_name(profile_name)
        if profile == DEFAULT_PROFILE:
            raise ValueError("coworkers must use an explicit named Ash profile")
        workspace_path = _normalize_workspace(workspace)
        if not profile_exists(profile, ash_dir=self.ash_dir):
            raise ValueError(f"Ash profile does not exist: {profile!r}")

        # Resolve and reject unsafe inherited settings before opening a session DB.
        config = AshConfig._load_for_profile(
            profile,
            ash_dir=self.ash_dir,
            workspace_root=workspace_path,
        )
        store = SessionStore(config.db_directory / "sessions.db")

        existing = self._get_by_profile(profile)
        if existing is not None:
            if existing.owner_id != owner or existing.workspace != workspace_path:
                raise ValueError("Ash profile is already bound to another coworker")
            handle = self.open_coworker(
                owner_id=owner,
                coworker_id=existing.coworker_id,
            )
            return handle.identity

        store.require_empty_for_coworker_profile()
        coworker_id = str(uuid4())
        canonical_session_id = str(uuid4())
        now = _now()
        existing_record: Coworker | None = None
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                prior = conn.execute(
                    "SELECT * FROM coworkers WHERE profile_name = ?",
                    (profile,),
                ).fetchone()
                if prior is not None:
                    record = _record_from_row(prior)
                    if record.owner_id != owner or record.workspace != workspace_path:
                        raise ValueError(
                            "Ash profile is already bound to another coworker"
                        )
                    existing_record = record
                else:
                    conn.execute(
                        """
                        INSERT INTO coworkers (
                            coworker_id, owner_id, owner_uid, display_name,
                            profile_name, workspace_path, canonical_session_id,
                            state, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'provisioning', ?, ?)
                        """,
                        (
                            coworker_id,
                            owner,
                            owner_uid,
                            name,
                            profile,
                            str(workspace_path),
                            canonical_session_id,
                            now,
                            now,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise ValueError("coworker ID or profile binding is already in use") from exc

        if existing_record is not None:
            return self.open_coworker(
                owner_id=owner,
                coworker_id=existing_record.coworker_id,
            ).identity

        handle = self.open_coworker(owner_id=owner, coworker_id=coworker_id)
        return handle.identity

    def rename(
        self,
        *,
        owner_id: str,
        coworker_id: str,
        display_name: str,
    ) -> Coworker:
        """Change presentation only; ID, owner, profile, and workspace stay fixed."""

        owner = _normalize_owner_id(owner_id)
        identity = _normalize_coworker_id(coworker_id)
        name = _normalize_display_name(display_name)
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "UPDATE coworkers SET display_name = ?, updated_at = ? "
                "WHERE owner_id = ? AND coworker_id = ?",
                (name, _now(), owner, identity),
            )
            if cursor.rowcount != 1:
                raise CoworkerNotFound("coworker not found")
        return self._get_owned(owner, identity)

    def open_coworker(self, *, owner_id: str, coworker_id: str) -> "CoworkerHandle":
        """Open a coworker only through its persisted owner and immutable ID."""

        owner = _normalize_owner_id(owner_id)
        record = self._get_owned(owner, coworker_id)
        handle = CoworkerHandle(self, record)
        if record.state == "provisioning":
            handle._finish_provisioning()
        else:
            handle._require_canonical()
        return handle

    def _mark_ready(self, *, owner_id: str, coworker_id: str) -> Coworker:
        owner = _normalize_owner_id(owner_id)
        identity = _normalize_coworker_id(coworker_id)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE coworkers SET state = 'ready', updated_at = ? "
                "WHERE owner_id = ? AND coworker_id = ? AND state = 'provisioning'",
                (_now(), owner, identity),
            )
        return self._get_owned(owner, identity)


class CoworkerHandle:
    """Owner-checked storage/config view with no model execution capability."""

    def __init__(self, directory: CoworkerDirectory, identity: Coworker) -> None:
        self._directory = directory
        self._identity = identity

    @property
    def identity(self) -> Coworker:
        return self._identity

    def effective_config(self) -> AshConfig:
        """Return the explicit profile snapshot; SDK runtime creation is denied."""

        return self._directory._effective_config(self.identity)

    def _store(self) -> SessionStore:
        record = self._directory._get_owned(
            self.identity.owner_id, self.identity.coworker_id
        )
        if (
            record.profile_name != self.identity.profile_name
            or record.workspace != self.identity.workspace
            or record.canonical_session_id != self.identity.canonical_session_id
        ):
            raise CoworkerDirectoryError("coworker session binding changed")
        return self._directory._session_store_for_record(record)

    def _finish_provisioning(self) -> CoworkerSessionRef:
        record = self.identity
        store = self._directory._session_store_for_record(record)
        lease = store.acquire_session_runtime_lease(record.canonical_session_id)
        try:
            store.ensure_coworker_conversation(
                owner_id=record.owner_id,
                coworker_id=record.coworker_id,
                session_id=record.canonical_session_id,
                project_path=record.workspace,
            )
        finally:
            lease.close()
        self._identity = self._directory._mark_ready(
            owner_id=record.owner_id,
            coworker_id=record.coworker_id,
        )
        return CoworkerSessionRef(
            coworker_id=record.coworker_id,
            session_id=record.canonical_session_id,
            canonical_session_id=record.canonical_session_id,
            run_id=None,
        )

    def _require_canonical(self) -> CoworkerSessionRef:
        store = self._store()
        session, binding = store.load_coworker_session(
            owner_id=self.identity.owner_id,
            coworker_id=self.identity.coworker_id,
            session_id=self.identity.canonical_session_id,
            project_path=self.identity.workspace,
        )
        if (
            binding.run_id is not None
            or binding.session_id != binding.canonical_session_id
            or session.session_id != self.identity.canonical_session_id
        ):
            raise CoworkerDirectoryError("canonical coworker session is invalid")
        return _session_ref(binding)

    def ensure_canonical_conversation(self) -> CoworkerSessionRef:
        """Return the durable canonical session, repairing provisioning after a crash."""

        if self.identity.state == "provisioning":
            return self._finish_provisioning()
        return self._require_canonical()

    def _ensure_background_run_under_lease(
        self,
        store: SessionStore,
        *,
        run_id: str,
        canonical_session_id: str,
    ) -> CoworkerSessionRef:
        self._require_canonical()
        binding = store.ensure_coworker_run(
            owner_id=self.identity.owner_id,
            coworker_id=self.identity.coworker_id,
            run_id=run_id,
            canonical_session_id=canonical_session_id,
            project_path=self.identity.workspace,
        )
        return _session_ref(binding)

    def begin_background_run(self, run_id: str) -> CoworkerSessionRef:
        """Create one fresh session per run key under the canonical session lease."""

        canonical = self.ensure_canonical_conversation()
        store = self._store()
        existing = store.get_coworker_run(
            owner_id=self.identity.owner_id,
            coworker_id=self.identity.coworker_id,
            run_id=run_id,
            canonical_session_id=canonical.session_id,
            project_path=self.identity.workspace,
        )
        if existing is not None:
            return _session_ref(existing)

        lease = store.acquire_session_runtime_lease(canonical.session_id)
        try:
            return self._ensure_background_run_under_lease(
                store,
                run_id=run_id,
                canonical_session_id=canonical.session_id,
            )
        finally:
            lease.close()

    def load_session(self, session_id: str) -> tuple[Session, CoworkerSessionRef]:
        """Load only session IDs in this owner/coworker association set."""

        session, binding = self._store().load_coworker_session(
            owner_id=self.identity.owner_id,
            coworker_id=self.identity.coworker_id,
            session_id=session_id,
            project_path=self.identity.workspace,
        )
        return session, _session_ref(binding)

    async def check_python_syntax(
        self,
        *,
        run_id: str,
        permitted_files: Sequence[str],
    ) -> CoworkerPythonSyntaxReport:
        """Check explicitly selected Python files in the fixed local Docker worker.

        The caller supplies filenames only. The worker image, command, mount,
        environment, network policy, and resource limits are fixed here. Source
        files are staged by value into a private read-only workspace; they are
        parsed with ``ast.parse`` and never executed.
        """

        current = self._directory._require_local_coworker_owner(self)
        names = _validated_python_names(permitted_files)
        session_ref = self.begin_background_run(run_id)
        store = self._store()
        lease = store.acquire_session_runtime_lease(session_ref.session_id)
        try:
            current = self._directory._require_local_coworker_owner(self)
            _, verified_ref = self.load_session(session_ref.session_id)
            if (
                verified_ref != session_ref
                or verified_ref.canonical_session_id != current.canonical_session_id
            ):
                raise CoworkerDirectoryError(
                    "background worker session binding changed"
                )
            with self.worker_process_environment() as environment:
                _stage_python_files(current.workspace, environment.working_directory, names)
                manager = SandboxManager(
                    workspace_root=environment.working_directory,
                    network=False,
                    workspace_read_only=True,
                    allow_scoped_fallback=False,
                    backend_preference="docker",
                    docker_image=DEFAULT_IMAGE,
                    docker_memory_mb=256,
                    docker_cpus=0.5,
                    require_resource_containment=True,
                )
                if (
                    manager.backend_name != "docker"
                    or not manager.has_aggregate_resource_limits()
                ):
                    raise SandboxBackendUnavailable(
                        "coworker syntax checks require bounded Docker execution"
                    )
                result = await manager.run_supervised_process(
                    (
                        "/usr/local/bin/python",
                        "-B",
                        "-I",
                        "-c",
                        _COWORKER_SYNTAX_WORKER,
                        *names,
                    ),
                    cwd=environment.working_directory,
                    timeout=_SYNTAX_WORKER_TIMEOUT_SECONDS,
                )
                if (
                    result.backend_name != "docker"
                    or result.fallback_used
                    or result.exit_code != 0
                    or result.timed_out
                    or result.outcome_unknown
                    or result.output_truncated
                ):
                    raise CoworkerRuntimeUnavailable(
                        "supervised syntax worker did not finish with a known result"
                    )
                return _parse_python_syntax_report(
                    result.stdout,
                    names,
                    session_ref,
                )
        finally:
            lease.close()

    async def run_offline_turn(
        self,
        *,
        run_id: str,
        prompt: str,
    ) -> CoworkerTurnResult:
        """Run one deterministic AshLoop turn inside the fixed Docker worker.

        This path admits no caller-selected provider, command, image, tool, or
        extension. The parent validates and leases the owner-bound canonical
        session; AshLoop acquires the separate background-session lease inside
        the worker container. A run with any existing transcript is refused so
        an ambiguous dispatch cannot be replayed.
        """

        current = self._directory._require_local_coworker_owner(self)
        if not isinstance(run_id, str):
            raise ValueError("run_id must be a string")
        if not isinstance(prompt, str) or not prompt.strip() or "\0" in prompt:
            raise ValueError("prompt must be non-empty text without NUL bytes")
        try:
            prompt_size = len(prompt.encode("utf-8", errors="strict"))
        except UnicodeEncodeError as exc:
            raise ValueError("prompt must be valid UTF-8 text") from exc
        if prompt_size > WORKER_MAX_PROMPT_BYTES:
            raise ValueError(
                f"prompt exceeds the {WORKER_MAX_PROMPT_BYTES}-byte worker limit"
            )

        canonical = self.ensure_canonical_conversation()
        store = self._directory._session_store_for_record(current)
        canonical_lease = store.acquire_session_runtime_lease(canonical.session_id)
        try:
            current = self._directory._require_local_coworker_owner(self)
            if current.canonical_session_id != canonical.session_id:
                raise CoworkerDirectoryError("canonical coworker binding changed")
            session_ref = self._ensure_background_run_under_lease(
                store,
                run_id=run_id,
                canonical_session_id=canonical.session_id,
            )
            background, binding = self.load_session(session_ref.session_id)
            if (
                binding != session_ref
                or binding.canonical_session_id != current.canonical_session_id
                or background.session_id != session_ref.session_id
            ):
                raise CoworkerDirectoryError(
                    "background worker session binding changed"
                )
            if background.messages or background.tool_calls:
                raise CoworkerRuntimeUnavailable(
                    "coworker run already contains turn data; refusing replay"
                )

            request_id = str(uuid4())
            state_directory = self._directory._profile_database_directory(current)
            with self.worker_process_environment() as environment:
                _stage_offline_worker_request(
                    environment.working_directory,
                    request_id=request_id,
                    session_id=session_ref.session_id,
                    prompt=prompt,
                )
                manager = SandboxManager(
                    workspace_root=environment.working_directory,
                    network=False,
                    workspace_read_only=True,
                    allow_scoped_fallback=False,
                    backend_preference="docker",
                    docker_image=COWORKER_IMAGE,
                    docker_memory_mb=1_024,
                    docker_cpus=0.5,
                    require_resource_containment=True,
                )
                if (
                    manager.backend_name != "docker"
                    or not manager.has_aggregate_resource_limits()
                ):
                    raise SandboxBackendUnavailable(
                        "offline coworker turns require bounded Docker execution"
                    )
                current = self._directory._require_local_coworker_owner(self)
                if current.canonical_session_id != canonical.session_id:
                    raise CoworkerDirectoryError("canonical coworker binding changed")
                latest_session, latest_binding = self.load_session(
                    session_ref.session_id
                )
                if (
                    latest_binding != session_ref
                    or latest_session.messages
                    or latest_session.tool_calls
                ):
                    raise CoworkerRuntimeUnavailable(
                        "coworker run changed before dispatch; refusing replay"
                    )
                store.update_session_model(session_ref.session_id, WORKER_MODEL)
                self._directory._claim_offline_turn(
                    handle=self,
                    run_id=run_id,
                    session_id=session_ref.session_id,
                    request_id=request_id,
                )
                result = await manager._run_supervised_coworker_worker(
                    cwd=environment.working_directory,
                    timeout=_OFFLINE_TURN_TIMEOUT_SECONDS,
                    state_directory=state_directory,
                )
                if (
                    result.backend_name != "docker"
                    or result.fallback_used
                    or result.exit_code != 0
                    or result.timed_out
                    or result.outcome_unknown
                    or result.output_truncated
                ):
                    raise CoworkerRuntimeUnavailable(
                        "offline coworker worker ended without a known result"
                    )
                response = _parse_offline_worker_response(
                    result.stdout,
                    request_id=request_id,
                    session_id=session_ref.session_id,
                )

            persisted, persisted_binding = self.load_session(session_ref.session_id)
            if (
                persisted_binding != session_ref
                or persisted.session_id != session_ref.session_id
                or persisted.model != WORKER_MODEL
                or len(persisted.messages) != 2
                or tuple(message.role for message in persisted.messages)
                != ("user", "assistant")
                or persisted.messages[0].content != prompt
                or persisted.messages[0].metadata.get("coworker_request_id")
                != request_id
                or persisted.messages[1].content != response
                or persisted.tool_calls
            ):
                raise CoworkerRuntimeUnavailable(
                    "offline coworker transcript did not match its request"
                )
            self._directory._require_local_coworker_owner(self)
            self._directory._complete_offline_turn(
                handle=self,
                run_id=run_id,
                session_id=session_ref.session_id,
                request_id=request_id,
            )
            return CoworkerTurnResult(
                session_ref=session_ref,
                request_id=request_id,
                response=response,
            )
        finally:
            canonical_lease.close()

    @contextmanager
    def worker_process_environment(self) -> Iterator[CoworkerProcessEnvironment]:
        """Yield scrubbed private paths for one fixed worker process.

        The context passes no workspace, database, provider, extension, or
        callback objects through process environment variables. A dedicated
        supervised Docker call may bind the checked session DB separately.
        Keep the context open until the child exits.
        """

        current = self._directory._get_owned(
            owner_id=self.identity.owner_id,
            coworker_id=self.identity.coworker_id,
        )
        if (
            current.profile_name != self.identity.profile_name
            or current.workspace != self.identity.workspace
            or current.canonical_session_id != self.identity.canonical_session_id
            or current.state != "ready"
        ):
            raise CoworkerDirectoryError("coworker process binding changed")

        state_root = self._directory.ash_dir.resolve(strict=True)
        workspace = current.workspace.resolve(strict=True)
        if state_root == workspace or state_root.is_relative_to(workspace):
            raise CoworkerDirectoryError(
                "coworker process storage must be outside the workspace"
            )

        prefix = f".ash-coworker-{current.coworker_id[:8]}-"
        with tempfile.TemporaryDirectory(
            prefix=prefix,
            dir=state_root,
        ) as temporary_root:
            root = Path(temporary_root)
            paths = {
                name: root / name
                for name in (
                    "home",
                    "config",
                    "data",
                    "cache",
                    "state",
                    "run",
                    "tmp",
                    "work",
                )
            }
            for path in paths.values():
                path.mkdir(mode=0o700)
            if os.name == "nt":
                (paths["home"] / "AppData").mkdir(mode=0o700)
                (paths["home"] / "AppData" / "Roaming").mkdir(mode=0o700)
                (paths["home"] / "AppData" / "Local").mkdir(mode=0o700)
            yield CoworkerProcessEnvironment(
                root=root,
                home=paths["home"],
                working_directory=paths["work"],
                temporary_directory=paths["tmp"],
                config_directory=paths["config"],
                data_directory=paths["data"],
                cache_directory=paths["cache"],
                state_directory=paths["state"],
                runtime_directory=paths["run"],
            )

    def start_runtime(self) -> None:
        """Fail closed until all provider/auth and extension roots are scoped."""

        raise CoworkerRuntimeUnavailable(
            "coworker runtime is unavailable until provider credentials and "
            "extension state are isolated"
        )
