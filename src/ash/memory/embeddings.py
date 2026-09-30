"""Optional embedding adapters for Ash project memory."""

from __future__ import annotations

import math
import inspect
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Sequence

from ash.core.redaction import redact_known_secrets


class EmbeddingBackendUnavailable(RuntimeError):
    """Raised when a configured embedding backend cannot be used."""


class EmbeddingAdapter(ABC):
    """Common contract for explicit semantic embedding providers."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Dimensionality of vectors produced by this adapter."""

        raise NotImplementedError

    @abstractmethod
    async def get_embedding(self, text: str) -> list[float]:
        """Return one embedding vector for ``text``."""

        raise NotImplementedError

    async def get_embeddings(self, texts: Sequence[str]) -> list[list[float]]:
        """Default batched implementation; providers may override for efficiency."""

        return [await self.get_embedding(text) for text in texts]

    async def aclose(self) -> None:
        """Release adapter-owned resources, if any."""

        return None


class ONNXLocalEmbedding(EmbeddingAdapter):
    """Offline MiniLM embeddings from an explicit local ONNX model/tokenizer pair."""

    DIMENSION = 384
    DEFAULT_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

    def __init__(
        self,
        model_path: Path | str | None = None,
        *,
        dimension: int = DIMENSION,
    ) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self._model_path = Path(model_path) if model_path is not None else None
        self._dimension = dimension
        self._session: Any = None
        self._tokenizer: Any = None
        self._load_attempted = False

    @property
    def dimension(self) -> int:
        return self._dimension

    def _ensure_loaded(self) -> None:
        if self._load_attempted:
            return
        self._load_attempted = True

        try:
            import onnxruntime  # type: ignore[import-not-found,import-untyped]
        except ImportError as exc:  # pragma: no cover - host dependent
            raise EmbeddingBackendUnavailable(
                "onnxruntime is not installed; install the 'local-embeddings' extra."
            ) from exc

        model_path = self._model_path
        if model_path is None or not model_path.is_file():
            raise EmbeddingBackendUnavailable(
                f"ONNX model not found at {model_path!s}; provide a valid model_path."
            )

        try:
            self._session = onnxruntime.InferenceSession(
                str(model_path),
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:
            raise EmbeddingBackendUnavailable(
                f"Failed to load ONNX session from {model_path}: {exc}"
            ) from exc

        try:
            from tokenizers import Tokenizer  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - host dependent
            raise EmbeddingBackendUnavailable(
                "tokenizers is not installed; install the 'local-embeddings' extra."
            ) from exc

        tokenizer_path = model_path.with_name("tokenizer.json")
        if not tokenizer_path.is_file():
            raise EmbeddingBackendUnavailable(
                f"ONNX tokenizer not found at {tokenizer_path}; provide tokenizer.json "
                "beside the model."
            )
        try:
            self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
            self._tokenizer.enable_truncation(max_length=256)
        except Exception as exc:
            raise EmbeddingBackendUnavailable(
                f"Failed to load ONNX tokenizer from {tokenizer_path}: {exc}"
            ) from exc

    async def get_embedding(self, text: str) -> list[float]:
        self._ensure_loaded()
        if self._tokenizer is None or self._session is None:
            raise EmbeddingBackendUnavailable("ONNX embedding backend is unavailable")

        try:
            encoding = self._tokenizer.encode(text)
            input_ids = [encoding.ids]
            attention_mask = [[1] * len(encoding.ids)]
            inputs: dict[str, Any] = {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
            }
            input_names = {str(item.name) for item in self._session.get_inputs()}
            if "token_type_ids" in input_names:
                inputs["token_type_ids"] = [[0] * len(encoding.ids)]
            outputs = self._session.run(None, inputs)
        except Exception as exc:
            raise EmbeddingBackendUnavailable(f"ONNX inference failed: {exc}") from exc

        token_vectors = outputs[0][0]
        mask = attention_mask[0]
        pooled = [
            sum(token_vectors[token][dimension] for token in range(len(mask)) if mask[token])
            / max(1, sum(mask))
            for dimension in range(self._dimension)
        ]
        norm = math.sqrt(sum(value * value for value in pooled))
        if norm > 0:
            pooled = [value / norm for value in pooled]
        return pooled


class OpenAIEmbedding(EmbeddingAdapter):
    """Remote embeddings through OpenAI's text-embedding-3-small model."""

    DIMENSION = 1536
    DEFAULT_MODEL = "text-embedding-3-small"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        client: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client = client
        self._owns_client = client is None
        self._closed = False

    @property
    def dimension(self) -> int:
        return self.DIMENSION

    def _resolve_client(self) -> Any:
        if self._closed:
            raise EmbeddingBackendUnavailable("OpenAI embedding client is closed")
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - host dependent
            raise EmbeddingBackendUnavailable(
                "The 'openai' package is not installed; install it for OpenAI embeddings."
            ) from exc
        self._client = (
            AsyncOpenAI(api_key=self._api_key) if self._api_key else AsyncOpenAI()
        )
        return self._client

    async def aclose(self) -> None:
        if self._closed:
            return
        client = self._client
        if not self._owns_client or client is None:
            self._closed = True
            return
        close = getattr(client, "close", None)
        if not callable(close):
            raise RuntimeError("owned OpenAI embedding client cannot be closed")
        result = close()
        if inspect.isawaitable(result):
            await result
        self._client = None
        self._closed = True

    async def get_embedding(self, text: str) -> list[float]:
        embeddings = await self.get_embeddings([text])
        return embeddings[0]

    async def get_embeddings(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._resolve_client()
        try:
            response = await client.embeddings.create(
                model=self._model,
                input=list(texts),
            )
        except Exception as exc:
            raise EmbeddingBackendUnavailable(
                "OpenAI embeddings call failed: "
                + redact_known_secrets(str(exc), self._api_key)
            ) from exc
        data = sorted(response.data, key=lambda item: int(getattr(item, "index", 0)))
        if len(data) != len(texts):
            raise EmbeddingBackendUnavailable(
                "OpenAI embeddings returned an unexpected number of vectors"
            )
        embeddings = [[float(value) for value in item.embedding] for item in data]
        if any(len(vector) != self.dimension for vector in embeddings):
            raise EmbeddingBackendUnavailable(
                "OpenAI embeddings returned an unexpected vector dimension"
            )
        return embeddings
