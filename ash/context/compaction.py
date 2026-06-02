"""Context compaction primitives for Ash."""

from __future__ import annotations

from pydantic import BaseModel, Field, computed_field


DEFAULT_WINDOW_SIZE = 30
DEFAULT_OVERLAP = 5


class Chunk(BaseModel):
    """A contiguous slice of a source file produced by the sliding-window chunker."""

    file_path: str
    start_line: int = Field(..., ge=1, description="1-indexed inclusive start line.")
    end_line: int = Field(..., ge=1, description="1-indexed inclusive end line.")
    content: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def chunk_key(self) -> str:
        """Stable identifier of the form ``file_path:start-end`` per spec section 5.1."""

        return f"{self.file_path}:{self.start_line}-{self.end_line}"


def sliding_window_chunk(
    content: str,
    file_path: str,
    window_size: int = DEFAULT_WINDOW_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """
    Split ``content`` into overlapping line-preserving chunks.

    Consecutive chunks share ``overlap`` lines so downstream retrieval can
    match phrases that straddle chunk boundaries. Defaults follow spec
    section 5.1 (30-line window, 5-line overlap).
    """

    if window_size <= 0:
        raise ValueError("window_size must be positive")
    if overlap < 0 or overlap >= window_size:
        raise ValueError("overlap must be non-negative and strictly less than window_size")

    if not content:
        return []

    lines = content.splitlines()
    if not lines:
        return []

    step = window_size - overlap
    chunks: list[Chunk] = []
    start = 0
    while start < len(lines):
        end = min(start + window_size, len(lines))
        chunks.append(
            Chunk(
                file_path=file_path,
                start_line=start + 1,
                end_line=end,
                content="\n".join(lines[start:end]),
            )
        )
        if end == len(lines):
            break
        start += step

    return chunks
