# ASH — Context & Memory Specification

This document details the context budgeting, token allocation, historical log compaction, and repository mapping algorithms for the Ash harness.

---

## 1. Context Window Budgets & Packing Order

When assembling the prompt context for a model invocation, the `ContextBuilder` packs information according to a strict priority hierarchy. If the total context footprint exceeds the model's max limit, lower-priority components are pruned or compacted.

```
┌─────────────────────────────────────────────────────────────┐ ▲ High Priority
│ 1. Core System Prompt (Safety + Tools Schema + XML Tags)    │ │
├─────────────────────────────────────────────────────────────┤ │
│ 2. Active Code Files (Directly requested or edited files)   │ │
├─────────────────────────────────────────────────────────────┤ │
│ 3. User Input (Current turn message)                        │ │
├─────────────────────────────────────────────────────────────┤ │
│ 4. Short-term Session History (Last 5 conversation turns)   │ │
├─────────────────────────────────────────────────────────────┤ │
│ 5. Compacted Session History Summary (Layered/Anchored)     │ │
├─────────────────────────────────────────────────────────────┤ │
│ 6. Dynamic Repository Map (Personalized PageRank symbols)    │ │
├─────────────────────────────────────────────────────────────┤ │
│ 7. Relevant Semantic Memories (FTS5 search results)         │ ▼ Low Priority
└─────────────────────────────────────────────────────────────┘
```

### Context Budgeting Strategy
For a standard `128,000` token context limit:
*   **System Prompt & Tools Schema**: Max 3,000 tokens (strict limit).
*   **Active Files Buffer**: Max 45,000 tokens.
*   **Active History**: Max 40,000 tokens (latest turns, raw).
*   **Compacted Summary**: Max 10,000 tokens.
*   **Repository Map**: Max 20,000 tokens.
*   **Semantic Memories**: Max 10,000 tokens.

---

## 2. Context Compaction Algorithms

When historical messages exceed the active history budget (e.g. 40,000 tokens), Ash executes a two-layer compaction routine: **Layered Truncation** followed by **Anchored Iterative Summarization**.

### 2.1 Layered Compaction
Instead of summarising the entire history from scratch (which loses specific code details), Ash applies structured truncation rules to historical turns:
1.  **Masking tool outputs**: Keep the tool name and argument JSON, but strip tool response content if the turn is older than 5 cycles.
2.  **Truncate lines**: If raw file reads or massive compiler error logs exist in older turns, strip the middle lines and preserve only the start/end lines along with a summary statement.
3.  **JSON extraction**: Extract tool usage records as structured JSON schemas to save space.

### 2.2 Anchored Iterative Summarization
To update the persistent history summary without rebuilding it, Ash sends the old summary, the latest active turns, and the current task to a helper LLM instance using the following schema:

```python
class HistorySummary(BaseModel):
    core_sprint_goal: str = Field(..., description="The main problem being solved.")
    key_decisions_made: list[str] = Field(..., description="Architectural choices or code patterns established.")
    completed_checkpoints: list[str] = Field(..., description="Files written, tests passed, or bugs fixed.")
    remaining_blockers: list[str] = Field(..., description="Outstanding tasks or bugs to address.")
    active_state_variables: dict[str, str] = Field(..., description="Key variables, functions, or file paths currently under modification.")

def anchor_summarization(
    previous_summary: HistorySummary,
    new_turns: list[dict],
    provider: ProviderABC
) -> HistorySummary:
    """
    Sends the previous structured summary and the new turns to the provider
    to get an updated HistorySummary object without losing historical state anchor points.
    """
    # LLM instruction asks to MERGE the new turns into the existing structure
    # and enforces output matching the HistorySummary JSON schema.
    ...
```

---

## 3. Token Counting & Rate Limiting

To avoid hitting API limits or causing unexpected billing spikes, Ash implements a **Token Bucket Rate Limiter** alongside provider-specific exact counting.

