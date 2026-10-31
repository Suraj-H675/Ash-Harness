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
