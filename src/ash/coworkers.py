"""Owner-bound coworker identities over Ash profiles and session storage.

This module establishes durable local identity and session associations only.
It deliberately does not start provider runtimes: current provider credentials,
ChatGPT authentication, and extension discovery still have process/global-home
readers that are not safe for concurrent coworker execution.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator
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


_COWORKER_DIRECTORY_SCHEMA_VERSION = 1
_MAX_OWNER_ID_CHARS = 128
_MAX_COWORKER_NAME_CHARS = 128


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
        display_name=str(row["display_name"]),
        profile_name=str(row["profile_name"]),
        workspace=Path(str(row["workspace_path"])),
        canonical_session_id=str(row["canonical_session_id"]),
        state=str(row["state"]),  # type: ignore[arg-type]
    )


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
                if version != _COWORKER_DIRECTORY_SCHEMA_VERSION:
                    raise CoworkerDirectoryError(
                        "coworker directory schema version is unsupported: "
                        f"{version}"
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
        expected_db = Path(
            os.path.abspath(
                profile_directory(record.profile_name, ash_dir=self.ash_dir) / "db"
            )
        )
        if config.db_directory != expected_db:
            raise CoworkerDirectoryError("bound coworker database path changed")
        return SessionStore(config.db_directory / "sessions.db")

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
                            coworker_id, owner_id, display_name, profile_name,
                            workspace_path, canonical_session_id, state,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'provisioning', ?, ?)
                        """,
                        (
                            coworker_id,
                            owner,
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
        config = self.effective_config()
        return self._directory._session_store(self.identity, config)

    def _finish_provisioning(self) -> CoworkerSessionRef:
        record = self.identity
        config = self.effective_config()
        store = self._directory._session_store(record, config)
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
            # Revalidate the canonical session while holding Ash's normal lease.
            self._require_canonical()
            binding = store.ensure_coworker_run(
                owner_id=self.identity.owner_id,
                coworker_id=self.identity.coworker_id,
                run_id=run_id,
                canonical_session_id=canonical.session_id,
                project_path=self.identity.workspace,
            )
        finally:
            lease.close()
        return _session_ref(binding)

    def load_session(self, session_id: str) -> tuple[Session, CoworkerSessionRef]:
        """Load only session IDs in this owner/coworker association set."""

        session, binding = self._store().load_coworker_session(
            owner_id=self.identity.owner_id,
            coworker_id=self.identity.coworker_id,
            session_id=session_id,
            project_path=self.identity.workspace,
        )
        return session, _session_ref(binding)

    @contextmanager
    def worker_process_environment(self) -> Iterator[CoworkerProcessEnvironment]:
        """Yield scrubbed default paths for one child; runtime remains unavailable.

        The context passes no workspace, database, provider, extension, or
        callback objects. It does not restrict the child's OS permissions.
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
