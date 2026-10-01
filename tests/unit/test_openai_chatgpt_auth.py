from __future__ import annotations

import base64
import json
import socket
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ash.providers.openai_chatgpt_auth import (
    CHATGPT_DYNAMIC_CLIENT_ID,
    CHATGPT_ISSUER,
    CHATGPT_REQUIRED_PLAN_SCOPE,
    CHATGPT_RESOURCE,
    ChatGPTAccount,
    ChatGPTAuthError,
    ChatGPTAuthManager,
    ChatGPTAuthSession,
    ChatGPTCredentialStore,
    ChatGPTDiscovery,
    ChatGPTHostStore,
    ChatGPTHTTPError,
    ChatGPTLoginRequired,
    _build_authorization_url,
    _receive_loopback_callback,
    validate_chatgpt_id_token,
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _account(
    *,
    access_token: str = "access-old",
    refresh_token: str = "refresh-old",
    expires_at: float | None = None,
) -> ChatGPTAccount:
    return ChatGPTAccount(
        client_id="oaiapp_test-client",
        subject="subject-1",
        email="user@example.com",
        issuer=CHATGPT_ISSUER,
        ext_agent_host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
        id_token="id-token",
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="Bearer",
        scopes=(
            CHATGPT_REQUIRED_PLAN_SCOPE,
            "email",
            "offline_access",
            "openid",
            "profile",
            "resource.invoke",
        ),
        expires_at=time.time() + 3600 if expires_at is None else expires_at,
        earliest_refresh_at=0.0,
        saved_at=time.time(),
    )


def test_chatgpt_store_keeps_accounts_separate_and_serializes_refresh(
    tmp_path: Path,
) -> None:
    store = ChatGPTCredentialStore(tmp_path / "credentials")
    first = _account()
    second = ChatGPTAccount(
        **{
            **first.__dict__,
            "client_id": "oaiapp_second-client",
            "subject": "subject-2",
            "email": "same@example.com",
        }
    )

    store.save(first)
    store.save(second, make_active=False)

    assert {account.client_id for account in store.list_accounts()} == {
        first.client_id,
        second.client_id,
    }
    assert store.active() == first
    assert store.claim_refresh(first.client_id, "lease-one", now=100.0) is True
    assert store.claim_refresh(first.client_id, "lease-two", now=101.0) is False

    replacement = _account(
        access_token="access-new",
        refresh_token="refresh-new",
        expires_at=time.time() + 3600,
    )
    assert (
        store.finish_refresh(
            client_id=first.client_id,
            lease_id="lease-one",
            expected_refresh_token="refresh-old",
            account=replacement,
        )
        is True
    )
    assert store.get(first.client_id) == replacement


@pytest.mark.asyncio
async def test_chatgpt_refresh_rotates_token_without_resending_scope(
    tmp_path: Path,
) -> None:
    store = ChatGPTCredentialStore(tmp_path / "credentials")
    store.save(_account(expires_at=time.time() - 1))
    seen_form: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_form
        seen_form = parse_qs(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "access_token": "access-new",
                "refresh_token": "refresh-new",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": (
                    f"{CHATGPT_REQUIRED_PLAN_SCOPE} email offline_access "
                    "openid profile resource.invoke"
                ),
            },
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    ) as client:
        token = await ChatGPTAuthSession(store=store, http_client=client).access_token()

    assert token == "access-new"
    assert seen_form["grant_type"] == ["refresh_token"]
    assert seen_form["client_id"] == ["oaiapp_test-client"]
    assert seen_form["refresh_token"] == ["refresh-old"]
    assert seen_form["resource"] == [CHATGPT_RESOURCE]
    assert "scope" not in seen_form
    persisted = store.active()
    assert persisted is not None
    assert persisted.refresh_token == "refresh-new"


