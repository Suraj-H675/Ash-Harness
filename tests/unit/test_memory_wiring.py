import pytest

from ash.context.compaction import Chunk
from ash.memory import (
    DeterministicEmbedding,
    FTS5FallbackIndex,
    InMemoryVectorIndex,
    VectorSearchPipeline,
)


@pytest.mark.asyncio
async def test_fts5_only_pipeline_indexes_and_searches_lexically(tmp_path) -> None:
    pipeline = VectorSearchPipeline(
        adapter=DeterministicEmbedding(),
        vector_index=InMemoryVectorIndex(),
        lexical_index=FTS5FallbackIndex(db_path=tmp_path / "memory.db"),
        vector_enabled=False,
    )
    chunks = [Chunk(file_path="a.py", start_line=1, end_line=1, content="uniquephrase")]
    assert await pipeline.index_chunks(chunks, "a.py") == 1
    hits, source = await pipeline.search("uniquephrase")
    assert source == "lexical"
    assert hits[0].file_path == "a.py"
    pipeline.clear()
    hits, _ = await pipeline.search("uniquephrase")
    assert hits == []
