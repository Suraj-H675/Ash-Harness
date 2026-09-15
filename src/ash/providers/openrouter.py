"""OpenRouter adapter with provider-owned model capability negotiation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ash.providers.capabilities import ProviderCapabilities
from ash.providers.openai import OpenAIProvider
from ash.providers.readiness import probe_model_catalog_metadata


class OpenRouterProvider(OpenAIProvider):
    """OpenAI-wire adapter whose capabilities come from OpenRouter metadata."""

    provider_family = "openrouter"

    def __init__(
        self,
        model_name: str,
        api_key: str,
        *,
        base_url: str,
        catalog_endpoint: str,
        catalog_headers: Mapping[str, str],
        client: Any | None = None,
    ) -> None:
        super().__init__(
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
            client=client,
        )
        self._catalog_endpoint = catalog_endpoint
        self._catalog_headers = dict(catalog_headers)
        self._dynamic_capabilities: ProviderCapabilities | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        # A shared wire protocol is not evidence of model capability.  Before a
        # successful catalog probe, fail closed to the canonical conservative set.
        return self._dynamic_capabilities or ProviderCapabilities()

    async def detect_capabilities(
        self, *, refresh: bool = False
    ) -> ProviderCapabilities:
        if self._dynamic_capabilities is not None and not refresh:
            return self._dynamic_capabilities
        self._dynamic_capabilities = None
        try:
            catalog = await asyncio.to_thread(
                probe_model_catalog_metadata,
                self._catalog_endpoint,
                headers=self._catalog_headers,
                catalog_format="openai",
                timeout=5.0,
            )
            metadata = next(
                (item for item in catalog if item.model_id == self.model_name),
                None,
            )
            if metadata is not None:
                params = metadata.supported_parameters
                self._dynamic_capabilities = ProviderCapabilities(
                    native_tools="tools" in params,
                    vision="image" in metadata.input_modalities,
                    reasoning=bool({"reasoning", "reasoning_effort"} & params),
                    context_window=metadata.context_window,
                    max_output_tokens=metadata.max_output_tokens,
                )
        except Exception:
            # Preserve the conservative runtime view, but let callers such as
            # /capabilities --refresh and FailoverProvider observe probe failure.
            self._dynamic_capabilities = ProviderCapabilities()
            raise
        if self._dynamic_capabilities is None:
            self._dynamic_capabilities = ProviderCapabilities()
        return self._dynamic_capabilities
