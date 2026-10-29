"""Explicit, bounded read-only workspace evidence for Crew model turns."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from ash.core.redaction import redact_known_secrets
from ash.safety.guard import SafetyGuard
from ash.safety.scoped_io import snapshot_scoped_file


MAX_EVIDENCE_FILES = 8
MAX_EVIDENCE_FILE_BYTES = 6 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 8 * 1024
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
class EvidenceProvenance:
    relative_path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class RepositoryEvidence:
    relative_path: str
    sha256: str
    size_bytes: int
    text: str

    @property
    def provenance(self) -> EvidenceProvenance:
        return EvidenceProvenance(
            relative_path=self.relative_path,
            sha256=self.sha256,
            size_bytes=self.size_bytes,
        )


def validate_evidence_paths(paths: Sequence[str]) -> tuple[str, ...]:
    if isinstance(paths, (str, bytes)):
        raise ValueError("evidence paths must be a sequence of workspace-relative paths")
    if len(paths) > MAX_EVIDENCE_FILES:
        raise ValueError(f"at most {MAX_EVIDENCE_FILES} evidence files may be listed")
    return tuple(_validate_evidence_path(path) for path in paths)


def read_explicit_evidence(
    workspace: Path,
    paths: Sequence[str],
    *,
    guard: SafetyGuard | None = None,
) -> tuple[RepositoryEvidence, ...]:
    normalized_paths = validate_evidence_paths(paths)
    guard = guard or SafetyGuard(workspace)
    evidence: list[RepositoryEvidence] = []
    total_bytes = 0
    for relative_path in normalized_paths:
        target, snapshot = snapshot_scoped_file(
            relative_path,
            guard,
            max_bytes=MAX_EVIDENCE_FILE_BYTES,
            require_single_link=True,
        )
        if not snapshot.exists:
            raise ValueError("an explicitly listed evidence file does not exist")
        content = snapshot.content
        total_bytes += len(content)
        if total_bytes > MAX_EVIDENCE_TOTAL_BYTES:
            raise ValueError(
                f"combined evidence exceeds {MAX_EVIDENCE_TOTAL_BYTES} bytes"
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
            RepositoryEvidence(
                relative_path=normalized_relative,
                sha256=snapshot.sha256,
                size_bytes=len(content),
                text=text,
            )
        )
    return tuple(evidence)


def evidence_manifest(items: Sequence[RepositoryEvidence]) -> list[dict[str, str | int]]:
    return [
        {
            "path": item.relative_path,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
            "text": item.text,
        }
        for item in items
    ]


def serialize_evidence(items: Sequence[RepositoryEvidence]) -> str:
    return json.dumps(
        evidence_manifest(items),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _validate_evidence_path(raw_path: str) -> str:
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
        raise ValueError("evidence paths must be non-empty relative paths")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in raw_path
    ):
        raise ValueError("evidence paths cannot contain control or format characters")
    windows_path = PureWindowsPath(raw_path)
    if Path(raw_path).is_absolute() or windows_path.is_absolute() or windows_path.drive:
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
