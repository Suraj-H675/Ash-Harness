"""Native command path for explicit, read-only coworker turns."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from ash.core.redaction import redact_known_secrets
from ash.core.session import SessionStorageError
from ash.coworkers import (
    Coworker,
    CoworkerDirectory,
    CoworkerDirectoryError,
    CoworkerNotFound,
    CoworkerRuntimeUnavailable,
    CoworkerTurnRequest,
)
from ash.exceptions import AshError
from ash.safety.guard import SafetyGuard, SafetyViolation
from ash.safety.scoped_io import ScopedIOError, snapshot_scoped_file
from ash.ui.safe_text import terminal_safe_text


_MAX_EVIDENCE_FILES = 8
_MAX_EVIDENCE_FILE_BYTES = 6 * 1024
_MAX_EVIDENCE_TOTAL_BYTES = 8 * 1024
_MAX_HISTORY_LIMIT = 100
_CREDENTIAL_DIRECTORIES = frozenset(
    {
        ".aws",
        ".azure",
        ".config",
        ".docker",
        ".git",
        ".gcloud",
        ".gnupg",
        ".kube",
        ".secrets",
        ".ssh",
        "credentials",
        "secrets",
    }
)
_SENSITIVE_PATH_COMPONENT = re.compile(
    r"(?:^|[._-])(?:credential|credentials|secret|secrets|password|passwords|"
    r"token|private[_-]?key|id_rsa|id_ed25519|id_ecdsa|id_dsa|"
    r"access[_-]?token|auth[_-]?token|api[_-]?key)(?:$|[._-])",
    re.IGNORECASE,
)
_SENSITIVE_FILE_SUFFIXES = frozenset(
    {".pem", ".key", ".p12", ".pfx", ".ppk", ".keystore", ".jks"}
)
_SENSITIVE_FILE_NAMES = frozenset(
    {".netrc", ".npmrc", ".pypirc", ".dockerconfigjson"}
)


@dataclass(frozen=True)
class _RepositoryEvidence:
    relative_path: str
    sha256: str
    size_bytes: int
    text: str


def run_crew_command(args: argparse.Namespace) -> int:
    if not _current_uid_available():
        _write_error("Blocked. The current local owner UID cannot be verified.")
        return 2
    action = args.crew_action
    if action == "create":
        return _create(args)
    if action == "list":
        return _list(args)
    if action == "ask":
        return _ask(args)
    if action == "history":
        return _history(args)
    _write_error("Unsupported Crew command.")
    return 2


def _create(args: argparse.Namespace) -> int:
    try:
        identity = CoworkerDirectory.open().create(
            owner_id=args.owner,
            display_name=args.name,
            profile_name=args.crew_profile,
            workspace=args.workspace,
        )
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        CoworkerRuntimeUnavailable,
        AshError,
        OSError,
        SafetyViolation,
        SessionStorageError,
        ValueError,
    ) as exc:
        _write_error(f"Coworker could not be created: {_safe_error(exc)}")
        return 2
    except Exception as exc:  # noqa: BLE001 - keep CLI failures secret-free
        _write_error(f"Coworker could not be created ({type(exc).__name__}).")
        return 2

    _write_line("Coworker ready.")
    _write_identity(identity)
    return 0


def _list(args: argparse.Namespace) -> int:
    ash_dir = Path.home() / ".ash"
    if not ash_dir.exists() and not ash_dir.is_symlink():
        _write_line("No coworkers found for this owner label.")
        return 0
    try:
        coworkers = CoworkerDirectory.open().list_for_local_owner(
            owner_id=args.owner
        )
    except (
        CoworkerDirectoryError,
        CoworkerRuntimeUnavailable,
        OSError,
        SafetyViolation,
        SessionStorageError,
        ValueError,
    ) as exc:
        _write_error(f"Coworker list is unavailable: {_safe_error(exc)}")
        return 2
    except Exception as exc:  # noqa: BLE001 - keep CLI failures secret-free
        _write_error(f"Coworker list is unavailable ({type(exc).__name__}).")
        return 2
    if not coworkers:
        _write_line("No coworkers found for this owner label.")
        return 0
    for identity in coworkers:
        _write_identity(identity)
    return 0


def _ask(args: argparse.Namespace) -> int:
    turn_id = args.turn_id or str(uuid4())
    _write_line(f"Turn ID: {_safe_text(turn_id, single_line=True)}")
    try:
        handle = CoworkerDirectory.open().open_coworker(
            owner_id=args.owner,
            coworker_id=args.coworker_id,
        )
        identity = handle.require_local_owner()
        evidence_files = _read_explicit_evidence(
            identity.workspace,
            tuple(args.evidence),
        )
        evidence = _serialize_evidence(evidence_files)
        request = CoworkerTurnRequest(
            turn_id=turn_id,
            question=args.question,
            evidence=evidence,
        )
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        CoworkerRuntimeUnavailable,
        AshError,
        OSError,
        SafetyViolation,
        SessionStorageError,
        ScopedIOError,
        ValueError,
    ) as exc:
        _write_error(f"Blocked before model dispatch: {_safe_error(exc)}")
        return 2
    except Exception as exc:  # noqa: BLE001 - no pre-dispatch details are needed
        _write_error(
            "Blocked before model dispatch "
            f"({type(exc).__name__}); no provider request was sent."
        )
        return 2

    for item in evidence_files:
        _write_line(
            "Evidence: "
            f"{_safe_text(item.relative_path, single_line=True)} "
            f"({item.size_bytes} bytes, SHA-256 {item.sha256})"
        )
    _write_line(
        f"Coworker ID: {_safe_text(identity.coworker_id, single_line=True)}"
    )
    _write_line("Status: attempting one read-only model turn.")
    try:
        result = asyncio.run(handle.run_readonly_turn(request))
    except CoworkerRuntimeUnavailable as exc:
        message = _safe_error(exc)
        if "configured profile provider is unavailable" in str(exc):
            _write_error(f"Blocked. {message}. No provider request was sent.")
        elif "already used" in str(exc) or "will not be replayed" in str(exc):
            _write_error(
                f"Turn is non-replayable. {message}. Inspect `ash crew history` before another ask."
            )
        else:
            _write_error(
                f"Turn failed or its outcome is unknown. {message}. "
                "Do not reuse this turn ID; inspect `ash crew history`."
            )
        return 2
    except ValueError:
        _write_error(
            "Turn failed or its outcome is unknown (ValueError). "
            "Do not reuse this turn ID; inspect `ash crew history`."
        )
        return 2
    except KeyboardInterrupt:
        _write_error(
            "Turn interrupted. Its outcome may be unknown. Do not reuse this turn ID; "
            "inspect `ash crew history`."
        )
        return 130
    except Exception as exc:  # noqa: BLE001 - never print provider or filesystem details
        _write_error(
            "Turn failed or its outcome is unknown "
            f"({type(exc).__name__}). Do not reuse this turn ID; inspect `ash crew history`."
        )
        return 2

    _write_line("Status: completed.")
    _write_line(f"Turn ID: {_safe_text(result.turn_id, single_line=True)}")
    _write_line(f"Run ID: {result.session_ref.run_id}")
    _write_line(f"Session ID: {result.session_ref.session_id}")
    _write_line(f"Model: {_safe_text(result.model, single_line=True)}")
    _write_line("Response:")
    _write_line(_safe_text(result.response))
    _write_line("Proposal only. No files were changed.")
    return 0


def _history(args: argparse.Namespace) -> int:
    try:
        handle = CoworkerDirectory.open().open_coworker(
            owner_id=args.owner,
            coworker_id=args.coworker_id,
        )
        handle.require_local_owner()
        entries = handle.list_readonly_turn_history(limit=args.limit)
    except (
        CoworkerDirectoryError,
        CoworkerNotFound,
        CoworkerRuntimeUnavailable,
        OSError,
        SafetyViolation,
        SessionStorageError,
        ValueError,
    ) as exc:
        _write_error(f"Coworker history is unavailable: {_safe_error(exc)}")
        return 2
    except Exception as exc:  # noqa: BLE001 - keep CLI failures secret-free
        _write_error(f"Coworker history is unavailable ({type(exc).__name__}).")
        return 2
    if not entries:
        _write_line("No model turns recorded for this coworker.")
        return 0

    for entry in entries:
        _write_line(
            f"Status: {entry.status}"
            if entry.status == "completed"
            else "Status: outcome unknown; claimed turns cannot be replayed."
        )
        _write_line(f"Run ID: {entry.session_ref.run_id}")
        _write_line(f"Session ID: {entry.session_ref.session_id}")
        _write_line(f"Created: {_safe_text(entry.created_at, single_line=True)}")
        if entry.completed_at is not None:
            _write_line(
                f"Completed: {_safe_text(entry.completed_at, single_line=True)}"
            )
        if entry.model is not None:
            _write_line(f"Model: {_safe_text(entry.model, single_line=True)}")
        if entry.response is not None:
            _write_line("Response:")
            _write_line(_safe_text(entry.response))
            _write_line("Proposal only. No files were changed.")
        _write_line("")
    return 0


def _read_explicit_evidence(
    workspace: Path,
    paths: Sequence[str],
) -> tuple[_RepositoryEvidence, ...]:
    if len(paths) > _MAX_EVIDENCE_FILES:
        raise ValueError(f"at most {_MAX_EVIDENCE_FILES} evidence files may be listed")
    guard = SafetyGuard(workspace)
    evidence: list[_RepositoryEvidence] = []
    total_bytes = 0
    for raw_path in paths:
        relative_path = _validate_evidence_path(raw_path)
        target, snapshot = snapshot_scoped_file(
            relative_path,
            guard,
            max_bytes=_MAX_EVIDENCE_FILE_BYTES,
            require_single_link=True,
        )
        if not snapshot.exists:
            raise ValueError("an explicitly listed evidence file does not exist")
        content = snapshot.content
        total_bytes += len(content)
        if total_bytes > _MAX_EVIDENCE_TOTAL_BYTES:
            raise ValueError(
                f"combined evidence exceeds {_MAX_EVIDENCE_TOTAL_BYTES} bytes"
            )
        if b"\0" in content:
            raise ValueError("evidence files must contain UTF-8 text, not binary data")
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("evidence files must contain UTF-8 text") from exc
        if redact_known_secrets(text) != text:
            raise ValueError("evidence appears to contain a known secret marker")
        if any(
            unicodedata.category(character) == "Cc"
            and character not in {"\n", "\r", "\t"}
            for character in text
        ):
            raise ValueError("evidence files contain binary control data")
        normalized_relative = target.relative_to(guard.project_root).as_posix()
        evidence.append(
            _RepositoryEvidence(
                relative_path=normalized_relative,
                sha256=snapshot.sha256,
                size_bytes=len(content),
                text=text,
            )
        )
    return tuple(evidence)


def _validate_evidence_path(raw_path: str) -> str:
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
        raise ValueError("evidence paths must be non-empty relative paths")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in raw_path):
        raise ValueError("evidence paths cannot contain control or format characters")
    windows_path = PureWindowsPath(raw_path)
    if (
        Path(raw_path).is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
    ):
        raise ValueError("evidence paths must be workspace-relative")
    components = raw_path.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("evidence paths cannot contain empty or traversal components")
    if any(_is_sensitive_path_component(component) for component in components):
        raise ValueError("evidence path is in a hidden credential area or sensitive file")
    return PurePosixPath(*components).as_posix()


def _is_sensitive_path_component(component: str) -> bool:
    normalized = component.casefold()
    return (
        normalized in _CREDENTIAL_DIRECTORIES
        or normalized.startswith(".env")
        or normalized in _SENSITIVE_FILE_NAMES
        or Path(normalized).suffix in _SENSITIVE_FILE_SUFFIXES
        or _SENSITIVE_PATH_COMPONENT.search(normalized) is not None
    )


def _current_uid_available() -> bool:
    get_effective_uid = getattr(os, "geteuid", None)
    if not callable(get_effective_uid):
        return False
    try:
        return type(get_effective_uid()) is int
    except OSError:
        return False


def _serialize_evidence(items: Sequence[_RepositoryEvidence]) -> str:
    return json.dumps(
        [
            {
                "path": item.relative_path,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "text": item.text,
            }
            for item in items
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _write_identity(identity: Coworker) -> None:
    _write_line(f"ID: {_safe_text(identity.coworker_id, single_line=True)}")
    _write_line(f"Name: {_safe_text(identity.display_name, single_line=True)}")
    _write_line(f"Owner: {_safe_text(identity.owner_id, single_line=True)}")
    _write_line(f"Profile: {_safe_text(identity.profile_name, single_line=True)}")
    _write_line(f"Workspace: {_safe_text(str(identity.workspace), single_line=True)}")
    _write_line(f"State: {_safe_text(identity.state, single_line=True)}")


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, CoworkerNotFound):
        return "no coworker matches the supplied owner label and ID"
    if isinstance(exc, (ScopedIOError, SafetyViolation, OSError)):
        return "the selected workspace file could not be read safely"
    message = str(exc)
    if not message:
        return type(exc).__name__
    return _safe_text(message, single_line=True)


def _safe_text(value: str, *, single_line: bool = False) -> str:
    return terminal_safe_text(
        redact_known_secrets(value),
        single_line=single_line,
    )


def _write_line(value: str) -> None:
    print(value)


def _write_error(value: str) -> None:
    print(f"Error: {value}", file=sys.stderr)
