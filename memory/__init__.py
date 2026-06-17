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
    "MarkdownMemoryStore",
    "FTS5Index",
]
