# Coworker conversations and explicitly scoped tasks

Crew supports a bounded, resumable conversation for each coworker without
turning the coworker into an unrestricted Ash runtime:

~~~bash
ash crew chat --owner local --coworker-id COWORKER_UUID \
  --message "Summarize our last agreed finding."

ash crew chat --owner local --coworker-id COWORKER_UUID \
  --message "What follows from that result?" --evidence ci.log
~~~

Each chat message is an independent, claimed-once, read-only model turn in its
own owner-bound background session. Under the canonical session lease, Crew
retrieves at most four recent **completed chat** exchanges from the same
coworker. It verifies their persisted source binding and transcript, then
includes them as **untrusted JSON context** in the next model turn. Model
responses remain proposals, not verified facts. Ordinary one-shot crew ask,
responsibility runs, other coworkers and interrupted/unknown chat turns are
not inherited as conversation history.

History is bounded by number of exchanges and the 16 KiB prompt limit.
Long previous exchanges may be omitted, oldest first, but the current
message and its explicitly granted evidence are never truncated.
Each invocation prints its one-shot turn ID and durable run/session/request
IDs; interrupted/claimed turn IDs must never be reused. Use crew history
to inspect persisted outcomes after restart. CLI conversations are sequential,
not an always-open chat UI.

To start fresh and keep later turns separate from older chat history:

~~~bash
ash crew chat --owner local --coworker-id COWORKER_UUID \
  --new --message "Start a new investigation."
~~~

The new boundary is persisted with the completed turn. It does not delete
old transcripts, revoke previously approved actions, or alter the owner-curated
coworker brief. Workspace evidence is granted **per turn only**. Crew still has
no autonomous model tools or general command access through chat.

## A governed task: explicit Python syntax checking

Crew already has a fixed, resource-bounded Docker worker for Python source
parsing. The CLI exposes it as an **owner-selected** non-executing task:

~~~bash
ash crew syntax --owner local --coworker-id COWORKER_UUID \
  --file src/example.py --file tests/test_example.py
~~~

Only the named, validated workspace Python files are staged by value to the
private worker. Python source is parsed, not executed. The Docker worker has
no network access, no writable project mount and no scoped fallback; it must
meet resource-containment checks or the command refuses execution. The report
shows each file's syntax status and error position. Syntax failure returns
exit code 1; a refused or uncertain operation returns 2. An automatically
generated run ID is printed. Supplying --run-id enables precise tracking,
but **do not reuse it after a claimed or ambiguous attempt**.

This is a deliberately narrow governed capability. It does not grant the model
a shell, a general tool dispatcher, dependency installation, source mutation,
or permission to run arbitrary code. Consequential file corrections remain
under the existing source-bound crew propose / approve / apply protocol.

## Model-selected, owner-confirmed syntax task

To let a coworker **choose a syntax check**, first explicitly grant it one or
more small Python files at the workspace root. The model sees only those source
snapshots, must propose one fixed python_syntax action and cannot execute it:

~~~bash
ash crew ask --owner local --coworker-id COWORKER_UUID \
  --question "Which of these Python files should be syntax checked?" \
  --evidence example.py --evidence helper.py --propose-syntax
~~~

A valid model response is one JSON object, for example:

~~~json
{"action":"python_syntax","files":["helper.py"]}
~~~

Copy the source **Run ID, Session ID, and Request ID** printed by the
completed request. Review and run the model suggestion via:

~~~bash
ash crew syntax --owner local --coworker-id COWORKER_UUID \
  --source-run-id RUN_UUID --source-session-id SESSION_UUID \
  --source-request-id REQUEST_UUID
~~~

Ash verifies the exact completed, owner-bound no-tool transcript and its
versioned syntax proposal prompt. A suggested filename must occur in the
explicit evidence grant: additional filenames, duplicate files, command
arguments, extra JSON properties, or unknown action names are refused.
The original granted source bytes are rechecked against their recorded
SHA-256 hashes. Ash shows the selected files and response hash; **a local
interactive TTY confirmation is required**, followed by another source check.
At staging, each file is compared again to its original hash. The fixed
networkless Docker worker runs at most once for a deterministic task run ID,
even across restart. No shell, unrestricted tool invocation, or file
mutation is delegated to the model.

