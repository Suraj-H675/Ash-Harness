# Context Management

Ash builds each provider request under an input limit derived from its
configured combined context allowance and response reserve. A provider's
combined `context_window`, input-only `max_input_tokens`, and response-only
`max_output_tokens` remain separate limits. Ash applies each known provider
limit and treats missing metadata as unknown; configured values remain local
Ash ceilings. The input limit covers chat messages and provider-facing tool
schemas. `/context` reports the effective input, response reserve, and combined
allowance for the last request.

Each turn allows 10 model request cycles by default. Set `max_turn_iterations`
in `~/.ash/ash.toml` or `ASH_MAX_TURN_ITERATIONS` to choose a value from 1 to
100. This user-owned limit does not include extra cycles Ash may allow to apply
queued steering messages. A configured `max_turn_total_tokens` also limits the
combined prompt and completion tokens for a turn; `0` disables that token
limit.

For a more involved **one-shot** coding task, explicitly opt into more cycles
without editing your persistent profile, for example:

```shell
ash -p "Fix the multi-file issue and run its tests" --max-turn-iterations 24
```

This overrides the iteration ceiling **for that CLI invocation only** (1–100).
The default remains 10, and other token, time, command, permission and sandbox
limits still apply. More requests can increase provider usage and cost; the
flag does not force the model to use them or prove that its edits are correct.

After a turn reaches `token_budget`, `iteration_limit`, or `circuit_breaker`,
the user may explicitly start another turn in the same saved session. On the
next turn, Ash reads only the latest completed turn's persisted terminal event
and provides the model with a brief **runtime-only** reminder that the previous
work may be unfinished. This reminder survives a process restart through the
normal session event history, is not written into the conversation transcript,
and does not repeat after a subsequent normal turn. It never restarts work,
executes a command, restores a file, or authorizes/replays a tool call. The
model must inspect current workspace state and verification evidence; a
prior edit's success does not prove the task was finished. A missing or
unrecognized terminal event does not provide a speculative continuity claim.

For new or resumed **one-shot** work, `ash -p "..." --turn-time-budget 270`
provides an optional 30–3600 second wall-clock budget (disabled by default).
Ash gives the model a short trusted budget notice, then at most two further
notices as the remaining time decreases. These notices are not stored in the
durable conversation. The budget covers all model requests, tools and opted-in
context restarts in that one-shot operation; it does not reduce reasoning
effort, grant permissions, execute verification commands automatically, or
extend an external supervisor's deadline. On expiry Ash cancels through the
normal interrupted-turn recovery path, preserves known completed work and
refuses to guess the outcome of ambiguous external effects. A timed-out run
exits nonzero; inspect its recorded tool outcomes and current files before
resuming. This is an **execution control**, not a verified coding-speed gain.

### Owner-selected post-verification checkpoint

For a **fresh one-shot** coding task, an operator may explicitly select the
command that should end the turn once the model has executed it successfully:

```shell
ash -p "Repair the issue and run the requested test" \
  --report-after-command "python3 -B qa_visible.py"
```

The flag **does not execute or approve the command**. The model must still
request `run_command` normally and all regular permission, sandbox, audit,
and recovery safeguards apply. The command string must match the recorded,
successfully dispatched command run from the project root. The checkpoint
requires at least one known-successful local workspace edit in the same fresh
session and turn, no failed, unknown or unrecognized tool actions, and no
unresolved external tasks or edits. The owner-selected check must be the
latest recorded tool action, and the audit chain must be intact.

At that explicit boundary Ash returns a **deterministic, evidence-limited
report** without another model request or command replay. The one-shot output
contains `owner_command_checkpoint=true`, and the recorded stop reason is
`owner_command_checkpoint`. The turn is completed with its tool records
intact. This is an **operator-defined stop condition**, not proof that hidden
tests or unrelated requirements pass. If the check fails, doesn't match, or
any prerequisite is missing, Ash continues the normal reasoning loop. The
feature is disabled by default; it cannot be used with resumed sessions or
JSON-schema output. It may be combined with bounded wall time and opt-in safe
context continuation.

