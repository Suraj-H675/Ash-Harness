"""ChromaDB-backed vector search and the embedding adapter pipeline.

Sprint 10 implements V3 semantic memory:

* :class:`EmbeddingAdapter` — abstract interface every embedder conforms to.
* :class:`ONNXLocalEmbedding` — offline 384-dim MiniLM via ONNX Runtime.
* :class:`OpenAIEmbedding` — remote 1536-dim text-embedding-3-small.
* :class:`DeterministicEmbedding` — hash-based 384-dim stub for tests and
  environments where neither ONNX nor OpenAI is reachable.
* :class:`InMemoryVectorIndex` and :class:`ChromaIndex` — vector backends.
* :class:`FTS5FallbackIndex` — lexical fallback wrapping the Sprint 6
  :class:`~ash.memory.fts5.FTS5Index`.
* :class:`VectorSearchPipeline` — tries the configured vector backend and
  falls back to FTS5 if the backend is missing or the query fails.
"""

from __future__ import annotations

import hashlib
import importlib
import math
import sqlite3
import struct
from abc import ABC, abstractmethod
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from ash.context.compaction import Chunk
from ash.core.redaction import redact_text
from ash.memory.fts5 import FTS5Index, query_lexical_fallback


# ---------------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------------


def cosine_similarity(query: Sequence[float], document: Sequence[float]) -> float:
    """
    Cosine similarity per spec section 5.3.

    Returns 0.0 when either vector has zero magnitude (avoids division by
    zero). Inputs are coerced to floats so callers can pass ints.
    """

    if len(query) != len(document):
        raise ValueError(
            f"Vector dimension mismatch: query={len(query)} document={len(document)}"
        )
    if not query:
        return 0.0

    dot = 0.0
    q_sq = 0.0
    d_sq = 0.0
    for q, d in zip(query, document, strict=True):
        qf = float(q)
        df = float(d)
        dot += qf * df
        q_sq += qf * qf
        d_sq += df * df
    if q_sq == 0.0 or d_sq == 0.0:
        return 0.0
    return dot / (math.sqrt(q_sq) * math.sqrt(d_sq))


def cosine_similarity_batch(
    query: Sequence[float],
    documents: Iterable[Sequence[float]],
) -> list[float]:
    """Vectorized cosine similarity over a sequence of document vectors."""

    return [cosine_similarity(query, doc) for doc in documents]


# ---------------------------------------------------------------------------
# Embedding adapters
# ---------------------------------------------------------------------------


class EmbeddingAdapter(ABC):
    """Common contract for embedding generators used by the vector index."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Dimensionality of the vectors produced by this adapter."""

        raise NotImplementedError

    @abstractmethod
    async def get_embedding(self, text: str) -> list[float]:
        """Return the embedding of ``text`` as a list of floats."""

        raise NotImplementedError

    async def get_embeddings(self, texts: Sequence[str]) -> list[list[float]]:
        """Default batched implementation; subclasses may override for speed."""

        return [await self.get_embedding(text) for text in texts]


class DeterministicEmbedding(EmbeddingAdapter):
    """
    Hash-based embedding adapter.

    Produces stable 384-dim vectors from a token-level hash, so tests do
    not require ONNX or a network call. Cosine similarity between
    identical text is 1.0; between disjoint text it is small but
    deterministic.
    """

    DIMENSION = 384

    def __init__(self, dimension: int = DIMENSION) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    async def get_embedding(self, text: str) -> list[float]:
        # Tokenize on whitespace + lowercase so common substrings align.
        tokens = text.lower().split()
        vec = [0.0] * self._dimension
        if not tokens:
            return vec

        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            # Two 32-bit ints per token — assign to two indices mod dim.
            idx_a = struct.unpack(">I", digest[:4])[0] % self._dimension
            idx_b = struct.unpack(">I", digest[4:8])[0] % self._dimension
            sign_a = 1.0 if digest[8] & 1 else -1.0
            sign_b = 1.0 if digest[9] & 1 else -1.0
            vec[idx_a] += sign_a
            vec[idx_b] += sign_b

        # L2-normalize so cosine similarity behaves like dot product.
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


