from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from ash.automation.schedules import parse_datetime, utc_now


MAX_CREW_EVENT_BYTES = 16 * 1024
MAX_CREW_EVENT_PAYLOAD_BYTES = 8 * 1024
_MAX_CREW_EVENT_KEY_BYTES = 128
_EVENT_SKEW = timedelta(minutes=5)
_SOURCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")
_EVENT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,63}\Z")
_SIGNATURE_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class CrewEventError(ValueError):
    """A signed Crew event or its owner-provisioned key file is invalid."""


@dataclass(frozen=True)
class VerifiedCrewEvent:
    source: str
    event_id: str
    issued_at: datetime
    responsibility_id: str
    payload: str

    @property
    def event_key(self) -> str:
        return f"{self.source}:{self.event_id}"


def parse_signed_crew_event(
    envelope: bytes,
    *,
    key_file: Path,
    workspace: Path,
    now: datetime | None = None,
) -> VerifiedCrewEvent:
    """Verify a bounded local HMAC envelope and return only inert event data."""

    if not isinstance(envelope, bytes) or len(envelope) > MAX_CREW_EVENT_BYTES:
        raise CrewEventError("signed Crew event exceeds the input limit")
    try:
        document = json.loads(
            envelope.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise CrewEventError("signed Crew event is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict) or set(document) != {
        "version",
        "source",
        "id",
        "issued_at",
        "responsibility_id",
        "payload",
        "signature",
    }:
        raise CrewEventError("signed Crew event has an unsupported shape")
    if type(document["version"]) is not int or document["version"] != 1:
        raise CrewEventError("signed Crew event version is unsupported")
    source = document["source"]
    event_id = document["id"]
    timestamp = document["issued_at"]
    responsibility_id = document["responsibility_id"]
    payload = document["payload"]
    signature = document["signature"]
    if not isinstance(source, str) or not _SOURCE_PATTERN.fullmatch(source):
        raise CrewEventError("signed Crew event source is invalid")
    if not isinstance(event_id, str) or not _EVENT_ID_PATTERN.fullmatch(event_id):
        raise CrewEventError("signed Crew event ID is invalid")
    if not isinstance(timestamp, str):
        raise CrewEventError("signed Crew event timestamp is invalid")
    if not isinstance(responsibility_id, str):
        raise CrewEventError("signed Crew event responsibility is invalid")
    try:
        parsed_responsibility_id = str(UUID(responsibility_id))
        issued_at = parse_datetime(timestamp, label="event issued_at")
    except ValueError as exc:
        raise CrewEventError("signed Crew event identity or timestamp is invalid") from exc
    if parsed_responsibility_id != responsibility_id:
        raise CrewEventError("signed Crew event responsibility ID is not canonical")
    if not isinstance(payload, str) or "\0" in payload:
        raise CrewEventError("signed Crew event payload is invalid")
    try:
        payload_size = len(payload.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise CrewEventError("signed Crew event payload is invalid") from exc
    if payload_size > MAX_CREW_EVENT_PAYLOAD_BYTES:
        raise CrewEventError("signed Crew event payload exceeds the input limit")
    if not isinstance(signature, str) or not _SIGNATURE_PATTERN.fullmatch(signature):
        raise CrewEventError("signed Crew event signature is invalid")

    current = now or utc_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("event verification time must include a UTC offset")
    if abs(current.astimezone(timezone.utc) - issued_at) > _EVENT_SKEW:
        raise CrewEventError("signed Crew event timestamp is outside the allowed skew")

    key = _read_owner_key(key_file, workspace=workspace)
    signed_fields = {key: value for key, value in document.items() if key != "signature"}
    canonical = json.dumps(
        signed_fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    expected_signature = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise CrewEventError("signed Crew event authentication failed")
    if _payload_contains_signing_key(payload, key):
        raise CrewEventError("signed Crew event payload contains its signing key")
    return VerifiedCrewEvent(
        source=source,
        event_id=event_id,
        issued_at=issued_at,
        responsibility_id=parsed_responsibility_id,
        payload=payload,
    )


def _payload_contains_signing_key(payload: str, key: bytes) -> bool:
    encoded_forms = {
        base64.b64encode(key).decode("ascii"),
        base64.urlsafe_b64encode(key).decode("ascii"),
        base64.urlsafe_b64encode(key).decode("ascii").rstrip("="),
    }
    try:
        decoded_key = key.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        decoded_key = ""
    if decoded_key and decoded_key.isprintable():
        encoded_forms.add(decoded_key)
    if any(value in payload for value in encoded_forms):
        return True
    return key.hex() in payload.casefold()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant {value}")


def _read_owner_key(key_file: Path, *, workspace: Path) -> bytes:
    if not isinstance(key_file, Path):
        raise CrewEventError("signed Crew event key path is invalid")
    if not hasattr(os, "geteuid") or not hasattr(os, "O_NOFOLLOW"):
        raise CrewEventError("signed Crew events require a supported local key-file check")
    try:
        workspace_root = workspace.resolve(strict=True)
        key_path = key_file.expanduser().resolve(strict=True)
        if key_path == workspace_root or workspace_root in key_path.parents:
            raise CrewEventError("signed Crew event key must be outside the workspace")
        descriptor = os.open(
            key_file.expanduser(),
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
    except CrewEventError:
        raise
    except OSError as exc:
        raise CrewEventError("signed Crew event key file is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > _MAX_CREW_EVENT_KEY_BYTES
        ):
            raise CrewEventError("signed Crew event key file permissions are invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            material = stream.read(_MAX_CREW_EVENT_KEY_BYTES + 1)
    finally:
        os.close(descriptor)
    if material.endswith(b"\n"):
        material = material[:-1]
    if len(material) != 64 or any(byte not in b"0123456789abcdef" for byte in material):
        raise CrewEventError("signed Crew event key file must contain 32-byte lowercase hex")
    return bytes.fromhex(material.decode("ascii"))
