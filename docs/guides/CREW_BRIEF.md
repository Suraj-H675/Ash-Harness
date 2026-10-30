# Coworker brief: bounded context across independent turns

Crew's canonical session and per-run transcripts are durable, but a new model
turn intentionally starts with clean context. A **coworker brief** is one
short note that the local owner explicitly writes. Ash includes it as inert
reference data in that coworker's future ask, manual responsibility, signed
event and scheduled model turns.

Use the coworker's immutable ID, not a shared profile or ambient session:

~~~bash
ash crew brief set --owner local --coworker-id ATLAS_UUID \
  --text "Current agreed release gate: verify the staging assertions first."
ash crew brief show --owner local --coworker-id ATLAS_UUID
ash crew brief clear --owner local --coworker-id ATLAS_UUID
~~~

Set replaces the entire brief; clear removes the content. The current
revision is shown for inspection. Avoid typing credentials or confidential
content into terminal arguments (which may appear in shell history or process
lists). Recognized secret patterns and configured profile keys are refused.
The limit is **2,048 UTF-8 bytes**, including whitespace. Text is persisted
under the local owner and coworker identity, not in a shared global memory
store. Paused coworkers can have their brief edited; archived coworkers allow
read-only inspection but no changes.

The note is **not automatically written from model output**. Model-generated
answers, untrusted evidence, and external events cannot update it. Context is
not a permission grant or an instruction override. It does not add tools,
write access, new file evidence grants, or sharing between coworkers.

Before any new model dispatch, Crew rechecks the exact stored text and
revision within the same transaction that claims the run. If the brief was
edited after the prompt was prepared, the turn is blocked **before provider
dispatch** and no durable run claim is consumed. Once a turn is claimed, it
uses its already verified snapshot; subsequent brief changes affect only
new turns. Existing transcripts and approvals keep their historical meaning.

There is no automatic summarization, semantic search, cross-coworker memory,
or retrospective context rewrite. This is a deliberately small owner-controlled
continuity primitive.