An adapter that does not accept a response ceiling can only use the reserve for
input admission; the ChatGPT-plan preview route currently does not transmit a
maximum response value.

### Required coding verification (opt-in, separate from early reporting)

For a fresh one-shot coding task, an operator who wants **a failed process
status unless a specific check passed after the final edit** can use:

```shell
ash -p "Fix both modules and verify the change" \
  --require-check-command "python3 -B qa_visible.py"
```

Unlike `--report-after-command`, this mode **does not stop after the first
passing check**. The agent can finish other intended edits and its final
report. Ash then independently checks its durable, audited tool record: known
successful workspace edits, an exact authorized `run_command` invocation from
the project root with exit code 0 after the last edit, no later shell commands
or edits, and no ambiguous or unresolved tool effects. Earlier failed commands
must be corrected and successfully rechecked; a passing result from before a
later edit cannot certify the later state. The check is read-only against
recorded evidence: Ash does **not** execute the command, retry tests, grant
permissions, or silently resume a session. It always obeys the existing user
approval, sandbox and command policy.

The one-shot process returns **exit 1** when this contract is not established,
even if the model says it passed, and includes `required_check_satisfied=false`
and a review warning in its structured result. A verified check returns exit
0 and `required_check_satisfied=true`. Streaming JSON defers the terminal
completion envelope until this evidence is evaluated, avoiding a premature
success signal. Neither value proves that all other behavior or hidden tests
pass; it certifies only the operator's selected command outcome after recorded
edits. The flag is not interchangeable with the optional early-report mode,
and cannot be combined with session resumption, automatic context
continuations, JSON-schema output or `--report-after-command`.
If `auto_commit` is enabled, Ash leaves these edits uncommitted instead of
committing before the required check evidence is finalized. The operator can
review and commit after the successful one-shot result.

## Budget buckets

The default allocation is:

| Bucket | Weight | Source |
|---|---:|---|
| system | 20% | Runtime prompt, persistent instructions, and active skill metadata |
| tools | 15% | Provider-facing tool declarations |
| history | 45% | Session transcript and compaction summary |
| repo_map | 10% | Ranked workspace symbols |
| memory | 10% | Retrieved semantic or lexical memory |

Weights are configurable through `context_budget_weights`. Bucket limits are
allocation targets; the combined provider input is the hard constraint. Tool
schema tokens are reserved before history compaction and are included in the
reported total.

## Deferred tool schemas

`search_tools` indexes the complete live built-in, plugin, skill, and MCP tool
catalog. When the catalog exceeds `tool_search_threshold` (32 by default), Ash
sends only a compact essential set and `search_tools` to the provider. A search
returns the names and descriptions of the best matches that can actually be
activated for the next model iteration. Native-tool providers receive the
exact argument schemas in the **next provider request**, not a duplicate
copy in the search result. Non-native discovery retains schema metadata
except under the existing compact-result policy for effective input limits
of 4,096 tokens or less. Runtime validation is unchanged.

The runtime bounds the combined incremental schema and discovery-result token
cost against the effective provider input limit. When matches cannot fit, the
result reports the omitted count and is marked truncated rather than claiming
those tools were activated. The reserved input budget protects space for
instructions and current-turn evidence, but cannot repair a session whose
protected history is already too large. Ash retains at most 64 recently
activated deferred schemas per session;
re-searching a tool refreshes its recency. Older activations may be evicted to
make room, but no match advertised in one result is evicted by a later match in
that same result. Deferred tools remain searchable. The window resets when the
session changes. Set
`tool_search_threshold = 0` to send the full catalog on every request.

Only visible schemas count against the tools budget. Deferred tools remain in
the runtime registry and continue through normal validation, permissions,
hooks, audit, event, and result-persistence paths after activation.

