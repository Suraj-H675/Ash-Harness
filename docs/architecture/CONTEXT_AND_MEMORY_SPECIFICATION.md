# ASH — Context & Memory Specification

This document describes the implemented provider-input, history compaction,
repository-map and memory boundaries. The live code, rather than an illustrative
algorithm, is authoritative if they differ.

## 1. Provider-input assembly and budgets

`AshLoop._build_messages()` in `src/ash/core/loop.py` gathers live project
instructions, the skill catalog, repository-map output, Goal and Sprint state,
untrusted prior-session/project memory, tool schemas, and canonical session
history. It preserves assistant tool-call IDs, tool results, and opaque provider
replay state. The current user message and its active tool chain are protected
from truncation.

For configured runtimes, the loop builds a typed `ContextAssemblyRequest` and
calls `ash.context.assembly.assemble_context()`. This deterministic function
returns provider-visible messages, a `ContextBudgetReport`, content-free usage
telemetry, and a proposed `CompactionResult`. It does **not** write SQLite,
modify a `Session`, emit events or update the UI. `AshLoop` owns those effects:
it publishes token usage, persists a redacted summary when needed, adjusts the
resident history window **after** persistence succeeds, and then returns the
assembled provider request. Unconfigured legacy/test loops retain the original
minimal, unbudgeted assembly path.

`resolve_request_token_budget()` computes a provider-aware combined context
limit, input limit and output reserve from configuration and declared provider
capabilities. A request must fit the input limit after reserving output tokens.
The `ContextBudgetAllocator` uses configurable relative weights, with these
defaults:

| Section | Default share | Content |
| --- | ---: | --- |
| System | 20% | Core instructions, persistent instructions, supplemental skills |
| Tools | 15% | Model-visible catalog/schema accounting |
| History | 45% | Current turn, session transcript and bounded historical summary |
| Repository map | 10% | Ranked code-symbol context |
| Memory | 10% | Goal/Sprint state, recalled memory and prior-session context |

These are **relative allocations**, not fixed hardcoded 128K-token quotas.
The system slice redistributes space between core, persistent and
supplemental content (initial weights 55/35/10). Persistent instructions
preserve both ends when truncated. A built-in untrusted-content boundary
is always retained; content from a repository, memory or tool does not gain
permission authority by appearing in the prompt.

Native tool schemas are charged outside the chat messages; non-native
catalogs are included in system text for the XML fallback protocol, without
double-charging. Provider-specific token counting is used for budgeting,
and actual provider usage is normalized separately after completion.

## 2. Compaction and state boundaries

`HistoryCompactor` in `src/ash/context/history.py` bounds stale tool output,
preserves complete assistant tool-call/result groups, and makes a deterministic,
extractive summary when the configured threshold requires it. The current
active turn's user input and tool chain cannot be silently dropped. If
protected material cannot fit, the request fails with
`ContextBudgetExceededError` rather than dispatching a broken prompt.

The durable SQLite transcript remains intact; only the model-visible
window and a separately stored bounded summary change. Old summaries carry
forward anchored historical decisions and tool evidence. The summary is
redacted before persistence, and resident message offsets count only durable
messages, not transient runtime-only entries. Failed summary persistence
must leave the live session history and summary unchanged.

**Opaque reasoning replay:** if history contains provider-owned reasoning
state, Ash preserves the original request prefix exactly instead of
locally pruning or summarizing that prefix. A request that cannot fit is
refused. Provider-native checkpointing, when genuinely supported, installs
a separate child session; an explicit `/restart-context` creates a new
reasoning window with a user handoff, only after recovery and action-integrity
checks. Neither mechanism silently replays tool side effects.

`/context` and the `context.usage` event expose section counts and
truncation status. Fragment provenance uses hashes and bounded metadata,
not a second persistent copy of source contents.

## 3. Provider counting and rate limiting

Input estimates are counted by the active provider's `count_tokens` method;
returned usage, cache reads and writes, and missing-usage estimates remain
distinguishable in the model-usage/reporting pipeline. The separate
`TokenBucketRateLimiter` in `src/ash/providers/rate_limiter.py` is an
exported utility, not an automatically attached per-provider request
throttle. The active provider transport instead applies the configured
retry, cooldown/circuit-breaker and failure classification policies.

## 4. Repository-map input

`RepoMap` in `src/ash/repo/repomap.py` incrementally parses supported source
languages with Tree-sitter, extracts definitions/imports/references, maintains
a sparse dependency graph and ranks files using personalized PageRank.
The ranking favors the current active-file context, or defaults to a
workspace-wide distribution when there is no active focus. A typical prompt
renders up to five ranked files with up to six selected symbols per file;
it does **not** place entire source files into the system prompt.

Refresh and rendering are best-effort: a pending map can be omitted; a
failed refresh is reported without turning untrusted repository text into
instructions. Budgeted assembly bounds this section separately. The map
does not replace explicit safe source reads by the coding agent.

---

## 5. Project Memory Retrieval Pipeline

Project memory is durable **derived state owned by Ash**. It is stored in one
per-workspace SQLite database under the configured Ash `db_directory`; it is
not stored in the repository and does not require a separate vector database.

