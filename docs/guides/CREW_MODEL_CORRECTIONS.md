# Atlas-drafted exact corrections

Atlas can **suggest** one complete replacement for a small, already existing
workspace text file it received as explicitly granted responsibility evidence.
This is a local, read-only model request followed by the existing **two separate
owner-approved** file-effect steps. Atlas does not execute tools or edit files.

Configure a coworker bound to the exact `atlas` profile and a responsibility
with explicit `--evidence src/example.py` (and relevant logs). Trigger the
responsibility using the existing **locally authenticated signed event**,
adding `--draft-correction`:

```bash
ash crew event --owner local --coworker-id ATLAS_UUID \
  --responsibility-id RESPONSIBILITY_UUID \
  --key-file /outside/workspace/event.key --input /path/to/signed-event.json \
  --draft-correction
```

The model is asked for exactly one JSON object:

```json
{"action":"replace","target":"src/example.py","replacement":"COMPLETE NEW FILE CONTENT\n"}
```

or `{"action":"no_change"}` if the evidence does not justify a complete
replacement. This is deliberately **not** an arbitrary code diff: the full
replacement is bounded by the existing model completion limit (512 tokens)
and file approval size cap. JSON syntax must be exact.

From the completed run output, copy the immutable run/session/request IDs.
Explicitly select the reviewed target path in the staging command:

```bash
ash crew propose --owner local --coworker-id ATLAS_UUID \
  --run-id RUN_UUID --session-id SESSION_UUID --request-id REQUEST_UUID \
  --target src/example.py --from-response
```

Staging refuses non-Atlas turns, non-draft prompts, malformed/no-change responses,
model-selected paths other than the **owner-selected** target, targets not present
in the source turn's evidence, stale target content, unsafe paths, recognized
secrets, redacted placeholders, disallowed policy, and duplicate proposals.
Existing file and response bytes are cryptographically bound to the approval.
**No file changes during staging.**

Use `ash crew approval show` to review the full proposed diff, source identity
and digest. Then complete the same existing owner-only TTY flow:

```bash
ash crew approve --owner local --coworker-id ATLAS_UUID --approval-id APPROVAL_UUID
ash crew apply --owner local --coworker-id ATLAS_UUID --approval-id APPROVAL_UUID
```

Each command requires its own exact interactive digest challenge. Denial or
revocation remain available. An approved correction is not automatically applied.
The durable dispatch/recovery journal prevents a consumed file effect from being
silently replayed after a crash. Verify the resulting file through normal
engineering tests; a plausible model correction is **not guaranteed correct**.

For an owner-created replacement file, the existing `ash crew propose
--replacement-file src/example.fixed.py` remains unchanged. The draft path
does not install a listener, monitor GitHub itself, expand evidence grants,
add multi-user authentication or permit arbitrary model tools.

## Manually assigned Atlas corrections (no signed event required)

An owner can now run an existing Atlas responsibility with a correction draft,
using the same strict correction response contract as signed events. The
responsibility must explicitly grant the relevant log and source file:

~~~bash
ash crew responsibility run --owner local --coworker-id ATLAS_UUID \
  --responsibility-id RESPONSIBILITY_UUID \
  --event-id unique-investigation-id --draft-correction
~~~

The result is a read-only, single-use model turn. It neither writes a file nor
grants new permissions. If the strict JSON is useful, take its printed
Run ID, Session ID and Request ID and stage an exact proposal:

~~~bash
ash crew propose --owner local --coworker-id ATLAS_UUID \
  --run-id RUN_UUID --session-id SESSION_UUID --request-id REQUEST_UUID \
  --target src/failing_file.py --from-response
~~~

The unchanged, explicitly granted target, complete replacement text, current
write policy and exact diff are verified as in the signed-event pathway.
Separate ash crew approve and ash crew apply commands require exact local owner
confirmations; denial, stale source or approval expiry blocks the write.
The manually triggered event ID is consumed once even when the model fails or
the result is not accepted. Scout cannot draft Atlas corrections.