@pytest.mark.parametrize("first_failure", ["network", "server"])
@pytest.mark.asyncio
async def test_chatgpt_refresh_retries_transient_failure_with_same_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_failure: str,
) -> None:
    from ash.providers import openai_chatgpt_auth as auth

    store = ChatGPTCredentialStore(tmp_path / "credentials")
    store.save(_account(expires_at=time.time() - 1))
    attempts = 0
    refresh_tokens: list[str] = []

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(auth.asyncio, "sleep", no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        form = parse_qs(request.content.decode("utf-8"))
        refresh_tokens.append(form["refresh_token"][0])
        if attempts == 1:
            if first_failure == "network":
                raise httpx.ConnectError("temporary network failure", request=request)
            return httpx.Response(503, content=b"temporarily unavailable", request=request)
        return httpx.Response(
            200,
            json={
                "access_token": "access-new",
                "refresh_token": "refresh-new",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": f"{CHATGPT_REQUIRED_PLAN_SCOPE} openid offline_access",
            },
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    ) as client:
        token = await ChatGPTAuthSession(store=store, http_client=client).access_token()

    assert token == "access-new"
    assert attempts == 2
    assert refresh_tokens == ["refresh-old", "refresh-old"]
    persisted = store.active()
    assert persisted is not None
    assert persisted.refresh_token == "refresh-new"


@pytest.mark.asyncio
async def test_terminal_refresh_error_keeps_registration_for_reauthorization(
    tmp_path: Path,
) -> None:
    store = ChatGPTCredentialStore(tmp_path / "credentials")
    account = _account(expires_at=time.time() - 1)
    store.save(account)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "invalid_grant"},
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    ) as client:
        with pytest.raises(ChatGPTLoginRequired, match=account.client_id):
            await ChatGPTAuthSession(store=store, http_client=client).access_token()

    persisted = store.active()
    assert persisted is not None
    assert persisted.client_id == account.client_id
    assert persisted.subject == account.subject
    assert persisted.access_token == ""
    assert persisted.refresh_token == ""
    assert persisted.id_token == ""


def test_authorization_url_requests_consent_only_when_explicitly_requested() -> None:
    discovery = ChatGPTDiscovery(
        issuer=CHATGPT_ISSUER,
        authorization_endpoint=f"{CHATGPT_ISSUER}/api/accounts/authorize",
        token_endpoint=f"{CHATGPT_ISSUER}/api/accounts/oauth/token",
        jwks_uri=f"{CHATGPT_ISSUER}/.well-known/jwks.json",
        revocation_endpoint=f"{CHATGPT_ISSUER}/api/accounts/revoke",
    )
    account = _account()
    common = {
        "discovery": discovery,
        "client_id": account.client_id,
        "redirect_uri": "http://127.0.0.1:41000/auth/callback",
        "host_id": account.ext_agent_host_id,
        "state": "state",
        "nonce": "nonce",
        "challenge": "challenge",
        "account": account,
    }

    ordinary = parse_qs(
        urlsplit(_build_authorization_url(**common)).query
    )
    consent = parse_qs(
        urlsplit(
            _build_authorization_url(**common, request_consent=True)
        ).query
    )

    assert "prompt" not in ordinary
    assert consent["prompt"] == ["consent"]


