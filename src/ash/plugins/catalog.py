"""Signed plugin catalog parsing and verification."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

CATALOG_VERSION = 1
MAX_CATALOG_BYTES = 256 * 1024
MAX_CATALOG_ENTRIES = 1_000
SIGNATURE_ALGORITHM = "ed25519"
_KEY_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_PLUGIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SOURCE = re.compile(r"^(https|file)://\S+$")
_DIGEST = re.compile(r"^[0-9a-f]{40,64}$")


class PluginCatalogError(ValueError):
    """Raised when a signed catalog is malformed or untrusted."""


def trusted_catalog_keys_path() -> Path:
    configured = os.environ.get("ASH_CATALOG_KEYS")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".ash" / "catalog-keys.json"
    )


def default_catalog_path() -> Path | None:
    configured = os.environ.get("ASH_PLUGIN_CATALOG")
    return Path(configured).expanduser() if configured else None


def catalog_cache_path(url: str) -> Path:
    """Return a stable private cache location for one HTTPS catalog URL."""

    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise PluginCatalogError("catalog URL must use HTTPS")
    identity = f"{parsed.hostname.lower()}{parsed.path or '/'}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", parsed.hostname.lower())[:80]
    return Path.home() / ".ash" / "cache" / "catalogs" / f"{safe_name}-{digest}.json"


def fetch_catalog(
    url: str,
    *,
    timeout_seconds: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    """Fetch a bounded HTTPS signed catalog into its stable cache path."""

    if not 1.0 <= timeout_seconds <= 60.0:
        raise PluginCatalogError("catalog fetch timeout must be 1 to 60 seconds")
    destination = catalog_cache_path(url)
    try:
        if transport is not None:
            with httpx.Client(
                transport=transport,
                timeout=timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = client.get(url, headers={"Accept": "application/json"})
        else:
            response = httpx.get(
                url,
                timeout=timeout_seconds,
                follow_redirects=False,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise PluginCatalogError(f"could not fetch plugin catalog: {exc}") from exc
    if response.status_code != 200:
        raise PluginCatalogError(
            f"plugin catalog endpoint returned HTTP {response.status_code}"
        )
    raw = response.content
    if len(raw) > MAX_CATALOG_BYTES:
        raise PluginCatalogError("plugin catalog exceeds 256 KiB")
    try:
        envelope = _parse_strict_json(raw.decode("utf-8"))
        if not isinstance(envelope, dict) or "keyId" not in envelope:
            raise ValueError("missing catalog key id")
    except (UnicodeError, ValueError, KeyError, TypeError):
        # Do not cache malformed or unsigned payloads.
        raise PluginCatalogError("invalid signed plugin catalog response") from None
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        destination.parent.chmod(0o700)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(raw)
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise PluginCatalogError(f"could not save plugin catalog: {exc}") from exc
    return destination


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    version: str
    source: str
    ref: str
    digest: str


@dataclass(frozen=True)
class SignedCatalog:
    sequence: int
    entries: dict[str, CatalogEntry]


def load_trusted_keys(path: Path) -> dict[str, bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PluginCatalogError(
            f"cannot read trusted catalog keys {path}: {exc}"
        ) from exc
    if len(raw) > 64 * 1024:
        raise PluginCatalogError(f"trusted catalog keys exceed 64 KiB: {path}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PluginCatalogError(f"invalid trusted catalog keys {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise PluginCatalogError(f"unsupported trusted catalog keys: {path}")
    keys_payload = payload.get("keys")
    if not isinstance(keys_payload, list) or not keys_payload:
        raise PluginCatalogError(f"trusted catalog keys are empty: {path}")
    trusted: dict[str, bytes] = {}
    for item in keys_payload:
        key_id, public_key = _decode_trusted_key(item, path)
        if key_id in trusted:
            raise PluginCatalogError(f"duplicate trusted catalog key id {key_id!r}")
        trusted[key_id] = public_key
    return trusted


def generate_catalog_signing_key() -> tuple[str, str, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    return (
        _encode_base64url(private.private_bytes_raw()),
        _encode_base64url(public),
        "ash-catalog-key",
    )


def sign_catalog(catalog: Mapping[str, Any], private_key_b64: str) -> str:
    canonical = _canonical_json(dict(catalog))
    try:
        seed = _decode_base64url(private_key_b64, expected=32)
        private = Ed25519PrivateKey.from_private_bytes(seed)
    except (TypeError, ValueError) as exc:
        raise PluginCatalogError("invalid catalog signing key") from exc
    signature = private.sign(canonical)
    return _encode_base64url(signature)


def parse_and_verify_catalog(
    path: Path,
    *,
    trusted_keys_path: Path,
) -> SignedCatalog:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PluginCatalogError(f"cannot read plugin catalog {path}: {exc}") from exc
    if len(raw) > MAX_CATALOG_BYTES:
        raise PluginCatalogError(f"plugin catalog exceeds 256 KiB: {path}")
    envelope = _parse_strict_json(raw.decode("utf-8"))
    if not isinstance(envelope, dict) or set(envelope) != {
        "catalog",
        "keyId",
        "algorithm",
        "signature",
    }:
        raise PluginCatalogError("invalid signed plugin catalog envelope")
    key_id = envelope["keyId"]
    if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
        raise PluginCatalogError("invalid plugin catalog key id")
    if envelope["algorithm"] != SIGNATURE_ALGORITHM:
        raise PluginCatalogError("unsupported plugin catalog signature algorithm")
    signature = _decode_base64url(envelope["signature"], expected=64)
    trusted_keys = load_trusted_keys(trusted_keys_path)
    public_seed = trusted_keys.get(key_id)
    if public_seed is None:
        raise PluginCatalogError(f"unknown plugin catalog signing key: {key_id}")
    catalog = envelope["catalog"]
    if not isinstance(catalog, dict):
        raise PluginCatalogError("plugin catalog must be an object")
    try:
        Ed25519PublicKey.from_public_bytes(public_seed).verify(
            signature, _canonical_json(catalog)
        )
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise PluginCatalogError("plugin catalog signature is invalid") from exc
    parsed = _validate_catalog(catalog)
    return parsed


def _validate_catalog(catalog: Mapping[str, Any]) -> SignedCatalog:
    if set(catalog) != {"version", "sequence", "entries"}:
        raise PluginCatalogError("invalid plugin catalog fields")
    if catalog["version"] != CATALOG_VERSION:
        raise PluginCatalogError("unsupported plugin catalog version")
    sequence = catalog["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise PluginCatalogError("invalid plugin catalog sequence")
    entries_payload = catalog["entries"]
    if not isinstance(entries_payload, list):
        raise PluginCatalogError("plugin catalog entries must be a list")
    if len(entries_payload) > MAX_CATALOG_ENTRIES:
        raise PluginCatalogError("plugin catalog exceeds entry limit")
    entries: dict[str, CatalogEntry] = {}
    for item in entries_payload:
        entry = _validate_entry(item)
        if entry.name in entries:
            raise PluginCatalogError(f"duplicate plugin catalog entry {entry.name!r}")
        entries[entry.name] = entry
    return SignedCatalog(sequence=sequence, entries=entries)


def _validate_entry(item: Any) -> CatalogEntry:
    if not isinstance(item, dict) or set(item) != {
        "name",
        "version",
        "source",
        "ref",
        "digest",
    }:
        raise PluginCatalogError("invalid plugin catalog entry")
    name = item["name"]
    version = item["version"]
    source = item["source"]
    ref = item["ref"]
    digest = item["digest"]
    if not isinstance(name, str) or not _PLUGIN_NAME.fullmatch(name):
        raise PluginCatalogError("invalid plugin catalog entry name")
    if not isinstance(version, str) or not version or len(version) > 128:
        raise PluginCatalogError("invalid plugin catalog entry version")
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        raise PluginCatalogError("invalid plugin catalog entry source")
    if (
        not isinstance(ref, str)
        or not ref
        or "\x00" in ref
        or "\n" in ref
        or "\r" in ref
        or len(ref) > 255
    ):
        raise PluginCatalogError("invalid plugin catalog entry ref")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise PluginCatalogError("invalid plugin catalog entry digest")
    return CatalogEntry(
        name=name, version=version, source=source, ref=ref, digest=digest
    )


def _decode_trusted_key(item: Any, path: Path) -> tuple[str, bytes]:
    if not isinstance(item, dict) or set(item) != {"keyId", "algorithm", "publicKey"}:
        raise PluginCatalogError(f"invalid trusted catalog key in {path}")
    key_id = item["keyId"]
    algorithm = item["algorithm"]
    encoded = item["publicKey"]
    if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
        raise PluginCatalogError(f"invalid trusted catalog key id in {path}")
    if algorithm != SIGNATURE_ALGORITHM:
        raise PluginCatalogError(f"unsupported trusted catalog key in {path}")
    try:
        return key_id, _decode_base64url(encoded, expected=32)
    except (TypeError, ValueError) as exc:
        raise PluginCatalogError(f"invalid trusted catalog key in {path}") from exc


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PluginCatalogError("catalog cannot be canonicalized") from exc


def _parse_strict_json(raw: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            raw, object_pairs_hook=unique_object, parse_constant=_reject_constant
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PluginCatalogError(f"invalid signed plugin catalog JSON: {exc}") from exc


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _decode_base64url(value: Any, *, expected: int | None = None) -> bytes:
    if not isinstance(value, str) or "=" in value:
        raise ValueError("invalid base64url string")
    if "+" in value or "/" in value or not re.fullmatch(r"[A-Za-z0-9_-]*", value):
        raise ValueError("invalid base64url alphabet")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, UnicodeError) as exc:
        raise ValueError("invalid base64url encoding") from exc
    if expected is not None and len(decoded) != expected:
        raise ValueError("unexpected decoded length")
    return decoded


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
