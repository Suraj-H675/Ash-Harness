# Ash Runtime Event Schema v1

Ash exposes one additive event envelope across `--output-format stream-json`,
the Python SDK, and HTTP server-sent events. JSON-RPC clients discover the
current event schema through `initialize.capabilities.event_schema_version`.
The runtime batches redacted events into SQLite schema v8 so clients can replay
them after disconnects or process restarts.

## Wire fields

Every event contains:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | Event contract version. Version 1 is the only accepted value. |
| `event_id` | string | Unique event UUID. |
| `timestamp` | string | UTC ISO 8601 emission time. |
| `source` | object | Producer identity. Built-in runtime events use `{"type":"runtime","id":"ash"}`. |
| `session_id` | string or null | Owning session when one exists. |
| `turn_id` | string or null | Owning turn when one exists. |
| `operation_id` | string or null | Tool call or other operation identifier. |
| `parent_event_id` | string or null | Parent event identifier when causal lineage is available. |
| `type` | string | Stable dotted event name such as `tool.started`. |

Event-specific fields remain at the top level. For example:

```json
{
  "schema_version": 1,
  "event_id": "a4ee088c-bfad-4a68-a36d-9b8266b6f364",
  "timestamp": "2026-07-10T08:00:00+00:00",
  "source": {"type": "runtime", "id": "ash"},
  "session_id": "session-id",
  "turn_id": "turn-id",
  "operation_id": "call-id",
  "parent_event_id": null,
  "type": "tool.started",
  "call_id": "call-id",
  "tool": "read_file",
  "arguments": {"file_path": "README.md"}
}
```

## Tool terminal events

`tool.started` is emitted only after Ash has durably recorded that dispatch is
beginning. Exactly one terminal event then follows for that call:
`tool.completed` for a completed invocation, `tool.error` for a failure or
ambiguous result, or `tool.skipped` when pre-execution middleware stopped the
call before dispatch.

Every terminal `tool.error` includes these stable fields:

| Field | Type | Meaning |
|---|---|---|
| `dispatched` | boolean | Ash persisted that tool dispatch began. |
| `ambiguous` | boolean | The tool may have performed an external side effect before its outcome was lost. |
| `replayed` | boolean | Always `false` for harness-owned tool dispatch. |
| `replay_policy` | string | Current automatic replay policy, `never`. |
| `recovered` | boolean, optional | Present and true when crash/cancellation recovery produced this terminal state. |

Recovery emits one terminal `tool.error` per unfinished call before the
aggregate `session.recovery` event. A recovered record with `dispatched: false`
is a confirmed unstarted call and therefore has `ambiguous: false`.

## Compatibility

Within schema version 1, Ash may add event types or optional event-specific
fields. It will not remove fields, change their types, or change the meaning of
an existing event type. Consumers must ignore unknown event types and fields.
A breaking change requires a new `schema_version` and an explicit migration.

The SDK keeps event-specific values in `AshEvent.data` and exposes envelope
metadata as typed attributes. `AshEvent.to_wire()` reconstructs the wire event.
For SSE, the event name is sent in the `event:` line and the remaining envelope
is sent as JSON in the `data:` line.

## Replay

- Python: `client.events(session_id, after_sequence=cursor, limit=1000)`
- HTTP: `GET /v1/sessions/{session_id}/events?after_sequence={cursor}`
- JSON-RPC: `event/list` with `session_id`, `after_sequence`, `turn_id`, and
  `limit` parameters

Replay results contain a monotonically increasing `sequence`, the canonical
event, and a `next_sequence` cursor at protocol boundaries. Cursors are
exclusive. Repeating a request is safe because event IDs are unique and event
insertion is idempotent. Persisted event payloads pass through Ash's secret
redactor; live UI delivery is unchanged.