class ONNXLocalEmbedding(EmbeddingAdapter):
    """
    Offline 384-dim embedding via an ONNX-exported MiniLM model.

    The class loads a local ``model.onnx`` and tokenizer when first used
    and caches the session. If either the ONNX runtime or the model
    files are missing, :meth:`get_embedding` raises
    :class:`EmbeddingBackendUnavailable` so the search pipeline can
    fall back to the FTS5 lexical index.
    """

    DIMENSION = 384
    DEFAULT_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

    def __init__(
        self, model_path: Path | str | None = None, *, dimension: int = DIMENSION
    ) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self._model_path = Path(model_path) if model_path is not None else None
        self._dimension = dimension
        self._session: Any = None
        self._tokenizer: Any = None
        self._load_attempted = False

    @property
    def dimension(self) -> int:
        return self._dimension

    def _ensure_loaded(self) -> None:
        if self._load_attempted:
            return
        self._load_attempted = True

        try:
            import onnxruntime  # type: ignore[import-not-found,import-untyped]
        except ImportError as exc:  # pragma: no cover - host dependent
            raise EmbeddingBackendUnavailable(
                "onnxruntime is not installed; install the 'vector' extra."
            ) from exc

        model_path = self._model_path
        if model_path is None or not model_path.exists():
            raise EmbeddingBackendUnavailable(
                f"ONNX model not found at {model_path!s}; provide a valid model_path."
            )

        try:
            self._session = onnxruntime.InferenceSession(
                str(model_path), providers=["CPUExecutionProvider"]
            )
        except Exception as exc:
            raise EmbeddingBackendUnavailable(
                f"Failed to load ONNX session from {model_path}: {exc}"
            ) from exc

        # Tokenizer is loaded best-effort from the optional tokenizers
        # package; if unavailable the embedding call returns a hash-based
        # fallback so the pipeline can still serve queries.
        try:
            from tokenizers import Tokenizer  # type: ignore[import-not-found]

            tokenizer_path = model_path.with_name("tokenizer.json")
            if tokenizer_path.exists():
                self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        except Exception:  # pragma: no cover - tokenizer is optional
            self._tokenizer = None

    async def get_embedding(self, text: str) -> list[float]:
        self._ensure_loaded()

        if self._tokenizer is None or self._session is None:
            # No real tokenizer: degrade to deterministic hash embedding.
            return await DeterministicEmbedding(self._dimension).get_embedding(text)

        try:
            encoding = self._tokenizer.encode(text)
            input_ids = [encoding.ids]
            attention_mask = [[1] * len(encoding.ids)]
            outputs = self._session.run(
                None,
                {
                    "input_ids": input_ids,
                    "attention_mask": attention_mask,
                },
            )
        except Exception as exc:
            raise EmbeddingBackendUnavailable(f"ONNX inference failed: {exc}") from exc

        # Mean-pool the token embeddings; first output is the token vectors.
        token_vectors = outputs[0][0]
        mask = attention_mask[0]
        pooled = [
            sum(token_vectors[t][d] for t in range(len(mask)) if mask[t])
            / max(1, sum(mask))
            for d in range(self._dimension)
        ]
        norm = math.sqrt(sum(v * v for v in pooled))
        if norm > 0:
            pooled = [v / norm for v in pooled]
        return pooled


class OpenAIEmbedding(EmbeddingAdapter):
    """Remote 1536-dim embedding via the OpenAI text-embedding-3-small API."""

    DIMENSION = 1536
    DEFAULT_MODEL = "text-embedding-3-small"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        client: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client = client
        self._owns_client = client is None

    @property
    def dimension(self) -> int:
        return self.DIMENSION

    def _resolve_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - host dependent
            raise EmbeddingBackendUnavailable(
                "The 'openai' package is not installed; install it for OpenAI embeddings."
            ) from exc
        self._client = (
            AsyncOpenAI(api_key=self._api_key) if self._api_key else AsyncOpenAI()
        )
        return self._client

    async def get_embedding(self, text: str) -> list[float]:
        client = self._resolve_client()
        try:
            response = await client.embeddings.create(model=self._model, input=text)
        except Exception as exc:
            raise EmbeddingBackendUnavailable(
                f"OpenAI embeddings call failed: {exc}"
            ) from exc
        return [float(x) for x in response.data[0].embedding]


class EmbeddingBackendUnavailable(RuntimeError):
    """Raised when an embedding adapter's backend cannot be reached."""


