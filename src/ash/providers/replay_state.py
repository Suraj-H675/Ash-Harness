"""Sealed durable state required to replay provider conversations exactly."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Literal, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ash.safety.private_store import PrivateStore, PrivateStoreError


_KEY_FILE = "replay-state-v1.key"
_KEY_MAGIC = b"ash-provider-replay-v1\x00"
_KEY_BYTES = 32
_NONCE_BYTES = 12
MAX_REPLAY_PLAINTEXT_BYTES = 3_000_000
_CREDENTIAL_BINDING_DOMAIN = b"ash-provider-replay-credential-binding-v1"
_CREDENTIAL_BINDING_SALT = b"ash-provider-replay-affinity-hkdf-v1"
_CREDENTIAL_BINDING_NONCE_BYTES = 16
_CREDENTIAL_BINDING_TAG_BYTES = 32


@dataclass(frozen=True, repr=False)
class ProviderReplayCredentialIdentity:
    """In-memory proof material for a credential that actually made a request."""

    kind: Literal["secret", "verified_subject"]
    _value: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.kind not in {"secret", "verified_subject"}
            or not isinstance(self._value, bytes)
            or not self._value
            or len(self._value) > 65_536
        ):
            raise ValueError("provider credential identity is unavailable")

    @classmethod
    def from_secret(
        cls,
        secret: str,
        *,
        scope: tuple[str, ...] = (),
    ) -> "ProviderReplayCredentialIdentity":
        if (
            not isinstance(secret, str)
            or not secret
            or not isinstance(scope, tuple)
            or any(not isinstance(item, str) for item in scope)
        ):
            raise ValueError("provider credential identity is unavailable")
        try:
            value = _frame(
                (
                    secret.encode("utf-8"),
                    *(item.encode("utf-8") for item in scope),
                )
            )
        except UnicodeEncodeError as exc:
            raise ValueError("provider credential identity is unavailable") from exc
        except AttributeError as exc:
            raise ValueError("provider credential identity is unavailable") from exc
        if len(value) > 65_536:
            raise ValueError("provider credential identity is unavailable")
        return cls("secret", value)

    @classmethod
    def from_verified_subject(
        cls,
        issuer: str,
        subject: str,
        *,
        scope: tuple[str, ...] = (),
    ) -> "ProviderReplayCredentialIdentity":
        if (
            not isinstance(issuer, str)
            or not issuer
            or not isinstance(subject, str)
            or not subject
            or not isinstance(scope, tuple)
            or any(not isinstance(item, str) for item in scope)
        ):
            raise ValueError("provider credential identity is unavailable")
        try:
            value = _frame(
                (
                    issuer.encode("utf-8"),
                    subject.encode("utf-8"),
                    *(item.encode("utf-8") for item in scope),
                )
            )
        except UnicodeEncodeError as exc:
            raise ValueError("provider credential identity is unavailable") from exc
        if len(value) > 8192:
            raise ValueError("provider credential identity is unavailable")
        return cls("verified_subject", value)

    def __repr__(self) -> str:
        return f"ProviderReplayCredentialIdentity(kind={self.kind!r}, value=<redacted>)"


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

    def bind_credential_identity(
        self,
        identity: ProviderReplayCredentialIdentity,
        *,
        session_scope_id: str,
        owner: str,
        profile: str | None,
        state: Any,
    ) -> dict[str, Any]:
        """Create a keyed, non-secret binding for one durable opaque state value."""

        nonce = os.urandom(_CREDENTIAL_BINDING_NONCE_BYTES)
        payload = _credential_binding_payload(
            identity,
            session_scope_id=session_scope_id,
            owner=owner,
            profile=profile,
            state=state,
            nonce=nonce,
        )
        key = _derive_credential_binding_key(self._load_or_create_key())
        tag = hmac.new(key, payload, hashlib.sha256).digest()
        return {
            "version": 1,
            "kind": identity.kind,
            "nonce": _encode(nonce),
            "tag": _encode(tag),
        }

    def verify_credential_identity(
        self,
        binding: Mapping[str, Any],
        identity: ProviderReplayCredentialIdentity,
        *,
        session_scope_id: str,
        owner: str,
        profile: str | None,
        state: Any,
    ) -> bool:
        """Verify a stored binding without creating or replacing the private key."""

        if not isinstance(binding, Mapping) or set(binding) != {
            "version",
            "kind",
            "nonce",
            "tag",
        }:
            return False
        if (
            type(binding.get("version")) is not int
            or binding.get("version") != 1
            or binding.get("kind") != identity.kind
        ):
            return False
        try:
            nonce = _decode(binding.get("nonce"), label="credential nonce")
            tag = _decode(binding.get("tag"), label="credential tag")
            if (
                len(nonce) != _CREDENTIAL_BINDING_NONCE_BYTES
                or len(tag) != _CREDENTIAL_BINDING_TAG_BYTES
            ):
                return False
            payload = _credential_binding_payload(
                identity,
                session_scope_id=session_scope_id,
                owner=owner,
                profile=profile,
                state=state,
                nonce=nonce,
            )
            key = _derive_credential_binding_key(self._load_existing_key())
        except (ProviderReplayStateError, TypeError, ValueError, UnicodeError):
            return False
        expected = hmac.new(key, payload, hashlib.sha256).digest()
        return hmac.compare_digest(tag, expected)

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


def _derive_credential_binding_key(key: bytes) -> bytes:
    return HKDF(
        algorithm=SHA256(),
        length=32,
        salt=_CREDENTIAL_BINDING_SALT,
        info=_CREDENTIAL_BINDING_DOMAIN,
    ).derive(key)


def _credential_binding_payload(
    identity: ProviderReplayCredentialIdentity,
    *,
    session_scope_id: str,
    owner: str,
    profile: str | None,
    state: Any,
    nonce: bytes,
) -> bytes:
    if not isinstance(identity, ProviderReplayCredentialIdentity):
        raise ProviderReplayStateError("provider replay credential identity is invalid")
    try:
        serialized_state = json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        fields = (
            _CREDENTIAL_BINDING_DOMAIN,
            identity.kind.encode("ascii"),
            session_scope_id.encode("utf-8"),
            owner.encode("utf-8"),
            b"\x00" if profile is None else b"\x01" + profile.encode("utf-8"),
            serialized_state,
            nonce,
            identity._value,
        )
    except (AttributeError, TypeError, ValueError, UnicodeError) as exc:
        raise ProviderReplayStateError(
            "provider replay credential binding is invalid"
        ) from exc
    if (
        not session_scope_id
        or not owner
        or len(serialized_state) > MAX_REPLAY_PLAINTEXT_BYTES
    ):
        raise ProviderReplayStateError(
            "provider replay credential binding is invalid"
        )
    return _frame(fields)


def _frame(fields: tuple[bytes, ...]) -> bytes:
    return b"".join(len(value).to_bytes(8, "big") + value for value in fields)


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
