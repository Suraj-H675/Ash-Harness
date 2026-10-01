"""OpenAI Sign in with ChatGPT credentials for the optional plan-backed route."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import math
import os
import re
import secrets
import time
import uuid
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from ash.profiles import active_profile_name, profile_directory
from ash.safe_io import strict_json_loads
from ash.safety.private_store import (
    PrivateStore,
    PrivateStoreError,
    PrivateStoreUnavailable,
)


CHATGPT_ISSUER = "https://auth.openai.com"
CHATGPT_DISCOVERY_URL = f"{CHATGPT_ISSUER}/.well-known/openid-configuration"
CHATGPT_RESOURCE = "https://api.openai.com/v1"
CHATGPT_AUTHORIZE_ENDPOINT = f"{CHATGPT_ISSUER}/api/accounts/authorize"
CHATGPT_TOKEN_ENDPOINT = f"{CHATGPT_ISSUER}/api/accounts/oauth/token"
CHATGPT_JWKS_URI = f"{CHATGPT_ISSUER}/.well-known/jwks.json"
CHATGPT_DYNAMIC_CLIENT_ID = "dynamic_agent_client"
CHATGPT_CALLBACK_PATH = "/auth/callback"
CHATGPT_REQUIRED_PLAN_SCOPE = "chatgpt.tokens.use.direct"
CHATGPT_REQUESTED_SCOPES = (
    "openid",
    "profile",
    "email",
    "offline_access",
    "resource.invoke",
    CHATGPT_REQUIRED_PLAN_SCOPE,
)

MAX_CHATGPT_AUTH_RECORD_BYTES = 2 * 1024 * 1024
MAX_CHATGPT_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CHATGPT_ACCOUNTS = 32
MAX_CHATGPT_TOKEN_BYTES = 256 * 1024
MAX_CHATGPT_IDENTITY_BYTES = 4096
MAX_CHATGPT_SCOPES = 64
CHATGPT_REFRESH_MARGIN_SECONDS = 120
CHATGPT_REFRESH_LEASE_SECONDS = 120
CHATGPT_REFRESH_WAIT_SECONDS = 15
CHATGPT_REFRESH_RETRY_ATTEMPTS = 3
CHATGPT_REFRESH_RETRY_BASE_SECONDS = 0.25
CHATGPT_LOGIN_TIMEOUT_SECONDS = 300
_CLIENT_ID = re.compile(r"^oaiapp_[A-Za-z0-9._~-]{1,240}$")
_SCOPE = re.compile(r"^[\x21\x23-\x5B\x5D-\x7E]+$")
_SAFE_HTTP_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")


class ChatGPTAuthError(RuntimeError):
    """A bounded sign-in, token, validation, or persistence failure."""


class ChatGPTLoginRequired(ChatGPTAuthError):
    """The selected ChatGPT registration needs interactive sign-in."""


class ChatGPTPlanUnavailable(ChatGPTAuthError):
    """The selected registration does not grant ChatGPT-plan inference."""


class ChatGPTHTTPError(ChatGPTAuthError):
    """A bounded OAuth/API HTTP failure with a machine-readable code."""

    def __init__(self, message: str, *, status_code: int, code: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class ChatGPTTransportError(ChatGPTAuthError):
    """A transient transport failure before an OAuth response was confirmed."""


@dataclass(frozen=True)
class ChatGPTAccount:
    client_id: str
    subject: str
    email: str
    issuer: str
    ext_agent_host_id: str
    id_token: str
    access_token: str
    refresh_token: str
    token_type: str
    scopes: tuple[str, ...]
    expires_at: float
    earliest_refresh_at: float
    saved_at: float

    @property
    def plan_enabled(self) -> bool:
        return CHATGPT_REQUIRED_PLAN_SCOPE in self.scopes

    @property
    def signed_in(self) -> bool:
        return bool(self.access_token or self.refresh_token or self.id_token)

    def access_usable(self, *, now: float | None = None) -> bool:
        if not self.access_token or not self.plan_enabled:
            return False
        current = time.time() if now is None else now
        return self.expires_at > current + CHATGPT_REFRESH_MARGIN_SECONDS


@dataclass(frozen=True)
class ChatGPTDiscovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    revocation_endpoint: str


def _bounded_text(value: Any, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ChatGPTAuthError(f"{label} must be text")
    if not value:
        raise ChatGPTAuthError(f"{label} cannot be empty")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ChatGPTAuthError(f"{label} must be valid UTF-8") from exc
    if size > maximum:
        raise ChatGPTAuthError(f"{label} exceeds {maximum} UTF-8 bytes")
    if any(ord(character) < 0x20 for character in value):
        raise ChatGPTAuthError(f"{label} contains control characters")
    return value


def _optional_text(value: Any, *, label: str, maximum: int) -> str:
    if value in {None, ""}:
        return ""
    return _bounded_text(value, label=label, maximum=maximum)


def _token_text(value: Any, *, label: str) -> str:
    if value in {None, ""}:
        return ""
    return _bounded_text(value, label=label, maximum=MAX_CHATGPT_TOKEN_BYTES)


def _finite_timestamp(value: Any, *, label: str, allow_zero: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChatGPTAuthError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (not allow_zero and result <= 0):
        raise ChatGPTAuthError(f"{label} is invalid")
    return result


def _validate_client_id(value: Any) -> str:
    client_id = _bounded_text(
        value,
        label="ChatGPT issued client ID",
        maximum=256,
    )
    if _CLIENT_ID.fullmatch(client_id) is None:
        raise ChatGPTAuthError("ChatGPT issued client ID has an invalid format")
    return client_id


def _validate_host_id(value: Any) -> str:
    host_id = _bounded_text(
        value,
        label="ChatGPT host ID",
        maximum=1024,
    )
    if host_id.startswith("urn:uuid:"):
        try:
            parsed = uuid.UUID(host_id.removeprefix("urn:uuid:"))
        except ValueError as exc:
            raise ChatGPTAuthError("ChatGPT host ID is not a valid UUID URI") from exc
        if str(parsed) != host_id.removeprefix("urn:uuid:").casefold():
            raise ChatGPTAuthError("ChatGPT host ID UUID is not canonical")
        return host_id
    if host_id.startswith("urn:ietf:params:oauth:jwk-thumbprint:") or host_id.startswith(
        "did:key:"
    ):
        return host_id
    raise ChatGPTAuthError("ChatGPT host ID uses an unsupported format")


def _normalize_scopes(value: Any) -> tuple[str, ...]:
    raw: list[str]
    if isinstance(value, str):
        raw = value.split()
    elif isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        raise ChatGPTAuthError("ChatGPT OAuth scopes are invalid")
    if len(raw) > MAX_CHATGPT_SCOPES:
        raise ChatGPTAuthError("ChatGPT OAuth scope count exceeded the limit")
    scopes: list[str] = []
    for item in raw:
        if not item or len(item) > 256 or _SCOPE.fullmatch(item) is None:
            raise ChatGPTAuthError("ChatGPT OAuth scope value is invalid")
        if item not in scopes:
            scopes.append(item)
    return tuple(sorted(scopes))


def _account_payload(account: ChatGPTAccount) -> dict[str, Any]:
    return {
        "client_id": account.client_id,
        "subject": account.subject,
        "email": account.email,
        "issuer": account.issuer,
        "ext_agent_host_id": account.ext_agent_host_id,
        "id_token": account.id_token,
        "access_token": account.access_token,
        "refresh_token": account.refresh_token,
        "token_type": account.token_type,
        "scopes": list(account.scopes),
        "expires_at": account.expires_at,
        "earliest_refresh_at": account.earliest_refresh_at,
        "saved_at": account.saved_at,
    }


def _decode_account(raw: Any) -> ChatGPTAccount:
    if not isinstance(raw, dict):
        raise ChatGPTAuthError("stored ChatGPT account record must be an object")
    issuer = _bounded_text(
        raw.get("issuer"),
        label="ChatGPT issuer",
        maximum=1024,
    )
    if issuer != CHATGPT_ISSUER:
        raise ChatGPTAuthError("stored ChatGPT issuer is not supported")
    token_type = _optional_text(
        raw.get("token_type", ""),
        label="ChatGPT token type",
        maximum=64,
    )
    if token_type and token_type.casefold() != "bearer":
        raise ChatGPTAuthError("stored ChatGPT token type is not Bearer")
    return ChatGPTAccount(
        client_id=_validate_client_id(raw.get("client_id")),
        subject=_bounded_text(
            raw.get("subject"),
            label="ChatGPT subject",
            maximum=MAX_CHATGPT_IDENTITY_BYTES,
        ),
        email=_optional_text(
            raw.get("email", ""),
            label="ChatGPT email",
            maximum=MAX_CHATGPT_IDENTITY_BYTES,
        ),
        issuer=issuer,
        ext_agent_host_id=_validate_host_id(raw.get("ext_agent_host_id")),
        id_token=_token_text(raw.get("id_token", ""), label="ChatGPT ID token"),
        access_token=_token_text(
            raw.get("access_token", ""),
            label="ChatGPT access token",
        ),
        refresh_token=_token_text(
            raw.get("refresh_token", ""),
            label="ChatGPT refresh token",
        ),
        token_type=token_type or "Bearer",
        scopes=_normalize_scopes(raw.get("scopes", [])),
        expires_at=_finite_timestamp(
            raw.get("expires_at", 0.0),
            label="ChatGPT access-token expiry",
        ),
        earliest_refresh_at=_finite_timestamp(
            raw.get("earliest_refresh_at", 0.0),
            label="ChatGPT earliest refresh time",
        ),
        saved_at=_finite_timestamp(
            raw.get("saved_at", 0.0),
            label="ChatGPT credential saved time",
        ),
    )

def _private_store(
    directory: Path,
    *,
    trusted_root: Path,
) -> PrivateStore:
    try:
        return PrivateStore(directory, trusted_root=trusted_root)
    except PrivateStoreError as exc:
        raise ChatGPTAuthError(str(exc)) from exc


class ChatGPTHostStore:
    """Persist the stable per-host identifier outside Ash profile state."""

    def __init__(self, directory: Path | None = None) -> None:
        home = Path.home()
        requested = directory or (home / ".ash" / "openai-chatgpt-host")
        trusted_root = home if directory is None else Path(
            os.path.abspath(Path(requested).expanduser())
        ).parent
        self._store = _private_store(
            Path(requested).expanduser(),
            trusted_root=trusted_root,
        )

    def get_or_create(self) -> str:
        selected = ""

        def update(current: bytes | None) -> bytes:
            nonlocal selected
            if current is not None:
                try:
                    payload = strict_json_loads(current)
                except (json.JSONDecodeError, ValueError, UnicodeError) as exc:
                    raise ChatGPTAuthError("invalid ChatGPT host record") from exc
                if not isinstance(payload, dict) or payload.get("version") != 1:
                    raise ChatGPTAuthError("unsupported ChatGPT host record")
                selected = _validate_host_id(payload.get("ext_agent_host_id"))
            else:
                selected = f"urn:uuid:{uuid.uuid4()}"
            return (
                json.dumps(
                    {
                        "version": 1,
                        "ext_agent_host_id": selected,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")

        try:
            self._store.update(
                "host.json",
                update,
                max_bytes=16 * 1024,
            )
        except (PrivateStoreUnavailable, PrivateStoreError) as exc:
            raise ChatGPTAuthError(
                "secure ChatGPT host identity persistence is unavailable"
            ) from exc
        return selected


class ChatGPTCredentialStore:
    """Profile-scoped private multi-account registration and token storage."""

    def __init__(self, directory: Path | None = None) -> None:
        home = Path.home()
        if directory is None:
            profile = active_profile_name(ash_dir=home / ".ash")
            state_directory = profile_directory(profile, ash_dir=home / ".ash")
            requested = state_directory / "openai-chatgpt"
            trusted_root = home
        else:
            requested = Path(directory).expanduser()
            trusted_root = Path(os.path.abspath(requested)).parent
        self._store = _private_store(requested, trusted_root=trusted_root)

    @staticmethod
    def _empty_record() -> dict[str, Any]:
        return {
            "version": 1,
            "active_client_id": "",
            "accounts": {},
            "refresh_leases": {},
        }

    def _decode_record(self, raw: bytes | None) -> dict[str, Any]:
        if raw is None:
            return self._empty_record()
        try:
            payload = strict_json_loads(raw)
        except (json.JSONDecodeError, ValueError, UnicodeError) as exc:
            raise ChatGPTAuthError("invalid ChatGPT credential record") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ChatGPTAuthError("unsupported ChatGPT credential record")
        accounts = payload.get("accounts")
        leases = payload.get("refresh_leases", {})
        if not isinstance(accounts, dict) or len(accounts) > MAX_CHATGPT_ACCOUNTS:
            raise ChatGPTAuthError("invalid ChatGPT account collection")
        if not isinstance(leases, dict) or len(leases) > MAX_CHATGPT_ACCOUNTS:
            raise ChatGPTAuthError("invalid ChatGPT refresh lease collection")
        decoded_accounts: dict[str, dict[str, Any]] = {}
        for key, value in accounts.items():
            account = _decode_account(value)
            if key != account.client_id:
                raise ChatGPTAuthError("stored ChatGPT account key mismatch")
            decoded_accounts[key] = _account_payload(account)
        active = payload.get("active_client_id", "")
        if active:
            active = _validate_client_id(active)
            if active not in decoded_accounts:
                raise ChatGPTAuthError("active ChatGPT account is missing")
        return {
            "version": 1,
            "active_client_id": active,
            "accounts": decoded_accounts,
            "refresh_leases": leases,
        }

    @staticmethod
    def _encode_record(record: dict[str, Any]) -> bytes:
        payload = (
            json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")
        if len(payload) > MAX_CHATGPT_AUTH_RECORD_BYTES:
            raise ChatGPTAuthError("ChatGPT credential record exceeded 2 MiB")
        return payload

    def _read_record(self) -> dict[str, Any]:
        try:
            raw = self._store.read(
                "accounts.json",
                max_bytes=MAX_CHATGPT_AUTH_RECORD_BYTES,
            )
        except (PrivateStoreUnavailable, PrivateStoreError) as exc:
            raise ChatGPTAuthError(
                "secure ChatGPT credential persistence is unavailable"
            ) from exc
        return self._decode_record(raw)

    def list_accounts(self) -> tuple[ChatGPTAccount, ...]:
        record = self._read_record()
        return tuple(
            _decode_account(payload)
            for _key, payload in sorted(record["accounts"].items())
        )

    def active(self) -> ChatGPTAccount | None:
        record = self._read_record()
        active = str(record["active_client_id"] or "")
        if not active:
            return None
        return _decode_account(record["accounts"][active])

    def get(self, client_id: str) -> ChatGPTAccount | None:
        client_id = _validate_client_id(client_id)
        record = self._read_record()
        payload = record["accounts"].get(client_id)
        return None if payload is None else _decode_account(payload)

    def save(self, account: ChatGPTAccount, *, make_active: bool = True) -> None:
        normalized = _decode_account(_account_payload(account))

        def update(current: bytes | None) -> bytes:
            record = self._decode_record(current)
            accounts = dict(record["accounts"])
            if normalized.client_id not in accounts and len(accounts) >= MAX_CHATGPT_ACCOUNTS:
                raise ChatGPTAuthError("ChatGPT account limit reached")
            accounts[normalized.client_id] = _account_payload(normalized)
            record["accounts"] = accounts
            if make_active:
                record["active_client_id"] = normalized.client_id
            return self._encode_record(record)

        try:
            self._store.update(
                "accounts.json",
                update,
                max_bytes=MAX_CHATGPT_AUTH_RECORD_BYTES,
            )
        except (PrivateStoreUnavailable, PrivateStoreError) as exc:
            raise ChatGPTAuthError(
                "secure ChatGPT credential persistence is unavailable"
            ) from exc

    def set_active(self, client_id: str) -> ChatGPTAccount:
        client_id = _validate_client_id(client_id)
        selected: ChatGPTAccount | None = None

        def update(current: bytes | None) -> bytes:
            nonlocal selected
            record = self._decode_record(current)
            payload = record["accounts"].get(client_id)
            if payload is None:
                raise KeyError(f"unknown ChatGPT registration: {client_id}")
            selected = _decode_account(payload)
            record["active_client_id"] = client_id
            return self._encode_record(record)

        self._store.update(
            "accounts.json",
            update,
            max_bytes=MAX_CHATGPT_AUTH_RECORD_BYTES,
        )
        assert selected is not None
        return selected

    def clear_tokens(self, client_id: str) -> ChatGPTAccount:
        client_id = _validate_client_id(client_id)
        cleared: ChatGPTAccount | None = None

        def update(current: bytes | None) -> bytes:
            nonlocal cleared
            record = self._decode_record(current)
            payload = record["accounts"].get(client_id)
            if payload is None:
                raise KeyError(f"unknown ChatGPT registration: {client_id}")
            account = _decode_account(payload)
            cleared = replace(
                account,
                id_token="",
                access_token="",
                refresh_token="",
                scopes=(),
                expires_at=0.0,
                earliest_refresh_at=0.0,
                saved_at=time.time(),
            )
            record["accounts"][client_id] = _account_payload(cleared)
            record["refresh_leases"].pop(client_id, None)
            return self._encode_record(record)

        self._store.update(
            "accounts.json",
            update,
            max_bytes=MAX_CHATGPT_AUTH_RECORD_BYTES,
        )
        assert cleared is not None
        return cleared

    def credential_state(self) -> str:
        try:
            account = self.active()
        except ChatGPTAuthError:
            return "invalid"
        if account is None:
            return "missing"
        if not account.signed_in:
            return "signed_out"
        if not account.plan_enabled:
            return "plan_disabled"
        if account.access_usable():
            return "usable"
        if account.refresh_token:
            return "refreshable"
        return "expired"

    def claim_refresh(self, client_id: str, lease_id: str, *, now: float) -> bool:
        client_id = _validate_client_id(client_id)
        lease_id = _bounded_text(
            lease_id,
            label="ChatGPT refresh lease",
            maximum=256,
        )
        claimed = False

        def update(current: bytes | None) -> bytes:
            nonlocal claimed
            record = self._decode_record(current)
            if client_id not in record["accounts"]:
                raise KeyError(f"unknown ChatGPT registration: {client_id}")
            leases = dict(record["refresh_leases"])
            existing = leases.get(client_id)
            if isinstance(existing, dict):
                existing_id = str(existing.get("lease_id", ""))
                try:
                    started_at = float(existing.get("started_at", 0.0))
                except (TypeError, ValueError):
                    started_at = 0.0
                if (
                    existing_id
                    and existing_id != lease_id
                    and math.isfinite(started_at)
                    and started_at + CHATGPT_REFRESH_LEASE_SECONDS > now
                ):
                    claimed = False
                    return self._encode_record(record)
            leases[client_id] = {"lease_id": lease_id, "started_at": now}
            record["refresh_leases"] = leases
            claimed = True
            return self._encode_record(record)

        self._store.update(
            "accounts.json",
            update,
            max_bytes=MAX_CHATGPT_AUTH_RECORD_BYTES,
        )
        return claimed

    def finish_refresh(
        self,
        *,
        client_id: str,
        lease_id: str,
        expected_refresh_token: str,
        account: ChatGPTAccount,
    ) -> bool:
        updated = False

        def update(current: bytes | None) -> bytes:
            nonlocal updated
            record = self._decode_record(current)
            lease = record["refresh_leases"].get(client_id)
            current_payload = record["accounts"].get(client_id)
            if not isinstance(lease, dict) or current_payload is None:
                return self._encode_record(record)
            current_account = _decode_account(current_payload)
            if (
                lease.get("lease_id") != lease_id
                or not secrets.compare_digest(
                    current_account.refresh_token,
                    expected_refresh_token,
                )
            ):
                return self._encode_record(record)
            record["accounts"][client_id] = _account_payload(account)
            record["refresh_leases"].pop(client_id, None)
            updated = True
            return self._encode_record(record)

        self._store.update(
            "accounts.json",
            update,
            max_bytes=MAX_CHATGPT_AUTH_RECORD_BYTES,
        )
        return updated

    def release_refresh(self, client_id: str, lease_id: str) -> None:
        def update(current: bytes | None) -> bytes:
            record = self._decode_record(current)
            lease = record["refresh_leases"].get(client_id)
            if isinstance(lease, dict) and lease.get("lease_id") == lease_id:
                record["refresh_leases"].pop(client_id, None)
            return self._encode_record(record)

        self._store.update(
            "accounts.json",
            update,
            max_bytes=MAX_CHATGPT_AUTH_RECORD_BYTES,
        )


def _validate_endpoint(value: Any, *, label: str) -> str:
    endpoint = _bounded_text(value, label=label, maximum=4096)
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise ChatGPTAuthError(f"{label} is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "auth.openai.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (port is not None and port != 443)
        or not _SAFE_HTTP_PATH.fullmatch(parsed.path)
    ):
        raise ChatGPTAuthError(f"{label} is outside the expected OpenAI origin")
    return endpoint.rstrip("/")


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    label: str,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        response = await client.request(
            method,
            url,
            data=data,
            headers=headers,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise ChatGPTTransportError(f"{label} request failed") from exc
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_CHATGPT_HTTP_RESPONSE_BYTES:
                raise ChatGPTAuthError(f"{label} response exceeded 2 MiB")
        except ValueError:
            pass
    if len(response.content) > MAX_CHATGPT_HTTP_RESPONSE_BYTES:
        raise ChatGPTAuthError(f"{label} response exceeded 2 MiB")
    try:
        payload = strict_json_loads(response.content)
    except (json.JSONDecodeError, ValueError, UnicodeError) as exc:
        if response.status_code >= 500:
            raise ChatGPTHTTPError(
                f"{label} failed (server_error)",
                status_code=response.status_code,
                code="server_error",
            ) from exc
        raise ChatGPTAuthError(f"{label} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ChatGPTAuthError(f"{label} returned an invalid payload")
    if response.status_code >= 400:
        error_value = payload.get("error", payload.get("code", "request_failed"))
        code = str(error_value)
        if isinstance(error_value, dict):
            code = str(
                error_value.get("code")
                or error_value.get("error")
                or error_value.get("type")
                or "request_failed"
            )
        raise ChatGPTHTTPError(
            f"{label} failed ({code})",
            status_code=response.status_code,
            code=code,
        )
    return payload


async def discover_chatgpt_oauth(client: httpx.AsyncClient) -> ChatGPTDiscovery:
    payload = await _request_json(
        client,
        "GET",
        CHATGPT_DISCOVERY_URL,
        label="OpenAI OIDC discovery",
    )
    issuer = _bounded_text(payload.get("issuer"), label="OpenAI issuer", maximum=1024)
    if issuer != CHATGPT_ISSUER:
        raise ChatGPTAuthError("OpenAI OIDC discovery returned an unexpected issuer")
    authorization_endpoint = _validate_endpoint(
        payload.get("authorization_endpoint"),
        label="OpenAI authorization endpoint",
    )
    token_endpoint = _validate_endpoint(
        payload.get("token_endpoint"),
        label="OpenAI token endpoint",
    )
    jwks_uri = _validate_endpoint(payload.get("jwks_uri"), label="OpenAI JWKS URI")
    revocation_endpoint = _validate_endpoint(
        payload.get("revocation_endpoint"),
        label="OpenAI revocation endpoint",
    )
    if authorization_endpoint != CHATGPT_AUTHORIZE_ENDPOINT:
        raise ChatGPTAuthError("OpenAI authorization endpoint changed unexpectedly")
    if token_endpoint != CHATGPT_TOKEN_ENDPOINT:
        raise ChatGPTAuthError("OpenAI token endpoint changed unexpectedly")
    if jwks_uri != CHATGPT_JWKS_URI:
        raise ChatGPTAuthError("OpenAI JWKS endpoint changed unexpectedly")
    return ChatGPTDiscovery(
        issuer=issuer,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        jwks_uri=jwks_uri,
        revocation_endpoint=revocation_endpoint,
    )


def _b64url_decode(value: str, *, label: str, maximum: int = 512 * 1024) -> bytes:
    if not isinstance(value, str) or len(value) > maximum * 2:
        raise ChatGPTAuthError(f"{label} is invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ChatGPTAuthError(f"{label} is invalid") from exc
    if len(decoded) > maximum:
        raise ChatGPTAuthError(f"{label} exceeded its size limit")
    return decoded


def _jwt_parts(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    _token_text(token, label="ChatGPT ID token")
    parts = token.split(".")
    if len(parts) != 3:
        raise ChatGPTAuthError("ChatGPT ID token is not a JWT")
    try:
        header = strict_json_loads(_b64url_decode(parts[0], label="JWT header"))
        payload = strict_json_loads(_b64url_decode(parts[1], label="JWT payload"))
    except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
        raise ChatGPTAuthError("ChatGPT ID token contains invalid JSON") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise ChatGPTAuthError("ChatGPT ID token payload is invalid")
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    signature = _b64url_decode(parts[2], label="JWT signature", maximum=16 * 1024)
    return header, payload, signing_input, signature


def _jwk_int(value: Any, *, label: str) -> int:
    text = _bounded_text(value, label=label, maximum=8192)
    return int.from_bytes(_b64url_decode(text, label=label, maximum=4096), "big")


def _verify_jwt_signature(
    *,
    header: dict[str, Any],
    signing_input: bytes,
    signature: bytes,
    jwks: dict[str, Any],
) -> None:
    algorithm = str(header.get("alg", ""))
    kid = str(header.get("kid", ""))
    if not kid or algorithm not in {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}:
        raise ChatGPTAuthError("ChatGPT ID token uses an unsupported signature")
    keys = jwks.get("keys")
    if not isinstance(keys, list) or not 1 <= len(keys) <= 64:
        raise ChatGPTAuthError("OpenAI JWKS payload is invalid")
    candidates = [
        key
        for key in keys
        if isinstance(key, dict)
        and key.get("kid") == kid
        and (not key.get("alg") or key.get("alg") == algorithm)
    ]
    if len(candidates) != 1:
        raise ChatGPTAuthError("OpenAI JWKS does not contain the ID-token key")
    key = candidates[0]
    hash_algorithm: hashes.HashAlgorithm
    if algorithm.endswith("256"):
        hash_algorithm = hashes.SHA256()
    elif algorithm.endswith("384"):
        hash_algorithm = hashes.SHA384()
    else:
        hash_algorithm = hashes.SHA512()
    try:
        if algorithm.startswith("RS") and key.get("kty") == "RSA":
            rsa_public_key = rsa.RSAPublicNumbers(
                _jwk_int(key.get("e"), label="JWKS exponent"),
                _jwk_int(key.get("n"), label="JWKS modulus"),
            ).public_key()
            rsa_public_key.verify(
                signature,
                signing_input,
                padding.PKCS1v15(),
                hash_algorithm,
            )
            return
        if algorithm.startswith("ES") and key.get("kty") == "EC":
            curve_name = str(key.get("crv", ""))
            curve: ec.EllipticCurve
            if curve_name == "P-256" and algorithm == "ES256":
                curve = ec.SECP256R1()
            elif curve_name == "P-384" and algorithm == "ES384":
                curve = ec.SECP384R1()
            elif curve_name == "P-521" and algorithm == "ES512":
                curve = ec.SECP521R1()
            else:
                raise ChatGPTAuthError("OpenAI JWKS EC key is incompatible")
            ec_public_key = ec.EllipticCurvePublicNumbers(
                _jwk_int(key.get("x"), label="JWKS EC x"),
                _jwk_int(key.get("y"), label="JWKS EC y"),
                curve,
            ).public_key()
            component_bytes = (curve.key_size + 7) // 8
            if len(signature) != component_bytes * 2:
                raise ChatGPTAuthError("ChatGPT ID-token EC signature is invalid")
            der_signature = encode_dss_signature(
                int.from_bytes(signature[:component_bytes], "big"),
                int.from_bytes(signature[component_bytes:], "big"),
            )
            ec_public_key.verify(
                der_signature,
                signing_input,
                ec.ECDSA(hash_algorithm),
            )
            return
    except InvalidSignature as exc:
        raise ChatGPTAuthError("ChatGPT ID-token signature verification failed") from exc
    except ValueError as exc:
        raise ChatGPTAuthError("OpenAI JWKS key is invalid") from exc
    raise ChatGPTAuthError("OpenAI JWKS key type does not match the ID token")


async def validate_chatgpt_id_token(
    token: str,
    *,
    client_id: str,
    nonce: str,
    discovery: ChatGPTDiscovery,
    http_client: httpx.AsyncClient,
    now: float | None = None,
) -> dict[str, Any]:
    header, payload, signing_input, signature = _jwt_parts(token)
    jwks = await _request_json(
        http_client,
        "GET",
        discovery.jwks_uri,
        label="OpenAI JWKS",
    )
    _verify_jwt_signature(
        header=header,
        signing_input=signing_input,
        signature=signature,
        jwks=jwks,
    )
    current = time.time() if now is None else now
    if payload.get("iss") != discovery.issuer:
        raise ChatGPTAuthError("ChatGPT ID-token issuer mismatch")
    audience = payload.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience
    if (
        not isinstance(audiences, list)
        or not audiences
        or any(not isinstance(item, str) for item in audiences)
        or client_id not in audiences
    ):
        raise ChatGPTAuthError("ChatGPT ID-token audience mismatch")
    authorized_party = payload.get("azp")
    if authorized_party is not None and authorized_party != client_id:
        raise ChatGPTAuthError("ChatGPT ID-token authorized-party mismatch")
    if len(audiences) > 1 and authorized_party != client_id:
        raise ChatGPTAuthError(
            "ChatGPT ID-token authorized party is required for multiple audiences"
        )
    if not secrets.compare_digest(str(payload.get("nonce", "")), nonce):
        raise ChatGPTAuthError("ChatGPT ID-token nonce mismatch")
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or float(exp) <= current - 60:
        raise ChatGPTAuthError("ChatGPT ID token is expired or has no valid expiry")
    nbf = payload.get("nbf")
    if nbf is not None and (
        isinstance(nbf, bool)
        or not isinstance(nbf, (int, float))
        or float(nbf) > current + 60
    ):
        raise ChatGPTAuthError("ChatGPT ID token is not yet valid")
    iat = payload.get("iat")
    if (
        isinstance(iat, bool)
        or not isinstance(iat, (int, float))
        or float(iat) > current + 60
    ):
        raise ChatGPTAuthError("ChatGPT ID token has an invalid issued-at time")
    _bounded_text(
        payload.get("sub"),
        label="ChatGPT subject",
        maximum=MAX_CHATGPT_IDENTITY_BYTES,
    )
    email = payload.get("email")
    if email is not None:
        _optional_text(
            email,
            label="ChatGPT email",
            maximum=MAX_CHATGPT_IDENTITY_BYTES,
        )
    return payload


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    return verifier, challenge


def _build_authorization_url(
    *,
    discovery: ChatGPTDiscovery,
    client_id: str,
    redirect_uri: str,
    host_id: str,
    state: str,
    nonce: str,
    challenge: str,
    account: ChatGPTAccount | None,
    request_consent: bool = False,
) -> str:
    params = {
        "client_id": client_id,
        "ext_agent_host_id": host_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(CHATGPT_REQUESTED_SCOPES),
        "resource": CHATGPT_RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    if client_id == CHATGPT_DYNAMIC_CLIENT_ID:
        params["agent_name_hint"] = "Ash"
    elif account is not None:
        if account.id_token:
            params["id_token_hint"] = account.id_token
        if account.email:
            params["login_hint"] = account.email
    if request_consent:
        params["prompt"] = "consent"
    return f"{discovery.authorization_endpoint}?{urlencode(params)}"


async def _receive_loopback_callback(
    *,
    expected_state: str,
    browser_opener: Callable[[str], bool],
    build_url: Callable[[str], str],
    timeout_seconds: float,
) -> tuple[dict[str, str], str]:
    loop = asyncio.get_running_loop()
    result: asyncio.Future[dict[str, str]] = loop.create_future()

    async def handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        status = "400 Bad Request"
        body = "Sign-in failed. You can close this tab."
        try:
            request = await asyncio.wait_for(reader.read(16 * 1024), timeout=5)
            if len(request) >= 16 * 1024:
                raise ChatGPTAuthError("OAuth callback request was too large")
            first_line = request.split(b"\r\n", 1)[0].decode("ascii", errors="strict")
            parts = first_line.split()
            if len(parts) != 3 or parts[0] != "GET" or not parts[2].startswith("HTTP/1."):
                raise ChatGPTAuthError("OAuth callback request was invalid")
            target = urlsplit(parts[1])
            if target.path != CHATGPT_CALLBACK_PATH or target.fragment:
                raise ChatGPTAuthError("OAuth callback path did not match")
            raw = parse_qs(target.query, keep_blank_values=True, strict_parsing=False)
            for sensitive in ("state", "code", "error", "client_id"):
                if sensitive in raw and len(raw[sensitive]) != 1:
                    raise ChatGPTAuthError(
                        f"OAuth callback contains duplicate {sensitive!r}"
                    )
            params = {
                key: values[0]
                for key, values in raw.items()
                if values and isinstance(values[0], str)
            }
            returned_state = params.get("state", "")
            if not returned_state or not secrets.compare_digest(
                returned_state,
                expected_state,
            ):
                raise ChatGPTAuthError("OAuth callback state mismatch")
            if "error" in params:
                raise ChatGPTAuthError(
                    f"ChatGPT authorization was not completed ({params['error']})"
                )
            if not params.get("code"):
                raise ChatGPTAuthError("OAuth callback did not include a code")
            if not result.done():
                result.set_result(params)
            status = "200 OK"
            body = "Ash sign-in completed. You can close this tab and return to the terminal."
        except BaseException as exc:
            if not result.done():
                result.set_exception(
                    exc
                    if isinstance(exc, ChatGPTAuthError)
                    else ChatGPTAuthError("OAuth callback failed")
                )
        finally:
            payload = body.encode("utf-8")
            writer.write(
                (
                    f"HTTP/1.1 {status}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    f"Content-Length: {len(payload)}\r\n"
                    "Cache-Control: no-store\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                + payload
            )
            try:
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

    server = await asyncio.start_server(handler, host="127.0.0.1", port=0)
    sockets: tuple[Any, ...] = tuple(server.sockets or ())
    if len(sockets) != 1:
        server.close()
        await server.wait_closed()
        raise ChatGPTAuthError("could not bind the ChatGPT loopback callback")
    port = int(sockets[0].getsockname()[1])
    redirect_uri = f"http://127.0.0.1:{port}{CHATGPT_CALLBACK_PATH}"
    authorization_url = build_url(redirect_uri)
    try:
        opened = await asyncio.to_thread(browser_opener, authorization_url)
        if opened is False:
            raise ChatGPTAuthError("could not open the system browser for ChatGPT sign-in")
        return (
            await asyncio.wait_for(result, timeout=timeout_seconds),
            redirect_uri,
        )
    except TimeoutError as exc:
        raise ChatGPTAuthError("ChatGPT sign-in timed out") from exc
    finally:
        server.close()
        await server.wait_closed()


def _account_from_token_payload(
    payload: dict[str, Any],
    *,
    client_id: str,
    host_id: str,
    identity: dict[str, Any],
    now: float,
) -> ChatGPTAccount:
    access_token = _token_text(payload.get("access_token"), label="ChatGPT access token")
    refresh_token = _token_text(
        payload.get("refresh_token"),
        label="ChatGPT refresh token",
    )
    id_token = _token_text(payload.get("id_token"), label="ChatGPT ID token")
    token_type = _bounded_text(
        payload.get("token_type", "Bearer"),
        label="ChatGPT token type",
        maximum=64,
    )
    if token_type.casefold() != "bearer":
        raise ChatGPTAuthError("ChatGPT token response did not use Bearer")
    expires_in = _finite_timestamp(
        payload.get("expires_in", 0),
        label="ChatGPT access-token lifetime",
        allow_zero=False,
    )
    if expires_in > 86_400:
        raise ChatGPTAuthError("ChatGPT access-token lifetime is unexpectedly large")
    earliest_refresh_at = payload.get("earliest_refresh_at", 0.0)
    try:
        earliest = (
            _finite_timestamp(
                earliest_refresh_at,
                label="ChatGPT earliest refresh time",
            )
            if earliest_refresh_at not in {None, ""}
            else 0.0
        )
    except ChatGPTAuthError:
        earliest = 0.0
    subject = _bounded_text(
        identity.get("sub"),
        label="ChatGPT subject",
        maximum=MAX_CHATGPT_IDENTITY_BYTES,
    )
    email = _optional_text(
        identity.get("email", ""),
        label="ChatGPT email",
        maximum=MAX_CHATGPT_IDENTITY_BYTES,
    )
    return ChatGPTAccount(
        client_id=_validate_client_id(client_id),
        subject=subject,
        email=email,
        issuer=CHATGPT_ISSUER,
        ext_agent_host_id=_validate_host_id(host_id),
        id_token=id_token,
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="Bearer",
        scopes=_normalize_scopes(payload.get("scope", "")),
        expires_at=now + expires_in,
        earliest_refresh_at=earliest,
        saved_at=now,
    )


class ChatGPTAuthManager:
    """Interactive registration, refresh, revocation, and model discovery."""

    def __init__(
        self,
        *,
        store: ChatGPTCredentialStore | None = None,
        host_store: ChatGPTHostStore | None = None,
        http_client: httpx.AsyncClient | None = None,
        browser_opener: Callable[[str], bool] = webbrowser.open,
    ) -> None:
        self.store = store or ChatGPTCredentialStore()
        self.host_store = host_store or ChatGPTHostStore()
        self.http_client = http_client
        self.browser_opener = browser_opener

    async def _with_client(self) -> tuple[httpx.AsyncClient, bool]:
        if self.http_client is not None:
            return self.http_client, False
        return httpx.AsyncClient(timeout=30.0, follow_redirects=False), True

    async def login(
        self,
        *,
        client_id: str | None = None,
        timeout_seconds: float = CHATGPT_LOGIN_TIMEOUT_SECONDS,
    ) -> ChatGPTAccount:
        existing: ChatGPTAccount | None = None
        if client_id is not None:
            existing = self.store.get(client_id)
            if existing is None:
                raise KeyError(f"unknown ChatGPT registration: {client_id}")
            requested_client_id = existing.client_id
        else:
            requested_client_id = CHATGPT_DYNAMIC_CLIENT_ID
        host_id = self.host_store.get_or_create()
        client, owns_client = await self._with_client()
        try:
            discovery = await discover_chatgpt_oauth(client)
            for attempt in range(2):
                state = secrets.token_urlsafe(32)
                nonce = secrets.token_urlsafe(32)
                verifier, challenge = _pkce_pair()
                callback, redirect_uri = await _receive_loopback_callback(
                    expected_state=state,
                    browser_opener=self.browser_opener,
                    timeout_seconds=timeout_seconds,
                    build_url=lambda redirect_uri: _build_authorization_url(
                        discovery=discovery,
                        client_id=requested_client_id,
                        redirect_uri=redirect_uri,
                        host_id=host_id,
                        state=state,
                        nonce=nonce,
                        challenge=challenge,
                        account=existing,
                        request_consent=(
                            existing is not None
                            and existing.signed_in
                            and not existing.plan_enabled
                        ),
                    ),
                )
                callback_client_id = callback.get("client_id", "")
                if requested_client_id == CHATGPT_DYNAMIC_CLIENT_ID:
                    if not callback_client_id:
                        raise ChatGPTAuthError(
                            "new ChatGPT registration did not return an issued client ID"
                        )
                    issued_client_id = _validate_client_id(callback_client_id)
                else:
                    issued_client_id = _validate_client_id(requested_client_id)
                    if callback_client_id and not secrets.compare_digest(
                        callback_client_id,
                        issued_client_id,
                    ):
                        raise ChatGPTAuthError("ChatGPT callback client ID mismatch")
                try:
                    token_payload = await _request_json(
                        client,
                        "POST",
                        discovery.token_endpoint,
                        label="ChatGPT authorization-code exchange",
                        data={
                            "grant_type": "authorization_code",
                            "client_id": issued_client_id,
                            "code": callback["code"],
                            "code_verifier": verifier,
                            "redirect_uri": redirect_uri,
                            "resource": CHATGPT_RESOURCE,
                        },
                    )
                except ChatGPTHTTPError as exc:
                    if (
                        attempt == 0
                        and existing is None
                        and requested_client_id == CHATGPT_DYNAMIC_CLIENT_ID
                        and exc.code == "invalid_grant"
                    ):
                        requested_client_id = issued_client_id
                        continue
                    raise
                id_token = _token_text(
                    token_payload.get("id_token"),
                    label="ChatGPT ID token",
                )
                identity = await validate_chatgpt_id_token(
                    id_token,
                    client_id=issued_client_id,
                    nonce=nonce,
                    discovery=discovery,
                    http_client=client,
                )
                account = _account_from_token_payload(
                    token_payload,
                    client_id=issued_client_id,
                    host_id=host_id,
                    identity=identity,
                    now=time.time(),
                )
                if existing is not None and not secrets.compare_digest(
                    existing.subject,
                    account.subject,
                ):
                    raise ChatGPTAuthError(
                        "ChatGPT reauthorization returned a different account identity"
                    )
            self.store.save(account, make_active=account.plan_enabled)
            return account
            raise ChatGPTAuthError("ChatGPT authorization could not be completed")
        finally:
            if owns_client:
                await client.aclose()

    async def list_models(self) -> tuple[tuple[str, str], ...]:
        token = await ChatGPTAuthSession(
            store=self.store,
            http_client=self.http_client,
        ).access_token()
        client, owns_client = await self._with_client()
        try:
            payload = await _request_json(
                client,
                "GET",
                f"{CHATGPT_RESOURCE}/models",
                label="ChatGPT model catalog",
                headers={"Authorization": f"Bearer {token}"},
            )
        finally:
            if owns_client:
                await client.aclose()
        models = payload.get("models")
        if not isinstance(models, list) or len(models) > 4096:
            raise ChatGPTAuthError("ChatGPT model catalog is invalid")
        result: list[tuple[str, str]] = []
        for item in models:
            if not isinstance(item, dict) or item.get("visibility") != "list":
                continue
            slug = _optional_text(
                item.get("slug", ""),
                label="ChatGPT model slug",
                maximum=512,
            )
            if not slug:
                continue
            display = _optional_text(
                item.get("display_name", ""),
                label="ChatGPT model display name",
                maximum=1024,
            )
            result.append((slug, display or slug))
        if not result:
            raise ChatGPTAuthError("ChatGPT account returned no selectable models")
        return tuple(result)

    async def logout(self) -> tuple[ChatGPTAccount | None, bool]:
        account = self.store.active()
        if account is None:
            return None, True
        lease_id = secrets.token_urlsafe(24)
        deadline = asyncio.get_running_loop().time() + CHATGPT_REFRESH_WAIT_SECONDS
        while not self.store.claim_refresh(
            account.client_id,
            lease_id,
            now=time.time(),
        ):
            current = self.store.get(account.client_id)
            if current is None or not current.signed_in:
                return current, True
            if asyncio.get_running_loop().time() >= deadline:
                raise ChatGPTAuthError(
                    "another Ash process is still updating ChatGPT credentials"
                )
            await asyncio.sleep(0.1)

        current = self.store.get(account.client_id) or account
        remote_confirmed = not bool(current.refresh_token)
        client, owns_client = await self._with_client()
        try:
            if current.refresh_token:
                try:
                    discovery = await discover_chatgpt_oauth(client)
                    remote_confirmed = await _revoke_refresh_token(
                        client,
                        discovery.revocation_endpoint,
                        current,
                    )
                except ChatGPTAuthError:
                    remote_confirmed = False
            cleared = self.store.clear_tokens(current.client_id)
            return cleared, remote_confirmed
        finally:
            try:
                self.store.release_refresh(account.client_id, lease_id)
            finally:
                if owns_client:
                    await client.aclose()


_TERMINAL_REFRESH_ERRORS = frozenset(
    {
        "invalid_grant",
        "invalid_refresh_token",
        "token_expired",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
    }
)


async def _revoke_refresh_token(
    client: httpx.AsyncClient,
    endpoint: str,
    account: ChatGPTAccount,
) -> bool:
    for attempt in range(3):
        try:
            response = await client.post(
                endpoint,
                data={
                    "token": account.refresh_token,
                    "token_type_hint": "refresh_token",
                    "client_id": account.client_id,
                },
                follow_redirects=False,
            )
        except httpx.HTTPError:
            if attempt == 2:
                return False
            await asyncio.sleep(0.25 * (2**attempt))
            continue
        if response.status_code == 200:
            return True
        if response.status_code < 500:
            return False
        if attempt == 2:
            return False
        await asyncio.sleep(0.25 * (2**attempt))
    return False


class ChatGPTAuthSession:
    """Return a valid plan-use access token, refreshing safely when required."""

    def __init__(
        self,
        *,
        store: ChatGPTCredentialStore | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.store = store or ChatGPTCredentialStore()
        self.http_client = http_client
        self._lock = asyncio.Lock()

    async def access_token(self) -> str:
        async with self._lock:
            account = self.store.active()
            if account is None or not account.signed_in:
                raise ChatGPTLoginRequired(
                    "Sign in with ChatGPT is required; use the ChatGPT auth login command."
                )
            if not account.plan_enabled:
                raise ChatGPTPlanUnavailable(
                    "The selected ChatGPT account did not grant plan usage; "
                    "reauthorize it or use an OpenAI API key."
                )
            if account.access_usable():
                return account.access_token
            if not account.refresh_token:
                raise ChatGPTLoginRequired(
                    "ChatGPT credentials expired; sign in again."
                )
            return await self._refresh(account)

    async def _refresh(self, account: ChatGPTAccount) -> str:
        now = time.time()
        if (
            account.earliest_refresh_at > now
            and account.expires_at > now
            and account.access_token
        ):
            return account.access_token

        lease_id = secrets.token_urlsafe(24)
        deadline = asyncio.get_running_loop().time() + CHATGPT_REFRESH_WAIT_SECONDS
        while True:
            now = time.time()
            if self.store.claim_refresh(account.client_id, lease_id, now=now):
                break
            current = self.store.get(account.client_id)
            if current is None or not current.signed_in:
                raise ChatGPTLoginRequired("ChatGPT credentials were signed out")
            if current.access_usable():
                return current.access_token
            if asyncio.get_running_loop().time() >= deadline:
                raise ChatGPTAuthError(
                    "another Ash process is still refreshing ChatGPT credentials"
                )
            await asyncio.sleep(0.1)

        client = self.http_client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        expected_refresh = account.refresh_token
        try:
            payload: dict[str, Any] | None = None
            for attempt in range(CHATGPT_REFRESH_RETRY_ATTEMPTS):
                try:
                    payload = await _request_json(
                        client,
                        "POST",
                        CHATGPT_TOKEN_ENDPOINT,
                        label="ChatGPT token refresh",
                        data={
                            "grant_type": "refresh_token",
                            "client_id": account.client_id,
                            "refresh_token": expected_refresh,
                            "resource": CHATGPT_RESOURCE,
                        },
                    )
                    break
                except ChatGPTHTTPError as exc:
                    if exc.code in _TERMINAL_REFRESH_ERRORS:
                        current = self.store.get(account.client_id)
                        if (
                            current is not None
                            and current.refresh_token
                            and not secrets.compare_digest(
                                current.refresh_token,
                                expected_refresh,
                            )
                            and current.access_usable()
                        ):
                            return current.access_token
                        self.store.clear_tokens(account.client_id)
                        raise ChatGPTLoginRequired(
                            "ChatGPT refresh credentials are no longer usable; "
                            "reauthorize the saved registration with "
                            f"ash auth chatgpt login {account.client_id}."
                        ) from exc
                    if (
                        exc.status_code < 500
                        or attempt + 1 >= CHATGPT_REFRESH_RETRY_ATTEMPTS
                    ):
                        raise
                except ChatGPTTransportError:
                    if attempt + 1 >= CHATGPT_REFRESH_RETRY_ATTEMPTS:
                        raise
                await asyncio.sleep(
                    CHATGPT_REFRESH_RETRY_BASE_SECONDS * (2**attempt)
                )
            if payload is None:
                raise ChatGPTAuthError("ChatGPT token refresh did not complete")
            refreshed = _refreshed_account(account, payload, now=time.time())
            if not refreshed.plan_enabled:
                self.store.clear_tokens(account.client_id)
                raise ChatGPTPlanUnavailable(
                    "ChatGPT token refresh no longer grants plan usage; sign in again."
                )
            if not self.store.finish_refresh(
                client_id=account.client_id,
                lease_id=lease_id,
                expected_refresh_token=expected_refresh,
                account=refreshed,
            ):
                current = self.store.get(account.client_id)
                if current is not None and current.access_usable():
                    return current.access_token
                raise ChatGPTAuthError(
                    "ChatGPT credential refresh lost ownership before persistence"
                )
            return refreshed.access_token
        finally:
            try:
                self.store.release_refresh(account.client_id, lease_id)
            finally:
                if owns_client:
                    await client.aclose()


def _refreshed_account(
    account: ChatGPTAccount,
    payload: dict[str, Any],
    *,
    now: float,
) -> ChatGPTAccount:
    access_token = _token_text(payload.get("access_token"), label="ChatGPT access token")
    refresh_token = _token_text(
        payload.get("refresh_token"),
        label="ChatGPT refresh token",
    )
    expires_in = _finite_timestamp(
        payload.get("expires_in", 0),
        label="ChatGPT access-token lifetime",
        allow_zero=False,
    )
    if expires_in > 86_400:
        raise ChatGPTAuthError("ChatGPT access-token lifetime is unexpectedly large")
    raw_scope = payload.get("scope")
    scopes = account.scopes if raw_scope in {None, ""} else _normalize_scopes(raw_scope)
    earliest_raw = payload.get("earliest_refresh_at", 0.0)
    earliest = (
        _finite_timestamp(
            earliest_raw,
            label="ChatGPT earliest refresh time",
        )
        if earliest_raw not in {None, ""}
        else 0.0
    )
    token_type = _optional_text(
        payload.get("token_type", account.token_type),
        label="ChatGPT token type",
        maximum=64,
    )
    if token_type and token_type.casefold() != "bearer":
        raise ChatGPTAuthError("ChatGPT refresh response did not use Bearer")
    return replace(
        account,
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="Bearer",
        scopes=scopes,
        expires_at=now + expires_in,
        earliest_refresh_at=earliest,
        saved_at=now,
    )