When the **effective** provider input limit is at least 24,576 tokens and
deferred discovery is active, Ash also exposes the existing read-only
`git_diff` schema directly. The generated system prompt includes concise
coding guidance for focused inspection, coordinated edits and authorized
testing. This does not grant shell access or alter any tool permissions.
Ordinary `read_file` calls default to **4,096 source characters**. This
reduces the amount of source content retained in subsequent
model requests, particularly when opaque provider reasoning state prevents
history compaction. The result still includes numbered lines, file SHA-256,
encoding and exact line/column continuation. Models and users may explicitly
request larger `max_chars` values (up to the existing hard limit) when a
fuller inspection is actually necessary. This is a context-economy default,
not a proven increase in independent coding benchmark pass rates.
For opaque reasoning history, Ash budgets built-in read-only navigation
results before they enter protected history. At effective input limits of
at least 24,576 tokens, it also reserves additional room based on recent
assistant-message sizes for a possible failed edit, repair and verification.
The additional margin is bounded and may shorten or refuse navigation sooner;
it cannot guarantee room for arbitrary future completions or tool output.
Ash does not discard or rewrite opaque replay state. Unknown middleware or
registered hooks retain the conservative non-projected behavior.
`search_text` also supports optional `context_lines=1..5` for nearby source
lines around each match without a separate read request. Adjacent windows are
merged, and the match count still controls `max_results`. Matching lines keep
`path:line:text` locations; neighboring lines use `path-line-text` so callers
can distinguish context. Context previews are limited to 512 characters per
line. Ordinary search now defaults to a **16,384-byte output capture** to avoid
unexpectedly retaining enormous search results in reasoning-replay history;
models can explicitly request `max_output_bytes` up to 2,000,000 for a larger
search. Contextual searches remain capped at 24,576 bytes regardless of that
request. Long matched lines return a bounded excerpt and, when the excerpt
starts after the beginning of the line, the starting column. The default
`context_lines=0` preserves match-only formatting. These bounded previews
can save an inspection round trip; they do not replace explicit
`read_file` slices for editing or prove a live coding-performance improvement.
The `read_files` tool remains discoverable through `search_tools` at every
context size rather than adding another default tool schema. It groups up to
four focused file ranges into
one request, with a 16,384-character combined source cap and the same path,
binary, SHA-256, encoding and truncation checks as `read_file`. This is a
convenience for model round trips, not a replacement for focused searching or
permissioned test execution. An initial large-context multi-file coding trial
used this tool but did not complete the independent task; eager promotion is
therefore not treated as a demonstrated coding improvement.
In trusted workspaces with language servers configured, successful file edits
may also carry advisory post-edit LSP diagnostics. The automatic preview now
prioritizes errors and is capped at 16 KiB of additional tool output, with at
most 24 diagnostics and six per edited file. Messages are shortened and
redacted; omitted counts are explicit. The existing `lsp` diagnostics query
remains available when the model needs more detail. A successful edit plus
LSP feedback does **not** establish that code is correct or verified, and the
preview never runs tests or grants additional execution permissions.
Batch requests inherit the managed, session and persistent `read_file`
permission rules for **each** requested path; a path-scoped deny blocks the
entire batch, and a required approval cannot be bypassed by grouping reads.
`apply_patch` remains deferred, including on large models; making its schema
eager previously harmed local-model multi-file trials, and its usefulness as
a general default has not been established. Models with smaller *actual*
input limits keep the original compact catalog and prompt, even if the user
configured a larger nominal context window.
When discovered, `apply_patch` accepts Git unified diffs and a bounded
`*** Begin Patch` / `*** Update File` model-oriented form. Both paths retain
the same permission, checkpoint and ambiguous-result contracts. Support for
the additional input syntax does not by itself establish fewer model steps.
A rejected model-oriented patch identifies the failing file and hunk for
context-matching failures. When translation or validation fails before any
write, Ash reports that this patch changed no files. An apply attempt with
an uncertain write outcome still requires inspection before retrying.
For explicit requests to edit several files or modules together, `search_tools`
prioritizes the deferred multi-file `apply_patch` tool. Read-only grouped
source requests still favor `read_files`, and multiple ranges within one
file still favor `replace_file_edits`. Discovery neither runs the patch nor
grants approval; normal validation, permission and ambiguous-result safeguards
remain in force.
The optional large-effective-context coding guidance also explains why several
sequential `replace_file_content` calls cannot reuse one file's original
`expected_sha256`: the first successful write makes later hashes stale. It
directs the model to discover `replace_file_edits` for one-file non-overlapping
changes or `apply_patch` across files. Hash validation remains mandatory when
the caller supplies it; this is a selection hint, not automatic mutation
coalescing or a demonstrated improvement in live task completion.

