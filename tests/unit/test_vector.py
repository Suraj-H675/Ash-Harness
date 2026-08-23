"""Unit tests for the ChromaDB vector search pipeline (Sprint 10)."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import pytest

from ash.context.compaction import Chunk
from ash.memory.fts5 import FTS5Index
from ash.memory.vector import (
    ChromaIndex,
    DeterministicEmbedding,
    EmbeddingAdapter,
    EmbeddingBackendUnavailable,
    FTS5FallbackIndex,
    InMemoryVectorIndex,
    ONNXLocalEmbedding,
    OpenAIEmbedding,
    VectorBackendUnavailable,
    VectorIndex,
    VectorSearchPipeline,
    cosine_similarity,
    cosine_similarity_batch,
    fts5_query,
    vector_extras_available,
)


# ---------------------------------------------------------------------------
# cosine_similarity
# ---------------------------------------------------------------------------


def test_cosine_similarity_identical_vectors_is_one() -> None:
    assert cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors_is_zero() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_opposite_vectors_is_negative_one() -> None:
    assert cosine_similarity([1.0, 2.0, 3.0], [-1.0, -2.0, -3.0]) == pytest.approx(-1.0)


def test_cosine_similarity_zero_vector_returns_zero() -> None:
    # Avoid division by zero on degenerate inputs.
    assert cosine_similarity([0.0, 0.0, 0.0], [1.0, 2.0, 3.0]) == 0.0
    assert cosine_similarity([1.0, 2.0, 3.0], [0.0, 0.0, 0.0]) == 0.0


def test_cosine_similarity_raises_on_dimension_mismatch() -> None:
    with pytest.raises(ValueError, match="dimension mismatch"):
        cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


def test_cosine_similarity_batch_returns_scores_in_order() -> None:
    docs = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]
    scores = cosine_similarity_batch([1.0, 0.0], docs)
    assert scores[0] == pytest.approx(1.0)
    assert scores[1] == pytest.approx(0.0)
    assert scores[2] == pytest.approx(1.0 / math.sqrt(2.0))


# ---------------------------------------------------------------------------
# DeterministicEmbedding
# ---------------------------------------------------------------------------


def test_deterministic_embedding_dimension_matches_property() -> None:
    adapter = DeterministicEmbedding()
    assert adapter.dimension == 384


def test_deterministic_embedding_is_normalized() -> None:
    async def runner() -> None:
        adapter = DeterministicEmbedding()
        vec = await adapter.get_embedding("the quick brown fox")
        norm = math.sqrt(sum(v * v for v in vec))
        assert norm == pytest.approx(1.0)

    asyncio.run(runner())


def test_deterministic_embedding_is_deterministic() -> None:
    async def runner() -> None:
        adapter = DeterministicEmbedding()
        a = await adapter.get_embedding("hello world")
        b = await adapter.get_embedding("hello world")
        assert a == b

    asyncio.run(runner())


def test_deterministic_embedding_close_text_has_high_similarity() -> None:
    async def runner() -> float:
        adapter = DeterministicEmbedding()
        a = await adapter.get_embedding("alpha beta gamma delta")
        b = await adapter.get_embedding("alpha beta gamma epsilon")
        return cosine_similarity(a, b)

    score = asyncio.run(runner())
    # They share 3 of 4 tokens; similarity should be clearly positive.
    assert score > 0.3


def test_deterministic_embedding_empty_text_returns_zero_vector() -> None:
    async def runner() -> list[float]:
        return await DeterministicEmbedding().get_embedding("")

    vec = asyncio.run(runner())
    assert all(v == 0.0 for v in vec)


# ---------------------------------------------------------------------------
# Adapter interface contract
# ---------------------------------------------------------------------------


def test_embedding_adapter_is_abstract() -> None:
    with pytest.raises(TypeError):
        EmbeddingAdapter()  # type: ignore[abstract]


def test_onxx_local_embedding_raises_when_model_missing(tmp_path: Path) -> None:
    adapter = ONNXLocalEmbedding(model_path=tmp_path / "missing.onnx")
    with pytest.raises(EmbeddingBackendUnavailable):
        asyncio.run(adapter.get_embedding("hi"))


def test_openai_embedding_raises_without_sdk(monkeypatch) -> None:
    # Force the SDK import to fail even if openai is installed locally.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openai" or name.startswith("openai."):
            raise ImportError("simulated missing openai")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    adapter = OpenAIEmbedding(api_key="x")
    # Reset any cached client.
    adapter._client = None
    with pytest.raises(EmbeddingBackendUnavailable):
        asyncio.run(adapter.get_embedding("hi"))


def test_openai_embedding_uses_dimension_constant() -> None:
    assert OpenAIEmbedding.DIMENSION == 1536
    assert OpenAIEmbedding(api_key="x").dimension == 1536


# ---------------------------------------------------------------------------
# InMemoryVectorIndex
# ---------------------------------------------------------------------------


def test_in_memory_index_round_trip() -> None:
    index: VectorIndex = InMemoryVectorIndex()
    index.add(
        ids=["a", "b", "c"],
        embeddings=[[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]],
        documents=["alpha", "beta", "gamma"],
        metadatas=[
            {"file_path": "a.py", "chunk_key": "a.py:1-1"},
            {"file_path": "b.py", "chunk_key": "b.py:1-1"},
            {"file_path": "c.py", "chunk_key": "c.py:1-1"},
        ],
    )
    hits = index.query([1.0, 0.0], top_k=2)
    assert len(hits) == 2
    assert hits[0].chunk_key == "a.py:1-1"
    assert hits[0].score == pytest.approx(1.0)


def test_in_memory_index_upsert_replaces_existing_id() -> None:
    index = InMemoryVectorIndex()
    index.add(ids=["x"], embeddings=[[1.0, 0.0]], documents=["v1"])
    index.add(ids=["x"], embeddings=[[0.0, 1.0]], documents=["v2"])
    assert len(index) == 1
    hits = index.query([0.0, 1.0], top_k=1)
    assert hits[0].content == "v2"


def test_in_memory_index_empty_returns_no_hits() -> None:
    index = InMemoryVectorIndex()
    assert index.query([1.0, 0.0]) == []


# ---------------------------------------------------------------------------
# ChromaIndex (only when chromadb is importable)
# ---------------------------------------------------------------------------


def test_chroma_index_round_trip(tmp_path: Path) -> None:
    if not vector_extras_available():
        pytest.skip("chromadb not installed in this environment")
    index = ChromaIndex(
        persist_directory=tmp_path / "chroma", collection_name="ash_test"
    )
    index.add(
        ids=["d1", "d2", "d3"],
        embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        documents=["doc1", "doc2", "doc3"],
        metadatas=[
            {"file_path": "f1", "chunk_key": "f1:1-1"},
            {"file_path": "f2", "chunk_key": "f2:1-1"},
            {"file_path": "f3", "chunk_key": "f3:1-1"},
        ],
    )
    hits = index.query([1.0, 0.0, 0.0], top_k=2)
    assert len(hits) == 2
    assert hits[0].chunk_key == "f1:1-1"


# ---------------------------------------------------------------------------
# FTS5 fallback
# ---------------------------------------------------------------------------


def test_fts5_fallback_returns_top_chunks(tmp_path: Path) -> None:
    fts = FTS5Index(tmp_path / "fts.db")
    chunks = [
        Chunk(
            file_path="alpha.py",
            start_line=1,
            end_line=2,
            content="def foo(): pass\nclass Bar: pass",
        ),
        Chunk(file_path="beta.py", start_line=1, end_line=1, content="x = 42"),
    ]
    fts.index_document("alpha.py", [chunks[0]])
    fts.index_document("beta.py", [chunks[1]])

    fallback = FTS5FallbackIndex(fts)
    hits = fallback.query("foo bar", top_k=2)
    assert len(hits) >= 1
    # The first hit should mention foo/bar.
    assert any("foo" in hit.content for hit in hits)


def test_fts5_query_helper_returns_rows(tmp_path: Path) -> None:
    fts = FTS5Index(tmp_path / "fts2.db")
    chunks = [
        Chunk(file_path="g.py", start_line=1, end_line=1, content="print('hello')")
    ]
    fts.index_document("g.py", chunks)
    rows = fts5_query(tmp_path / "fts2.db", "hello", top_k=5)
    assert isinstance(rows, list)
    assert any("hello" in str(row.get("content", "")) for row in rows)


# ---------------------------------------------------------------------------
# VectorSearchPipeline
# ---------------------------------------------------------------------------


def test_pipeline_serves_vector_path_when_index_available() -> None:
    async def runner() -> tuple[list, str]:
        adapter = DeterministicEmbedding()
        index = InMemoryVectorIndex()
        pipeline = VectorSearchPipeline(
            adapter=adapter, vector_index=index, lexical_index=None
        )
        chunks = [
            Chunk(file_path="a.py", start_line=1, end_line=1, content="alpha"),
            Chunk(file_path="b.py", start_line=1, end_line=1, content="beta"),
        ]
        await pipeline.index_chunks(chunks, file_path="a.py")
        return await pipeline.search("alpha")

    hits, source = asyncio.run(runner())
    assert source == "vector"
    assert len(hits) >= 1
    assert hits[0].content == "alpha"


def test_pipeline_falls_back_to_lexical_when_vector_backend_unavailable(
    tmp_path: Path,
) -> None:
    fts = FTS5Index(tmp_path / "fb.db")
    fts.index_document(
        "a.py",
        [
            Chunk(
                file_path="a.py", start_line=1, end_line=1, content="def hello(): pass"
            )
        ],
    )
    fallback = FTS5FallbackIndex(fts)

    class BrokenIndex(InMemoryVectorIndex):
        def add(self, *args, **kwargs):  # noqa: D401, ARG002
            raise VectorBackendUnavailable("simulated")

        def query(self, query_embedding, top_k: int = 5):  # noqa: ARG002
            raise VectorBackendUnavailable("simulated")

    pipeline = VectorSearchPipeline(
        adapter=DeterministicEmbedding(),
        vector_index=BrokenIndex(),
        lexical_index=fallback,
    )

    async def runner() -> tuple[list, str]:
        return await pipeline.search("hello")

    hits, source = asyncio.run(runner())
    assert source == "lexical"
    assert len(hits) >= 1
    assert "hello" in hits[0].content


def test_pipeline_falls_back_when_embedding_adapter_unavailable(tmp_path: Path) -> None:
    fts = FTS5Index(tmp_path / "fb2.db")
    fts.index_document(
        "a.py",
        [
            Chunk(
                file_path="a.py", start_line=1, end_line=1, content="def greet(): pass"
            )
        ],
    )
    fallback = FTS5FallbackIndex(fts)

    class BrokenAdapter(EmbeddingAdapter):
        @property
        def dimension(self) -> int:
            return 4

        async def get_embedding(self, text: str) -> list[float]:
            raise EmbeddingBackendUnavailable("simulated")

    pipeline = VectorSearchPipeline(
        adapter=BrokenAdapter(),
        vector_index=InMemoryVectorIndex(),
        lexical_index=fallback,
    )

    async def runner() -> tuple[list, str]:
        return await pipeline.search("greet")

    hits, source = asyncio.run(runner())
    assert source == "lexical"
    assert len(hits) >= 1


def test_pipeline_index_chunks_returns_count() -> None:
    pipeline = VectorSearchPipeline(
        adapter=DeterministicEmbedding(),
        vector_index=InMemoryVectorIndex(),
    )

    async def runner() -> int:
        return await pipeline.index_chunks(
            [
                Chunk(file_path="a.py", start_line=1, end_line=1, content="x"),
                Chunk(file_path="a.py", start_line=2, end_line=2, content="y"),
            ],
            file_path="a.py",
        )

    assert asyncio.run(runner()) == 2


def test_pipeline_index_empty_chunks_is_noop() -> None:
    pipeline = VectorSearchPipeline(
        adapter=DeterministicEmbedding(),
        vector_index=InMemoryVectorIndex(),
    )

    async def runner() -> int:
        return await pipeline.index_chunks([], file_path="a.py")

    assert asyncio.run(runner()) == 0


def test_pipeline_export_is_bounded_and_redacted(tmp_path: Path) -> None:
    secret = "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz"
    pipeline = VectorSearchPipeline(
        adapter=DeterministicEmbedding(),
        vector_index=InMemoryVectorIndex(),
        lexical_index=FTS5FallbackIndex(FTS5Index(tmp_path / "export.db")),
    )
    asyncio.run(
        pipeline.index_chunks(
            [
                Chunk(
                    file_path="secret.py",
                    start_line=1,
                    end_line=1,
                    content=f"token {secret}",
                )
            ],
            file_path="secret.py",
        )
    )
    asyncio.run(
        pipeline.index_chunks(
            [
                Chunk(
                    file_path="lexical.py",
                    start_line=1,
                    end_line=1,
                    content=f"lexical token {secret}",
                )
            ],
            file_path="lexical.py",
        )
    )

    exported = pipeline.export(limit=1)

    assert exported["redacted"] is True
    assert exported["count"] == 1
    assert len(exported["records"]) == 1
    assert secret not in json.dumps(exported)
    assert "[REDACTED" in json.dumps(exported)
