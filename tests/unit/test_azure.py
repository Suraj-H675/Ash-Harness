from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ash.providers.azure import (
    AzureBearerTokenProvider,
    AzureCredentialError,
    AzureProvider,
    normalize_azure_openai_base_url,
)


def test_azure_base_url_normalizes_resource_and_project_routes() -> None:
    assert normalize_azure_openai_base_url(
        "https://resource.openai.azure.com"
    ) == "https://resource.openai.azure.com/openai/v1"
    assert normalize_azure_openai_base_url(
        "https://resource.openai.azure.com/openai/v1/"
    ) == "https://resource.openai.azure.com/openai/v1"
    assert normalize_azure_openai_base_url(
        "https://resource.services.ai.azure.com/api/projects/project-a"
    ) == (
        "https://resource.services.ai.azure.com/api/projects/project-a/openai/v1"
    )


@pytest.mark.parametrize(
    "value",
    [
        "http://resource.openai.azure.com/openai/v1",
        "https://example.com/openai/v1",
        "https://resource.openai.azure.com/other/path",
        "https://resource.services.ai.azure.com/api/projects/../openai/v1",
        "https://resource.services.ai.azure.com/evil/api/projects/project-a",
        "https://user:pass@resource.openai.azure.com/openai/v1",
        "https://resource.openai.azure.com/openai/v1?api-version=preview",
        "https://resource.openai.azure.com/openai/\nv1",
    ],
)
def test_azure_base_url_rejects_unsafe_or_non_v1_routes(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_azure_openai_base_url(value)


@pytest.mark.asyncio
async def test_azure_bearer_provider_validates_and_remembers_recent_tokens() -> None:
    issued = iter(["token-one", "token-two", "token-three"])

    async def issue() -> str:
        return next(issued)

    provider = AzureBearerTokenProvider(issue)

    assert await provider() == "token-one"
    assert await provider() == "token-two"
    assert await provider() == "token-three"
    assert provider.redaction_secrets() == ("token-two", "token-three")


@pytest.mark.asyncio
async def test_azure_bearer_provider_rejects_invalid_token() -> None:
    async def issue() -> str:
        return ""

    provider = AzureBearerTokenProvider(issue)

    with pytest.raises(AzureCredentialError, match="invalid access token"):
        await provider()


@pytest.mark.asyncio
async def test_azure_entra_provider_verifies_and_closes_owned_credential() -> None:
    calls = 0

    async def issue() -> str:
        nonlocal calls
        calls += 1
        return "entra-token"

    class Credential:
        def __init__(self) -> None:
            self.closed = 0

        async def close(self) -> None:
            self.closed += 1

    credential = Credential()
    provider = AzureProvider(
        "deployment-a",
        base_url="https://resource.openai.azure.com",
        auth_mode="entra",
        token_provider=issue,
        credential=credential,
        client=SimpleNamespace(),
    )

    await provider.verify_credentials()
    await provider.aclose()

    assert calls == 1
    assert credential.closed == 1
    assert provider._azure_credential is None


def test_azure_api_key_provider_does_not_require_identity_extra() -> None:
    provider = AzureProvider(
        "deployment-a",
        base_url="https://resource.openai.azure.com",
        auth_mode="api_key",
        api_key="azure-key",
        client=SimpleNamespace(),
    )

    assert provider.provider_family == "azure"
    assert provider.model_name == "deployment-a"
    assert provider._base_url == "https://resource.openai.azure.com/openai/v1"


@pytest.mark.asyncio
async def test_azure_stream_requests_and_normalizes_provider_usage() -> None:
    class Stream:
        def __init__(self, chunks: list[Any]) -> None:
            self.chunks = chunks

        def __aiter__(self):
            async def generate():
                for chunk in self.chunks:
                    yield chunk

            return generate()

    class Completions:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] = {}

        async def create(self, **kwargs: Any) -> Stream:
            self.kwargs = kwargs
            usage = SimpleNamespace(
                prompt_tokens=120,
                completion_tokens=8,
                prompt_tokens_details=SimpleNamespace(cached_tokens=20),
            )
            return Stream(
                [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(content="done", tool_calls=None),
                                finish_reason="stop",
                            )
                        ],
                        usage=None,
                    ),
                    SimpleNamespace(choices=[], usage=usage),
                ]
            )

    completions = Completions()
    provider = AzureProvider(
        "deployment-a",
        base_url="https://resource.openai.azure.com",
        auth_mode="api_key",
        api_key="azure-key",
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )

    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [{"role": "user", "content": "hello"}]
        )
    ]

    assert completions.kwargs["stream_options"] == {"include_usage": True}
    assert chunks[-1].prompt_tokens == 120
    assert chunks[-1].completion_tokens == 8
    assert chunks[-1].cache_read_tokens == 20
    assert chunks[-1].usage_source == "provider"


def test_azure_api_key_provider_requires_key() -> None:
    with pytest.raises(ValueError, match="API key is required"):
        AzureProvider(
            "deployment-a",
            base_url="https://resource.openai.azure.com",
            auth_mode="api_key",
            client=SimpleNamespace(),
        )
