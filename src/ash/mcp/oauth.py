"""OAuth 2.1 authorization and persisted refresh support for remote MCP."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import secrets
import stat
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import parse_http_list, parse_keqv_list

import httpx


MAX_OAUTH_RESPONSE_BYTES = 1_000_000
MAX_OAUTH_RECORD_BYTES = 1_000_000
TOKEN_REFRESH_MARGIN_SECONDS = 60
SAFE_SERVER_NAME = re.compile(r"[^A-Za-z0-9._-]+")
NEXT_AUTH_SCHEME = re.compile(
    r",\s*[!#$%&'*+\-.^_`|~0-9A-Za-z]+\s+"
    r"(?=[!#$%&'*+\-.^_`|~0-9A-Za-z]+=)"
)
SCOPE = re.compile(r"[\x21\x23-\x5B\x5D-\x7E]+(?: [\x21\x23-\x5B\x5D-\x7E]+)*")


class MCPOAuthError(RuntimeError):
    """A bounded OAuth protocol, persistence, or discovery failure."""


class MCPAuthorizationRequired(MCPOAuthError):
    """Interactive authorization is required before runtime connection."""


@dataclass(frozen=True)
class OAuthDiscovery:
    resource: str
    scopes: tuple[str, ...]
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str = ""


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str = ""


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str = ""
    token_type: str = "Bearer"
    scope: str = ""
    expires_at: float = 0.0

    def usable(self, *, now: float | None = None) -> bool:
        if not self.access_token:
            return False
        if self.expires_at <= 0:
            return True
        current = time.time() if now is None else now
        return self.expires_at > current + TOKEN_REFRESH_MARGIN_SECONDS


@dataclass(frozen=True)
class OAuthBundle:
    resource: str
    discovery: OAuthDiscovery
    client: OAuthClient
    tokens: OAuthTokens


class MCPOAuthTokenStore:
    """Atomic private storage bound to one MCP server name and resource URI."""

    def __init__(self, server_name: str, directory: Path | None = None) -> None:
        safe_name = SAFE_SERVER_NAME.sub("_", server_name).strip("._")[:128]
        self.server_name = safe_name or "server"
        self.directory = directory or (Path.home() / ".ash" / "mcp-oauth")
        self.path = self.directory / f"{self.server_name}.json"

    def load(self, resource: str) -> OAuthBundle | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self._read_record())
            if (
                not isinstance(raw, dict)
                or isinstance(raw.get("version"), bool)
                or raw.get("version") != 1
            ):
                raise ValueError("unsupported token record")
            if raw.get("resource") != resource:
                raise MCPOAuthError(
                    f"stored OAuth credentials for {self.server_name!r} are bound "
                    "to a different MCP resource; run `ash mcp login` again"
                )
            discovery_raw = _required_mapping(raw, "discovery")
            client_raw = _required_mapping(raw, "client")
            tokens_raw = _required_mapping(raw, "tokens")
            scopes_raw = discovery_raw.get("scopes", [])
            if not isinstance(scopes_raw, list) or not all(
                isinstance(item, str) for item in scopes_raw
            ):
                raise ValueError("discovery scopes must be a list of strings")
            discovery = OAuthDiscovery(
                resource=_required_text(discovery_raw, "resource"),
                scopes=tuple(scopes_raw),
                issuer=_required_text(discovery_raw, "issuer"),
                authorization_endpoint=_required_text(
                    discovery_raw, "authorization_endpoint"
                ),
                token_endpoint=_required_text(discovery_raw, "token_endpoint"),
                registration_endpoint=_optional_text(
                    discovery_raw, "registration_endpoint"
                ),
            )
            client = OAuthClient(
                client_id=_required_text(client_raw, "client_id"),
                client_secret=_optional_text(client_raw, "client_secret"),
            )
            tokens = OAuthTokens(
                access_token=_required_text(tokens_raw, "access_token"),
                refresh_token=_optional_text(tokens_raw, "refresh_token"),
                token_type=_required_text(tokens_raw, "token_type"),
                scope=_optional_text(tokens_raw, "scope"),
                expires_at=_optional_number(tokens_raw, "expires_at"),
            )
        except MCPOAuthError:
            raise
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise MCPOAuthError(
                f"invalid OAuth credential record for {self.server_name!r}; "
                "remove it with `ash mcp logout` and authorize again"
            ) from exc
        bundle = OAuthBundle(resource, discovery, client, tokens)
        _validate_bundle(bundle)
        return bundle

    def save(self, bundle: OAuthBundle) -> None:
        _validate_bundle(bundle)
        payload = {
            "version": 1,
            "resource": bundle.resource,
            "discovery": {
                "resource": bundle.discovery.resource,
                "scopes": list(bundle.discovery.scopes),
                "issuer": bundle.discovery.issuer,
                "authorization_endpoint": bundle.discovery.authorization_endpoint,
                "token_endpoint": bundle.discovery.token_endpoint,
                "registration_endpoint": bundle.discovery.registration_endpoint,
            },
            "client": {
                "client_id": bundle.client.client_id,
                "client_secret": bundle.client.client_secret,
            },
            "tokens": {
                "access_token": bundle.tokens.access_token,
                "refresh_token": bundle.tokens.refresh_token,
                "token_type": bundle.tokens.token_type,
                "scope": bundle.tokens.scope,
                "expires_at": bundle.tokens.expires_at,
            },
        }
        if self.directory.is_symlink():
            raise MCPOAuthError("refusing to use a symlinked MCP OAuth directory")
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise MCPOAuthError("MCP OAuth credential path is not a private directory")
        if os.name != "nt":
            self.directory.chmod(0o700)
        descriptor, temporary = tempfile.mkstemp(
            dir=self.directory,
            prefix=f".{self.server_name}.",
            suffix=".tmp",
        )
        try:
            if os.name != "nt":
                os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                self.path.chmod(0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def remove(self) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True

    def _read_record(self) -> str:
        if self.path.is_symlink():
            raise MCPOAuthError("refusing to read a symlinked MCP OAuth token record")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags)
        except OSError as exc:
            raise MCPOAuthError(
                "refusing to read an unsafe MCP OAuth token record"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise MCPOAuthError("MCP OAuth token record is not a regular file")
            if metadata.st_size > MAX_OAUTH_RECORD_BYTES:
                raise MCPOAuthError("MCP OAuth token record exceeded 1 MB")
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                content = handle.read(MAX_OAUTH_RECORD_BYTES + 1)
            if len(content.encode("utf-8")) > MAX_OAUTH_RECORD_BYTES:
                raise MCPOAuthError("MCP OAuth token record exceeded 1 MB")
            return content
        finally:
            if descriptor >= 0:
                os.close(descriptor)


class MCPOAuthSession:
    """Load and refresh resource-bound credentials without user interaction."""

    def __init__(
        self,
        server_name: str,
        resource: str,
        *,
        oauth_config: dict[str, Any] | None = None,
        store: MCPOAuthTokenStore | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.server_name = server_name
        self.resource = canonical_resource_uri(resource)
        self.oauth_config = dict(oauth_config or {})
        self.store = store or MCPOAuthTokenStore(server_name)
        self.http_client = http_client
        self._lock = asyncio.Lock()

    async def authorization_header(
        self,
        *,
        force_refresh: bool = False,
        rejected_access_token: str = "",
    ) -> str:
        async with self._lock:
            bundle = self.store.load(self.resource)
            if bundle is None:
                raise MCPAuthorizationRequired(self._login_guidance())
            configured_client = _configured_client(self.oauth_config)
            if configured_client is not None:
                if configured_client.client_id != bundle.client.client_id:
                    raise MCPAuthorizationRequired(
                        self._login_guidance("configured OAuth client changed")
                    )
                bundle = OAuthBundle(
                    bundle.resource,
                    bundle.discovery,
                    configured_client,
                    bundle.tokens,
                )
            if (
                force_refresh
                and rejected_access_token
                and not secrets.compare_digest(
                    bundle.tokens.access_token, rejected_access_token
                )
            ):
                return f"Bearer {bundle.tokens.access_token}"
            if bundle.tokens.usable() and not force_refresh:
                return f"Bearer {bundle.tokens.access_token}"
            if not bundle.tokens.refresh_token:
                raise MCPAuthorizationRequired(self._login_guidance("token expired"))
            refreshed = await self._refresh(bundle)
            self.store.save(refreshed)
            return f"Bearer {refreshed.tokens.access_token}"

    async def _refresh(self, bundle: OAuthBundle) -> OAuthBundle:
        client = self.http_client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=30.0)
        data = {
            "grant_type": "refresh_token",
            "refresh_token": bundle.tokens.refresh_token,
            "client_id": bundle.client.client_id,
            "resource": bundle.resource,
        }
        if bundle.client.client_secret:
            data["client_secret"] = bundle.client.client_secret
        try:
            payload = await _request_json(
                client,
                "POST",
                bundle.discovery.token_endpoint,
                "OAuth token refresh",
                data=data,
            )
        except (httpx.HTTPError, MCPOAuthError) as exc:
            raise MCPAuthorizationRequired(
                self._login_guidance("token refresh failed")
            ) from exc
        finally:
            if owns_client:
                await client.aclose()
        try:
            tokens = _tokens_from_payload(
                payload,
                fallback_refresh=bundle.tokens.refresh_token,
                fallback_scope=bundle.tokens.scope,
            )
        except MCPOAuthError as exc:
            raise MCPAuthorizationRequired(
                self._login_guidance("token refresh returned invalid credentials")
            ) from exc
        return OAuthBundle(bundle.resource, bundle.discovery, bundle.client, tokens)

    def _login_guidance(self, reason: str = "authorization required") -> str:
        return (
            f"MCP server {self.server_name!r}: {reason}; run "
            f"`ash mcp login {self.server_name}` interactively"
        )


async def authorize_mcp_server(
    server_name: str,
    server_url: str,
    *,
    oauth_config: dict[str, Any] | None = None,
    store: MCPOAuthTokenStore | None = None,
    http_client: httpx.AsyncClient | None = None,
    open_browser: Callable[[str], bool] = webbrowser.open,
    announce: Callable[[str], None] = print,
    timeout_seconds: float = 300.0,
    manual_paste: bool = False,
    requested_scope: str = "",
) -> OAuthBundle:
    """Run an explicit authorization-code flow with S256 PKCE."""

    config = dict(oauth_config or {})
    resource = canonical_resource_uri(server_url)
    explicit_scope = normalize_oauth_scope(requested_scope, "requested OAuth scope")
    token_store = store or MCPOAuthTokenStore(server_name)
    client = http_client
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    callback_future: asyncio.Future[tuple[str, str]] = (
        asyncio.get_running_loop().create_future()
    )
    state = secrets.token_urlsafe(32)
    callback_server: asyncio.AbstractServer | None = None
    try:
        port = int(config.get("redirect_port", 0) or 0)
        if not 0 <= port <= 65535:
            raise MCPOAuthError("OAuth redirect_port must be between 0 and 65535")
        callback_server = await asyncio.start_server(
            lambda reader, writer: _handle_callback(
                reader, writer, callback_future, expected_state=state
            ),
            "127.0.0.1",
            port,
        )
        socket_value = callback_server.sockets[0].getsockname()
        redirect_uri = f"http://127.0.0.1:{socket_value[1]}/callback"
        discovery = await discover_oauth(
            client,
            resource,
            challenged_scope=str(config.get("scope", "")),
        )
        registered = _configured_client(config)
        if registered is None:
            registered = await register_oauth_client(
                client,
                discovery,
                redirect_uri,
                client_name=str(config.get("client_name", "Ash")),
            )
        verifier = _b64url(secrets.token_bytes(64))
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        scope = explicit_scope or " ".join(discovery.scopes)
        query: dict[str, str] = {
            "response_type": "code",
            "client_id": registered.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": resource,
        }
        if scope:
            query["scope"] = scope
        authorization_url = (
            discovery.authorization_endpoint
            + ("&" if "?" in discovery.authorization_endpoint else "?")
            + urlencode(query)
        )
        announce(
            f"Open this URL to authorize MCP server {server_name!r}:\n{authorization_url}"
        )
        opened = False
        try:
            opened = bool(open_browser(authorization_url))
        except (OSError, webbrowser.Error):
            opened = False
        if not opened:
            announce(
                "A browser was not opened automatically. Open the URL manually; "
                "the localhost callback will complete this command."
            )
        if manual_paste:
            _start_manual_callback_reader(
                callback_future,
                expected_state=state,
                announce=announce,
            )
        code, returned_state = await asyncio.wait_for(
            callback_future, timeout=timeout_seconds
        )
        if not secrets.compare_digest(returned_state, state):
            raise MCPOAuthError("OAuth callback state did not match")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": registered.client_id,
            "code_verifier": verifier,
            "resource": resource,
        }
        if registered.client_secret:
            data["client_secret"] = registered.client_secret
        token_payload = await _request_json(
            client,
            "POST",
            discovery.token_endpoint,
            "OAuth token exchange",
            data=data,
        )
        tokens = _tokens_from_payload(token_payload, fallback_scope=scope)
        bundle = OAuthBundle(resource, discovery, registered, tokens)
        token_store.save(bundle)
        return bundle
    except asyncio.TimeoutError as exc:
        raise MCPOAuthError("OAuth authorization timed out") from exc
    finally:
        if callback_server is not None:
            callback_server.close()
            await callback_server.wait_closed()
        if owns_client:
            await client.aclose()


async def discover_oauth(
    client: httpx.AsyncClient,
    server_url: str,
    *,
    challenged_scope: str = "",
) -> OAuthDiscovery:
    resource = canonical_resource_uri(server_url)
    challenge_header = ""
    try:
        request = client.build_request(
            "POST",
            server_url,
            json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
        response = await client.send(request, stream=True, follow_redirects=False)
        try:
            if response.status_code == 401:
                challenge_header = response.headers.get("www-authenticate", "")
        finally:
            await response.aclose()
    except httpx.HTTPError:
        pass
    metadata_urls = protected_resource_metadata_urls(resource, challenge_header)
    protected: dict[str, Any] | None = None
    issuer_hint = ""
    for url in metadata_urls:
        try:
            candidate = await _request_json(
                client,
                "GET",
                url,
                "OAuth protected resource discovery",
                headers={"Accept": "application/json"},
            )
            advertised_resource = candidate.get("resource")
            if not isinstance(advertised_resource, str) or not advertised_resource:
                raise MCPOAuthError("protected resource metadata omitted resource")
            if canonical_resource_uri(advertised_resource) != resource:
                raise MCPOAuthError(
                    "protected resource metadata has a mismatched resource URI"
                )
            auth_servers = candidate.get("authorization_servers", [])
            if (
                not isinstance(auth_servers, list)
                or not auth_servers
                or not isinstance(auth_servers[0], str)
            ):
                raise MCPOAuthError(
                    "protected resource metadata omitted authorization_servers"
                )
            issuer_hint = _validate_oauth_url(auth_servers[0])
        except (httpx.HTTPError, MCPOAuthError):
            continue
        protected = candidate
        break
    if protected is None:
        raise MCPOAuthError("MCP server did not provide protected resource metadata")
    discovered_server: tuple[str, str, str, str] | None = None
    for url in authorization_metadata_urls(issuer_hint):
        try:
            metadata = await _request_json(
                client,
                "GET",
                url,
                "OAuth authorization server discovery",
                headers={"Accept": "application/json"},
            )
            issuer_value = metadata.get("issuer")
            if not isinstance(issuer_value, str):
                raise MCPOAuthError("authorization server metadata omitted issuer")
            issuer = _validate_oauth_url(issuer_value)
            if issuer != issuer_hint:
                raise MCPOAuthError(
                    "authorization server issuer did not match discovery URI"
                )
            methods = metadata.get("code_challenge_methods_supported", [])
            if not isinstance(methods, list) or "S256" not in methods:
                raise MCPOAuthError("authorization server does not advertise S256 PKCE")
            authorization_value = metadata.get("authorization_endpoint")
            token_value = metadata.get("token_endpoint")
            if not isinstance(authorization_value, str) or not isinstance(
                token_value, str
            ):
                raise MCPOAuthError(
                    "authorization server metadata omitted required endpoints"
                )
            authorization_endpoint = _validate_oauth_url(authorization_value)
            token_endpoint = _validate_oauth_url(token_value)
            registration_value = metadata.get("registration_endpoint", "")
            if not isinstance(registration_value, str):
                raise MCPOAuthError("OAuth registration_endpoint is invalid")
            registration_endpoint = (
                _validate_oauth_url(registration_value) if registration_value else ""
            )
        except (httpx.HTTPError, MCPOAuthError):
            continue
        discovered_server = (
            issuer,
            authorization_endpoint,
            token_endpoint,
            registration_endpoint,
        )
        break
    if discovered_server is None:
        raise MCPOAuthError("authorization server metadata discovery failed")
    issuer, authorization_endpoint, token_endpoint, registration_endpoint = (
        discovered_server
    )
    challenge_scope = bearer_challenge_parameters(challenge_header).get("scope", "")
    selected_scope = normalize_oauth_scope(
        challenge_scope or challenged_scope,
        "OAuth discovery scope",
    )
    supported_scopes = protected.get("scopes_supported", [])
    if selected_scope:
        scopes = tuple(selected_scope.split())
    elif isinstance(supported_scopes, list):
        if not all(isinstance(item, str) for item in supported_scopes):
            raise MCPOAuthError("protected resource scopes_supported is invalid")
        scopes = tuple(
            normalize_oauth_scope(item, "protected resource scope")
            for item in supported_scopes
            if item
        )
    else:
        raise MCPOAuthError("protected resource scopes_supported is invalid")
    return OAuthDiscovery(
        resource,
        scopes,
        issuer,
        authorization_endpoint,
        token_endpoint,
        registration_endpoint,
    )


async def register_oauth_client(
    client: httpx.AsyncClient,
    discovery: OAuthDiscovery,
    redirect_uri: str,
    *,
    client_name: str,
) -> OAuthClient:
    if not discovery.registration_endpoint:
        raise MCPOAuthError(
            "authorization server does not support dynamic client registration; "
            "configure oauth.client_id for this MCP server"
        )
    payload = await _request_json(
        client,
        "POST",
        discovery.registration_endpoint,
        "OAuth client registration",
        json_body={
            "client_name": client_name[:100] or "Ash",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        headers={"Accept": "application/json"},
    )
    client_id = payload.get("client_id", "")
    client_secret = payload.get("client_secret", "")
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        raise MCPOAuthError("dynamic client registration returned invalid credentials")
    if not client_id or len(client_id) > 2048:
        raise MCPOAuthError("dynamic client registration omitted a valid client_id")
    if len(client_secret) > 64 * 1024:
        raise MCPOAuthError("dynamic client registration client_secret is too long")
    return OAuthClient(client_id, client_secret)


def protected_resource_metadata_urls(
    resource: str, challenge_header: str = ""
) -> tuple[str, ...]:
    urls: list[str] = []
    challenged = bearer_challenge_parameters(challenge_header).get(
        "resource_metadata", ""
    )
    if challenged:
        return (_validate_oauth_url(challenged),)
    parsed = urlparse(resource)
    path = parsed.path.rstrip("/")
    origin = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    if path:
        urls.append(f"{origin}/.well-known/oauth-protected-resource{path}")
    urls.append(f"{origin}/.well-known/oauth-protected-resource")
    return tuple(dict.fromkeys(urls))


def authorization_metadata_urls(issuer: str) -> tuple[str, ...]:
    parsed = urlparse(_validate_oauth_url(issuer))
    path = parsed.path.rstrip("/")
    origin = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    return tuple(
        dict.fromkeys(
            (
                f"{origin}/.well-known/oauth-authorization-server{path}",
                f"{origin}/.well-known/openid-configuration{path}",
                f"{issuer.rstrip('/')}/.well-known/openid-configuration",
            )
        )
    )


def canonical_resource_uri(value: str) -> str:
    parsed = urlparse(_validate_oauth_url(value, allow_loopback_http=True))
    return urlunparse(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path,
            "",
            parsed.query,
            "",
        )
    )


def _validate_oauth_url(value: str, *, allow_loopback_http: bool = False) -> str:
    if not value or len(value) > 4096:
        raise MCPOAuthError("OAuth endpoint URL is missing or too long")
    parsed = urlparse(value)
    if parsed.username or parsed.password or parsed.fragment:
        raise MCPOAuthError(
            "OAuth endpoint URLs cannot contain credentials or fragments"
        )
    host = (parsed.hostname or "").casefold()
    loopback = host in {"127.0.0.1", "::1", "localhost"}
    if parsed.scheme != "https" and not (
        allow_loopback_http and parsed.scheme == "http" and loopback
    ):
        exception = ", except localhost" if allow_loopback_http else ""
        raise MCPOAuthError(f"OAuth URLs must use HTTPS{exception}")
    if not host:
        raise MCPOAuthError("OAuth endpoint URL must include a hostname")
    return value


async def _bounded_json_response(
    response: httpx.Response, label: str
) -> dict[str, Any]:
    try:
        if response.status_code >= 400:
            raise MCPOAuthError(f"{label} returned HTTP {response.status_code}")
        content_type = response.headers.get("content-type", "").casefold()
        if content_type and "json" not in content_type:
            raise MCPOAuthError(f"{label} returned a non-JSON content type")
        length = response.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_OAUTH_RESPONSE_BYTES:
            raise MCPOAuthError(f"{label} response exceeded 1 MB")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_OAUTH_RESPONSE_BYTES:
                raise MCPOAuthError(f"{label} response exceeded 1 MB")
            chunks.append(chunk)
        try:
            payload = json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MCPOAuthError(f"{label} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise MCPOAuthError(f"{label} returned a non-object response")
        return payload
    finally:
        await response.aclose()


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    label: str,
    *,
    data: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request = client.build_request(
        method,
        url,
        data=data,
        json=json_body,
        headers=headers,
    )
    response = await client.send(request, stream=True, follow_redirects=False)
    return await _bounded_json_response(response, label)


def _tokens_from_payload(
    payload: dict[str, Any],
    *,
    fallback_refresh: str = "",
    fallback_scope: str = "",
) -> OAuthTokens:
    access_token = payload.get("access_token")
    token_type = payload.get("token_type")
    refresh_token = payload.get("refresh_token", fallback_refresh)
    scope = payload.get("scope", fallback_scope)
    if not isinstance(access_token, str) or not access_token:
        raise MCPOAuthError("OAuth token response omitted a valid access_token")
    if len(access_token) > 64 * 1024:
        raise MCPOAuthError("OAuth access_token is too long")
    if not isinstance(token_type, str):
        raise MCPOAuthError("OAuth token response omitted a valid token_type")
    if token_type.casefold() != "bearer":
        raise MCPOAuthError("OAuth token response token_type is not Bearer")
    if not isinstance(refresh_token, str) or not isinstance(scope, str):
        raise MCPOAuthError("OAuth token response returned invalid string fields")
    if len(refresh_token) > 64 * 1024:
        raise MCPOAuthError("OAuth refresh_token is too long")
    expires_in = payload.get("expires_in")
    expires_at = 0.0
    if expires_in is not None:
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, str)):
            raise MCPOAuthError("OAuth token expires_in is invalid")
        try:
            seconds = int(expires_in)
        except (TypeError, ValueError) as exc:
            raise MCPOAuthError("OAuth token expires_in is invalid") from exc
        if not 0 <= seconds <= 10 * 365 * 24 * 60 * 60:
            raise MCPOAuthError("OAuth token expires_in is out of range")
        expires_at = time.time() + seconds
    return OAuthTokens(
        access_token,
        refresh_token,
        token_type,
        normalize_oauth_scope(scope, "OAuth token scope"),
        expires_at,
    )


def _validate_bundle(bundle: OAuthBundle) -> None:
    if bundle.resource != bundle.discovery.resource:
        raise MCPOAuthError("OAuth credential resource binding does not match")
    if canonical_resource_uri(bundle.resource) != bundle.resource:
        raise MCPOAuthError("OAuth credential resource URI is not canonical")
    for endpoint in (
        bundle.discovery.issuer,
        bundle.discovery.authorization_endpoint,
        bundle.discovery.token_endpoint,
    ):
        _validate_oauth_url(endpoint)
    if bundle.discovery.registration_endpoint:
        _validate_oauth_url(bundle.discovery.registration_endpoint)
    if not bundle.client.client_id or len(bundle.client.client_id) > 2048:
        raise MCPOAuthError("OAuth client_id is missing or too long")
    if len(bundle.client.client_secret) > 64 * 1024:
        raise MCPOAuthError("OAuth client_secret is too long")
    if not bundle.tokens.access_token or len(bundle.tokens.access_token) > 64 * 1024:
        raise MCPOAuthError("OAuth access token is missing or too long")
    if len(bundle.tokens.refresh_token) > 64 * 1024:
        raise MCPOAuthError("OAuth refresh token is too long")
    if len(bundle.tokens.scope) > 8192:
        raise MCPOAuthError("OAuth token scope is too long")
    normalize_oauth_scope(bundle.tokens.scope, "stored OAuth token scope")
    for scope in bundle.discovery.scopes:
        normalize_oauth_scope(scope, "stored OAuth discovery scope")
    if bundle.tokens.token_type.casefold() != "bearer":
        raise MCPOAuthError("stored MCP OAuth token type is not Bearer")
    if not math.isfinite(bundle.tokens.expires_at) or bundle.tokens.expires_at < 0:
        raise MCPOAuthError("OAuth token expiry is invalid")


def _configured_client(config: dict[str, Any]) -> OAuthClient | None:
    client_id_value = config.get("client_id", "")
    client_secret = config.get("client_secret", "")
    if not isinstance(client_id_value, str) or not isinstance(client_secret, str):
        raise MCPOAuthError("OAuth client configuration is invalid")
    client_id = client_id_value.strip()
    if not client_id:
        return None
    if len(client_id) > 2048:
        raise MCPOAuthError("OAuth client_id is too long")
    if len(client_secret) > 64 * 1024:
        raise MCPOAuthError("OAuth client_secret is too long")
    return OAuthClient(client_id, client_secret)


async def _handle_callback(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    future: asyncio.Future[tuple[str, str]],
    *,
    expected_state: str,
) -> None:
    status = "400 Bad Request"
    message = "Authorization failed. You may close this window."
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        parts = line.decode("ascii", errors="replace").split(" ", 2)
        target = parts[1] if len(parts) == 3 and parts[0] == "GET" else ""
        parsed = urlparse(target)
        query = parse_qs(parsed.query)
        code = query.get("code", [""])[0]
        state = query.get("state", [""])[0]
        error = query.get("error", [""])[0]
        if (
            parsed.path == "/callback"
            and error
            and secrets.compare_digest(state, expected_state)
        ):
            if not future.done():
                safe_error = re.sub(r"[^A-Za-z0-9_.-]", "_", error)[:100]
                future.set_exception(
                    MCPOAuthError(f"authorization server returned {safe_error}")
                )
        elif (
            parsed.path == "/callback"
            and code
            and secrets.compare_digest(state, expected_state)
        ):
            if not future.done():
                future.set_result((code, state))
            status = "200 OK"
            message = "Authorization complete. You may close this window."
    except (asyncio.TimeoutError, OSError, ValueError):
        pass
    payload = message.encode("utf-8")
    writer.write(
        (
            f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        + payload
    )
    try:
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


def bearer_challenge_parameters(header: str) -> dict[str, str]:
    match = re.search(r"(?:^|,)\s*Bearer\s+", header or "", re.IGNORECASE)
    if match is None:
        return {}
    segment = (header or "")[match.end() :]
    next_scheme = NEXT_AUTH_SCHEME.search(segment)
    if next_scheme is not None:
        segment = segment[: next_scheme.start()]
    try:
        parsed = parse_keqv_list(parse_http_list(segment))
    except (TypeError, ValueError):
        return {}
    return {
        str(key).casefold(): str(value)
        for key, value in parsed.items()
        if isinstance(key, str)
    }


def normalize_oauth_scope(value: str, label: str = "OAuth scope") -> str:
    scope = value.strip()
    if not scope:
        return ""
    if len(scope) > 8192 or SCOPE.fullmatch(scope) is None:
        raise MCPOAuthError(f"{label} is invalid")
    return scope


def _start_manual_callback_reader(
    future: asyncio.Future[tuple[str, str]],
    *,
    expected_state: str,
    announce: Callable[[str], None],
) -> None:
    loop = asyncio.get_running_loop()

    def read() -> None:
        while not future.done():
            try:
                value = input(
                    "Paste the final localhost redirect URL here, or leave this "
                    "prompt open for automatic callback: "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not value:
                return
            if len(value) > 4096:
                announce("The pasted redirect URL was too long.")
                continue
            parsed = urlparse(value)
            query = parse_qs(parsed.query)
            code = query.get("code", [""])[0]
            state = query.get("state", [""])[0]
            if (
                parsed.path != "/callback"
                or not code
                or not secrets.compare_digest(state, expected_state)
            ):
                announce("The pasted redirect URL was invalid or had the wrong state.")
                continue

            def accept() -> None:
                if not future.done():
                    future.set_result((code, state))

            loop.call_soon_threadsafe(accept)
            return

    threading.Thread(target=read, name="ash-mcp-oauth-paste", daemon=True).start()


def _required_mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise ValueError(f"{key} must be an object")
    return item


def _required_text(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key} must be a non-empty string")
    return item


def _optional_text(value: dict[str, Any], key: str) -> str:
    item = value.get(key, "")
    if not isinstance(item, str):
        raise ValueError(f"{key} must be a string")
    return item


def _optional_number(value: dict[str, Any], key: str) -> float:
    item = value.get(key, 0.0)
    if isinstance(item, bool) or not isinstance(item, (int, float)):
        raise ValueError(f"{key} must be a number")
    return float(item)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
