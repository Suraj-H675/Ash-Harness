import pytest

from ash.config import AshConfig
from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.safety.guard import SafetyGuard
from ash.ui.headless import HeadlessUI

from ash.context.compaction import Chunk
from ash.memory import (
    DeterministicEmbedding,
    FTS5FallbackIndex,
    InMemoryVectorIndex,
    VectorSearchPipeline,
)


class MemoryTestProvider(ProviderABC):
    model_name = "memory-test"

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        yield StreamChunk(content="done", is_done=True)

    def count_tokens(self, text: str) -> int:
        return len(text.split())


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


@pytest.mark.asyncio
async def test_project_memory_auto_index_is_bounded_and_respects_excludes(
    tmp_path,
) -> None:
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "included.py").write_text("alpha\n" * 10, encoding="utf-8")
    (tmp_path / "excluded.py").write_text("beta\n", encoding="utf-8")
    (tmp_path / "large.py").write_text("gamma\n" * 100_000, encoding="utf-8")

    config = AshConfig(
        model="openai/memory-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="fts5",
        chroma_persist_dir=tmp_path / "memory",
        repo_map_exclude_patterns=["excluded.py"],
    )
    loop = AshLoop(
        SessionStore(config.db_directory / "sessions.db"),
        MemoryTestProvider(),
        SafetyGuard(project_root=tmp_path),
        None,
        tmp_path,
        config=config,
        enable_semantic_memory=True,
        memory_backend="fts5",
        chroma_persist_dir=tmp_path / "memory",
    )
    try:
        indexed = await loop.index_project_memory(
            max_files=2,
            max_bytes_per_file=1_000,
        )
        assert indexed == 1
        hits = await loop.semantic_search("beta")
        assert all(hit.file_path != "excluded.py" for hit in hits)
        hits = await loop.semantic_search("alpha")
        assert any(hit.file_path.endswith("included.py") for hit in hits)
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_manual_memory_index_skips_oversized_file(tmp_path) -> None:
    path = tmp_path / "large.py"
    path.write_bytes(b"x" * 9)
    config = AshConfig(
        model="openai/memory-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="fts5",
        chroma_persist_dir=tmp_path / "memory",
    )
    loop = AshLoop(
        session_store=SessionStore(config.db_directory / "sessions.db"),
        provider=MemoryTestProvider(),
        ui=HeadlessUI(output_format="text"),
        safety_guard=SafetyGuard(project_root=tmp_path),
        project_root=tmp_path,
        config=config,
        enable_semantic_memory=True,
        memory_backend="fts5",
        chroma_persist_dir=tmp_path / "memory",
    )
    try:
        assert await loop.index_file_for_memory(path, max_bytes_per_file=8) == 0
        assert await loop.semantic_search("x") == []
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_large_repository_memory_indexing_is_bounded(tmp_path) -> None:
    file_count = 120
    for index in range(file_count + 50):
        directory = tmp_path / "packages" / f"pkg-{index % 25}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"module-{index}.py").write_text(
            f"def function_{index}():\n    return {index}\n" * 20,
            encoding="utf-8",
        )

    config = AshConfig(
        model="openai/memory-test",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="fts5",
        chroma_persist_dir=tmp_path / "memory",
    )
    loop = AshLoop(
        session_store=SessionStore(config.db_directory / "sessions.db"),
        provider=MemoryTestProvider(),
        ui=HeadlessUI(output_format="text"),
        safety_guard=SafetyGuard(project_root=tmp_path),
        project_root=tmp_path,
        config=config,
        enable_semantic_memory=True,
        memory_backend="fts5",
        chroma_persist_dir=tmp_path / "memory",
    )
    try:
        indexed = await loop.index_project_memory(
            max_files=100,
            max_bytes_per_file=4_096,
        )
        assert indexed == 100

        hits = await loop.semantic_search("function_100")
        assert hits
        assert all(hit.file_path.endswith("module-100.py") for hit in hits)

        hits = await loop.semantic_search("function_99")
        assert not any(hit.file_path.endswith("module-99.py") for hit in hits)
    finally:
        await loop.aclose()