After a request is assembled, `ContextBudgetReport.fragments` records each
fragment's typed kind, source, trust class, token use and limit, truncation
state, SHA-256, and non-content metadata. The report intentionally does not
retain a second copy of prompt content. Use `/context` to inspect current
bucket use and fragment provenance. `/context --provenance` shows the exact
provider-visible tool count, an eight-name preview, and how many registry tools
remain deferred. It also reports whether core instructions, loaded persistent
instructions, or supplemental system context were truncated.

### Diagnosing request context

Use `/context` to compare the most recently recorded successful request
assembly with the last dispatched attempt, inspect a local pre-dispatch stop,
and read the last persisted cumulative turn usage. The command reads only the
selected session's latest 300 runtime events; it does not read the transcript
or contact a provider. `ash sessions activity --session SESSION_ID --json`
shows the bounded event timeline with request/context correlation IDs, route
fields, usage source, and rejection details.

The local `~current/maximum` value is an Ash token estimate and effective
input ceiling. A completed request's prompt usage is a separate measurement:
it may be provider-reported or estimated when the provider omits usage. Cache
read and write values are subsets of prompt usage. Cumulative turn usage sums
request usage across the turn, so it is not a single request's context size.
Provider-reported prompt usage may omit retained opaque reasoning or count
schemas differently; it does not prove the total occupied context. Ash does
not calibrate its admission estimate from historical usage ratios.

For the first-party ChatGPT plan adapter using Ash's offline ASCII token
estimator, long URL-safe base64-shaped top-level reasoning payloads are
estimated by their approximate decoded byte length (three bytes per token,
retaining padding and envelope overhead). This follows the distinction
between model-visible content and transport expansion in Codex's public
`rust-v0.169.0` history accounting, with a more conservative length estimate.
It is still a heuristic, not a provider token count or a guarantee of fit.
The same estimate applies to request assembly and navigation admission.
Other adapters, custom token counters, unrecognized payloads and native
compaction checkpoints retain their previous accounting. This changes only
local token estimates; no replay bytes are decrypted, discarded or rewritten,
and forced local compaction and over-budget replay remain fail-closed.

`context.request.rejected` identifies one request attempt that Ash stopped
before dispatch because of the local input limit, an opaque-context constraint,
or the cumulative turn budget. Earlier provider requests may already have
completed. The current stop reason and hard limits remain authoritative. For a
request without a recorded outcome, `/context` checks the exact turn journal
row in the selected session: a started turn remains pending, while an
interrupted turn is shown as interrupted with provider outcome and usage
unknown. A completed turn without a request outcome is shown as missing;
absent or unreadable turn state is unknown. None of these states establishes
whether a provider completed the request or whether retrying is safe. A
failed/cancelled request has no completed usage value. Route parity is unknown
unless both requested and effective provider/model routes are present for the
same correlated attempt.

