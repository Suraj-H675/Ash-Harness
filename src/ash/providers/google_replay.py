"""Durable replay for Google Gemini thought signatures on OpenAI-wire routes."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ash.providers.messages import MessageInput, normalize_messages
from ash.providers.replay_state import (
    ProviderReplayStateCipher,
    ProviderReplayStateError,
)


class GoogleThoughtSignatureReplay:
    """Capture, seal, and restore opaque Gemini tool-call thought signatures."""

    def __init__(
        self,
        cipher: ProviderReplayStateCipher | None,
        *,
        state_provider: str,
        requires_signature: Callable[[str], bool],
    ) -> None:
        self._cipher = cipher
        self._state_provider = state_provider
        self._requires_signature = requires_signature

    def prepare_messages(
        self,
        messages: Sequence[MessageInput],
        prepared: list[dict[str, Any]],
        *,
        model_name: str,
    ) -> list[dict[str, Any]]:
        normalized = normalize_messages(messages)
        for source, item in zip(normalized, prepared, strict=True):
            if source.get("role") != "assistant" or not source.get("tool_calls"):
                continue
            signatures = self._replay_signatures(source)
            if not signatures:
                if self._requires_signature(model_name):
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

    def capture_tool_call(
        self,
        partial: Any,
        tool_call: Any,
    ) -> tuple[str, ...]:
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
            raise RuntimeError(
                "Google Gemini tool call contained an invalid thought signature"
            )
        previous = partial.provider_data.get("google_thought_signature")
        if previous is not None and previous != signature:
            raise RuntimeError(
                "Google Gemini tool call changed thought signature mid-stream"
            )
        partial.provider_data["google_thought_signature"] = signature
        return (signature,) if previous is None else ()

    def provider_state(
        self,
        partials: Sequence[Any],
        *,
        model_name: str,
    ) -> list[dict[str, Any]] | None:
        if not partials:
            return None
        signatures = {
            partial.id: signature
            for partial in partials
            if (
                signature := partial.provider_data.get("google_thought_signature")
            )
        }
        if not signatures:
            if self._requires_signature(model_name):
                raise RuntimeError(
                    "Google Gemini tool call omitted required thought signature"
                )
            return None
        if self._cipher is None:
            raise RuntimeError(
                "Google Gemini thought-signature replay requires durable provider state"
            )
        try:
            return [
                self._cipher.seal(
                    provider=self._state_provider,
                    kind="thought_signatures",
                    text=json.dumps(signatures, separators=(",", ":"), sort_keys=True),
                )
            ]
        except ProviderReplayStateError as exc:
            raise RuntimeError(
                "Google Gemini thought-signature replay could not be persisted safely"
            ) from exc

    def _replay_signatures(self, message: Mapping[str, Any]) -> dict[str, str]:
        matching = [
            item
            for item in message.get("provider_state") or []
            if isinstance(item, dict)
            and item.get("type") == "sealed_provider_state"
            and item.get("provider") == self._state_provider
            and item.get("kind") == "thought_signatures"
        ]
        if not matching:
            return {}
        if len(matching) != 1 or self._cipher is None:
            raise RuntimeError(
                "Google Gemini thought-signature replay state is unavailable"
            )
        try:
            raw = self._cipher.open(
                matching[0],
                provider=self._state_provider,
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
