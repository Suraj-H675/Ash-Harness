from __future__ import annotations

from pathlib import Path

import pytest

from ash.context.compaction import Chunk
from ash.memory.pipeline import MemorySearchPipeline
from ash.memory.sqlite_index import SQLiteMemoryIndex
from ash.memory.embeddings import EmbeddingAdapter, EmbeddingBackendUnavailable


class MappingEmbedding(EmbeddingAdapter):
    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = mapping
        self.fail = False

    @property
    def dimension(self) -> int:
        return 2

    async def get_embedding(self, text: str) -> list[float]:
        if self.fail:
            raise EmbeddingBackendUnavailable("embedding backend unavailable")
        return list(self.mapping.get(text, [0.0, 1.0]))


def _chunk(path: str, content: str) -> Chunk:
    return Chunk(file_path=path, start_line=1, end_line=1, content=content)


def _index(tmp_path: Path) -> SQLiteMemoryIndex:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return SQLiteMemoryIndex(
        tmp_path / "state" / "memory.db",
        workspace_root=workspace,
        chunking_version="test-v1",
    )


@pytest.mark.asyncio
async def test_pipeline_hybrid_search_rewards_cross_channel_agreement(
    tmp_path: Path,
) -> None:
    adapter = MappingEmbedding(
        {
            "alpha shared": [1.0, 0.0],
            "alpha lexical": [0.0, 1.0],
            "semantic only": [1.0, 0.0],
            "alpha": [1.0, 0.0],
        }
    )
    pipeline = MemorySearchPipeline(
        index=_index(tmp_path),
        adapter=adapter,
        embedding_identity="mapping-v1",
    )
    await pipeline.index_documents(
        [
            ([_chunk("shared.py", "alpha shared")], "shared.py"),
            ([_chunk("lexical.py", "alpha lexical")], "lexical.py"),
            ([_chunk("semantic.py", "semantic only")], "semantic.py"),
        ]
    )

    hits, source = await pipeline.search("alpha", top_k=3)

    assert source == "hybrid"
    assert hits[0].file_path == "shared.py"
    assert hits[0].metadata["lexical_rank"] is not None
    assert hits[0].metadata["vector_rank"] is not None
    assert hits[0].score > hits[1].score


@pytest.mark.asyncio
async def test_pipeline_prefers_lexical_candidate_on_equal_single_channel_rank(
    tmp_path: Path,
) -> None:
    adapter = MappingEmbedding(
        {
            "literal needle": [0.0, 1.0],
            "semantic candidate": [1.0, 0.0],
            "needle": [1.0, 0.0],
        }
    )
    pipeline = MemorySearchPipeline(
        index=_index(tmp_path),
        adapter=adapter,
        embedding_identity="mapping-v1",
    )
    await pipeline.index_documents(
        [
            ([_chunk("literal.py", "literal needle")], "literal.py"),
            ([_chunk("semantic.py", "semantic candidate")], "semantic.py"),
        ]
    )

    hits, source = await pipeline.search("needle", top_k=2)

    assert source == "hybrid"
    assert hits[0].file_path == "literal.py"
    assert hits[0].metadata["lexical_rank"] == 1


@pytest.mark.asyncio
async def test_pipeline_query_embedding_failure_falls_back_to_lexical(
    tmp_path: Path,
) -> None:
    adapter = MappingEmbedding({"lexical fallback marker": [1.0, 0.0]})
    pipeline = MemorySearchPipeline(
        index=_index(tmp_path),
        adapter=adapter,
        embedding_identity="mapping-v1",
    )
    await pipeline.index_chunks(
        [_chunk("fallback.py", "lexical fallback marker")],
        "fallback.py",
    )
    adapter.fail = True

    hits, source = await pipeline.search("lexical fallback marker", top_k=5)

    assert source == "lexical"
    assert [hit.file_path for hit in hits] == ["fallback.py"]


@pytest.mark.asyncio
async def test_failed_reembedding_publishes_new_lexical_text_without_stale_vector(
    tmp_path: Path,
) -> None:
    index = _index(tmp_path)
    adapter = MappingEmbedding({"old semantic text": [1.0, 0.0]})
    pipeline = MemorySearchPipeline(
        index=index,
        adapter=adapter,
        embedding_identity="mapping-v1",
    )
    await pipeline.index_chunks(
        [_chunk("changing.py", "old semantic text")],
        "changing.py",
    )
    assert [hit.file_path for hit in index.vector_search([1.0, 0.0], limit=5)] == [
        "changing.py"
    ]

    adapter.fail = True
    await pipeline.index_chunks(
        [_chunk("changing.py", "fresh lexical replacement")],
        "changing.py",
    )

    assert index.vector_search([1.0, 0.0], limit=5) == []
    assert [
        hit.file_path
        for hit in index.lexical_search("fresh lexical replacement", limit=5)
    ] == ["changing.py"]
    assert index.lexical_search("old semantic text", limit=5) == []


@pytest.mark.asyncio
async def test_pipeline_without_embedding_provider_is_honestly_lexical_only(
    tmp_path: Path,
) -> None:
    pipeline = MemorySearchPipeline(index=_index(tmp_path))
    await pipeline.index_chunks(
        [_chunk("plain.py", "plain lexical memory")],
        "plain.py",
    )

    hits, source = await pipeline.search("plain lexical memory", top_k=5)

    assert source == "lexical"
    assert [hit.file_path for hit in hits] == ["plain.py"]
    assert pipeline.index.embedding_dimension() is None


@pytest.mark.asyncio
async def test_pipeline_export_is_bounded_and_redacted(tmp_path: Path) -> None:
    pipeline = MemorySearchPipeline(index=_index(tmp_path))
    secret = "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz"
    await pipeline.index_chunks([_chunk("secret.py", secret)], "secret.py")

    exported = pipeline.export(limit=1)

    assert exported["count"] == 1
    assert exported["redacted"] is True
    assert secret not in str(exported)


def test_pipeline_requires_explicit_identity_for_semantic_adapter(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="explicit identity"):
        MemorySearchPipeline(
            index=_index(tmp_path),
            adapter=MappingEmbedding({}),
        )
