"""Versioned public runtime event envelopes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4


EVENT_SCHEMA_VERSION = 1
EVENT_SOURCE = {"type": "runtime", "id": "ash"}
EVENT_METADATA_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "timestamp",
        "source",
        "session_id",
        "turn_id",
        "operation_id",
        "parent_event_id",
    }
)


@dataclass(frozen=True)
class EventContext:
    session_id: str | None = None
    turn_id: str | None = None
    operation_id: str | None = None
    parent_event_id: str | None = None


def envelope_event(
    payload: dict[str, Any],
    *,
    context: EventContext | None = None,
    source: dict[str, str] | None = None,
    event_id_factory: Callable[[], str] | None = None,
    timestamp_factory: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Return a v1 event while preserving all existing event payload fields."""

    event_type = payload.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("runtime event type must be a non-empty string")
    version = payload.get("schema_version")
    if version is not None and version != EVENT_SCHEMA_VERSION:
        raise ValueError(f"unsupported runtime event schema version: {version!r}")
    active_context = context or EventContext()
    metadata = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": payload.get("event_id")
        or (event_id_factory or (lambda: str(uuid4())))(),
        "timestamp": payload.get("timestamp")
        or (timestamp_factory or _utc_timestamp)(),
        "source": dict(payload.get("source") or source or EVENT_SOURCE),
        "session_id": payload.get("session_id", active_context.session_id),
        "turn_id": payload.get("turn_id", active_context.turn_id),
        "operation_id": payload.get("operation_id", active_context.operation_id),
        "parent_event_id": payload.get(
            "parent_event_id", active_context.parent_event_id
        ),
    }
    return {**metadata, **payload}


def event_data(payload: dict[str, Any]) -> dict[str, Any]:
    """Return event-specific data without the type or envelope metadata."""

    return {
        key: value
        for key, value in payload.items()
        if key != "type" and key not in EVENT_METADATA_FIELDS
    }


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
