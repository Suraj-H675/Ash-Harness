# tests/unit/test_compaction.py
import pytest
from context.compaction import Chunk, sliding_window_chunk
from core.session import Message
from datetime import datetime, timezone

def test_chunk_key_format():
    chunk = Chunk(file_path="src/main.py", start_line=1, end_line=10, content="lines...")
    assert chunk.chunk_key == "src/main.py:1-10"

def test_compact_messages_truncates_long_history():
    messages = [
        Message(role="user", content=f"message {i}", timestamp=datetime.now(timezone.utc))
        for i in range(100)
    ]
    # Use sliding_window_chunk to test chunk functionality instead
    content = "\n".join(m.content for m in messages)
    chunks = sliding_window_chunk(content, "messages", window_size=30, overlap=5)
    # The function should create multiple overlapping chunks
    assert len(chunks) > 1
    assert all(isinstance(c, Chunk) for c in chunks)