The terminal view uses durable events scoped to the active session. Older
records without correlation fields and unavailable, corrupt, or evicted events
are reported as unknown/unavailable rather than joined by chronology. The
context assembly and planner events omit credential profile labels. A
successful tool-free final report records its own request and cumulative usage
under a distinct reporting turn; a failed or cancelled report does not replace
the prior turn usage. The
`/context --provenance` fragment report remains process-local and is unavailable
after resume until a new context assembly is built.

## Compaction

The full transcript remains in SQLite. Compaction changes only the provider
request and the working `context_summary`:

1. Bound stale tool outputs while retaining their call identity.
2. Leave tool output from the active turn intact. Keep assistant tool calls
   with their matching results when moving older history into the digest.
3. Extract user requirements and explicit earlier user constraints, assistant
   decisions and progress, referenced paths, tool actions, recent tool outputs,
   and compacted events into bounded, labeled sections. Only user-role text
   contributes to historical user constraints; tool outputs remain untrusted
   evidence.
4. Fit the digest together with the retained tail under the provider input
   limit. Ash may move additional oldest, safely removable history into the
   digest to keep it visible in the request. A required digest that cannot fit
   with the protected current turn stops the request before Ash persists a new
   summary or advances the durable history offset.
5. Preserve both the beginning and end of an earlier summary.
6. Redact secrets before persisting the updated summary.

The current summary is deterministic and extractive. Ash does not claim a
model-assisted compactor yet. A future model-assisted path must be optional,
bounded, provenance-tagged, failure-isolated, and fall back to this algorithm
without recursively exhausting the same context window.

## Explicit restart after a context failure

Some reasoning-capable providers return opaque state that must be replayed
without modifying earlier input. If that protected input cannot fit within
the provider limit, Ash stops with a context error rather than silently
discarding state, rewriting tool results, or repeating actions. This can
happen partway through a multi-file coding task.

A different context failure occurs if a provider returns opaque reasoning state
but Ash cannot securely bind it to the credential that produced it (for example,
when private state-key storage is unavailable or the credential identity cannot
be verified). Ash may have already streamed useful assistant text and completed
some authorized actions. The current turn is nevertheless **incomplete**:
headless clients receive a nonzero exit and `turn.error` with stop reason
`provider_replay_binding` rather than `turn.completed`. The saved answer and
known tool results remain in the original session for inspection; replay is
refused. This failure never automatically retries an earlier tool action.
Fix the underlying storage or credential problem as appropriate, inspect
`ash sessions recovery --session SESSION` and the actual workspace, and then
use an explicit new-window handoff. Do not treat the streamed text as proof the
whole coding task finished.

Inspect the workspace and any recovery warnings before opening a new reasoning
window. In the interactive terminal, use `/restart-context <explicit handoff>`.
From the source workspace, the equivalent one-shot workflow is:

```bash
ash sessions activity --session SESSION --json
ash sessions recovery --session SESSION
ash sessions restart-context --session SESSION \
  --handoff "Describe the objective, observed edits, and checks still needed." --json
ash --resume CHILD_SESSION -p "Continue from the handoff."
```

The restart command returns the child and parent IDs without contacting a
model. Use the same Ash profile and database for the restart and resume. The
parent retains its transcript and audit log. Ash reconciles the interruption
under the session lease and refuses restart if another process owns that lease
or recovery cannot establish the outcome of an earlier action. Active MCP
tasks and terminal tasks without a recorded local outcome require ordinary
session recovery. The child retains the
parent's saved permission mode and starts with the redacted handoff. Earlier
tool calls, approvals, and opaque reasoning stay in the parent. Verify the
workspace and run authorized checks before relying on partial edits.

This is **manual recovery**, not transparent automatic continuation or a
substitute for long-task context management. `/compact-native` remains
unavailable on the ChatGPT-plan route until the native contract is verified.

`ash sessions activity` and interactive `/status` show the last incomplete turn
when Ash has a recorded stop. A context-budget error, cancellation, or interrupted
process can leave work and verification unfinished. Inspect recovery reports
separately for unknown tool outcomes or unresolved files.

