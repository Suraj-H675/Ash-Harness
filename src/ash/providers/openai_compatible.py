from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ash.providers.capabilities import ProviderCapabilities
from ash.providers.openai import OpenAIProvider
from ash.providers.readiness import (
    CatalogFormat,
    probe_model_catalog_metadata,
    select_provider_model_metadata,
)


class CatalogOpenAIProvider(OpenAIProvider):
    """OpenAI-wire adapter whose model features come from provider metadata."""

    def __init__(
        self,
        model_name: str,
        api_key: str | None,
        *,
        provider_family: str,
        base_url: str,
        catalog_endpoint: str,
        catalog_format: CatalogFormat,
        catalog_headers: Mapping[str, str],
        allow_anonymous: bool = False,
        local: bool = False,
        client: Any | None = None,
    ) -> None:
        super().__init__(
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
            allow_anonymous=allow_anonymous,
            client=client,
        )
        self.provider_family = provider_family
        self._catalog_endpoint = catalog_endpoint
        self._catalog_format = catalog_format
        self._catalog_headers = dict(catalog_headers)
        self._local = local
        self._dynamic_capabilities: ProviderCapabilities | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        # Wire compatibility is not capability evidence. Until the provider's
        # own catalog is successfully inspected, use a conservative manifest.
        return self._dynamic_capabilities or ProviderCapabilities(local=self._local)

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
                catalog_format=self._catalog_format,
                timeout=5.0,
            )
            metadata = select_provider_model_metadata(catalog, self.model_name)
            if metadata is not None:
                params = metadata.supported_parameters
                self._dynamic_capabilities = ProviderCapabilities(
                    native_tools=(
                        metadata.native_tools
                        if metadata.native_tools is not None
                        else "tools" in params
                    ),
                    vision=(
                        metadata.vision
                        if metadata.vision is not None
                        else "image" in metadata.input_modalities
                    ),
                    reasoning=(
                        metadata.reasoning
                        if metadata.reasoning is not None
                        else bool({"reasoning", "reasoning_effort"} & params)
                    ),
                    local=self._local,
                    context_window=metadata.context_window,
                    max_output_tokens=metadata.max_output_tokens,
                )
        except Exception:
            self._dynamic_capabilities = ProviderCapabilities(local=self._local)
            raise
        if self._dynamic_capabilities is None:
            self._dynamic_capabilities = ProviderCapabilities(local=self._local)
        return self._dynamic_capabilities
