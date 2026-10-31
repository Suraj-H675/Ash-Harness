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
