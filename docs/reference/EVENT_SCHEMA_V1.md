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

After truncating live `run_command` output, Ash can emit `tool.output.tail`
before the terminal event. It has the same operation ID, `stream`, and `delta`
fields as `tool.output`. Each tail contains up to 12,000 redacted characters
from the end of the omitted output for that stream. It can include a test
summary or other diagnostics. Use the terminal event to check the outcome.

Recovery emits one terminal `tool.error` per unfinished call before the
aggregate `session.recovery` event. A recovered record with `dispatched: false`
is a confirmed unstarted call and therefore has `ambiguous: false`.

## Model request lifecycle

Every provider completion attempt emits:

- `model.request.started` with provider/model identity, attempt number,
  message/tool counts, and whether the native tool protocol is active;
- exactly one of `model.request.completed`, `model.request.error`, or
  `model.request.cancelled` for that request operation ID.

For the main assembled request path, each successful `context.usage` event has
an opaque `context_id` and `assembly_purpose: "request"`. Its exact messages
carry that ID through each retry. A request attempt keeps its own `operation_id`,
so retries share a context ID and have distinct operation IDs. Preview-only
context builds use `assembly_purpose: "preview"` and are never joined to a
request. Other request paths may have no matching context assembly; consumers
must show the association as unavailable instead of matching by event order.

Request lifecycle events may include `context_id`, `requested_route`, and
`effective_route`. Routes use the provider's safe provider/model identity. The
effective route is `"unknown"` before dispatch and on failed or cancelled
attempts. On completion, failover reports the route that served the request;
existing credential-pool routing behavior is unchanged. Context-assembly and
planner events omit credential profile labels. The
`context.usage.estimator_route` is `"unknown"` for composite estimators such
as failover. `estimator_count_mode` is `"unknown"` unless an adapter can
honestly identify its counting mode. Route equality indicates provider/model
route parity only; it does not establish tokenizer or context accounting
parity.

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

For the configured context-assembly path, `context.usage.current` is Ash's
local input estimate and `maximum` is the effective input ceiling. An
unconfigured message build may report both as null with
`estimate_source: "unavailable"`.
The event also includes optional content-free accounting fields:
`combined_limit`, `response_limit`, and `input_limit` record the configured
request ceilings; `message_estimate_tokens` and `native_tool_schema_tokens`
explain `current`. `serialized_message_tokens` is the local count of cleaned
JSON messages, including their message envelope. `image_count` and
`image_estimate_tokens` identify images whose bytes were omitted and charged
with Ash's fixed image estimate. `opaque_transport_estimate_enabled` and
`opaque_transport_estimate_tokens` identify the eligible opaque reasoning
payload estimate when that route-specific heuristic is enabled. The message
estimate is the sum of those three pieces; native schemas are added for
`current`. The message estimate includes the serialized JSON message envelope,
so no separate envelope-token count is implied. Section counts below are
attribution views and must not be added again to the message estimate. These
remain Ash estimates, not provider capacity measurements.

The event retains the section fields `system_tokens`, `tool_schema_tokens`,
`history_tokens`, `repo_map_tokens`, and `memory_tokens`, and adds character
counts for assistant text, assistant tool-call arguments, and serialized tool
results. Counts are numeric and content-free. Canonical object arguments are
measured as local JSON, string arguments as supplied text; unsupported argument
shapes and non-string result messages are counted separately. These character
counts are not additive token charges.
`estimate_source`, `estimator_route`, and `estimator_count_mode` identify the
local counter provenance; `unknown` means the implementation could not be
identified safely. Older events may omit these fields.

When local admission succeeds only after deferring old optional native
tool declarations, the admitted `context.usage` event also records
`deferred_catalog_omitted_count` and `original_tool_schema_tokens`.
The actual `native_tool_schema_tokens` remains the reduced declaration
estimate. The failed larger assembly is not a dispatched model request;
no prior tool calls, results, or opaque replay items were removed. These
optional numeric fields identify a local catalog reduction, not provider
capacity or a guaranteed ability to finish the turn.

`model.request.started.requested_completion_limit` records the completion
limit Ash requested for that call when known. Provider enforcement varies;
the ChatGPT-plan adapter does not enforce a per-call output cap. This is not
an estimate of input headroom for a later request. Existing event consumers
may continue to ignore these optional fields.

`model.request.completed.usage_source` identifies whether its `prompt_tokens`
are provider-reported or locally estimated. Failed and cancelled requests have
no completed usage measurement. `cache_read_tokens` and `cache_write_tokens`
are subsets of prompt usage and must not be added to `prompt_tokens`. `turn.usage`
is cumulative across the turn and likewise reports those cache subsets; it is a
different measure from one request's input.

