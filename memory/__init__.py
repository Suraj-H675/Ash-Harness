"""Long-term memory and search indices for Ash."""

from memory.vector import (
    VectorSearchPipeline,
    VectorHit,
    InMemoryVectorIndex,
    ChromaIndex,
    EmbeddingAdapter,
    DeterministicEmbedding,
    ONNXLocalEmbedding,
    OpenAIEmbedding,
    EmbeddingBackendUnavailable,
    VectorBackendUnavailable,
    FTS5FallbackIndex,
)
from memory.markdown_store import MarkdownMemoryStore
from memory.fts5 import FTS5Index

__all__ = [
    "VectorSearchPipeline",
    "VectorHit",
    "InMemoryVectorIndex",
    "ChromaIndex",
    "EmbeddingAdapter",
    "DeterministicEmbedding",
    "ONNXLocalEmbedding",
    "OpenAIEmbedding",
    "EmbeddingBackendUnavailable",
    "VectorBackendUnavailable",
    "FTS5FallbackIndex",
    "MarkdownMemoryStore",
    "FTS5Index",
]
