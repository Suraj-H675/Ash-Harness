"""Google Vertex AI OpenAI-compatible provider with ADC authentication."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

from ash.providers.capabilities import (
    ProviderCapabilities,
    vertex_google_capabilities,
    vertex_google_requires_tool_thought_signature,
)
from ash.providers.google_replay import GoogleThoughtSignatureReplay
from ash.providers.messages import MessageInput
from ash.providers.openai import OpenAIProvider
from ash.providers.replay_state import ProviderReplayStateCipher


_VERTEX_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_MAX_ADC_TOKEN_CHARS = 65_536


class VertexBackendUnavailable(ImportError):
    """Raised when the optional Google authentication dependency is missing."""


class VertexCredentialError(RuntimeError):
    """Raised when Application Default Credentials cannot provide a safe token."""


def vertex_openai_base_url(project: str, location: str) -> str:
    """Return the exact Vertex OpenAI-compatible API root."""

    project_name = _safe_vertex_segment(project, label="project")
    location_name = _safe_vertex_segment(location, label="location").casefold()
    host = (
        "aiplatform.googleapis.com"
        if location_name == "global"
        else f"{location_name}-aiplatform.googleapis.com"
    )
    return (
        f"https://{host}/v1/projects/{quote(project_name, safe='-._:')}"
        f"/locations/{quote(location_name, safe='-._')}/endpoints/openapi"
    )


class GoogleAdcTokenProvider:
    """Async callable that refreshes Google ADC and remembers tokens for redaction."""

    def __init__(
        self,
        *,
        credentials: Any | None = None,
        request: Any | None = None,
    ) -> None:
        self._credentials = credentials
        self._request = request
        self._lock = asyncio.Lock()
        self._recent_tokens: deque[str] = deque(maxlen=2)

    async def __call__(self) -> str:
        async with self._lock:
            if self._credentials is None or self._request is None:
                await self._load_default_credentials()
            credentials = self._credentials
            assert credentials is not None
            if not _credentials_are_fresh(credentials):
                try:
                    await asyncio.to_thread(credentials.refresh, self._request)
                except Exception as exc:  # noqa: BLE001 - external credential chain
                    raise VertexCredentialError(
                        "Vertex ADC refresh failed "
                        f"({type(exc).__name__})"
                    ) from exc
            token = _validated_adc_token(getattr(credentials, "token", None))
            self._recent_tokens.append(token)
            return token

    def redaction_secrets(self) -> tuple[str, ...]:
        return tuple(self._recent_tokens)

    async def _load_default_credentials(self) -> None:
        try:
            import google.auth  # type: ignore[import-untyped]
            from google.auth.transport.requests import Request  # type: ignore[import-untyped]
        except ImportError as exc:
            raise VertexBackendUnavailable(
                "Vertex AI ADC support requires the 'gcp' extra; "
                "install Ash with --extra gcp."
            ) from exc

        def load() -> tuple[Any, Any]:
            credentials, _project = google.auth.default(scopes=[_VERTEX_SCOPE])
            return credentials, Request()

        try:
            credentials, request = await asyncio.to_thread(load)
        except Exception as exc:  # noqa: BLE001 - external credential chain
            raise VertexCredentialError(
                "Vertex ADC discovery failed "
                f"({type(exc).__name__})"
            ) from exc
        self._credentials = credentials
        self._request = request


class VertexProvider(OpenAIProvider):
    """OpenAI-wire Vertex route authenticated by short-lived Google ADC tokens."""

    provider_family = "vertex"

    def __init__(
        self,
        model_name: str,
        *,
        project: str,
        location: str,
        token_provider: GoogleAdcTokenProvider | None = None,
        replay_state_directory: Path | None = None,
        replay_state_trusted_root: Path | None = None,
        replay_state_cipher: ProviderReplayStateCipher | None = None,
        client: Any | None = None,
    ) -> None:
        self.project = _safe_vertex_segment(project, label="project")
        self.location = _safe_vertex_segment(location, label="location").casefold()
        self._adc = token_provider or GoogleAdcTokenProvider()
        model_capabilities = vertex_google_capabilities(model_name)
        cipher = replay_state_cipher
        if (
            model_capabilities.native_tools
            and cipher is None
            and replay_state_directory is not None
        ):
            cipher = ProviderReplayStateCipher(
                replay_state_directory,
                trusted_root=replay_state_trusted_root
                or replay_state_directory.parent,
            )
        self._google_replay = (
            GoogleThoughtSignatureReplay(
                cipher,
                state_provider="vertex",
                requires_signature=vertex_google_requires_tool_thought_signature,
            )
            if model_capabilities.native_tools
            else None
        )
        super().__init__(
            model_name=model_name,
            api_key=self._adc,
            base_url=vertex_openai_base_url(self.project, self.location),
            error_secrets_supplier=self._adc.redaction_secrets,
            client=client,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return vertex_google_capabilities(self.model_name)

    def _prepare_messages(
        self,
        messages: Sequence[MessageInput],
    ) -> list[dict[str, Any]]:
        prepared = super()._prepare_messages(messages)
        replay = self._google_replay
        if replay is None:
            return prepared
        return replay.prepare_messages(messages, prepared, model_name=self.model_name)

    def _capture_tool_call_provider_data(
        self,
        partial: Any,
        tool_call: Any,
    ) -> tuple[str, ...]:
        replay = self._google_replay
        return replay.capture_tool_call(partial, tool_call) if replay is not None else ()

    def _provider_state_for_tool_calls(
        self,
        partials: Sequence[Any],
    ) -> list[dict[str, Any]] | None:
        replay = self._google_replay
        if replay is None:
            return None
        return replay.provider_state(partials, model_name=self.model_name)

    async def verify_credentials(self) -> None:
        """Resolve/refresh ADC without issuing a model request."""

        await self._adc()


def _credentials_are_fresh(credentials: Any) -> bool:
    if not getattr(credentials, "valid", False):
        return False
    token = getattr(credentials, "token", None)
    if not isinstance(token, str) or not token:
        return False
    expiry = getattr(credentials, "expiry", None)
    if not isinstance(expiry, datetime):
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry > datetime.now(timezone.utc)


def _validated_adc_token(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_ADC_TOKEN_CHARS:
        raise VertexCredentialError("Vertex ADC returned an invalid access token")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise VertexCredentialError("Vertex ADC returned an invalid access token")
    return value


def _safe_vertex_segment(value: str, *, label: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"Vertex {label} is required")
    if len(normalized) > 128:
        raise ValueError(f"Vertex {label} is too long")
    if any(
        character.isspace()
        or ord(character) < 33
        or character in {"/", "\\", "?", "#"}
        for character in normalized
    ):
        raise ValueError(f"Vertex {label} must be one safe path segment")
    return normalized