A successful tool-free final report has a distinct durable reporting turn.
Its context assembly and request lifecycle carry that report's `turn_id`, and
exactly one `turn.usage` is emitted after the report message and usage snapshot
are saved and the turn is completed. Provider, estimated, and unavailable
sources remain distinct, including zero-valued token and cache fields. A failed
or cancelled report leaves its reporting turn interrupted and emits no
`turn.usage`; the interrupted coding turn's usage is unchanged. The
`turn.finalized.tool_free` event retains `source_interrupted_turn_id`.

The estimate and provider-reported usage are separate units of evidence. A
provider's reported prompt count may omit retained opaque reasoning or count
schemas and other input differently. It does not prove the full occupied
context window. Consumers must not use historic estimate/usage ratios as an
admission adjustment, sum overlapping per-request input values as a single
context size, or treat provider-reported input alone as total context
occupancy. Tool-message character counts include model-visible serialized
results, not file paths or source snippets.

`context.request.rejected` records a local attempt stopped before provider
dispatch. It includes `cause` (`input_limit`, `opaque_context`, or
`turn_budget`), `scope: "request_attempt"`, `dispatch_status: "not_dispatched"`,
the required input estimate and input ceiling when known, and bounded boolean
indicators for protected current-turn content and preserved opaque replay
state. Cumulative-turn fields are present when relevant. The event says only
that this attempt was not dispatched; earlier provider calls in the same turn
may already have completed. It does not change turn stop reasons or admission
guards. For message-compactor rejection, the legacy `required_input_tokens`
and `input_limit` are message-only. `native_tool_schema_tokens`,
`required_total_input_tokens`, and `total_input_limit` identify the same
rejection in total-input units, including native schemas. Optional numeric
components come from that rejected estimate, not an earlier successful
request. A required-summary rejection can provide only a conservative
minimum requirement, without exact message-component attribution. Older
records and other rejection paths may lack the normalized fields.

The terminal `/context` command reads at most the latest 300 runtime events for
the selected session. It shows the latest recorded successful request
assembly, the last dispatched attempt joined by `context_id` and `operation_id`,
the latest local rejection, and the last persisted cumulative turn usage.
`/context --provenance` retains its existing in-memory fragment detail. Legacy
records without these fields, missing/corrupt/evicted events, and ambiguous
associations remain unavailable; chronology is never used to fill in missing
IDs. If a matched request start has no recorded outcome, the terminal view
checks that exact `turn_id` in the selected session's durable turn journal:
`started` remains pending, `interrupted` reports provider outcome unknown,
and `completed` without a request outcome is missing. Missing or unreadable
journal state is unknown. This lookup adds no runtime event and does not imply
that a request completed or is safe to retry. The command does not load the
transcript or make a provider request.

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

`turn.error` reports `stop_reason` as `opaque_context` when retained provider
reasoning prevents a context fit, `context_budget` on other protected input
overflows, or `error` for other failures. `turn.cancelled` reports
`wall_time_budget` if the configured wall-time deadline triggered cancellation,
and `cancelled` otherwise. Session activity reports `interrupted` for a
journaled interruption without a terminal event.

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

An active Goal pauses when its current step stops at an iteration or token
limit or trips the circuit breaker. Resume the Goal explicitly to grant
further automatic continuations.

For a fresh one-shot `ash -p --require-check-command EXACT_COMMAND`, Ash also
includes `required_check_satisfied` in the final JSON/stream-JSON result.
Only confirmed successful execution of the exact operator-selected command
after the last recorded edit, with complete safe tool history, sets this to
`true`; otherwise it is `false`, the result carries a review warning, and
the CLI exits with code **1** even if the model claimed success. Streaming
completion is withheld until this evidence check finishes. The ordinary
runtime `turn.completed` event retains its execution-boundary meaning and
there is no automatic test execution or replay. This flag does not establish
correctness beyond the selected command and does not change ACP/A2A behavior.

Protocol adapters must not confuse this terminal boundary with task success.
On ACP, `token_budget` maps to the standard `max_tokens` stop reason and
`iteration_limit` maps to `max_turn_requests`. Circuit-breaker stops and a
stream that ends without a terminal turn result are reported as errors.
On A2A, all three unfinished stop reasons (`token_budget`, `iteration_limit`,
`circuit_breaker`) and a missing terminal event produce a **failed** task
status, not `TASK_STATE_COMPLETED`; partial response artifacts remain
available to review. Normal successful and legacy terminal turns retain
their existing completion behavior. Neither adapter retries tool effects.

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