### 3.1 Token Bucket Algorithm
```python
import time
from typing import Tuple

class TokenBucketRateLimiter:
    def __init__(self, capacity: int, fill_rate: float) -> None:
        self.capacity = capacity      # Max tokens/requests bucket can hold
        self.fill_rate = fill_rate    # Tokens/requests added per second
        self.tokens = float(capacity)
        self.last_update = time.monotonic()

    def consume(self, tokens_needed: int) -> Tuple[bool, float]:
        """
        Attempts to consume tokens.
        Returns: (success, wait_time_seconds)
        """
        now = time.monotonic()
        elapsed = now - self.last_update
        self.last_update = now

        # Add accumulated tokens based on elapsed time
        self.tokens = min(self.capacity, self.tokens + (elapsed * self.fill_rate))

        if self.tokens >= tokens_needed:
            self.tokens -= tokens_needed
            return True, 0.0

        # Calculate how long to wait to accumulate enough tokens
        deficit = tokens_needed - self.tokens
        wait_time = deficit / self.fill_rate
        return False, wait_time
```

### 3.2 Provider Token Counts
*   **Anthropic**: Queries client-side token calculators or uses headers returned in the response stream.
*   **OpenAI**: Uses the `tiktoken` library with the specific encoding matching the active model (e.g. `cl100k_base` or `o200k_base`).

---

## 4. Repository Mapping

The repository map allows Ash to understand code definitions and imports across the entire workspace without loading all files into context. It utilizes **Tree-sitter** for symbol parsing and a **Personalized PageRank (PPR)** algorithm to rank symbol importance relative to the user's active file focus.

```
1. File Parse   --->  2. Construct Dependency Graph  --->  3. Calculate PPR Ranks
┌─────────────┐       ┌───────────────────────────┐         ┌───────────────────┐
│ config.py   │ ───>  │ config.py                 │  ───>   │ config: PPR=0.45  │
│ main.py     │       │   └─> db.py (uses config) │         │ db:     PPR=0.35  │
│ db.py       │       │   └─> main.py (uses db)   │         │ main:   PPR=0.20  │
└─────────────┘       └───────────────────────────┘         └───────────────────┘
```

### 4.1 Tree-sitter Parser Wrapper
```python
from pathlib import Path
from tree_sitter import Parser, Language
import tree_sitter_python as tspython

class SymbolExtractor:
    def __init__(self) -> None:
        self.parser = Parser()
        # Initialize parser using Python grammar
        self.language = Language(tspython.language())
        self.parser.set_language(self.language)

    def extract_symbols(self, file_path: Path) -> list[dict]:
        """
        Parses a file and returns a list of symbols:
        Classes, functions, imports, method declarations, and global variables.
        """
        content = file_path.read_bytes()
        tree = self.parser.parse(content)
        query_str = """
        (class_definition name: (identifier) @class.name)
        (function_definition name: (identifier) @function.name)
        (import_statement) @import
        (import_from_statement) @import
        """
        query = self.language.query(query_str)
        captures = query.captures(tree.root_node)
        # Parse captures into structured dicts containing name, type, and start/end lines
        ...
```

### 4.2 Personalized PageRank (PPR)
PPR computes symbol importance starting from a specific set of **Active Files** (the teleport vector), ensuring the repo map displays contextually relevant files first instead of just globals.

$$\mathbf{v} = (1 - \alpha) \mathbf{M} \mathbf{v} + \alpha \mathbf{p}$$

Where:
*   $\mathbf{v}$: PageRank score vector.
*   $\mathbf{M}$: Transition probability matrix (edges represent code dependencies, class inheritances, or method calls).
*   $\alpha$: Damping factor (typically $0.85$).
*   $\mathbf{p}$: Teleport vector, concentrated entirely on the user's currently active files.

