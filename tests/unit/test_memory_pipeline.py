"""Unit tests for the Vector Memory Pipeline wiring in AshLoop (H-12)."""

from __future__ import annotations

import asyncio
from typing import Any
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ash.context.compaction import Chunk
from ash.memory.vector import (
    DeterministicEmbedding,
    InMemoryVectorIndex,
    VectorSearchPipeline,
)


# ---------------------------------------------------------------------------
# VectorSearchPipeline — index and search
# ---------------------------------------------------------------------------


def test_vector_pipeline_indexes_and_searches() -> None:
    """Pipeline indexes chunks and returns relevant hits on search."""

    async def runner() -> tuple[list, str]:
        adapter = DeterministicEmbedding()
        index = InMemoryVectorIndex()
        pipeline = VectorSearchPipeline(
            adapter=adapter,
            vector_index=index,
            lexical_index=None,
        )

        chunks = [
            Chunk(
                file_path="auth.py",
                start_line=1,
                end_line=3,
                content="def authenticate(user, password):\n    return check credentials",
            ),
            Chunk(
                file_path="utils.py",
                start_line=10,
                end_line=12,
                content="def helper(): pass",
            ),
            Chunk(
                file_path="auth.py",
                start_line=5,
                end_line=7,
                content="def verify_token(token):\n    return validate(token)",
            ),
        ]

        indexed = await pipeline.index_chunks(chunks, file_path="auth.py")
        assert indexed == 3

        hits, source = await pipeline.search("authentication", top_k=2)
        assert source == "vector"
        assert len(hits) >= 1
        # The auth.py chunks should rank higher than utils.py for auth query.
        assert all(hit.file_path == "auth.py" for hit in hits)

    asyncio.run(runner())


def test_deterministic_embedding_produces_stable_vectors() -> None:
    """DeterministicEmbedding returns identical vectors for identical text."""

    async def runner() -> None:
        adapter = DeterministicEmbedding()

        vec_a = await adapter.get_embedding("hello world from ash")
        vec_b = await adapter.get_embedding("hello world from ash")
        vec_c = await adapter.get_embedding("hello world from ash")

        assert vec_a == vec_b == vec_c

        # Identical text should have cosine similarity of exactly 1.0.
        from ash.memory.vector import cosine_similarity

        similarity = cosine_similarity(vec_a, vec_b)
        assert similarity == pytest.approx(1.0)

        # Different text should produce different (non-identical) vectors.
        vec_d = await adapter.get_embedding("completely different text")
        assert vec_a != vec_d
        # And similarity should be less than 1.0.
        diff_sim = cosine_similarity(vec_a, vec_d)
        assert diff_sim < 1.0

    asyncio.run(runner())


def test_semantic_memory_injects_into_system_prompt(tmp_path: Path) -> None:
    """When semantic memory is enabled, relevant hits are injected into the system prompt."""

    from ash.core.loop import AshLoop
    from ash.core.session import Session, SessionStore
    from ash.providers.base import ProviderABC, StreamChunk
    from ash.safety.guard import SafetyGuard
    from ash.ui.terminal import TerminalUI

    # Set up a minimal loop with semantic memory enabled.
    session_store = MagicMock(spec=SessionStore)
    from datetime import datetime, timezone

    fake_session = Session(
        session_id="test-session",
        project_path=str(tmp_path),
        created_at=datetime.now(timezone.utc),
    )
    session_store.create_session.return_value = fake_session
    session_store.get_recent_session_summaries.return_value = []

    provider = MagicMock(spec=ProviderABC)

    # Simulate a streaming response with no tool calls.
    async def mock_stream(messages, temperature=0.0, tools=None):
        yield StreamChunk(content="Hello, I can help you.", is_done=True)

    provider.stream_chat = mock_stream

    safety_guard = MagicMock(spec=SafetyGuard)
    ui = MagicMock(spec=TerminalUI)
    ui.request_tool_approval.return_value = True
    ui.begin_turn.return_value.__enter__ = MagicMock(return_value=None)
    ui.begin_turn.return_value.__exit__ = MagicMock(return_value=None)
    ui.finalize_turn.return_value = None

    # Create a test file to index.
    test_file = tmp_path / "hello.py"
    test_file.write_text("def greet(name):\n    return f'Hello, {name}!'\n")

    loop = AshLoop(
        session_store=session_store,
        provider=provider,
        safety_guard=safety_guard,
        ui=ui,
        project_root=tmp_path,
        enable_semantic_memory=True,
        memory_backend="auto",
        embedding_provider="auto",
    )

    # Index the test file into semantic memory.
    asyncio.run(loop.index_file_for_memory(test_file))

    # Verify the vector pipeline was initialized.
    assert loop._vector_pipeline is not None

    # Run a turn with a query that should match the indexed file.
    captured_messages: list[dict[str, Any]] = []

    async def capturing_stream(messages, temperature=0.0, tools=None):
        captured_messages.extend(messages)
        yield StreamChunk(content="Hi.", is_done=True)

    provider.stream_chat = capturing_stream

    asyncio.run(loop.run_turn("what does greet do?"))

    # The system prompt passed to the provider should include
    # injected context from semantic memory.
    assert len(captured_messages) >= 1
    system_msg = captured_messages[0]
    assert system_msg["role"] == "system"
    # The Relevant Context section should mention hello.py or greet.
    assert (
        "Relevant Context" in system_msg["content"]
        or "hello.py" in system_msg["content"]
        or "greet" in system_msg["content"]
    )
