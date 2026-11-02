# Crew's local authenticated event receiver

Crew can receive **already signed** event envelopes through an explicit local
HTTP listener. It uses the same HMAC verification, owner/coworker/responsibility
binding, durable event claim, model restrictions and no-replay behavior as the
existing crew event command. It introduces **no general public webhook**.

## Setup and operation

Provision a separate private 32-byte HMAC signing key, **outside the coworker
workspace**. The key file must belong to the local OS owner, have exactly
0600 permissions, and contain 64 lowercase hexadecimal characters. Never
store it in a repository, put it in CLI arguments, or include it in events.

Start the listener only when you want to receive events:

~~~bash
ash crew listen --owner local --coworker-id COWORKER_UUID \
  --responsibility-id RESPONSIBILITY_UUID \
  --key-file /private/path/crew-event.key --port 8781
~~~

Use --investigate for the bounded two-turn diagnostic workflow, or
--draft-correction for Atlas's exact replacement proposal. These modes
cannot run together. Corrections still require separate, explicit local owner
approval and application; HTTP transport never approves or writes them.

The receiver binds only to 127.0.0.1, not 0.0.0.0, IPv6 wildcard or a
remote interface; the host cannot be configured via CLI. Send
POST /crew/event with Content-Type: application/json, a matching
Host header, an exact Content-Length, and the pre-existing signed Crew
JSON envelope. No chunking, compressed content, proxy upgrade, persistent
connection or multi-request session is supported. An HTTP request is
processed synchronously and serially. At most eight client sockets may be
open, headers/body have five-second read limits, and the envelope is limited
to 16 KiB. A second complete request while the model is running receives
HTTP 429.

For example, after a trusted local sender creates a valid signed envelope:

~~~bash
curl --fail-with-body \
  -H 'Content-Type: application/json' \
  --data-binary @signed-event.json \
  http://127.0.0.1:8781/crew/event
~~~

The signed envelope must contain exactly version, source, id,
issued_at, responsibility_id, payload and signature, using the
existing Crew signing contract (canonical JSON of the six non-signature
fields with sorted keys and compact separators, HMAC-SHA256, lowercase hex).
The issued timestamp must be within the existing five-minute window, and
event identity must be unique per responsibility. Keys are never sent over
HTTP; only the signature and event content are transmitted.

An HTTP 200 response reports a **completed model workflow** and opaque run,
session and request identifiers. It does not mean a correction was approved,
applied, published, or notified remotely. HTTP 403 rejects invalid signed
events; 409 indicates a duplicate/claimed event; 422 denies a mismatched or
inactive responsibility; 429 indicates load shedding; and 500 may mean the
outcome is **unknown**, even if the model was dispatched. 503 indicates a
provider-readiness failure before a confirmed provider dispatch. Responses
contain no model text, source contents, key material or private exception
details. Do not automatically retry a failed/aborted request with the same
event ID. Use crew responsibility runs and crew history to inspect
durable state before deciding what to do next.

This listener runs only while the local process is alive. An external service
such as GitHub cannot post its native webhook body directly to Crew: a
**trusted, explicitly configured sender/bridge** must verify the external
service and create the Crew-signed envelope. Do not expose this plain-HTTP
listener through a public tunnel or proxy without independently reviewing
authentication, TLS, source trust and operational security. There is no
durable pre-ingress queue or guaranteed remote delivery; a host shutdown
before a run claim may lose an HTTP event if the sender does not reconcile.
Remote hosting and push alerts remain separate capabilities.

For post-event recovery, the owner can inspect the authenticated event's
responsibility with ash crew responsibility timeline --owner local
--coworker-id ID --responsibility-id ID. This verifies any separately
claimed investigation analysis and reported correction approval status even
after a process restart, without redispatching the event. A completed plan
with no completed analysis remains **partial**, even if the original HTTP
connection closed before a response reached the sender.

## Optional durable intake (explicit owner-controlled dispatch)

If the model must not run during HTTP admission, start the same loopback
listener with --store-only. For valid HMAC-signed events this returns HTTP
202 Accepted **only after** the signed envelope has been committed to the
owner-bound SQLite database. A 202 does not indicate model execution.
The old synchronous listener remains the default when this flag is omitted.

~~~bash
ash crew listen --owner local --coworker-id COWORKER_UUID \
  --responsibility-id RESPONSIBILITY_UUID \
  --key-file /private/path/crew-event.key --port 8781 --store-only
~~~

Inspect and explicitly deliver a stored event when the owner wants Crew to
work on it:

~~~bash
ash crew intake list --owner local --coworker-id COWORKER_UUID
ash crew intake dispatch --owner local --coworker-id COWORKER_UUID \
  --responsibility-id RESPONSIBILITY_UUID \
  --event-key ci:unique-event-id \
  --key-file /private/path/crew-event.key
~~~

A local owner can also store a previously authenticated envelope from a
private file or stdin without launching the server:

~~~bash
ash crew intake accept --owner local --coworker-id COWORKER_UUID \
  --responsibility-id RESPONSIBILITY_UUID \
  --key-file /private/path/crew-event.key --input signed-event.json \
  --investigate
~~~

At acceptance Crew validates owner, active coworker/responsibility, target,
the signing key, HMAC, five-minute timestamp freshness, event identity and
the configured follow-up mode. It stores the exact signed bytes with their
SHA-256 digest and receipt time. A maximum of 128 not-yet-dispatched events
per coworker is permitted. At explicit dispatch Crew verifies the original
signature again using the trusted persisted acceptance time; queued events
can therefore be delivered after their original signature has aged beyond
five minutes without accepting fresh *expired* events from callers. The same
private key must remain available.

Before the model can be contacted, Crew atomically reserves the receipt as
dispatching and erases its persisted event body. After successful processing
it records completed run/session identities; on an ordinary failure it
records unknown, and after a crash the older dispatching receipt remains
**uncertain**. Neither state is automatically replayed. Duplicates, including
a receipt with an already-consumed signed event identity, are rejected.
The existing responsibility-run and investigation timelines can be used to
reconcile downstream model outcomes.

This does **not** provide a durable network queue while the computer is
powered off. Requests only enter the receipt journal while the loopback
listener is running and reaches SQLite; a sender must still deal with
connection failures. It also does not run the backlog autonomously: an
owner explicitly dispatches individual accepted receipts. A successful
HTTP 202 confirms storage only, not processing or an applied correction.

## Completed receipt verification

The crew intake list operation does not trust a receipt's completed flag on
its own. For each completed event it verifies the deterministic signed-event
run identity against the actual persisted, owner/coworker-bound, tool-free
model transcript and its responsibility/trigger. For a queued investigation
it also verifies the distinct completed analysis session and its original
response SHA-256, trigger, event and responsibility binding. A missing,
altered or unrelated underlying record causes the listing to fail closed
instead of reporting an unearned completion.

An unknown or dispatching receipt remains non-replayable, even if
subsequent inspection finds that some work completed. Use the responsibility
timeline to investigate a partial outcome; the intake reader performs no
model calls, network operations, status changes or automatic retries.
