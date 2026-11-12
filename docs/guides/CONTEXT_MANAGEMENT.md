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

An adapter that does not accept a response ceiling can only use the reserve for
input admission; the ChatGPT-plan preview route currently does not transmit a
maximum response value.

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
returns the best matching names, descriptions, and exact input schemas and
activates those matches for the next model iteration. Ash retains at most 64
recently activated deferred schemas per session; re-searching a tool refreshes
its recency, and older activations fall out of the provider-visible window while
remaining available through `search_tools`. The activation window resets when
the session changes. Set
`tool_search_threshold = 0` to send the full catalog on every request.

Only visible schemas count against the tools budget. Deferred tools remain in
the runtime registry and continue through normal validation, permissions,
hooks, audit, event, and result-persistence paths after activation.

When the **effective** provider input limit is at least 24,576 tokens and
deferred discovery is active, Ash also exposes the existing read-only
`git_diff` schema directly. The generated system prompt includes concise
coding guidance for focused inspection, coordinated edits and authorized
testing. This does not grant shell access or alter any tool permissions.
The `read_files` tool remains discoverable through `search_tools` at every
context size rather than adding another default tool schema. It groups up to
four focused file ranges into
one request, with a 16,384-character combined source cap and the same path,
binary, SHA-256, encoding and truncation checks as `read_file`. This is a
convenience for model round trips, not a replacement for focused searching or
permissioned test execution. An initial large-context multi-file coding trial
used this tool but did not complete the independent task; eager promotion is
therefore not treated as a demonstrated coding improvement.
Batch requests inherit the managed, session and persistent `read_file`
permission rules for **each** requested path; a path-scoped deny blocks the
entire batch, and a required approval cannot be bypassed by grouping reads.
`apply_patch` remains deferred, including on large models; making its schema
eager previously harmed local-model multi-file trials, and its usefulness as
a general default has not been established. Models with smaller *actual*
input limits keep the original compact catalog and prompt, even if the user
configured a larger nominal context window.

After a request is assembled, `ContextBudgetReport.fragments` records each
fragment's typed kind, source, trust class, token use and limit, truncation
state, SHA-256, and non-content metadata. The report intentionally does not
retain a second copy of prompt content. Use `/context` to inspect current
bucket use and fragment provenance. `/context --provenance` shows the exact
provider-visible tool count, an eight-name preview, and how many registry tools
remain deferred. It also reports whether core instructions, loaded persistent
instructions, or supplemental system context were truncated.

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

After inspecting the workspace and any recovery warnings, an interactive
user may run `/restart-context <explicit handoff>` to open a **new reasoning
window**. The old session, action history and audit log remain available for
review. Ash reconciles the interrupted journal under the session lease before
the fork. Only completed, known tool outcomes (or safely compensated edits)
permit this explicit restart. Pending turns, ambiguous external actions,
unresolved file checkpoints and active external tasks still block it. The
child inherits neither previous tool approvals nor opaque reasoning items;
no actions are automatically replayed. The handoff is user-supplied, not
a model-generated completion claim, and the user must verify partial edits.

This is **manual recovery**, not transparent automatic continuation or a
substitute for long-task context management. `/compact-native` remains
unavailable on the ChatGPT-plan route until the native contract is verified.

### Opt-in one-shot continuation

For a **new** non-interactive coding session, `ash -p "..." \
--context-continuations 1` authorizes at most one extra reasoning window
after an opaque reasoning-state context overflow. `2` allows two; the default
is **0 (disabled)**. This is not enabled for resumed sessions, attachments,
or structured JSON-schema outputs.

The original user task is preserved verbatim in a bounded handoff. Ash first
closes and reconciles the interrupted turn, verifies its durable recovery
record, and forks a fresh context without importing any reasoning items,
tool results, approvals or tool dispatches. Automatic continuation is
**refused** if the previous turn dispatched any shell command or other
external tool, even when its result is known, or if it has ambiguous actions,
unfinished edits or unverified external tasks. Only known-successful local
read/search and direct workspace edit tools may precede an automatic handoff.
The new turn is instructed to inspect the actual workspace state and verify
the task before reporting completion; any tool execution in the new turn
still passes normal permissions, sandbox and audit checks.

This option does not resolve context limits automatically in every case. It
does not extend the outer process deadline, retries neither provider outages
nor failed tests, and stops after the configured number of restarts. A
failure can leave partial edits in the workspace; always inspect the
recorded session lineage and working tree. It must not be interpreted as an
exactly-once guarantee for arbitrary external actions.
