"""Azure OpenAI / Microsoft Foundry v1 provider."""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ash.providers.openai import OpenAIProvider


_AZURE_AI_SCOPE = "https://ai.azure.com/.default"
_AZURE_PUBLIC_HOST_SUFFIXES = (
    ".openai.azure.com",
    ".services.ai.azure.com",
)
_MAX_AZURE_TOKEN_CHARS = 65_536


class AzureBackendUnavailable(ImportError):
    """Raised when the optional Azure identity dependency is unavailable."""


class AzureCredentialError(RuntimeError):
    """Raised when Microsoft Entra credentials cannot provide a safe token."""


class AzureBearerTokenProvider:
    """Wrap an async Entra token provider and retain only recent tokens in memory."""

    def __init__(self, provider: Callable[[], Awaitable[str]]) -> None:
        self._provider = provider
        self._recent_tokens: deque[str] = deque(maxlen=2)

    async def __call__(self) -> str:
        try:
            token = await self._provider()
        except Exception as exc:  # noqa: BLE001 - external credential chain
            raise AzureCredentialError(
                f"Azure Entra token acquisition failed ({type(exc).__name__})"
            ) from exc
        if (
            not isinstance(token, str)
            or not token
            or len(token) > _MAX_AZURE_TOKEN_CHARS
            or any(ord(character) < 32 or ord(character) == 127 for character in token)
        ):
            raise AzureCredentialError("Azure Entra returned an invalid access token")
        self._recent_tokens.append(token)
        return token

    def redaction_secrets(self) -> tuple[str, ...]:
        return tuple(self._recent_tokens)


class AzureProvider(OpenAIProvider):
    """OpenAI-compatible Azure v1 route with key or Entra authentication."""

    provider_family = "azure"

    def __init__(
        self,
        model_name: str,
        *,
        base_url: str,
        auth_mode: str,
        api_key: str = "",
        token_provider: Callable[[], Awaitable[str]] | None = None,
        credential: Any | None = None,
        client: Any | None = None,
    ) -> None:
        self.auth_mode = _normalize_azure_auth_mode(auth_mode)
        self._azure_credential = credential
        self._owns_azure_credential = credential is not None
        normalized_url = normalize_azure_openai_base_url(base_url)

        selected_key: str | Callable[[], Awaitable[str]]
        error_secrets_supplier = None
        if self.auth_mode == "api_key":
            if not api_key:
                raise ValueError("Azure OpenAI API key is required")
            selected_key = api_key
        else:
            if token_provider is None:
                credential, token_provider = _default_entra_token_provider()
                self._azure_credential = credential
                self._owns_azure_credential = True
            wrapped = AzureBearerTokenProvider(token_provider)
            selected_key = wrapped
            error_secrets_supplier = wrapped.redaction_secrets
            self._entra_token_provider = wrapped

        super().__init__(
            model_name=model_name,
            api_key=selected_key,
            base_url=normalized_url,
            error_secrets_supplier=error_secrets_supplier,
            include_stream_usage=True,
            client=client,
        )

    async def verify_credentials(self) -> None:
        if self.auth_mode == "entra":
            await self._entra_token_provider()

    async def aclose(self) -> None:
        close_error: BaseException | None = None
        try:
            await super().aclose()
        except BaseException as exc:
            close_error = exc
        credential = self._azure_credential
        if self._owns_azure_credential and credential is not None:
            try:
                result = credential.close()
                if hasattr(result, "__await__"):
                    await result
            except BaseException as exc:
                if close_error is None:
                    close_error = exc
                else:
                    close_error.add_note(
                        "Azure credential cleanup also failed: "
                        f"{type(exc).__name__}"
                    )
            else:
                if self._azure_credential is credential:
                    self._azure_credential = None
                    self._owns_azure_credential = False
        if close_error is not None:
            raise close_error


def normalize_azure_openai_base_url(value: str) -> str:
    """Normalize one public Azure v1 resource/project endpoint."""

    raw = str(value or "").strip()
    if not raw or any(character.isspace() or ord(character) < 33 for character in raw):
        raise ValueError("Azure OpenAI base URL contains invalid whitespace/control data")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Azure OpenAI base URL is invalid") from exc
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme != "https" or not hostname:
        raise ValueError("Azure OpenAI base URL must use HTTPS")
    if not any(hostname.endswith(suffix) for suffix in _AZURE_PUBLIC_HOST_SUFFIXES):
        raise ValueError(
            "Azure OpenAI base URL must use a public Azure AI resource hostname"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Azure OpenAI base URL must not contain credentials or query")
    if port not in {None, 443}:
        raise ValueError("Azure OpenAI base URL must use the standard HTTPS port")

    path = parsed.path.rstrip("/")
    if not path:
        path = "/openai/v1"
    elif path == "/openai/v1":
        pass
    elif hostname.endswith(".services.ai.azure.com"):
        parts = path.split("/")
        is_project_root = (
            len(parts) == 4
            and parts[:3] == ["", "api", "projects"]
            and _safe_azure_project_segment(parts[3])
        )
        is_project_v1 = (
            len(parts) == 6
            and parts[:3] == ["", "api", "projects"]
            and _safe_azure_project_segment(parts[3])
            and parts[4:] == ["openai", "v1"]
        )
        if is_project_root:
            path = f"{path}/openai/v1"
        elif not is_project_v1:
            raise ValueError(
                "Azure Foundry project URL must use /api/projects/PROJECT/openai/v1"
            )
    else:
        raise ValueError(
            "Azure OpenAI resource URL must end in /openai/v1"
        )
    return urlunsplit(("https", parsed.netloc, path, "", ""))


def _normalize_azure_auth_mode(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in {"api_key", "entra"}:
        raise ValueError("Azure auth mode must be api_key or entra")
    return normalized


def _safe_azure_project_segment(value: str) -> bool:
    return bool(
        value
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and all(not character.isspace() and ord(character) >= 33 for character in value)
    )


def _default_entra_token_provider() -> tuple[Any, Callable[[], Awaitable[str]]]:
    try:
        from azure.identity.aio import (  # type: ignore[import-not-found]
            DefaultAzureCredential,
            get_bearer_token_provider,
        )
    except ImportError as exc:
        raise AzureBackendUnavailable(
            "Azure Entra support requires the 'azure' extra; "
            "install Ash with --extra azure."
        ) from exc
    try:
        credential = DefaultAzureCredential()
        provider = get_bearer_token_provider(credential, _AZURE_AI_SCOPE)
    except ImportError as exc:
        raise AzureBackendUnavailable(
            "Azure Entra async transport is incomplete; reinstall Ash with "
            "--extra azure."
        ) from exc
    return credential, provider