# ---------------------------------------------------------------------------
# Vector index backends
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VectorHit:
    """A single match returned by a vector index."""

    chunk_key: str
    file_path: str
    content: str
    score: float
    metadata: dict[str, Any]


class VectorIndex(ABC):
    """Common contract for vector index backends."""

    @abstractmethod
    def add(
        self,
        ids: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        documents: Sequence[str],
        metadatas: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        """Insert or replace vectors in the index."""

        raise NotImplementedError

    @abstractmethod
    def query(
        self,
        query_embedding: Sequence[float],
        top_k: int = 5,
    ) -> list[VectorHit]:
        """Return the top-k matches ranked by descending similarity."""

        raise NotImplementedError

    @abstractmethod
    def clear(self) -> None:
        """Delete every indexed record."""
        raise NotImplementedError


class InMemoryVectorIndex(VectorIndex):
    """
    Pure-Python vector index backed by a list of records.

    Used as the test-friendly default and as a last-resort fallback when
    ChromaDB is not importable. Cosine similarity is computed on demand
    so the index does not need to know the embedding dimension up front.
    """

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    def add(
        self,
        ids: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        documents: Sequence[str],
        metadatas: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        if not (len(ids) == len(embeddings) == len(documents)):
            raise ValueError("ids, embeddings, and documents must be the same length")
        meta_iter: Iterable[dict[str, Any]] = (
            metadatas if metadatas is not None else [{}] * len(ids)
        )
        for chunk_id, emb, doc, meta in zip(
            ids, embeddings, documents, meta_iter, strict=True
        ):
            record = {
                "id": chunk_id,
                "embedding": [float(x) for x in emb],
                "document": doc,
                "metadata": dict(meta),
            }
            # Replace if id already present.
            for i, existing in enumerate(self._records):
                if existing["id"] == chunk_id:
                    self._records[i] = record
                    break
            else:
                self._records.append(record)

    def query(
        self,
        query_embedding: Sequence[float],
        top_k: int = 5,
    ) -> list[VectorHit]:
        scored: list[tuple[float, dict[str, Any]]] = []
        for record in self._records:
            score = cosine_similarity(query_embedding, record["embedding"])
            scored.append((score, record))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        results: list[VectorHit] = []
        for score, record in scored[:top_k]:
            results.append(
                VectorHit(
                    chunk_key=str(record["metadata"].get("chunk_key", record["id"])),
                    file_path=str(record["metadata"].get("file_path", "")),
                    content=record["document"],
                    score=score,
                    metadata=record["metadata"],
                )
            )
        return results

    def __len__(self) -> int:
        return len(self._records)

    def clear(self) -> None:
        self._records.clear()


class ChromaIndex(VectorIndex):
    """ChromaDB-backed vector index using an embedded persistent client."""

    DEFAULT_COLLECTION = "ash_chunks"

    def __init__(
        self,
        persist_directory: Path | str,
        *,
        collection_name: str = DEFAULT_COLLECTION,
        client: Any | None = None,
    ) -> None:
        self._persist_directory = str(persist_directory)
        self._collection_name = collection_name
        self._owns_client = client is None
        self._client = client
        self._collection: Any = None
        self._init_attempted = False

    def _ensure_ready(self) -> None:
        if self._collection is not None:
            return
        if self._init_attempted and self._client is None:
            return
        self._init_attempted = True
        try:
            import chromadb  # type: ignore[import-not-found]
        except ImportError as exc:
            raise VectorBackendUnavailable(
                "chromadb is not installed; install the 'vector' extra or use InMemoryVectorIndex."
            ) from exc
        if self._client is None:
            self._client = chromadb.PersistentClient(path=self._persist_directory)
        self._collection = self._client.get_or_create_collection(self._collection_name)

    def add(
        self,
        ids: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        documents: Sequence[str],
        metadatas: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        self._ensure_ready()
        meta_list: list[dict[str, Any]] = (
            list(metadatas) if metadatas is not None else [{} for _ in ids]
        )
        # Chroma requires non-empty metadata dicts and JSON-serializable values.
        clean_meta: list[dict[str, Any]] = []
        for meta in meta_list:
            clean_meta.append({k: _to_chroma_value(v) for k, v in meta.items()})
        self._collection.upsert(
            ids=list(ids),
            embeddings=[[float(x) for x in e] for e in embeddings],
            documents=list(documents),
            metadatas=clean_meta,
        )

    def query(
        self,
        query_embedding: Sequence[float],
        top_k: int = 5,
    ) -> list[VectorHit]:
        self._ensure_ready()
        result = self._collection.query(
            query_embeddings=[[float(x) for x in query_embedding]],
            n_results=max(1, top_k),
        )
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        hits: list[VectorHit] = []
        for idx, chunk_id in enumerate(ids):
            doc = documents[idx] if idx < len(documents) else ""
            meta = metadatas[idx] if idx < len(metadatas) else {}
            # Chroma returns squared L2 distance by default for cosine; convert
            # to a similarity-like score in [0, 1] so callers can rank uniformly.
            distance = distances[idx] if idx < len(distances) else 0.0
            score = max(0.0, 1.0 - float(distance) / 2.0)
            hits.append(
                VectorHit(
                    chunk_key=str(meta.get("chunk_key", chunk_id)),
                    file_path=str(meta.get("file_path", "")),
                    content=doc,
                    score=score,
                    metadata=dict(meta),
                )
            )
        return hits

    def clear(self) -> None:
        self._ensure_ready()
        if self._client is None:
            raise VectorBackendUnavailable("Chroma client is unavailable")
        self._client.delete_collection(self._collection_name)
        self._collection = self._client.get_or_create_collection(self._collection_name)


class VectorBackendUnavailable(RuntimeError):
    """Raised when the configured vector backend cannot be used."""


def _to_chroma_value(value: Any) -> str | int | float | bool:
    """Coerce Python values to the subset Chroma accepts."""

    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# ---------------------------------------------------------------------------
# Lexical fallback
# ---------------------------------------------------------------------------


class FTS5FallbackIndex:
    """Wrap :class:`FTS5Index` so it conforms to the search pipeline contract."""

    def __init__(
        self, fts_index: FTS5Index | None = None, db_path: Path | str | None = None
    ) -> None:
        if fts_index is None and db_path is None:
            raise ValueError("Provide either fts_index or db_path")
        if fts_index is None:
            assert db_path is not None
            fts_index = FTS5Index(db_path)
        self._index = fts_index

    def add(
        self, chunks: Sequence[Chunk], file_path: str, sha256: str | None = None
    ) -> int:
        return self._index.index_document(file_path, list(chunks), sha256=sha256)

    def query(self, query_text: str, top_k: int = 5) -> list[VectorHit]:
        rows = self._index.query(query_text, limit=top_k)
        return [
            VectorHit(
                chunk_key=f"{row.get('file_path', '')}:unknown",
                file_path=str(row.get("file_path", "")),
                content=str(row.get("content", "")),
                score=float(row.get("rank", 0.0) or 0.0),
                metadata={"symbol_tags": str(row.get("symbol_tags", ""))},
            )
            for row in rows
        ]

    def clear(self) -> None:
        self._index.clear()


def fts5_query(
    db_path: Path | str, query_text: str, top_k: int = 5
) -> list[dict[str, Any]]:
    """
    Run a raw BM25-ranked FTS5 query against the supplied database.

    Mirrors :func:`ash.memory.fts5.query_lexical_fallback`; provided here
    so the vector module is self-contained for tests and ad-hoc scripts.
    """

    db_path_str = str(Path(db_path).expanduser())
    conn = sqlite3.connect(db_path_str)
    conn.row_factory = sqlite3.Row
    try:
        return query_lexical_fallback(conn, query_text, limit=top_k)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Combined pipeline
# ---------------------------------------------------------------------------


class VectorSearchPipeline:
    """
    Orchestrate vector lookups with graceful fallback to FTS5.

    The pipeline tries the configured :class:`VectorIndex` first. If the
    vector backend raises :class:`VectorBackendUnavailable` or
    :class:`EmbeddingBackendUnavailable`, the pipeline silently falls
    back to the supplied :class:`FTS5FallbackIndex` so user queries are
    always served.
    """

    def __init__(
        self,
        *,
        adapter: EmbeddingAdapter,
        vector_index: VectorIndex,
        lexical_index: FTS5FallbackIndex | None = None,
        vector_enabled: bool = True,
    ) -> None:
        self._adapter = adapter
        self._vector_index = vector_index
        self._lexical_index = lexical_index
        self._vector_enabled = vector_enabled

    @property
    def adapter(self) -> EmbeddingAdapter:
        return self._adapter

    @property
    def vector_index(self) -> VectorIndex:
        return self._vector_index

    @property
    def lexical_index(self) -> FTS5FallbackIndex | None:
        return self._lexical_index

    async def index_chunks(self, chunks: Sequence[Chunk], file_path: str) -> int:
        """Embed every chunk and upsert into the vector index."""

        if not chunks:
            return 0
        if self._lexical_index is not None:
            self._lexical_index.add(chunks, file_path)
        if not self._vector_enabled:
            return len(chunks)
        texts = [chunk.content for chunk in chunks]
        embeddings = await self._adapter.get_embeddings(texts)
        ids: list[str] = []
        metadatas: list[dict[str, Any]] = []
        for chunk in chunks:
            chunk_key = chunk.chunk_key
            ids.append(chunk_key)
            metadatas.append(
                {
                    "chunk_key": chunk_key,
                    "file_path": file_path,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                }
            )
        self._vector_index.add(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas,
        )
        return len(ids)

    async def search(
        self,
        query_text: str,
        *,
        top_k: int = 5,
    ) -> tuple[list[VectorHit], str]:
        """
        Return ``(hits, source)`` where ``source`` is ``"vector"`` or
        ``"lexical"`` so callers can log which backend served the query.
        """

        try:
            if not self._vector_enabled:
                if self._lexical_index is None:
                    return [], "lexical"
                return self._lexical_index.query(query_text, top_k=top_k), "lexical"
            query_embedding = await self._adapter.get_embedding(query_text)
            hits = self._vector_index.query(query_embedding, top_k=top_k)
            return hits, "vector"
        except (VectorBackendUnavailable, EmbeddingBackendUnavailable):
            if self._lexical_index is None:
                return [], "lexical"
            return self._lexical_index.query(query_text, top_k=top_k), "lexical"

    def clear(self) -> None:
        self._vector_index.clear()
        if self._lexical_index is not None:
            self._lexical_index.clear()

    def export(
        self,
        *,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """Return bounded, redacted indexed content for privacy inspection."""

        if limit < 1 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        exported = self._export_records(limit)
        return {
            "count": len(exported),
            "limit": limit,
            "redacted": True,
            "records": exported,
        }

    def _export_records(self, limit: int) -> list[dict[str, Any]]:
        vector_records: list[dict[str, Any]] = []
        if isinstance(self._vector_index, InMemoryVectorIndex):
            vector_records = [
                {
                    "source": "vector",
                    "chunk_key": str(
                        record.get("metadata", {}).get("chunk_key", record["id"])
                    ),
                    "file_path": redact_text(
                        str(record.get("metadata", {}).get("file_path", ""))
                    ),
                    "content": redact_text(str(record["document"])[:4_000]),
                    "metadata": {
                        key: value
                        for key, value in record.get("metadata", {}).items()
                        if isinstance(value, (str, int, float, bool))
                    },
                }
                for record in self._vector_index._records[:limit]
            ]
            if len(vector_records) >= limit:
                return vector_records[:limit]
        lexical = self._lexical_index
        if isinstance(lexical, FTS5FallbackIndex):
            with closing(sqlite3.connect(str(lexical._index.db_path))) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT file_path, content, symbol_tags
                    FROM fts_index
                    ORDER BY rowid
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                vector_records.extend(
                    {
                        "source": "lexical",
                        "chunk_key": f"{row['file_path']}:{index + 1}",
                        "file_path": redact_text(row["file_path"]),
                        "content": redact_text(row["content"][:4_000]),
                        "symbol_tags": redact_text(row["symbol_tags"]),
                    }
                    for index, row in enumerate(rows)
                )
        seen: set[tuple[str, str]] = set()
        unique: list[dict[str, Any]] = []
        for record in vector_records:
            identity = (record["source"], record["content"])
            if identity in seen:
                continue
            seen.add(identity)
            unique.append(record)
        return unique


# ---------------------------------------------------------------------------
# Optional importability probe
# ---------------------------------------------------------------------------


def vector_extras_available() -> bool:
    """Return ``True`` when the ``[vector]`` optional dependencies import."""

    for module_name in ("chromadb", "onnxruntime"):
        try:
            importlib.import_module(module_name)
        except Exception:
            return False
    return True
