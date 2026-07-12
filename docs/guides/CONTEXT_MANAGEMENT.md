# Context Management

Ash builds each provider request under a hard input limit derived from the
configured model window and completion reserve. The limit covers chat messages
and provider-facing tool schemas.

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
activates those matches for the next model iteration. Activation lasts for the
current session and resets when the session changes. Set
`tool_search_threshold = 0` to send the full catalog on every request.

Only visible schemas count against the tools budget. Deferred tools remain in
the runtime registry and continue through normal validation, permissions,
hooks, audit, event, and result-persistence paths after activation.

After a request is assembled, `ContextBudgetReport.fragments` records each
fragment's typed kind, source, trust class, token use and limit, truncation
state, SHA-256, and non-content metadata. The report intentionally does not
retain a second copy of prompt content. Use `/context` to inspect current
bucket use and fragment provenance.

## Compaction

The full transcript remains in SQLite. Compaction changes only the provider
request and the working `context_summary`:

1. Bound stale tool outputs while retaining their call identity.
2. Keep the configured recent message tail and complete assistant/tool pairs.
3. Extract prior user requests, referenced paths, tool actions, and assistant
   outcomes into a structured state section.
4. Preserve both the beginning and end of an earlier summary.
5. Redact secrets before persisting the updated summary.
6. Drop older complete entries only when the retained tail still exceeds the
   hard input limit.

The current summary is deterministic and extractive. Ash does not claim a
model-assisted compactor yet. A future model-assisted path must be optional,
bounded, provenance-tagged, failure-isolated, and fall back to this algorithm
without recursively exhausting the same context window.
