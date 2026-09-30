"""Durable lexical/vector memory pipeline backed by one SQLite index."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ash.context.compaction import Chunk, DEFAULT_OVERLAP, DEFAULT_WINDOW_SIZE
from ash.core.redaction import redact_text
from ash.memory.embeddings import EmbeddingAdapter, EmbeddingBackendUnavailable
from ash.memory.sqlite_index import (
    MAX_MEMORY_SEARCH_RESULTS,
    MemoryCandidate,
    MemoryDocument,
    SQLiteMemoryIndex,
)


RRF_K = 60
MEMORY_CHUNKING_VERSION = (
    f"sliding-lines-{DEFAULT_WINDOW_SIZE}-overlap-{DEFAULT_OVERLAP}-v1"
)


@dataclass(frozen=True)
class MemoryHit:
    """One ranked result returned from project memory."""

    chunk_key: str
    file_path: str
    content: str
    score: float
    metadata: dict[str, Any]


class MemorySearchPipeline:
    """Publish and search one transactional memory index.

    Lexical search is always available.  Semantic retrieval is enabled only
    when an explicit real embedding adapter is supplied.  Vector and lexical
    ranks are fused rather than mixing their backend-specific raw score scales.
    """

    def __init__(
        self,
        *,
        index: SQLiteMemoryIndex,
        adapter: EmbeddingAdapter | None = None,
        embedding_identity: str | None = None,
    ) -> None:
        if adapter is None and embedding_identity is not None:
            raise ValueError("embedding_identity requires an embedding adapter")
        if adapter is not None and not (embedding_identity or "").strip():
            raise ValueError("embedding adapters require an explicit identity")
        self._index = index
        self._adapter = adapter
        self._embedding_identity = (
            embedding_identity.strip() if embedding_identity is not None else None
        )
        if adapter is not None:
            assert self._embedding_identity is not None
            index.configure_embedding(self._embedding_identity, adapter.dimension)

    @property
    def adapter(self) -> EmbeddingAdapter | None:
        return self._adapter

    @property
    def index(self) -> SQLiteMemoryIndex:
        return self._index

    async def aclose(self) -> None:
        if self._adapter is not None:
            await self._adapter.aclose()

    async def index_chunks(self, chunks: Sequence[Chunk], file_path: str) -> int:
        return await self.index_documents(((chunks, file_path),))

    async def index_documents(
        self,
        documents: Sequence[tuple[Sequence[Chunk], str]],
        *,
        batch_size: int = 64,
    ) -> int:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        file_paths = [file_path for _chunks, file_path in documents]
        if len(file_paths) != len(set(file_paths)):
            raise ValueError("documents must contain unique file paths")

        indexed = 0
        for offset in range(0, len(documents), batch_size):
            batch = list(documents[offset : offset + batch_size])
            if not batch:
                continue
            all_chunks = [chunk for chunks, _path in batch for chunk in chunks]
            indexed += len(all_chunks)
            embeddings: list[list[float]] | None = None
            if self._adapter is not None and all_chunks:
                try:
                    embeddings = await self._adapter.get_embeddings(
                        [chunk.content for chunk in all_chunks]
                    )
                except EmbeddingBackendUnavailable:
                    embeddings = None
                if embeddings is not None and len(embeddings) != len(all_chunks):
                    raise ValueError(
                        "embedding adapter returned a different number of vectors"
                    )

            prepared: list[MemoryDocument] = []
            embedding_offset = 0
            for chunks, file_path in batch:
                chunk_list = list(chunks)
                chunk_embeddings: list[list[float]] | None = None
                if embeddings is not None:
                    chunk_embeddings = embeddings[
                        embedding_offset : embedding_offset + len(chunk_list)
                    ]
                embedding_offset += len(chunk_list)
                prepared.append(
                    MemoryDocument(
                        file_path=file_path,
                        chunks=chunk_list,
                        embeddings=chunk_embeddings,
                    )
                )
            self._index.replace_documents(prepared)
        return indexed

    async def search(
        self,
        query_text: str,
        *,
        top_k: int = 5,
    ) -> tuple[list[MemoryHit], str]:
        if top_k < 1:
            return [], "lexical"
        candidate_limit = min(
            MAX_MEMORY_SEARCH_RESULTS,
            max(top_k, top_k * 4),
        )
        lexical = self._index.lexical_search(query_text, limit=candidate_limit)
        vector: list[MemoryCandidate] = []
        if self._adapter is not None:
            try:
                query_embedding = await self._adapter.get_embedding(query_text)
                vector = self._index.vector_search(
                    query_embedding,
                    limit=candidate_limit,
                )
            except EmbeddingBackendUnavailable:
                vector = []

        if lexical and vector:
            source = "hybrid"
        elif vector:
            source = "vector"
        else:
            source = "lexical"
        return _fuse_ranked_candidates(lexical, vector, top_k=top_k), source

    def delete_document(self, file_path: str) -> int:
        return self._index.delete_document(file_path)

    def document_paths(self, *, limit: int = 10_000) -> set[str]:
        return self._index.document_paths(limit=limit)

    def clear(self) -> None:
        self._index.clear()

    def export(self, *, limit: int = 1000) -> dict[str, Any]:
        records = self._index.export_records(limit=limit)
        exported = [
            {
                "source": "memory",
                "chunk_key": redact_text(str(record["chunk_key"])),
                "file_path": redact_text(str(record["file_path"])),
                "content": redact_text(str(record["content"])[:4_000]),
                "start_line": int(str(record["start_line"])),
                "end_line": int(str(record["end_line"])),
                "has_embedding": bool(record["has_embedding"]),
            }
            for record in records
        ]
        return {
            "count": len(exported),
            "limit": limit,
            "redacted": True,
            "records": exported,
        }


def _fuse_ranked_candidates(
    lexical: Sequence[MemoryCandidate],
    vector: Sequence[MemoryCandidate],
    *,
    top_k: int,
) -> list[MemoryHit]:
    """Fuse lexical and semantic rankings using equal-weight normalized RRF."""

    if top_k < 1:
        return []
    channels = [results for results in (lexical, vector) if results]
    if not channels:
        return []
    by_key: dict[str, dict[str, Any]] = {}
    max_score = len(channels) / (RRF_K + 1)
    for results in channels:
        for rank, candidate in enumerate(results, start=1):
            state = by_key.setdefault(
                candidate.chunk_key,
                {
                    "candidate": candidate,
                    "score": 0.0,
                    "lexical_rank": None,
                    "vector_rank": None,
                },
            )
            state["score"] += 1.0 / (RRF_K + rank)
            if results is lexical:
                state["lexical_rank"] = rank
            elif results is vector:
                state["vector_rank"] = rank
            # Prefer the higher-information payload if a future backend returns
            # richer content for the same stable chunk identity.
            if len(candidate.content) > len(state["candidate"].content):
                state["candidate"] = candidate

    ranked = sorted(
        by_key.values(),
        key=lambda state: (
            -float(state["score"]),
            state["lexical_rank"] is None,
            state["lexical_rank"] or 1_000_000,
            state["vector_rank"] or 1_000_000,
            state["candidate"].file_path,
            state["candidate"].start_line,
        ),
    )
    hits: list[MemoryHit] = []
    for state in ranked[:top_k]:
        selected: MemoryCandidate = state["candidate"]
        hits.append(
            MemoryHit(
                chunk_key=selected.chunk_key,
                file_path=selected.file_path,
                content=selected.content,
                score=float(state["score"]) / max_score,
                metadata={
                    "start_line": selected.start_line,
                    "end_line": selected.end_line,
                    "lexical_rank": state["lexical_rank"],
                    "vector_rank": state["vector_rank"],
                },
            )
        )
    return hits
