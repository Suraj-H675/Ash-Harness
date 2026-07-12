from datetime import datetime
from uuid import UUID

import pytest

from ash.core.events import (
    EVENT_SCHEMA_VERSION,
    EventContext,
    envelope_event,
    event_data,
)


def test_event_envelope_has_versioned_identity_and_context() -> None:
    event = envelope_event(
        {"type": "tool.started", "tool": "read_file"},
        context=EventContext(
            session_id="session-1",
            turn_id="turn-1",
            operation_id="call-1",
        ),
        event_id_factory=lambda: "event-1",
        timestamp_factory=lambda: "2026-07-10T00:00:00+00:00",
    )

    assert event == {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": "event-1",
        "timestamp": "2026-07-10T00:00:00+00:00",
        "source": {"type": "runtime", "id": "ash"},
        "session_id": "session-1",
        "turn_id": "turn-1",
        "operation_id": "call-1",
        "parent_event_id": None,
        "type": "tool.started",
        "tool": "read_file",
    }


def test_event_envelope_is_idempotent_and_payload_is_extractable() -> None:
    first = envelope_event({"type": "assistant.delta", "text": "hello"})
    second = envelope_event(first)

    assert second == first
    assert event_data(second) == {"text": "hello"}
    assert UUID(second["event_id"])
    assert datetime.fromisoformat(second["timestamp"])


def test_event_envelope_rejects_unknown_schema_versions() -> None:
    with pytest.raises(ValueError, match="unsupported runtime event schema"):
        envelope_event({"type": "turn.started", "schema_version": 999})


def test_event_envelope_requires_a_type() -> None:
    with pytest.raises(ValueError, match="event type"):
        envelope_event({})
