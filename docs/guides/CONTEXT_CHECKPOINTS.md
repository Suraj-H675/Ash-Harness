# Reasoning-aware context checkpoints

Ash retains the original session transcript, tool journal and audit records.
Never replay an earlier tool action merely because the context window changes.

## Native checkpoint

The `/compact-native` command is **currently disabled** for the
production OpenAI Responses provider. The standalone endpoint returns a
complete replacement input window: retained user messages followed by
one encrypted compaction item. Ash now has a bounded envelope that stores
this entire array inside a fresh child session and expands it unchanged
into subsequent Responses input. Unexpected retained item types, invalid
or oversized checkpoints fail closed without creating a child. Currently
only retained text input blocks are supported; image, file, audio and
other returned item shapes remain unsupported until verified.

The compact request omits `instructions`; Ash's normal trusted
instructions are rebuilt on the next turn. Transport errors, invalid
checkpoints, concurrent transcript changes, or changes to the source
session's model or permission mode prevent checkpoint installation.

**Availability:** the complete-window round-trip, session transaction and
concurrent-history protections have synthetic test coverage only. The
installed OpenAI Python SDK's *strict* model validation rejects the
documented retained user input item, although its normal HTTP response
parsing accepts that example. Ash now reads the SDK's raw HTTP JSON for
compaction to preserve the complete array without type coercion.
This has been verified with the real SDK and a mocked HTTP transport,
including malformed, duplicate-key, oversized and unsupported-item
rejection. Provider exceptions also suppress sensitive original tracebacks.
Fresh child sessions never inherit a parent context summary. It has
**not** been verified against a live compact response.
Do not enable native compaction until a real supported response is
verified. The current
ChatGPT-plan OAuth route returned HTTP
401 (`hardened_oauth_rule_missing`) on a synthetic
`/responses/compact` probe. OpenAI's official compaction guidance
requires replaying the returned context window *as-is*. Other providers
must establish their own supported contract.

## Local-first restart

When native compaction is unavailable, explicitly start a new reasoning
window using a user-authored handoff:

`/restart-context "Goal: repair redirects. Verified: ... Remaining: ..."

The child contains the redacted handoff and **no** earlier tool calls,
temporary approvals, session allow rules or encrypted reasoning. An
explicit parent permission **mode** is preserved to prevent a context
restart from silently relaxing a stricter mode such as `plan` or
`dry_run`. Ordinary session forks continue to reset their mode.
The parent is unchanged and can be inspected separately.
A handoff is not proof of completed work; verify
current files, tests and external effects before acting.

Both paths refuse unfinished/interrupted turns, unconfirmed tool actions,
active external MCP tasks and archived ambiguous task handles. Use the
existing recovery flow to resolve those first. Neither path silently
replays an action or lifts a permission restriction.

Local extractive `/compact` remains available when there is no
opaque reasoning state. Otherwise Ash preserves the provider prefix
unchanged or fails closed rather than silently discarding reasoning.
