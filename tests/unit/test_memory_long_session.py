from __future__ import annotations

import time
from pathlib import Path

import pytest

from ash.config import AshConfig
from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.safety.guard import SafetyGuard
from ash.ui.headless import HeadlessUI


FILE_COUNT = 1_000
SEARCH_ROUNDS = 20
SEARCH_BUDGET_SECONDS = 0.05


class BenchmarkProvider(ProviderABC):
    model_name = "memory-benchmark"

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        yield StreamChunk(content="done", is_done=True)

    def count_tokens(self, text: str) -> int:
        return len(text.split())


@pytest.mark.asyncio
async def test_long_session_memory_index_and_recall_remain_bounded(
    tmp_path: Path,
) -> None:
    for index in range(FILE_COUNT):
        directory = tmp_path / "packages" / f"pkg-{index % 50}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"module-{index}.py").write_text(
            f"def unique_function_{index}():\n    return {index}\n",
            encoding="utf-8",
        )

    config = AshConfig(
        model="openai/memory-benchmark",
        workspace_root=tmp_path,
        db_directory=tmp_path / "db",
        memory_backend="fts5",
        chroma_persist_dir=tmp_path / "memory",
    )
    loop = AshLoop(
        session_store=SessionStore(config.db_directory / "sessions.db"),
        provider=BenchmarkProvider(),
        ui=HeadlessUI(output_format="text"),
        safety_guard=SafetyGuard(project_root=tmp_path),
        project_root=tmp_path,
        config=config,
        enable_semantic_memory=True,
        memory_backend="fts5",
        chroma_persist_dir=tmp_path / "memory",
    )
    try:
        started = time.perf_counter()
        indexed = await loop.index_project_memory(max_files=FILE_COUNT)
        indexing_seconds = time.perf_counter() - started

        durations: list[float] = []
        hit_counts: list[int] = []
        for index in range(SEARCH_ROUNDS):
            target = (index * 37) % FILE_COUNT
            started = time.perf_counter()
            hits = await loop.semantic_search(f'"unique_function_{target}"', top_k=5)
            durations.append(time.perf_counter() - started)
            hit_counts.append(len(hits))
            assert hits and hits[0].file_path.endswith(f"module-{target}.py"), (
                f"missing deterministic match {target}"
            )

        assert indexed == FILE_COUNT
        assert set(hit_counts) == {1}
        assert indexing_seconds < 10.0
        assert max(durations) < SEARCH_BUDGET_SECONDS, max(durations)
    finally:
        await loop.aclose()