@pytest.mark.asyncio
async def test_loopback_callback_rejects_duplicate_state_parameter() -> None:
    expected_state = "expected-state"

    def opener(authorization_url: str) -> bool:
        query = parse_qs(urlsplit(authorization_url).query)
        redirect = urlsplit(query["redirect_uri"][0])
        callback_query = urlencode(
            [
                ("state", expected_state),
                ("state", "attacker-state"),
                ("code", "code-1"),
            ]
        )
        request = (
            f"GET {redirect.path}?{callback_query} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{redirect.port}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        with socket.create_connection(("127.0.0.1", int(redirect.port or 0)), timeout=2) as sock:
            sock.sendall(request)
            sock.recv(4096)
        return True

    with pytest.raises(ChatGPTAuthError, match="duplicate 'state'"):
        await _receive_loopback_callback(
            expected_state=expected_state,
            browser_opener=opener,
            build_url=lambda redirect_uri: (
                "https://auth.openai.com/api/accounts/authorize?"
                + urlencode(
                    {
                        "redirect_uri": redirect_uri,
                        "state": expected_state,
                    }
                )
            ),
            timeout_seconds=2,
        )


@pytest.mark.asyncio
async def test_id_token_validation_checks_signature_nonce_issuer_and_audience() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private_key.public_key().public_numbers()
    client_id = "oaiapp_verified-client"
    nonce = "expected-nonce"
    now = int(time.time())
    header = {"alg": "RS256", "kid": "test-key", "typ": "JWT"}
    payload = {
        "iss": CHATGPT_ISSUER,
        "aud": client_id,
        "sub": "verified-subject",
        "email": "verified@example.com",
        "nonce": nonce,
        "iat": now,
        "nbf": now - 1,
        "exp": now + 3600,
    }
    head = _b64url(json.dumps(header, separators=(",", ":")).encode())
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    token = f"{head}.{body}.{_b64url(signature)}"
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "test-key",
                "alg": "RS256",
                "n": _b64url(
                    public.n.to_bytes((public.n.bit_length() + 7) // 8, "big")
                ),
                "e": _b64url(
                    public.e.to_bytes((public.e.bit_length() + 7) // 8, "big")
                ),
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jwks, request=request)

    discovery = ChatGPTDiscovery(
        issuer=CHATGPT_ISSUER,
        authorization_endpoint=f"{CHATGPT_ISSUER}/api/accounts/authorize",
        token_endpoint=f"{CHATGPT_ISSUER}/api/accounts/oauth/token",
        jwks_uri=f"{CHATGPT_ISSUER}/.well-known/jwks.json",
        revocation_endpoint=f"{CHATGPT_ISSUER}/api/accounts/revoke",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    ) as client:
        validated = await validate_chatgpt_id_token(
            token,
            client_id=client_id,
            nonce=nonce,
            discovery=discovery,
            http_client=client,
            now=float(now),
        )
        with pytest.raises(ChatGPTAuthError, match="nonce mismatch"):
            await validate_chatgpt_id_token(
                token,
                client_id=client_id,
                nonce="wrong-nonce",
                discovery=discovery,
                http_client=client,
                now=float(now),
            )

    assert validated["sub"] == "verified-subject"

    without_iat = dict(payload)
    without_iat.pop("iat")
    missing_body = _b64url(
        json.dumps(without_iat, separators=(",", ":")).encode()
    )
    missing_input = f"{head}.{missing_body}".encode("ascii")
    missing_signature = private_key.sign(
        missing_input,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    missing_token = f"{head}.{missing_body}.{_b64url(missing_signature)}"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    ) as client:
        with pytest.raises(ChatGPTAuthError, match="issued-at"):
            await validate_chatgpt_id_token(
                missing_token,
                client_id=client_id,
                nonce=nonce,
                discovery=discovery,
                http_client=client,
                now=float(now),
            )


@pytest.mark.asyncio
async def test_id_token_multiple_audiences_require_matching_authorized_party() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private_key.public_key().public_numbers()
    client_id = "oaiapp_verified-client"
    nonce = "expected-nonce"
    now = int(time.time())
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "test-key",
                "alg": "RS256",
                "n": _b64url(
                    public.n.to_bytes((public.n.bit_length() + 7) // 8, "big")
                ),
                "e": _b64url(
                    public.e.to_bytes((public.e.bit_length() + 7) // 8, "big")
                ),
            }
        ]
    }
    discovery = ChatGPTDiscovery(
        issuer=CHATGPT_ISSUER,
        authorization_endpoint=f"{CHATGPT_ISSUER}/api/accounts/authorize",
        token_endpoint=f"{CHATGPT_ISSUER}/api/accounts/oauth/token",
        jwks_uri=f"{CHATGPT_ISSUER}/.well-known/jwks.json",
        revocation_endpoint=f"{CHATGPT_ISSUER}/api/accounts/revoke",
    )

    def token(azp: str | None) -> str:
        header = {"alg": "RS256", "kid": "test-key", "typ": "JWT"}
        payload = {
            "iss": CHATGPT_ISSUER,
            "aud": [client_id, "another-audience"],
            "sub": "verified-subject",
            "nonce": nonce,
            "iat": now,
            "exp": now + 3600,
        }
        if azp is not None:
            payload["azp"] = azp
        head = _b64url(json.dumps(header, separators=(",", ":")).encode())
        body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
        signing_input = f"{head}.{body}".encode("ascii")
        signature = private_key.sign(
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return f"{head}.{body}.{_b64url(signature)}"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=jwks, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    ) as client:
        with pytest.raises(ChatGPTAuthError, match="authorized party"):
            await validate_chatgpt_id_token(
                token(None),
                client_id=client_id,
                nonce=nonce,
                discovery=discovery,
                http_client=client,
                now=float(now),
            )
        with pytest.raises(ChatGPTAuthError, match="authorized-party mismatch"):
            await validate_chatgpt_id_token(
                token("wrong-client"),
                client_id=client_id,
                nonce=nonce,
                discovery=discovery,
                http_client=client,
                now=float(now),
            )
        validated = await validate_chatgpt_id_token(
            token(client_id),
            client_id=client_id,
            nonce=nonce,
            discovery=discovery,
            http_client=client,
            now=float(now),
        )

    assert validated["azp"] == client_id


@pytest.mark.asyncio
async def test_dynamic_registration_invalid_grant_retries_with_issued_client_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers import openai_chatgpt_auth as auth

    store = ChatGPTCredentialStore(tmp_path / "credentials")
    host_store = ChatGPTHostStore(tmp_path / "host")
    issued_client_id = "oaiapp_retry-client"
    authorization_client_ids: list[str] = []
    exchange_client_ids: list[str] = []
    callback_count = 0
    exchange_count = 0
    discovery = ChatGPTDiscovery(
        issuer=CHATGPT_ISSUER,
        authorization_endpoint=f"{CHATGPT_ISSUER}/api/accounts/authorize",
        token_endpoint=f"{CHATGPT_ISSUER}/api/accounts/oauth/token",
        jwks_uri=f"{CHATGPT_ISSUER}/.well-known/jwks.json",
        revocation_endpoint=f"{CHATGPT_ISSUER}/api/accounts/revoke",
    )

    async def discover(_client):
        return discovery

    async def receive_callback(
        *,
        expected_state,
        browser_opener,
        build_url,
        timeout_seconds,
    ):
        nonlocal callback_count
        del expected_state, browser_opener, timeout_seconds
        callback_count += 1
        redirect_uri = f"http://127.0.0.1:{41000 + callback_count}/auth/callback"
        query = parse_qs(build_url(redirect_uri).split("?", 1)[1])
        authorization_client_ids.append(query["client_id"][0])
        callback = {"code": f"code-{callback_count}"}
        if callback_count == 1:
            callback["client_id"] = issued_client_id
        return callback, redirect_uri

    async def request_json(_client, method, url, *, label, data=None, headers=None):
        nonlocal exchange_count
        del method, url, label, headers
        exchange_count += 1
        assert data is not None
        exchange_client_ids.append(data["client_id"])
        if exchange_count == 1:
            raise ChatGPTHTTPError(
                "authorization exchange failed",
                status_code=400,
                code="invalid_grant",
            )
        return {
            "access_token": "access-final",
            "refresh_token": "refresh-final",
            "id_token": "id-final",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": f"{CHATGPT_REQUIRED_PLAN_SCOPE} openid offline_access",
        }

    async def validate_id_token(
        token,
        *,
        client_id,
        nonce,
        discovery,
        http_client,
        now=None,
    ):
        del token, nonce, discovery, http_client, now
        assert client_id == issued_client_id
        return {"sub": "subject-retry", "email": "retry@example.com"}

    monkeypatch.setattr(auth, "discover_chatgpt_oauth", discover)
    monkeypatch.setattr(auth, "_receive_loopback_callback", receive_callback)
    monkeypatch.setattr(auth, "_request_json", request_json)
    monkeypatch.setattr(auth, "validate_chatgpt_id_token", validate_id_token)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, request=request)
        )
    ) as client:
        account = await ChatGPTAuthManager(
            store=store,
            host_store=host_store,
            http_client=client,
            browser_opener=lambda _url: True,
        ).login()

    assert authorization_client_ids == [CHATGPT_DYNAMIC_CLIENT_ID, issued_client_id]
    assert exchange_client_ids == [issued_client_id, issued_client_id]
    assert account.client_id == issued_client_id
    assert account.subject == "subject-retry"
    assert store.active() == account


