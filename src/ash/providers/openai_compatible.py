from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from ash.providers.capabilities import ProviderCapabilities
from ash.providers.openai import OpenAIProvider
from ash.providers.readiness import (
    CatalogFormat,
    ProviderModelMetadata,
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
        additional_catalog_sources: Sequence[
            tuple[str, CatalogFormat, Mapping[str, str]]
        ] = (),
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
        self._catalog_sources = (
            (catalog_endpoint, catalog_format, dict(catalog_headers)),
            *(
                (endpoint, source_format, dict(source_headers))
                for endpoint, source_format, source_headers in additional_catalog_sources
            ),
        )
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
        catalogs: list[tuple[ProviderModelMetadata, ...]] = []
        direct_matches: list[tuple[int, ProviderModelMetadata]] = []
        failures: list[Exception] = []
        for endpoint, catalog_format, headers in self._catalog_sources:
            try:
                catalog = await asyncio.to_thread(
                    probe_model_catalog_metadata,
                    endpoint,
                    headers=headers,
                    catalog_format=catalog_format,
                    timeout=5.0,
                )
            except Exception as exc:  # noqa: BLE001 - partial sources may still verify
                failures.append(exc)
                continue
            catalog_index = len(catalogs)
            catalogs.append(catalog)
            metadata = select_provider_model_metadata(catalog, self.model_name)
            if metadata is not None:
                direct_matches.append((catalog_index, metadata))

        matched = [metadata for _, metadata in direct_matches]
        canonical_ids = {metadata.model_id for _, metadata in direct_matches}
        if len(canonical_ids) > 1:
            self._dynamic_capabilities = ProviderCapabilities(local=self._local)
            return self._dynamic_capabilities
        if len(canonical_ids) == 1:
            canonical_id = next(iter(canonical_ids))
            directly_matched = {index for index, _ in direct_matches}
            for index, catalog in enumerate(catalogs):
                if index in directly_matched:
                    continue
                metadata = select_provider_model_metadata(catalog, canonical_id)
                if metadata is not None:
                    if metadata.model_id != canonical_id:
                        self._dynamic_capabilities = ProviderCapabilities(
                            local=self._local
                        )
                        return self._dynamic_capabilities
                    matched.append(metadata)

        if matched:
            params = frozenset().union(*(item.supported_parameters for item in matched))
            modalities = frozenset().union(*(item.input_modalities for item in matched))
            native_tools = _merge_capability_boolean(
                [item.native_tools for item in matched],
                fallback=self.provider_family != "vllm" and "tools" in params,
            )
            vision = _merge_capability_boolean(
                [item.vision for item in matched],
                fallback="image" in modalities,
            )
            reasoning = _merge_capability_boolean(
                [item.reasoning for item in matched],
                fallback=bool({"reasoning", "reasoning_effort"} & params),
            )
            context_windows = [
                item.context_window for item in matched if item.context_window is not None
            ]
            output_limits = [
                item.max_output_tokens
                for item in matched
                if item.max_output_tokens is not None
            ]
            self._dynamic_capabilities = ProviderCapabilities(
                native_tools=native_tools,
                vision=vision,
                reasoning=reasoning,
                local=self._local,
                context_window=min(context_windows) if context_windows else None,
                max_output_tokens=min(output_limits) if output_limits else None,
            )
        elif failures and len(failures) == len(self._catalog_sources):
            self._dynamic_capabilities = ProviderCapabilities(local=self._local)
            raise failures[0]
        if self._dynamic_capabilities is None:
            self._dynamic_capabilities = ProviderCapabilities(local=self._local)
        return self._dynamic_capabilities


def _merge_capability_boolean(
    values: Sequence[bool | None],
    *,
    fallback: bool,
) -> bool:
    known = {value for value in values if value is not None}
    if len(known) > 1:
        return False
    if known:
        return next(iter(known))
    return fallback
