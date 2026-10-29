from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from ash.crew_events import CrewEventError, parse_signed_crew_event


def _signed_event(
    key: bytes,
    *,
    now: datetime,
    responsibility_id: str,
    payload: str = "CI run 42 failed in test_authentication.",
    source: str = "github-actions",
    event_id: str = "run-42",
) -> bytes:
    fields: dict[str, object] = {
        "version": 1,
        "source": source,
        "id": event_id,
        "issued_at": now.isoformat(),
        "responsibility_id": responsibility_id,
        "payload": payload,
    }
    canonical = json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    document = {**fields, "signature": hmac.new(key, canonical, hashlib.sha256).hexdigest()}
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _write_key(path: Path, key: bytes) -> Path:
    path.write_text(key.hex() + "\n", encoding="ascii")
    path.chmod(0o600)
    return path


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or not hasattr(os, "O_NOFOLLOW"),
    reason="signed Crew events require POSIX owner and no-follow checks",
)
def test_signed_crew_event_verifies_canonical_payload_and_binds_target(
    tmp_path: Path,
) -> None:
    key = bytes(range(32))
    now = datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    key_file = _write_key(tmp_path / "crew.key", key)
    responsibility_id = str(uuid4())
    envelope = _signed_event(
        key,
        now=now,
        responsibility_id=responsibility_id,
        payload="CI run 42 failed in test_authentication.",
    )

    event = parse_signed_crew_event(
        envelope,
        key_file=key_file,
        workspace=workspace,
        now=now,
    )

    assert event.source == "github-actions"
    assert event.event_id == "run-42"
    assert event.event_key == "github-actions:run-42"
    assert event.responsibility_id == responsibility_id
    assert event.payload == "CI run 42 failed in test_authentication."
    assert not hasattr(event, "signature")
    assert not hasattr(event, "key")


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or not hasattr(os, "O_NOFOLLOW"),
    reason="signed Crew events require POSIX owner and no-follow checks",
)
def test_signed_event_with_non_utf8_signing_key_does_not_reject_inert_payload(
    tmp_path: Path,
) -> None:
    key = bytes([0xFF]) * 32
    now = datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    key_file = _write_key(tmp_path / "crew.key", key)
    envelope = _signed_event(
        key,
        now=now,
        responsibility_id=str(uuid4()),
        payload="Synthetic CI failure: expected 2 checks but observed 1.",
    )

    event = parse_signed_crew_event(
        envelope,
        key_file=key_file,
        workspace=workspace,
        now=now,
    )

    assert event.payload == "Synthetic CI failure: expected 2 checks but observed 1."


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or not hasattr(os, "O_NOFOLLOW"),
    reason="signed Crew events require POSIX owner and no-follow checks",
)
def test_signed_crew_event_rejects_payload_that_contains_its_signing_key(
    tmp_path: Path,
) -> None:
    key = bytes(range(32))
    now = datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    key_file = _write_key(tmp_path / "crew.key", key)
    envelope = _signed_event(
        key,
        now=now,
        responsibility_id=str(uuid4()),
        payload=f"Configuration contained key {key.hex()}.",
    )

    with pytest.raises(CrewEventError, match="contains its signing key"):
        parse_signed_crew_event(
            envelope,
            key_file=key_file,
            workspace=workspace,
            now=now,
        )


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or not hasattr(os, "O_NOFOLLOW"),
    reason="signed Crew events require POSIX owner and no-follow checks",
)
@pytest.mark.parametrize("minutes", [-6, 6])
def test_signed_crew_event_rejects_expired_or_future_timestamp(
    tmp_path: Path,
    minutes: int,
) -> None:
    key = bytes(range(32))
    now = datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    key_file = _write_key(tmp_path / "crew.key", key)
    envelope = _signed_event(
        key,
        now=now + timedelta(minutes=minutes),
        responsibility_id=str(uuid4()),
    )

    with pytest.raises(CrewEventError, match="timestamp"):
        parse_signed_crew_event(
            envelope,
            key_file=key_file,
            workspace=workspace,
            now=now,
        )


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or not hasattr(os, "O_NOFOLLOW"),
    reason="signed Crew events require POSIX owner and no-follow checks",
)
@pytest.mark.parametrize(
    "mutation",
    ["bad_signature", "extra_field", "duplicate_key", "bad_id", "oversized_payload"],
)
def test_signed_crew_event_rejects_untrusted_or_malformed_input(
    tmp_path: Path,
    mutation: str,
) -> None:
    key = bytes(range(32))
    now = datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    key_file = _write_key(tmp_path / "crew.key", key)
    responsibility_id = str(uuid4())
    envelope = _signed_event(
        key,
        now=now,
        responsibility_id=responsibility_id,
    )
    document = json.loads(envelope)
    if mutation == "bad_signature":
        document["signature"] = "0" * 64
        envelope = json.dumps(document).encode("utf-8")
    elif mutation == "extra_field":
        document["model"] = "operator-selected-model"
        envelope = json.dumps(document).encode("utf-8")
    elif mutation == "duplicate_key":
        envelope = envelope.replace(b'"version":1', b'"version":1,"version":1', 1)
    elif mutation == "bad_id":
        document["id"] = "../../workspace/file"
        envelope = json.dumps(document).encode("utf-8")
    else:
        envelope = _signed_event(
            key,
            now=now,
            responsibility_id=responsibility_id,
            payload="x" * (8 * 1024 + 1),
        )

    with pytest.raises(CrewEventError):
        parse_signed_crew_event(
            envelope,
            key_file=key_file,
            workspace=workspace,
            now=now,
        )


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or not hasattr(os, "O_NOFOLLOW"),
    reason="signed Crew events require POSIX owner and no-follow checks",
)
@pytest.mark.parametrize("key_kind", ["weak_mode", "symlink", "hardlink", "workspace"])
def test_signed_crew_event_rejects_unsafe_key_files(
    tmp_path: Path,
    key_kind: str,
) -> None:
    key = bytes(range(32))
    now = datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_key = _write_key(tmp_path / "outside.key", key)
    if key_kind == "weak_mode":
        outside_key.chmod(0o640)
        key_file = outside_key
    elif key_kind == "symlink":
        key_file = tmp_path / "linked.key"
        key_file.symlink_to(outside_key)
    elif key_kind == "hardlink":
        key_file = tmp_path / "linked.key"
        os.link(outside_key, key_file)
    else:
        key_file = _write_key(workspace / "crew.key", key)
    envelope = _signed_event(
        key,
        now=now,
        responsibility_id=str(uuid4()),
    )

    with pytest.raises(CrewEventError, match="key"):
        parse_signed_crew_event(
            envelope,
            key_file=key_file,
            workspace=workspace,
            now=now,
        )