The current implementation deliberately supports **workspace-root Python
filenames** for model-selected tasks. Other files and deeper paths are not
silently added to the grant. This is the first constrained action type,
not a generalized autonomous task engine. An incorrect or inconclusive model
response may simply be rejected rather than guessed into an executable tool.

## Bounded, model-selected investigation

A coworker can choose and perform one predefined read-only text-search action
before producing a grounded analysis. The local owner supplies the complete
allowlist:

~~~bash
ash crew investigate --owner local --coworker-id COWORKER_UUID \
  --investigation-id inspect-ci-001 \
  --question "Find the failing assertion in the CI log" \
  --file ci.log --file tests.log
~~~

The allowlist accepts up to eight explicitly selected UTF-8 text files under
the existing evidence budget (6 KiB per file, 8 KiB total). Sensitive paths,
symlinks, stale reads, binary content and known secret markers retain their
existing evidence checks. The **first model turn receives only filenames,
sizes and SHA-256 hashes**, not the file text. Its only valid tool proposal is
a strict JSON object:

~~~json
{"action":"search_literal","files":["ci.log"],"query":"FAILED"}
~~~

Ash rejects extra keys, duplicate keys, unknown actions, regex/command
options, path escapes, unauthorized files and malformed queries. After
verifying the first turn is completed, owner-bound and tool-free, Ash
re-snapshots the exact owner-granted files and refuses changed input. Its
fixed search compares a case-insensitive literal string; it runs **no
commands**, network requests or user source code. The search returns at most
24 matching lines, excerpts limited to 220 characters, plus source file
hashes and omission counts. A second independently claimed, persisted
no-tool model turn receives only those results and the original question,
then explains findings and uncertainty.

Both model turns have stable, separate run identities. The first completed
turn alone does not imply the second turn succeeded. Interruptions, model
failures, invalid proposals or changed files do not automatically retry
or expand grants. Use a **new investigation ID** for new work, and inspect
Crew history after ambiguous outcomes. The existing coworker
identity, approval and correction boundaries remain unchanged.

This is a fixed read-only diagnostic action, not a general terminal, web,
repository traversal, test runner or autonomous coding loop. It currently
supports one model-selected search followed by one analysis; it does not
silently use arbitrary Ash tools or apply model-generated fixes.

## Investigations for standing responsibilities

The same bounded, model-selected literal search can now run for a stored
responsibility using only its existing owner-granted evidence files. This
supports manual runs, authenticated signed events and due scheduled work.

For a manual trigger:

~~~bash
ash crew responsibility run --owner local --coworker-id ATLAS_UUID \
  --responsibility-id RESPONSIBILITY_UUID \
  --event-id investigation-001 --investigate
~~~

For an already authenticated event, add --investigate to the existing
ash crew event command, keeping its owner-provisioned key file, signed
envelope and responsibility ID. To investigate due scheduled work, choose
ash crew tick --investigate or ash crew worker --investigate.
The flags are opt-in; ordinary task behavior is unchanged.

The plan remains a durable responsibility run with its normal manual,
signed-event or scheduled trigger attribution. Its model receives the task
scope, objective, success criteria, trigger context and hashes/sizes of
granted files, **not the file contents**. After validating that completed
plan and its still-active grant, Ash performs one fixed literal search.
A second, separately claimed no-tool model turn analyzes the bounded
results. For signed events, that analysis can also receive a bounded
excerpt of the authenticated event payload. Run and session records
remain inspectable through the responsibility ledger and coworker
history, respectively.

For scheduled investigations, the schedule result retains the
responsibility-bound plan and separately records the analysis result.
If the second step fails after the first completes, the first run is
still consumed and the follow-up may be incomplete. Ash will not infer
that the investigation succeeded or automatically replay it. A subsequent
new occurrence can run normally.

The investigate option cannot be combined with draft-correction in the
same manual or signed-event invocation. If Atlas identifies a correction
worth making, use a separate, explicitly authorized correction workflow
and the existing exact-owner-approval gates. Scheduled investigations
also cannot combine investigate with only-if-changed: no-change
skipping is verified for ordinary scheduled model turns, not for the
two-step investigation contract.

This is still not unrestricted filesystem discovery, shell execution,
test running, remote webhook serving or unsupervised source editing.
