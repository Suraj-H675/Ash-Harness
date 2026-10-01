"""Shared live verification for first-party OpenAI ChatGPT-plan auth."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from ash.providers.identifiers import parse_model_string
from ash.providers.openai_chatgpt_auth import (
    CHATGPT_RESOURCE,
    ChatGPTAuthError,
    ChatGPTAuthManager,
)
from ash.providers.readiness import (
    ProviderConfigurationError,
    ProviderConnection,
    ProviderVerification,
    ProviderVerificationError,
)

if TYPE_CHECKING:
    from ash.config import AshConfig


async def verify_chatgpt_plan_connection(
    config: "AshConfig",
    *,
    timeout: float = 10.0,
) -> ProviderVerification:
    """Verify the active plan session and selected model against OpenAI."""

    provider, model_name = parse_model_string(config.model)
    if provider != "openai":
        raise ProviderConfigurationError(
            "ChatGPT plan authentication applies only to first-party OpenAI models"
        )
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
    ) as client:
        try:
            catalog = await ChatGPTAuthManager(http_client=client).list_models()
        except ChatGPTAuthError as exc:
            raise ProviderVerificationError(str(exc)) from exc
    models = tuple(model for model, _display_name in catalog)
    connection = ProviderConnection(
        provider="openai",
        model_name=model_name,
        base_url=CHATGPT_RESOURCE,
        catalog_endpoint=f"{CHATGPT_RESOURCE}/models",
        catalog_format="openai",
        auth_mode="chatgpt",
        uses_default_base_url=True,
    )
    return ProviderVerification(
        connection=connection,
        models=models,
        selected_model_available=model_name in models,
    )
