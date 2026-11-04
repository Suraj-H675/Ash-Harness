# Crew: one-shot native Git diagnostics

Crew can execute one owner-selected, read-only Git diff through Ash's normal
tool permission, intent, audit and recovery machinery. This is a separate
tool-only session, not a tool-enabled coworker model. Atlas and Scout's ordinary
model runs remain tool-free.

## Run

The coworker workspace must itself be the root of a non-bare Git repository
with its own .git directory. The selected file must be a safe, existing,
UTF-8 workspace-relative file of at most 6 KiB. Paths in credential areas,
traversal, symlinks and suspected secrets are rejected by Crew's existing
scoped-evidence reader.

    ash crew git-diff run \
      --owner local \
      --coworker-id COWORKER_UUID \
      --run-id 4a876f5a-3114-4f87-9982-a94c56ef2cb8 \
      --file src/example.py

If --run-id is omitted, Crew generates and displays one before the action.
The owner must be at a real local TTY and type exactly GIT DIFF <run-id>
<source-sha256>. Only after that confirmation does Crew persist an exclusive,
one-shot, owner/UID/coworker-bound claim. It verifies that the workspace and
selected file are still the same, then starts a distinct local-only AshLoop
with only git_diff registered and the permission mode pinned to plan.
The tool receives staged=false and the exact owner-granted path:
no arbitrary Git flags, shell, plugins, MCP calls, model calls, network tools
or writes are exposed.

The existing read-only Git tool rejects untrusted Git configuration and avoids
external diff/text-conversion execution. A bounded Crew-specific output filter
rejects oversized, truncated, binary/control-bearing or recognizably secret
patches before Ash stores the tool result/audit entry. A blocked result has
an empty recorded output. This filter is not a substitute for avoiding
sensitive source history in Git.

## Inspect after restart

    ash crew git-diff status --owner local \
      --coworker-id COWORKER_UUID \
      --run-id 4a876f5a-3114-4f87-9982-a94c56ef2cb8

    ash crew git-diff status --owner local \
      --coworker-id COWORKER_UUID \
      --run-id 4a876f5a-3114-4f87-9982-a94c56ef2cb8 --show-output

The show-output option reads only the previously completed and verified tool
session; it never dispatches again. Completed receipts require an
actual persisted, matching Ash tool call with exactly the expected path,
read-only arguments and successful outcome, and an output digest matching the
Crew receipt. Invalid or missing tool evidence fails closed.

A crash leaves claimed (uncertain); a caught exception becomes unknown.
A failed/sensitive result is blocked. None of these can reuse the run ID.
If completion was durably recorded, the status is completed. The separate
tool-call journal is stored in the coworker's isolated profile database at
db/crew_native_tool_sessions.db. No tool session is inserted into the
coworker's model-session database.

This is an owner-invoked native-tool execution milestone. It does not
authorize model-selected Git operations, unattended tool use, arbitrary
commands or broad Ash tool access. Those require a separate, explicitly
reviewed capability/approval interface.

Inspecting a purported completed result with a missing tool journal fails
closed; status inspection does not create a replacement journal to make the
receipt appear valid.
