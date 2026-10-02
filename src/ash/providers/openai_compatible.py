from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ash.providers.capabilities import (
    ProviderCapabilities,
    google_capabilities,
    google_requires_tool_thought_signature,
)
from ash.providers.messages import MessageInput, normalize_messages
from ash.providers.openai import OpenAIProvider
from ash.providers.replay_state import (
    ProviderReplayStateCipher,
    ProviderReplayStateError,
)
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
        default_headers: Mapping[str, str] | None = None,
        declared_capabilities: ProviderCapabilities | None = None,
        replay_state_cipher: ProviderReplayStateCipher | None = None,
        google_thought_signature_replay: bool = False,
        client: Any | None = None,
    ) -> None:
        super().__init__(
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
            allow_anonymous=allow_anonymous,
            default_headers=default_headers,
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
        self._declared_capabilities = declared_capabilities or ProviderCapabilities(
            local=local
        )
        self._replay_state_cipher = replay_state_cipher
        self._google_thought_signature_replay = google_thought_signature_replay
        self._canonical_model_id: str | None = None
        self._dynamic_capabilities: ProviderCapabilities | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        # Wire compatibility is not capability evidence. Until the provider's
        # own catalog is successfully inspected, use a conservative manifest.
        return self._dynamic_capabilities or self._declared_capabilities

    def _prepare_messages(
        self,
        messages: Sequence[MessageInput],
    ) -> list[dict[str, Any]]:
        prepared = super()._prepare_messages(messages)
        if not self._google_thought_signature_replay:
            return prepared
        normalized = normalize_messages(messages)
        for source, item in zip(normalized, prepared, strict=True):
            if source.get("role") != "assistant" or not source.get("tool_calls"):
                continue
            signatures = self._google_replay_signatures(source)
            if not signatures:
                if google_requires_tool_thought_signature(
                    self._canonical_model_id or self.model_name
                ):
                    raise RuntimeError(
                        "Google Gemini tool-call history is missing required "
                        "thought-signature replay state"
                    )
                continue
            wire_calls = item.get("tool_calls")
            if not isinstance(wire_calls, list):
                continue
            for call in wire_calls:
                if not isinstance(call, dict):
                    continue
                call_id = call.get("id")
                signature = signatures.get(call_id) if isinstance(call_id, str) else None
                if signature is not None:
                    call["extra_content"] = {
                        "google": {"thought_signature": signature}
                    }
        return prepared

    def _google_replay_signatures(self, message: Mapping[str, Any]) -> dict[str, str]:
        matching = [
            item
            for item in message.get("provider_state") or []
            if isinstance(item, dict)
            and item.get("type") == "sealed_provider_state"
            and item.get("provider") == "google"
            and item.get("kind") == "thought_signatures"
        ]
        if not matching:
            return {}
        if len(matching) != 1 or self._replay_state_cipher is None:
            raise RuntimeError("Google Gemini thought-signature replay state is unavailable")
        try:
            raw = self._replay_state_cipher.open(
                matching[0],
                provider="google",
                kind="thought_signatures",
            )
            decoded = json.loads(raw)
        except (ProviderReplayStateError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "Google Gemini thought-signature replay could not be recovered safely"
            ) from exc
        if not isinstance(decoded, dict) or len(decoded) > 64:
            raise RuntimeError("Google Gemini thought-signature replay state is invalid")
        signatures: dict[str, str] = {}
        for call_id, signature in decoded.items():
            if (
                not isinstance(call_id, str)
                or not call_id
                or not isinstance(signature, str)
                or not signature
            ):
                raise RuntimeError(
                    "Google Gemini thought-signature replay state is invalid"
                )
            signatures[call_id] = signature
        return signatures

    def _capture_tool_call_provider_data(
        self,
        partial: Any,
        tool_call: Any,
    ) -> tuple[str, ...]:
        if not self._google_thought_signature_replay:
            return ()
        extra = getattr(tool_call, "extra_content", None)
        if extra is None:
            return ()
        if not isinstance(extra, Mapping):
            raise RuntimeError("Google Gemini tool call contained invalid extra content")
        google = extra.get("google")
        if google is None:
            return ()
        if not isinstance(google, Mapping):
            raise RuntimeError("Google Gemini tool call contained invalid signature data")
        signature = google.get("thought_signature")
        if signature is None:
            return ()
        if not isinstance(signature, str) or not signature:
            raise RuntimeError("Google Gemini tool call contained an invalid thought signature")
        previous = partial.provider_data.get("google_thought_signature")
        if previous is not None and previous != signature:
            raise RuntimeError("Google Gemini tool call changed thought signature mid-stream")
        partial.provider_data["google_thought_signature"] = signature
        return (signature,) if previous is None else ()

    def _provider_state_for_tool_calls(
        self,
        partials: Sequence[Any],
    ) -> list[dict[str, Any]] | None:
        if not self._google_thought_signature_replay or not partials:
            return None
        signatures = {
            partial.id: signature
            for partial in partials
            if (
                signature := partial.provider_data.get("google_thought_signature")
            )
        }
        if not signatures:
            if google_requires_tool_thought_signature(
                self._canonical_model_id or self.model_name
            ):
                raise RuntimeError(
                    "Google Gemini tool call omitted required thought signature"
                )
            return None
        if self._replay_state_cipher is None:
            raise RuntimeError(
                "Google Gemini thought-signature replay requires durable provider state"
            )
        try:
            return [
                self._replay_state_cipher.seal(
                    provider="google",
                    kind="thought_signatures",
                    text=json.dumps(signatures, separators=(",", ":"), sort_keys=True),
                )
            ]
        except ProviderReplayStateError as exc:
            raise RuntimeError(
                "Google Gemini thought-signature replay could not be persisted safely"
            ) from exc

    async def detect_capabilities(
        self, *, refresh: bool = False
    ) -> ProviderCapabilities:
        if self._dynamic_capabilities is not None and not refresh:
            return self._dynamic_capabilities
        self._dynamic_capabilities = None
        self._canonical_model_id = None
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
            self._canonical_model_id = canonical_id
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
            declared_capabilities = self._declared_capabilities
            if self._google_thought_signature_replay and self._canonical_model_id:
                canonical_google = google_capabilities(self._canonical_model_id)
                if canonical_google != ProviderCapabilities():
                    declared_capabilities = canonical_google
            params = frozenset().union(*(item.supported_parameters for item in matched))
            modalities = frozenset().union(*(item.input_modalities for item in matched))
            native_tools = _merge_capability_boolean(
                [item.native_tools for item in matched],
                fallback=(
                    declared_capabilities.native_tools
                    or (self.provider_family != "vllm" and "tools" in params)
                ),
            )
            vision = _merge_capability_boolean(
                [item.vision for item in matched],
                fallback=declared_capabilities.vision or "image" in modalities,
            )
            reasoning = _merge_capability_boolean(
                [item.reasoning for item in matched],
                fallback=(
                    declared_capabilities.reasoning
                    or bool({"reasoning", "reasoning_effort"} & params)
                ),
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
                context_window=(
                    min(context_windows)
                    if context_windows
                    else declared_capabilities.context_window
                ),
                max_output_tokens=(
                    min(output_limits)
                    if output_limits
                    else declared_capabilities.max_output_tokens
                ),
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
