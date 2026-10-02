from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ash.providers.vertex import (
    GoogleAdcTokenProvider,
    VertexCredentialError,
    VertexProvider,
    vertex_openai_base_url,
)


class _Credentials:
    def __init__(self, *, token: str | None = None, valid: bool = False) -> None:
        self.token = token
        self.valid = valid
        self.expiry = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.refresh_count = 0

    def refresh(self, request) -> None:
        assert request == "request"
        self.refresh_count += 1
        self.token = f"adc-token-{self.refresh_count}"
        self.valid = True
        self.expiry = datetime.now(timezone.utc) + timedelta(hours=1)


def test_vertex_openai_base_url_requires_explicit_safe_scope() -> None:
    assert vertex_openai_base_url("project-123", "us-central1") == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/project-123/"
        "locations/us-central1/endpoints/openapi"
    )
    assert vertex_openai_base_url("project-123", "global") == (
        "https://aiplatform.googleapis.com/v1/projects/project-123/"
        "locations/global/endpoints/openapi"
    )
    with pytest.raises(ValueError, match="safe path segment"):
        vertex_openai_base_url("project-123", "../../global")


@pytest.mark.asyncio
async def test_vertex_adc_refresh_is_serialized_and_cached() -> None:
    credentials = _Credentials()
    provider = GoogleAdcTokenProvider(
        credentials=credentials,
        request="request",
    )

    tokens = await asyncio.gather(provider(), provider(), provider())

    assert tokens == ["adc-token-1"] * 3
    assert credentials.refresh_count == 1
    assert provider.redaction_secrets() == ("adc-token-1", "adc-token-1")


@pytest.mark.asyncio
async def test_vertex_provider_redacts_recent_adc_token_from_provider_error() -> None:
    credentials = _Credentials(token="sensitive-adc-token", valid=True)
    credentials.expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    token_provider = GoogleAdcTokenProvider(
        credentials=credentials,
        request="request",
    )
    assert await token_provider() == "sensitive-adc-token"

    class FailingCompletions:
        async def create(self, **kwargs):
            del kwargs
            raise RuntimeError("upstream echoed sensitive-adc-token")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=FailingCompletions()),
    )
    provider = VertexProvider(
        "google/gemini-test",
        project="project-123",
        location="us-central1",
        token_provider=token_provider,
        client=client,
    )

    with pytest.raises(RuntimeError) as exc_info:
        _ = [
            chunk
            async for chunk in provider.stream_chat(
                [{"role": "user", "content": "hello"}]
            )
        ]

    message = str(exc_info.value)
    assert "sensitive-adc-token" not in message
    assert "[REDACTED]" in message


@pytest.mark.asyncio
async def test_vertex_verify_credentials_does_not_issue_model_request() -> None:
    credentials = _Credentials()
    token_provider = GoogleAdcTokenProvider(
        credentials=credentials,
        request="request",
    )

    class Completions:
        async def create(self, **kwargs):
            raise AssertionError("model request must not run")

    provider = VertexProvider(
        "google/gemini-test",
        project="project-123",
        location="global",
        token_provider=token_provider,
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
    )

    await provider.verify_credentials()

    assert credentials.refresh_count == 1


@pytest.mark.asyncio
async def test_vertex_adc_rejects_invalid_refreshed_token() -> None:
    class Credentials(_Credentials):
        def refresh(self, request) -> None:
            assert request == "request"
            self.refresh_count += 1
            self.token = ""
            self.valid = True
            self.expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    provider = GoogleAdcTokenProvider(
        credentials=Credentials(),
        request="request",
    )

    with pytest.raises(VertexCredentialError, match="invalid access token"):
        await provider()
