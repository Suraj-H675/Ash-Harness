"""Amazon Bedrock Runtime provider using the official OpenAI SDK provider."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from ash.providers.openai import OpenAIProvider


MAX_BEDROCK_DISCOVERED_MODELS = 10_000


class BedrockBackendUnavailable(ImportError):
    """Raised when the optional Runtime-capable AWS provider is unavailable."""


class BedrockDiscoveryError(RuntimeError):
    """Raised when Bedrock model/profile discovery cannot complete safely."""


class BedrockProvider(OpenAIProvider):
    """Bedrock Runtime OpenAI Chat Completions via AWS SigV4 credentials."""

    provider_family = "bedrock"

    def __init__(
        self,
        model_name: str,
        *,
        region: str,
        profile: str = "",
        client: Any | None = None,
    ) -> None:
        self.region = _safe_aws_region(region)
        self.profile = _safe_aws_profile(profile)
        owns_client = client is None
        if client is None:
            client = _build_bedrock_client(self.region, self.profile)
        super().__init__(
            model_name=model_name,
            api_key=None,
            allow_anonymous=True,
            include_stream_usage=False,
            client=client,
        )
        self._owns_client = owns_client


def discover_bedrock_models(
    *,
    region: str,
    profile: str = "",
    session_factory: Callable[[str], Any] | None = None,
) -> tuple[str, ...]:
    """List accessible Runtime foundation models and inference profiles."""

    region_name = _safe_aws_region(region)
    profile_name = _safe_aws_profile(profile)
    session = (
        session_factory(profile_name)
        if session_factory is not None
        else _botocore_session(profile_name)
    )
    try:
        client = session.create_client("bedrock", region_name=region_name)
    except Exception as exc:  # noqa: BLE001 - external AWS credential/config chain
        raise BedrockDiscoveryError(
            "Bedrock discovery client could not be created "
            f"({type(exc).__name__})"
        ) from exc

    models: list[str] = []
    try:
        foundation = client.list_foundation_models()
        for item in _response_items(foundation, "modelSummaries"):
            _append_model_id(models, item.get("modelId"))

        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"maxResults": 1000}
            if next_token:
                kwargs["nextToken"] = next_token
            page = client.list_inference_profiles(**kwargs)
            for item in _response_items(page, "inferenceProfileSummaries"):
                _append_model_id(models, item.get("inferenceProfileId"))
            raw_next = page.get("nextToken") if isinstance(page, dict) else None
            next_token = raw_next if isinstance(raw_next, str) and raw_next else None
            if next_token is None:
                break
            if len(models) >= MAX_BEDROCK_DISCOVERED_MODELS:
                raise BedrockDiscoveryError(
                    "Bedrock discovery exceeded the model safety limit"
                )
    except BedrockDiscoveryError:
        raise
    except Exception as exc:  # noqa: BLE001 - external AWS service
        raise BedrockDiscoveryError(
            "Bedrock model discovery failed "
            f"({type(exc).__name__})"
        ) from exc
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    return tuple(dict.fromkeys(models))


def bedrock_runtime_base_url(
    *,
    region: str,
    profile: str = "",
    session_factory: Callable[[str], Any] | None = None,
) -> str:
    """Resolve the partition-aware Runtime API root without credentials/network."""

    region_name = _safe_aws_region(region)
    profile_name = _safe_aws_profile(profile)
    session = (
        session_factory(profile_name)
        if session_factory is not None
        else _botocore_session(profile_name)
    )
    try:
        from botocore.regions import EndpointResolver  # type: ignore[import-not-found,import-untyped]

        endpoint_data = session.get_data("endpoints")
        resolver = EndpointResolver(endpoint_data, uses_builtin_data=True)
        endpoint = resolver.construct_endpoint("bedrock-runtime", region_name)
    except Exception as exc:  # noqa: BLE001 - optional SDK internals
        raise BedrockBackendUnavailable(
            "Amazon Bedrock endpoint resolution requires the 'aws' extra"
        ) from exc
    if not isinstance(endpoint, dict):
        raise BedrockBackendUnavailable(
            f"Amazon Bedrock Runtime is unavailable in region {region_name!r}"
        )
    hostname = endpoint.get("hostname")
    protocols = endpoint.get("protocols")
    if not isinstance(hostname, str) or not hostname:
        raise BedrockBackendUnavailable(
            f"Amazon Bedrock Runtime is unavailable in region {region_name!r}"
        )
    scheme = (
        "https"
        if not isinstance(protocols, list) or "https" in protocols
        else str(protocols[0])
    )
    if scheme != "https":
        raise BedrockBackendUnavailable(
            "Amazon Bedrock Runtime endpoint must use HTTPS"
        )
    return f"https://{hostname}/openai/v1"


def _build_bedrock_client(region: str, profile: str) -> Any:
    try:
        import botocore  # type: ignore[import-not-found,import-untyped]  # noqa: F401
        from openai import AsyncOpenAI
        from openai.providers import bedrock
    except ImportError as exc:
        raise BedrockBackendUnavailable(
            "Amazon Bedrock SigV4 support requires the 'aws' extra; "
            "install Ash with --extra aws."
        ) from exc
    if "endpoint" not in inspect.signature(bedrock).parameters:
        raise BedrockBackendUnavailable(
            "Amazon Bedrock Runtime requires a newer OpenAI SDK; "
            "install Ash with --extra aws."
        )
    provider_factory: Any = bedrock
    provider = provider_factory(
        endpoint="runtime",
        region=region,
        profile=profile or None,
        api_key=None,
    )
    return AsyncOpenAI(provider=provider, max_retries=0)


def _botocore_session(profile: str) -> Any:
    try:
        import botocore.session  # type: ignore[import-not-found,import-untyped]
    except ImportError as exc:
        raise BedrockBackendUnavailable(
            "Amazon Bedrock SigV4 support requires the 'aws' extra; "
            "install Ash with --extra aws."
        ) from exc
    return botocore.session.Session(profile=profile or None)


def _response_items(response: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        raise BedrockDiscoveryError("Bedrock discovery returned an invalid payload")
    raw = response.get(key, [])
    if not isinstance(raw, list):
        raise BedrockDiscoveryError("Bedrock discovery returned an invalid payload")
    return [item for item in raw if isinstance(item, dict)]


def _append_model_id(models: list[str], value: Any) -> None:
    if not isinstance(value, str) or not value:
        return
    if len(value) > 1024 or any(ord(character) < 32 for character in value):
        raise BedrockDiscoveryError("Bedrock discovery returned an invalid model ID")
    models.append(value)
    if len(models) > MAX_BEDROCK_DISCOVERED_MODELS:
        raise BedrockDiscoveryError("Bedrock discovery exceeded the model safety limit")


def _safe_aws_region(value: str) -> str:
    normalized = str(value).strip().casefold()
    if (
        not normalized
        or len(normalized) > 64
        or any(
            character.isspace()
            or ord(character) < 33
            or character in {"/", "\\", "?", "#"}
            for character in normalized
        )
    ):
        raise ValueError("Amazon Bedrock region is invalid")
    return normalized


def _safe_aws_profile(value: str) -> str:
    normalized = str(value).strip()
    if len(normalized) > 128 or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized
    ):
        raise ValueError("Amazon Bedrock profile is invalid")
    return normalized