```text
<db_directory>/memory/v1-<workspace-hash>/memory.db
```

```
                  ┌────────────────────────────────┐
                  │      User Input Query          │
                  └────────────────────────────────┘
                                  │
                 ┌────────────────┴────────────────┐
                 ▼                                 ▼
       [ Optional Embeddings ]             [ SQLite FTS5 MATCH ]
       (explicit provider only)              (always available)
                 │                                 │
       Cosine Similarity                  AND-first BM25 Search
                                           OR relaxation if empty
                 │                                 │
                 └────────────────┬────────────────┘
                                  ▼
                     Reciprocal-Rank Fusion
                                  ▼
                    Inject as untrusted context
```

### 5.1 Single Transactional Store

`memory.db` contains document metadata, canonical chunks, FTS5 rows, optional
embedding BLOBs, and index-identity metadata. Replacing a document publishes
its text, lexical rows, and vectors in one SQLite transaction, so a failed
replacement cannot expose mixed document generations.

Vectors are stored as bounded little-endian `float32` values with a validated
dimension. Corrupt or incompatible payloads fail closed. The database runs in
Ash uses WAL mode on SQLite runtimes containing the upstream WAL-reset
corruption fix and falls back to rollback-journal mode on affected
versions. Database and any SQLite sidecar files remain private on POSIX
systems.

On Linux, SQLite is opened through the held directory descriptor
(`/proc/self/fd/<dirfd>/memory.db`), so ordinary replacement of the visible
parent directory cannot redirect the DB or its sidecars. Other supported
platforms use Ash's anchored-directory and path-identity checks.

### 5.2 Document Chunking Strategy
To prevent code blocks from losing structural context, documents are chunked using a **Line-Preserving Sliding Window**:
*   **Window Size**: 30 lines (approx. 1,500 characters).
*   **Overlap**: 5 lines (approx. 250 characters).
*   **Chunk Key**: `workspace_relative_path:start_line-end_line`.

The chunking contract has an explicit version. Incompatible future chunking
changes invalidate derived memory instead of silently mixing generations.

### 5.3 Lexical Retrieval Is the Baseline

SQLite FTS5 is always available when project memory is enabled. Ash converts
natural-language input into bounded literal terms and first issues an **AND**
query. If that returns no results, it deliberately relaxes to **OR** recall.
BM25 scores rank candidates only inside the lexical channel; they are never
compared directly with cosine scores.

### 5.4 Semantic Retrieval Is Explicit

Semantic embeddings are optional. Ash never enables them merely because an API
key or optional package happens to be installed.

`embedding_provider` supports:

* `none` — default; lexical memory only and no embedding network cost.
* `openai` — `text-embedding-3-small`; chunk text supplied for indexing and
  memory-search queries are sent to OpenAI to produce embeddings.
* `onnx` — explicit local ONNX model plus adjacent `tokenizer.json`; install
  the `local-embeddings` capability pack.

There is no deterministic/hash embedding fallback in production. If a real
embedding backend is unavailable, fresh lexical state is still published and
that document simply has no vector. Query-time embedding failures likewise
degrade to lexical retrieval.

The embedding adapter interface is:

```python
from abc import ABC, abstractmethod

class EmbeddingAdapter(ABC):
    @abstractmethod
    async def get_embedding(self, text: str) -> list[float]:
        """Returns a list of floats representing the text representation."""
        ...

class ONNXLocalEmbedding(EmbeddingAdapter): ...
class OpenAIEmbedding(EmbeddingAdapter): ...
```

### 5.5 Embedding Identity and Cosine Matching

Stored vectors are bound to an explicit embedding identity and dimension. For
OpenAI the identity includes the model and dimension. For local ONNX it also
includes model/tokenizer filesystem identity, size, and modification metadata.
When that identity changes, Ash discards incompatible vectors while preserving
lexical chunks for continued search and incremental re-embedding.

Vectors returned from the embedding adapter are compared using the cosine similarity metric:

$$\text{Cosine Similarity}(\mathbf{q}, \mathbf{d}) = \frac{\sum_{i=1}^{D} q_i d_i}{\sqrt{\sum_{i=1}^{D} q_i^2} \sqrt{\sum_{i=1}^{D} d_i^2}}$$

Where:
*   $\mathbf{q}$: Query embedding vector.
*   $\mathbf{d}$: Indexed document chunk vector.
*   $D$: Vector dimension (384 or 1536).

### 5.6 Hybrid Ranking

When both lexical and semantic channels return candidates, Ash combines their
**ranks**, not their incompatible raw score scales, using normalized reciprocal
rank fusion. Cross-channel agreement is rewarded; lexical rank is the
deterministic tie-break so exact source evidence wins equal fusion scores.

### 5.7 Bounds, Privacy, and Trust

Indexing is bounded by file count, file size, scan depth, and scan-entry caps.
Software vector scanning is additionally capped at 100,000 records. Current
benchmarks show that in-process cosine search is sufficient at Ash's intended
repository scale, so a native vector extension is deferred until measured
workloads justify one.

Memory exports are bounded and redacted. Recalled content is injected as
**untrusted workspace data**: it can supply factual context but never grants
permissions, authorizes tools, overrides system instructions, or changes the
safety policy.