```python
import numpy as np
from typing import Dict, List

def calculate_personalized_pagerank(
    adjacency_matrix: np.ndarray,
    teleport_indices: List[int],
    alpha: float = 0.85,
    max_iter: int = 100,
    tol: float = 1e-6
) -> np.ndarray:
    """
    Computes Personalized PageRank for a directed dependency graph.
    teleport_indices: indices of files open/edited in current session.
    """
    n = adjacency_matrix.shape[0]
    if n == 0:
        return np.array([])

    # Normalize adjacency matrix columns (transitions)
    column_sums = adjacency_matrix.sum(axis=0)
    # Handle dangling nodes (nodes with no outbound edges)
    normalized_matrix = np.zeros_like(adjacency_matrix, dtype=float)
    for col in range(n):
        if column_sums[col] > 0:
            normalized_matrix[:, col] = adjacency_matrix[:, col] / column_sums[col]
        else:
            normalized_matrix[:, col] = np.ones(n) / n

    # Initialize teleport vector p
    p = np.zeros(n)
    if teleport_indices:
        p[teleport_indices] = 1.0 / len(teleport_indices)
    else:
        p[:] = 1.0 / n

    # Power iteration
    v = np.copy(p)
    for _ in range(max_iter):
        v_next = (1.0 - alpha) * np.dot(normalized_matrix, v) + alpha * p
        if np.linalg.norm(v_next - v, 1) < tol:
            return v_next
        v = v_next
    return v
```
Using the PPR scores, Ash extracts code definitions (signatures) from the highest-ranking files and writes them to a structured Markdown representation in the context.

---

## 5. Semantic Memory & FTS5 Lookup Pipeline

To retrieve relevant code blocks from past sessions or files that are not currently open, Ash executes a dual-tier search pipeline.

```
                  ┌────────────────────────────────┐
                  │      User Input Query          │
                  └────────────────────────────────┘
                                  │
                 ┌────────────────┴────────────────┐
                 ▼                                 ▼
       [ Vector Embeddings ]              [ SQLite FTS5 MATCH ]
    (ChromaDB / Optional Package)      (Lexical Fallback / Standard)
                 │                                 │
     Cosine Similarity Check               BM25 Lexical Score
                 │                                 │
                 └────────────────┬────────────────┘
                                  ▼
                     Merge & Rank Top 5 Chunks
                                  ▼
                    Inject to Context Builder
```

### 5.1 Document Chunking Strategy
To prevent code blocks from losing structural context, documents are chunked using a **Line-Preserving Sliding Window**:
*   **Window Size**: 30 lines (approx. 1,500 characters).
*   **Overlap**: 5 lines (approx. 250 characters).
*   **Chunk Key**: `file_path:start_line-end_line`.

### 5.2 Embedding Adapter Interface
If `chromadb` is available, Ash uses the following interface for vector lookups:

```python
from abc import ABC, abstractmethod

class EmbeddingAdapter(ABC):
    @abstractmethod
    async def get_embedding(self, text: str) -> list[float]:
        """Returns a list of floats representing the text representation."""
        ...

class ONNXLocalEmbedding(EmbeddingAdapter):
    """
    Local embedding generator using a quantized sentence-transformers/all-MiniLM-L6-v2
    model executed via ONNX runtime, ensuring 100% offline functionality.
    Produces 384-dimensional vectors.
    """
    ...

class OpenAIEmbedding(EmbeddingAdapter):
    """
    Remote embedding generator using text-embedding-3-small (1536 dimensions).
    """
    ...
```

### 5.3 Cosine Similarity Matching
Vectors returned from the embedding adapter are compared using the cosine similarity metric:

$$\text{Cosine Similarity}(\mathbf{q}, \mathbf{d}) = \frac{\sum_{i=1}^{D} q_i d_i}{\sqrt{\sum_{i=1}^{D} q_i^2} \sqrt{\sum_{i=1}^{D} d_i^2}}$$

Where:
*   $\mathbf{q}$: Query embedding vector.
*   $\mathbf{d}$: Indexed document chunk vector.
*   $D$: Vector dimension (384 or 1536).

### 5.4 SQLite FTS5 BM25 Lexical Fallback
If the vector dependencies are missing or the API returns an error, the pipeline falls back gracefully to a lexical query in `fts5.db`:

```python
def query_lexical_fallback(db_conn, query_str: str, limit: int = 5) -> list[dict]:
    """
    Queries the FTS5 virtual table using SQLite's built-in BM25 scoring algorithm.
    """
    cursor = db_conn.cursor()
    # sqlite3's bm25 ranking matches lower scores first (descending relevance)
    cursor.execute(
        """
        SELECT file_path, content, symbol_tags, bm25(fts_index) as rank
        FROM fts_index
        WHERE fts_index MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (query_str, limit)
    )
    return [dict(row) for row in cursor.fetchall()]
```
The resulting top chunks are formatted and injected as a supplemental background reference block inside the context window.
