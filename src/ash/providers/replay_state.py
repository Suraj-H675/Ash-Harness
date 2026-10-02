"""Sealed durable state required to replay provider conversations exactly."""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ash.safety.private_store import PrivateStore, PrivateStoreError


_KEY_FILE = "replay-state-v1.key"
_KEY_MAGIC = b"ash-provider-replay-v1\x00"
_KEY_BYTES = 32
_NONCE_BYTES = 12
MAX_REPLAY_PLAINTEXT_BYTES = 3_000_000


class ProviderReplayStateError(RuntimeError):
    """Provider replay state cannot be sealed or recovered safely."""


class ProviderReplayStateCipher:
    """Seal exact provider replay text while persisting only ciphertext."""

    def __init__(
        self,
        directory: str | Path,
        *,
        trusted_root: str | Path,
    ) -> None:
        try:
            self._store = PrivateStore(directory, trusted_root=trusted_root)
        except PrivateStoreError as exc:
            raise ProviderReplayStateError(
                "secure provider replay-state storage is unavailable"
            ) from exc
        self._key: bytes | None = None

    def seal(self, *, provider: str, kind: str, text: str) -> dict[str, Any]:
        provider_name = _bounded_label(provider, label="provider")
        state_kind = _bounded_label(kind, label="state kind")
        if not isinstance(text, str) or not text:
            raise ProviderReplayStateError("provider replay text must be non-empty")
        try:
            plaintext = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ProviderReplayStateError(
                "provider replay text must be valid UTF-8"
            ) from exc
        if len(plaintext) > MAX_REPLAY_PLAINTEXT_BYTES:
            raise ProviderReplayStateError(
                "provider replay state exceeds the durable safety limit"
            )

        key = self._load_or_create_key()
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(key).encrypt(
            nonce,
            plaintext,
            _associated_data(provider_name, state_kind),
        )
        return {
            "type": "sealed_provider_state",
            "version": 1,
            "provider": provider_name,
            "kind": state_kind,
            "nonce": _encode(nonce),
            "ciphertext": _encode(ciphertext),
        }

    def open(
        self,
        item: Mapping[str, Any],
        *,
        provider: str,
        kind: str,
    ) -> str:
        provider_name = _bounded_label(provider, label="provider")
        state_kind = _bounded_label(kind, label="state kind")
        if (
            item.get("type") != "sealed_provider_state"
            or item.get("version") != 1
            or item.get("provider") != provider_name
            or item.get("kind") != state_kind
        ):
            raise ProviderReplayStateError("provider replay state does not match route")
        nonce = _decode(item.get("nonce"), label="nonce")
        ciphertext = _decode(item.get("ciphertext"), label="ciphertext")
        if len(nonce) != _NONCE_BYTES or not ciphertext:
            raise ProviderReplayStateError("provider replay state is malformed")
        if len(ciphertext) > MAX_REPLAY_PLAINTEXT_BYTES + 32:
            raise ProviderReplayStateError(
                "provider replay state exceeds the durable safety limit"
            )

        key = self._load_existing_key()
        try:
            plaintext = AESGCM(key).decrypt(
                nonce,
                ciphertext,
                _associated_data(provider_name, state_kind),
            )
        except InvalidTag as exc:
            raise ProviderReplayStateError(
                "provider replay state failed integrity verification"
            ) from exc
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProviderReplayStateError(
                "provider replay state is not valid UTF-8"
            ) from exc

    def _load_existing_key(self) -> bytes:
        if self._key is not None:
            return self._key
        try:
            record = self._store.read(
                _KEY_FILE,
                max_bytes=len(_KEY_MAGIC) + _KEY_BYTES,
            )
        except PrivateStoreError as exc:
            raise ProviderReplayStateError(
                "secure provider replay-state key cannot be read"
            ) from exc
        if record is None:
            raise ProviderReplayStateError(
                "secure provider replay-state key is missing"
            )
        self._key = _decode_key_record(record)
        return self._key

    def _load_or_create_key(self) -> bytes:
        if self._key is not None:
            return self._key
        selected: bytes | None = None

        def update(current: bytes | None) -> bytes:
            nonlocal selected
            if current is None:
                selected = os.urandom(_KEY_BYTES)
                return _KEY_MAGIC + selected
            selected = _decode_key_record(current)
            return current

        try:
            self._store.update(
                _KEY_FILE,
                update,
                max_bytes=len(_KEY_MAGIC) + _KEY_BYTES,
            )
        except PrivateStoreError as exc:
            raise ProviderReplayStateError(
                "secure provider replay-state key cannot be established"
            ) from exc
        assert selected is not None
        self._key = selected
        return selected


def _decode_key_record(record: bytes) -> bytes:
    if (
        len(record) != len(_KEY_MAGIC) + _KEY_BYTES
        or not record.startswith(_KEY_MAGIC)
    ):
        raise ProviderReplayStateError(
            "secure provider replay-state key record is invalid"
        )
    return record[len(_KEY_MAGIC) :]


def _bounded_label(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ProviderReplayStateError(f"provider replay {label} is invalid")
    if any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ProviderReplayStateError(f"provider replay {label} is invalid")
    return value


def _associated_data(provider: str, kind: str) -> bytes:
    return f"ash-provider-replay:v1:{provider}:{kind}".encode("ascii")


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: Any, *, label: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ProviderReplayStateError(f"provider replay {label} is invalid")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ProviderReplayStateError(
            f"provider replay {label} is invalid"
        ) from exc
