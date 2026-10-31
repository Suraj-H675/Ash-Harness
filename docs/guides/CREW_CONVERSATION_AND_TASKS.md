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
