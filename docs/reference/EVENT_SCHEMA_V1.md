# Ash Runtime Event Schema v1

Ash exposes one additive event envelope across `--output-format stream-json`,
the Python SDK, and HTTP server-sent events. JSON-RPC clients discover the
current event schema through `initialize.capabilities.event_schema_version`.
The runtime batches redacted events into the durable session database so clients
can replay them after disconnects or process restarts.

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
`tool.completed` when an invocation returns a known result (including an
unsuccessful result), `tool.error` for execution errors or ambiguous outcomes,
or `tool.skipped` when pre-execution middleware stopped the call before dispatch.
Consumers must inspect `success` rather than treating `tool.completed` as proof
of success. For example, a `run_command` that exits with status 23 emits
`tool.completed` with `success: false` and `exit_code: 23`.

Every terminal `tool.error` includes these stable fields:

| Field | Type | Meaning |
|---|---|---|
| `dispatched` | boolean | Ash persisted that tool dispatch began. |
| `ambiguous` | boolean | The tool may have performed an external side effect before its outcome was lost. |
| `replayed` | boolean | Always `false` for harness-owned tool dispatch. |
| `replay_policy` | string | Current automatic replay policy, `never`. |
| `recovered` | boolean, optional | Present and true when crash/cancellation recovery produced this terminal state. |

When a `run_command` result has a known process status, its terminal event also
includes the integer `exit_code`. Ash omits that field when the command timed
out or its outcome is unknown.

Recovery emits one terminal `tool.error` per unfinished call before the
aggregate `session.recovery` event. A recovered record with `dispatched: false`
is a confirmed unstarted call and therefore has `ambiguous: false`.

## Model request lifecycle

Every provider completion attempt emits:

- `model.request.started` with provider/model identity, attempt number,
  message/tool counts, and whether the native tool protocol is active;
- exactly one of `model.request.completed`, `model.request.error`, or
  `model.request.cancelled` for that request operation ID.

Ash also emits a content-free `model.request.first_output` when the first
retained provider output arrives. When a request remains without retained
output for 30 seconds, it emits at most one `model.request.waiting` event for
that attempt with `elapsed_seconds` and `attempt`, correlated to the same
operation ID. Empty stream/transport chunks are not counted as output. The
interactive terminal shows a non-error waiting status that clears on the
first output, a retry, cancellation, or completion; screen-reader mode gets
the same semantic update. The notice neither cancels nor retries a request,
and cannot determine whether the delay is provider inference, queueing, or
transport. The default request timeout and reasoning effort are unaffected.

Completed events include provider-reported or estimated token/cache usage and
the normalized stop category. Error events include bounded operational
classification such as HTTP status, retryability, whether output was already
emitted, and the exception class name. Provider error text is not part of this
stable lifecycle contract.

These events make retry/failover behavior observable without requiring
consumers to infer provider attempts from assistant deltas.

## Provider input budget telemetry

`context.usage` retains `current` (the locally estimated token count for
the next provider input) and `maximum` (the effective input-token ceiling).
It also includes optional content-free accounting fields:
`system_tokens`, `tool_schema_tokens`, `history_tokens`,
`repo_map_tokens`, `memory_tokens`, `history_message_count`,
`tool_message_count`, and `tool_message_chars`.

These measurements describe **locally assembled input**, not provider-billed
tokens or a task's completion quality. Tool-message character counts include
the model-visible serialized results, not file paths or source snippets.
Actual `model.request.completed.prompt_tokens` remains the authoritative
provider-reported number when available. Consumers must not sum overlapping
per-request input values and label them a single context-window size.

## Turn completion identity

When a turn fails or is cancelled after a successful Ash file-edit operation,
`turn.error` and `turn.cancelled` include `edited_files`: the count of distinct
file paths whose completed edits Ash could inspect after writing. The count is
content-free: it contains no paths, source code, or command output. It is not a
claim that the task was verified or that the diff still contains those edits;
it does not count changes made by arbitrary shell commands or uncertain tool
outcomes. A zero count cannot prove the workspace is unchanged. Interactive
and one-shot CLI users receive an actionable review-and-test warning when the
count is positive, and JSON errors expose the same count and remedy. Recovery
continues to preserve known completed work and never blindly replay uncertain
effects.

`turn.completed` means the Ash turn reached its terminal boundary, not that
the user's task passed verification. In particular, a provider may produce a
normal terminal response with empty text. Automation must inspect the response,
`stop_reason`, and relevant tool outcomes or independent verification evidence
before claiming that the requested work is complete.

For one-shot `ash -p` invocations, the process exits with code **1** when
`stop_reason` is `iteration_limit`, `token_budget`, or `circuit_breaker`.
The terminal `turn.completed` event and saved session are still emitted:
the process ended normally but did **not** finish the turn. An ordinary
`stop_reason=completed` exits with code 0 unless another error occurs.
Neither code 0 nor `turn.completed` proves that the user's requested task
passed independent tests or review. Interactive sessions retain their
existing process-exit behavior.

`turn.completed` preserves the legacy model-only `model` field and may also
include `model_id`, the canonical `provider/model` identity of the provider that
actually served the turn. Consumers that need route-aware attribution should
prefer `model_id` when present and fall back to `model` for older producers.
For failover turns, `model_id` identifies the provider/model that produced the
successful completion rather than the originally configured primary route.

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

External runtime observers such as Ash's OpenTelemetry adapter receive a
stricter, content-free projection of the live event stream. That projection
retains envelope IDs and allowlisted operational fields but removes prompts,
assistant output, tool arguments/results, file paths, denial/error text, and
other content-bearing values before observer code runs.
