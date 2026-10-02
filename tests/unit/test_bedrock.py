from __future__ import annotations

from types import SimpleNamespace

import pytest

from ash.providers.bedrock import (
    BedrockDiscoveryError,
    BedrockProvider,
    bedrock_runtime_base_url,
    discover_bedrock_models,
)


class _BedrockControlClient:
    def __init__(self) -> None:
        self.closed = False
        self.profile_calls: list[dict[str, object]] = []

    def list_foundation_models(self):
        return {
            "modelSummaries": [
                {"modelId": "anthropic.claude-model"},
                {"modelId": "amazon.nova-model"},
            ]
        }

    def list_inference_profiles(self, **kwargs):
        self.profile_calls.append(kwargs)
        if "nextToken" not in kwargs:
            return {
                "inferenceProfileSummaries": [
                    {"inferenceProfileId": "us.anthropic.claude-model"},
                    {"inferenceProfileId": "anthropic.claude-model"},
                ],
                "nextToken": "page-2",
            }
        assert kwargs["nextToken"] == "page-2"
        return {
            "inferenceProfileSummaries": [
                {"inferenceProfileId": "global.amazon.nova-model"}
            ]
        }

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, *, client=None, endpoint=None) -> None:
        self.client = client
        self.endpoint = endpoint
        self.created: list[tuple[str, str]] = []

    def create_client(self, service: str, *, region_name: str):
        self.created.append((service, region_name))
        assert self.client is not None
        return self.client

    def get_data(self, name: str):
        assert name == "endpoints"
        return {
            "version": 3,
            "partitions": [
                {
                    "partition": "aws-us-gov",
                    "regionRegex": r"^us\-gov\-\w+\-\d+$",
                    "dnsSuffix": "amazonaws.com",
                    "regions": {"us-gov-west-1": {}},
                    "services": {
                        "bedrock-runtime": {
                            "endpoints": {
                                "us-gov-west-1": {
                                    "hostname": (
                                        "bedrock-runtime.us-gov-west-1.amazonaws.com"
                                    ),
                                    "protocols": ["https"],
                                }
                            }
                        }
                    },
                }
            ],
        }


def test_bedrock_native_discovery_includes_models_and_profiles() -> None:
    client = _BedrockControlClient()
    session = _Session(client=client)
    seen_profiles: list[str] = []

    models = discover_bedrock_models(
        region="us-west-2",
        profile="engineering",
        session_factory=lambda profile: (seen_profiles.append(profile) or session),
    )

    assert seen_profiles == ["engineering"]
    assert session.created == [("bedrock", "us-west-2")]
    assert models == (
        "anthropic.claude-model",
        "amazon.nova-model",
        "us.anthropic.claude-model",
        "global.amazon.nova-model",
    )
    assert client.profile_calls == [
        {"maxResults": 1000},
        {"maxResults": 1000, "nextToken": "page-2"},
    ]
    assert client.closed is True


def test_bedrock_runtime_endpoint_uses_partition_aware_resolver() -> None:
    session = _Session(
        endpoint={
            "hostname": "bedrock-runtime.us-gov-west-1.amazonaws.com",
            "protocols": ["https"],
        }
    )

    result = bedrock_runtime_base_url(
        region="us-gov-west-1",
        profile="gov",
        session_factory=lambda profile: session,
    )

    assert result == (
        "https://bedrock-runtime.us-gov-west-1.amazonaws.com/openai/v1"
    )


def test_bedrock_discovery_rejects_unsafe_model_id() -> None:
    class Client(_BedrockControlClient):
        def list_foundation_models(self):
            return {"modelSummaries": [{"modelId": "bad\nmodel"}]}

    client = Client()
    with pytest.raises(BedrockDiscoveryError, match="invalid model ID"):
        discover_bedrock_models(
            region="us-west-2",
            session_factory=lambda profile: _Session(client=client),
        )
    assert client.closed is True


@pytest.mark.asyncio
async def test_bedrock_provider_can_use_injected_openai_wire_client() -> None:
    terminal = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="done", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
    )

    class Stream:
        def __aiter__(self):
            async def iterator():
                yield terminal

            return iterator()

    class Completions:
        def __init__(self) -> None:
            self.kwargs = None

        async def create(self, **kwargs):
            self.kwargs = kwargs
            return Stream()

    completions = Completions()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )
    provider = BedrockProvider(
        "anthropic.claude-model",
        region="us-west-2",
        client=client,
    )
    provider.configure_max_tokens(128)

    chunks = [chunk async for chunk in provider.stream_chat([])]

    assert chunks[-1].content == "done"
    assert chunks[-1].is_done is True
    assert completions.kwargs["max_tokens"] == 128
    assert "stream_options" not in completions.kwargs