### Opt-in one-shot continuation

For a **new** non-interactive coding session, `ash -p "..." \
--context-continuations 1` authorizes at most one extra reasoning window
after an opaque reasoning-state context overflow. `2` allows two; the default
is **0 (disabled)**. This is not enabled for resumed sessions, attachments,
or structured JSON-schema outputs.

The original user task is preserved verbatim in a bounded handoff. Ash first
closes and reconciles the interrupted turn, verifies its durable recovery
record, and checks the parent session's audit hash chain within the same
durable transaction that authorizes the automatic context fork. A broken
audit chain blocks the new agent turn until the owner investigates. Ash then
forks a fresh context without importing any reasoning items,
tool results, approvals or tool dispatches. Automatic continuation is
**refused** if the previous turn dispatched any shell command or other
external tool, even when its result is known, or if it has ambiguous actions,
unfinished edits or unverified external tasks. Known-successful local
read/search tools, including `search_tools` catalog discovery, and direct
workspace edit tools may precede an automatic handoff. Narrow exceptions
cover proven harmless pre-dispatch rejections.
While this option is active, the exact built-in navigation tools can record
an invalid argument, invalid search pattern, or context-budget refusal as a
known failure without approving, dispatching or executing the call. This
requires the normal trusted runtime, no registered hooks or custom approval
callbacks, and no applicable policy denial. The automatic fork checks the
matching durable call and its audit witness within the same transaction as
the parent audit-chain check. A missing, inconsistent or duplicate witness,
a generic dispatched failure, an untrusted implementation or a denied action
still prevents the fork. No rejected navigation call is replayed.
While this option is active, the built-in `replace_file_content` tool also
preflights an approved edit **before** tool dispatch, tool hooks or middleware.
If the exact replacement is already invalid, Ash records a rejection without
executing it. An accepted preflight still goes through the usual tool path,
which rechecks the file before writing. Such a rejection permits automatic
handoff only if the durable call was approved but never dispatched or executed,
and no registered hooks or custom approval callbacks could have caused external
effects. Failed dispatched
edits, denied approvals and unknown outcomes remain blocked. This neither
automatically retries an edit nor bypasses filesystem safeguards.
The new turn is instructed to inspect the actual workspace state and verify
the task before reporting completion; any tool execution in the new turn
still passes normal permissions, sandbox and audit checks.

This option does not resolve context limits automatically in every case. It
does not extend the outer process deadline, retries neither provider outages
nor failed tests, and stops after the configured number of restarts. A
failure can leave partial edits in the workspace; always inspect the
recorded session lineage and working tree. It must not be interpreted as an
exactly-once guarantee for arbitrary external actions.

If a new one-shot coding turn with this option enabled already made **known
successful local edits and a known successful authorized shell command**, but
exhausts opaque context before producing its final response, the ordinary
automatic coding-context fork remains forbidden: commands must never be
silently repeated. When its one interrupted turn is fully reconciled, all
action outcomes are known and successful, no external plugin/task or file
checkpoint remains uncertain, and its audit chain is intact, Ash may use the
remaining context-continuation allowance for **one tool-free final report**.
This is a fresh, bounded model request containing only the original task and
compact redacted durable evidence. No tool schemas, previous opaque reasoning,
shell privileges or approval grants are supplied. The old coding turn stays
interrupted in the journal; its tool actions are neither imported nor replayed.
Ash records a separate completed reporting turn with explicit provenance and
sets `tool_free_finalized=true` in one-shot JSON output. The report must
distinguish a successful recorded command from proof that every user
requirement was met. Missing, failed, ambiguous or unrecognized actions,
failed provider finalization, and explicitly configured cumulative turn-token
caps still fail closed. This does not certify the correctness of arbitrary
code or make finalization a replacement for testing.
