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
2. Keep the configured recent message tail and complete assistant/tool pairs.
3. Extract user requirements, assistant decisions and progress, referenced
   paths, tool actions, recent tool outputs, and compacted events into bounded,
   labeled sections. Tool outputs remain untrusted evidence.
4. Preserve both the beginning and end of an earlier summary.
5. Redact secrets before persisting the updated summary.
6. Drop older complete entries only when the retained tail still exceeds the
   hard input limit.

The current summary is deterministic and extractive. Ash does not claim a
model-assisted compactor yet. A future model-assisted path must be optional,
bounded, provenance-tagged, failure-isolated, and fall back to this algorithm
without recursively exhausting the same context window.
