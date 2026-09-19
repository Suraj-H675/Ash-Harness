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
from ash.safe_io import (
    atomic_write_unlinked_bytes,
    ensure_anchored_directory,
    read_bounded_open_file,
    strict_json_loads,
    validate_unlinked_path,
)

CATALOG_VERSION = 2
LEGACY_CATALOG_VERSION = 1
MAX_CATALOG_BYTES = 256 * 1024
MAX_CATALOG_ENTRIES = 1_000
SIGNATURE_ALGORITHM = "ed25519"
_KEY_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_PUBLISHER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PLUGIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SOURCE = re.compile(r"^(https|file)://\S+$")
_DIGEST = re.compile(r"^[0-9a-f]{40,64}$")


class PluginCatalogError(ValueError):
    """Raised when a signed catalog is malformed or untrusted."""


def _catalog_trusted_root(path: Path) -> Path:
    candidate = Path(os.path.abspath(path.expanduser()))
    home = Path(os.path.abspath(Path.home().expanduser()))
    try:
        candidate.relative_to(home)
    except ValueError:
        root = candidate.parent.parent
    else:
        root = home
    while not root.exists() and root != root.parent:
        root = root.parent
    return root


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
    destination = Path(os.path.abspath(catalog_cache_path(url).expanduser()))
    trusted_root = _catalog_trusted_root(destination)
    try:
        destination = validate_unlinked_path(
            destination,
            trusted_root=trusted_root,
            label="plugin catalog cache",
        )
    except ValueError as exc:
        raise PluginCatalogError(str(exc)) from exc
    try:
        with httpx.Client(
            transport=transport,
            timeout=timeout_seconds,
            follow_redirects=False,
        ) as client:
            with client.stream(
                "GET", url, headers={"Accept": "application/json"}
            ) as response:
                if response.status_code != 200:
                    raise PluginCatalogError(
                        "plugin catalog endpoint returned "
                        f"HTTP {response.status_code}"
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as exc:
                        raise PluginCatalogError(
                            "plugin catalog returned an invalid Content-Length"
                        ) from exc
                    if declared_length < 0:
                        raise PluginCatalogError(
                            "plugin catalog returned an invalid Content-Length"
                        )
                    if declared_length > MAX_CATALOG_BYTES:
                        raise PluginCatalogError("plugin catalog exceeds 256 KiB")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_CATALOG_BYTES:
                        raise PluginCatalogError("plugin catalog exceeds 256 KiB")
                    chunks.append(chunk)
                raw = b"".join(chunks)
    except httpx.HTTPError as exc:
        raise PluginCatalogError(f"could not fetch plugin catalog: {exc}") from exc
    try:
        envelope = _parse_strict_json(raw.decode("utf-8"))
        if not isinstance(envelope, dict) or "keyId" not in envelope:
            raise ValueError("missing catalog key id")
    except (UnicodeError, ValueError, KeyError, TypeError):
        # Do not cache malformed or unsigned payloads.
        raise PluginCatalogError("invalid signed plugin catalog response") from None
    try:
        ensure_anchored_directory(
            destination.parent,
            trusted_root=trusted_root,
            label="plugin catalog cache directory",
            mode=0o700,
        )
    except (OSError, ValueError) as exc:
        raise PluginCatalogError(f"could not prepare plugin catalog cache: {exc}") from exc
    try:
        validate_unlinked_path(
            destination,
            trusted_root=trusted_root,
            label="plugin catalog cache",
        )
    except ValueError as exc:
        raise PluginCatalogError(str(exc)) from exc
    try:
        atomic_write_unlinked_bytes(
            destination,
            raw,
            label="plugin catalog cache",
            mode=0o600,
            trusted_root=trusted_root,
        )
    except (OSError, ValueError) as exc:
        raise PluginCatalogError(f"could not save plugin catalog: {exc}") from exc
    return destination


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    version: str
    source: str
    ref: str
    digest: str
    publisher: str | None = None


@dataclass(frozen=True)
class SignedCatalog:
    sequence: int
    entries: dict[str, CatalogEntry]
    publisher: str | None = None


def load_trusted_keys(path: Path) -> dict[str, bytes]:
    candidate = Path(os.path.abspath(path.expanduser()))
    trusted_root = _catalog_trusted_root(candidate)
    try:
        path = validate_unlinked_path(
            candidate,
            trusted_root=trusted_root,
            label="trusted catalog keys",
        )
    except ValueError as exc:
        raise PluginCatalogError(str(exc)) from exc
    try:
        raw = read_bounded_open_file(
            path,
            64 * 1024,
            label="trusted catalog keys",
            trusted_root=trusted_root,
        )
    except (OSError, ValueError) as exc:
        if "exceeds" in str(exc):
            raise PluginCatalogError(
                f"trusted catalog keys exceed 64 KiB: {path}"
            ) from exc
        raise PluginCatalogError(
            f"cannot read trusted catalog keys {path}: {exc}"
        ) from exc
    try:
        payload = strict_json_loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
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
    candidate = Path(os.path.abspath(path.expanduser()))
    trusted_root = _catalog_trusted_root(candidate)
    try:
        raw = read_bounded_open_file(
            candidate,
            MAX_CATALOG_BYTES,
            label="plugin catalog",
            trusted_root=trusted_root,
        )
    except (OSError, ValueError) as exc:
        if "exceeds" in str(exc):
            raise PluginCatalogError(
                f"plugin catalog exceeds 256 KiB: {path}"
            ) from exc
        raise PluginCatalogError(f"cannot read plugin catalog {path}: {exc}") from exc
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
    version = catalog.get("version")
    publisher: str | None
    if version == LEGACY_CATALOG_VERSION:
        if set(catalog) != {"version", "sequence", "entries"}:
            raise PluginCatalogError("invalid plugin catalog fields")
        publisher = None
    elif version == CATALOG_VERSION:
        if set(catalog) != {"version", "publisher", "sequence", "entries"}:
            raise PluginCatalogError("invalid plugin catalog fields")
        raw_publisher = catalog["publisher"]
        if not isinstance(raw_publisher, str) or not _PUBLISHER.fullmatch(
            raw_publisher
        ):
            raise PluginCatalogError("invalid plugin catalog publisher")
        publisher = raw_publisher
    else:
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
        entry = _validate_entry(item, publisher=publisher)
        if entry.name in entries:
            raise PluginCatalogError(f"duplicate plugin catalog entry {entry.name!r}")
        entries[entry.name] = entry
    return SignedCatalog(sequence=sequence, entries=entries, publisher=publisher)


def _validate_entry(item: Any, *, publisher: str | None = None) -> CatalogEntry:
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
        name=name,
        version=version,
        source=source,
        ref=ref,
        digest=digest,
        publisher=publisher,
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
    try:
        return strict_json_loads(raw)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PluginCatalogError(f"invalid signed plugin catalog JSON: {exc}") from exc


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
