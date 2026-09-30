from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ash.memory.embeddings import (
    EmbeddingAdapter,
    EmbeddingBackendUnavailable,
    ONNXLocalEmbedding,
    OpenAIEmbedding,
)


def test_embedding_adapter_is_abstract() -> None:
    with pytest.raises(TypeError):
        EmbeddingAdapter()  # type: ignore[abstract]


def test_onnx_embedding_raises_when_model_missing(tmp_path: Path) -> None:
    adapter = ONNXLocalEmbedding(model_path=tmp_path / "missing.onnx")

    with pytest.raises(EmbeddingBackendUnavailable, match="model not found"):
        asyncio.run(adapter.get_embedding("hi"))


def test_onnx_embedding_requires_tokenizer_beside_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = tmp_path / "model.onnx"
    model.write_bytes(b"placeholder")

    class FakeSession:
        pass

    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(InferenceSession=lambda *args, **kwargs: FakeSession()),
    )
    adapter = ONNXLocalEmbedding(model_path=model)

    with pytest.raises(EmbeddingBackendUnavailable, match="tokenizer not found"):
        asyncio.run(adapter.get_embedding("hi"))


def test_openai_embedding_raises_without_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openai" or name.startswith("openai."):
            raise ImportError("simulated missing openai")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    adapter = OpenAIEmbedding(api_key="x")

    with pytest.raises(EmbeddingBackendUnavailable, match="openai.*not installed"):
        asyncio.run(adapter.get_embedding("hi"))


def test_openai_embedding_uses_dimension_constant() -> None:
    assert OpenAIEmbedding.DIMENSION == 1536
    assert OpenAIEmbedding(api_key="x").dimension == 1536


def test_openai_embedding_batches_requests_and_restores_response_order() -> None:
    class EmbeddingsAPI:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[str]]] = []

        async def create(self, *, model: str, input: list[str]):
            self.calls.append((model, list(input)))
            first = [1.0] + [0.0] * 1535
            second = [2.0] + [0.0] * 1535
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=1, embedding=second),
                    SimpleNamespace(index=0, embedding=first),
                ]
            )

    api = EmbeddingsAPI()
    adapter = OpenAIEmbedding(
        api_key="x",
        client=SimpleNamespace(embeddings=api),
    )

    vectors = asyncio.run(adapter.get_embeddings(["first", "second"]))

    assert api.calls == [(OpenAIEmbedding.DEFAULT_MODEL, ["first", "second"])]
    assert vectors[0][0] == 1.0
    assert vectors[1][0] == 2.0


def test_openai_embedding_closes_only_client_it_owns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmbeddingsAPI:
        async def create(self, **_kwargs):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=0, embedding=[1.0] + [0.0] * 1535),
                ]
            )

    class FakeClient:
        def __init__(self) -> None:
            self.embeddings = EmbeddingsAPI()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    owned_client = FakeClient()
    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(AsyncOpenAI=lambda **_kwargs: owned_client),
    )
    owned = OpenAIEmbedding(api_key="x")
    asyncio.run(owned.get_embedding("first"))
    asyncio.run(owned.aclose())

    assert owned_client.close_calls == 1
    with pytest.raises(EmbeddingBackendUnavailable, match="closed"):
        asyncio.run(owned.get_embedding("again"))

    injected_client = FakeClient()
    injected = OpenAIEmbedding(api_key="x", client=injected_client)
    asyncio.run(injected.aclose())

    assert injected_client.close_calls == 0


def test_openai_embedding_redacts_exact_configured_credential_from_errors() -> None:
    api_key = "tiny-k"

    class EmbeddingsAPI:
        async def create(self, **_kwargs):
            raise RuntimeError(f"upstream echoed credential {api_key}")

    adapter = OpenAIEmbedding(
        api_key=api_key,
        client=SimpleNamespace(embeddings=EmbeddingsAPI()),
    )

    with pytest.raises(EmbeddingBackendUnavailable) as error:
        asyncio.run(adapter.get_embeddings(["first"]))

    assert api_key not in str(error.value)
    assert "[REDACTED]" in str(error.value)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([SimpleNamespace(index=0, embedding=[0.0] * 1536)], "number of vectors"),
        (
            [
                SimpleNamespace(index=0, embedding=[0.0] * 3),
                SimpleNamespace(index=1, embedding=[0.0] * 3),
            ],
            "vector dimension",
        ),
    ],
)
def test_openai_embedding_rejects_malformed_batch_response(data, message: str) -> None:
    class EmbeddingsAPI:
        async def create(self, **_kwargs):
            return SimpleNamespace(data=data)

    adapter = OpenAIEmbedding(
        api_key="x",
        client=SimpleNamespace(embeddings=EmbeddingsAPI()),
    )

    with pytest.raises(EmbeddingBackendUnavailable, match=message):
        asyncio.run(adapter.get_embeddings(["first", "second"]))