@pytest.mark.asyncio
async def test_logout_serializes_with_refresh_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.providers import openai_chatgpt_auth as auth

    account = _account()
    events: list[str] = []

    class Store:
        def active(self):
            return account

        def claim_refresh(self, client_id, lease_id, *, now):
            del client_id, lease_id, now
            events.append("claim")
            return True

        def get(self, client_id):
            del client_id
            events.append("get")
            return account

        def clear_tokens(self, client_id):
            del client_id
            events.append("clear")
            return ChatGPTAccount(
                **{
                    **account.__dict__,
                    "id_token": "",
                    "access_token": "",
                    "refresh_token": "",
                }
            )

        def release_refresh(self, client_id, lease_id):
            del client_id, lease_id
            events.append("release")

    async def discover(_client):
        return ChatGPTDiscovery(
            issuer=CHATGPT_ISSUER,
            authorization_endpoint=f"{CHATGPT_ISSUER}/api/accounts/authorize",
            token_endpoint=f"{CHATGPT_ISSUER}/api/accounts/oauth/token",
            jwks_uri=f"{CHATGPT_ISSUER}/.well-known/jwks.json",
            revocation_endpoint=f"{CHATGPT_ISSUER}/api/accounts/revoke",
        )

    async def revoke(_client, _endpoint, revoked_account):
        assert revoked_account is account
        events.append("revoke")
        return True

    monkeypatch.setattr(auth, "discover_chatgpt_oauth", discover)
    monkeypatch.setattr(auth, "_revoke_refresh_token", revoke)
    async with httpx.AsyncClient() as client:
        cleared, confirmed = await ChatGPTAuthManager(
            store=Store(),  # type: ignore[arg-type]
            http_client=client,
        ).logout()

    assert confirmed is True
    assert cleared is not None
    assert cleared.signed_in is False
    assert events == ["claim", "get", "revoke", "clear", "release"]
