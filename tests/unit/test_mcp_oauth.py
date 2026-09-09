from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest

import ash.safety.private_store as private_store
from ash.mcp.client import MCPClient
from ash.mcp.oauth import (
    MCPAuthorizationRequired,
    MCPOAuthError,
    MCPOAuthStoreUnavailable,
    MCPOAuthSession,
    MCPOAuthTokenStore,
    OAuthBundle,
    OAuthClient,
    OAuthDiscovery,
    OAuthTokens,
    authorize_mcp_server,
    authorization_metadata_urls,
    bearer_challenge_parameters,
    canonical_resource_uri,
    discover_oauth,
    protected_resource_metadata_urls,
)
from ash.mcp.server import MCPServerConfig


@pytest.mark.asyncio
async def test_oauth_json_response_rejects_duplicate_fields() -> None:
    from ash.mcp.oauth import _bounded_json_response

    response = httpx.Response(
        200,
        content=b'{"access_token":"first","access_token":"second"}',
        headers={"content-type": "application/json"},
    )

    with pytest.raises(MCPOAuthError, match="returned invalid JSON"):
        await _bounded_json_response(response, "token endpoint")


@pytest.mark.asyncio
@pytest.mark.parametrize("content_length", ["not-a-number", "-1", "9" * 5000])
async def test_oauth_json_response_rejects_malformed_content_length(
    content_length: str,
) -> None:
    from ash.mcp.oauth import _bounded_json_response

    response = httpx.Response(
        200,
        content=b'{"access_token":"token"}',
        headers={
            "content-type": "application/json",
            "content-length": content_length,
        },
    )

    with pytest.raises(MCPOAuthError, match="invalid Content-Length"):
        await _bounded_json_response(response, "token endpoint")


def _bundle(resource: str, *, expired: bool = False) -> OAuthBundle:
    canonical = canonical_resource_uri(resource)
    return OAuthBundle(
        canonical,
        OAuthDiscovery(
            canonical,
            ("files:read",),
            "https://auth.example.test",
            "https://auth.example.test/authorize",
            "https://auth.example.test/token",
            "https://auth.example.test/register",
        ),
        OAuthClient("client-id", "client-secret"),
        OAuthTokens(
            "old-access",
            "old-refresh",
            "Bearer",
            "files:read",
            1.0 if expired else 0.0,
        ),
    )


def _redirect_visible_store_after_open(
    store: MCPOAuthTokenStore,
    outside: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_opened = private_store.PrivateStore.opened

    @contextmanager
    def redirected(instance: private_store.PrivateStore, *, create: bool = True):
        with original_opened(instance, create=create) as descriptor:
            if instance is store._private_store:
                displaced = store.directory.with_name(
                    f"{store.directory.name}-displaced"
                )
                store.directory.rename(displaced)
                outside.mkdir(parents=True, exist_ok=True)
                store.directory.symlink_to(outside, target_is_directory=True)
            yield descriptor

    monkeypatch.setattr(private_store.PrivateStore, "opened", redirected)


def test_oauth_store_is_private_resource_bound_and_rejects_symlinks(
    tmp_path: Path,
) -> None:
    store = MCPOAuthTokenStore("docs/server", tmp_path / "tokens")
    bundle = _bundle("https://mcp.example.test/rpc?tenant=one")

    store.save(bundle)

    assert store.path.name == "docs_server.json"
    assert store.load(bundle.resource) == bundle
    if os.name != "nt":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    with pytest.raises(MCPOAuthError, match="different MCP resource"):
        store.load("https://mcp.example.test/rpc?tenant=two")

    store.remove()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    store.directory.mkdir(exist_ok=True)
    store.path.symlink_to(target)
    with pytest.raises(MCPOAuthError, match="symlinked"):
        store.load(bundle.resource)


def test_oauth_store_rejects_oversized_records_and_symlinked_directory(
    tmp_path: Path,
) -> None:
    resource = "https://mcp.example.test/rpc"
    directory = tmp_path / "tokens"
    directory.mkdir()
    oversized = MCPOAuthTokenStore("remote", directory)
    oversized.path.write_bytes(b" " * 1_000_001)
    with pytest.raises(MCPOAuthError, match="exceeded 1 MB"):
        oversized.load(resource)

    target = tmp_path / "target"
    target.mkdir()
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(target, target_is_directory=True)
    linked = MCPOAuthTokenStore("remote", linked_directory)
    with pytest.raises(MCPOAuthError, match="symlinked MCP OAuth directory"):
        linked.save(_bundle(resource))


def test_oauth_store_rejects_symlinked_user_state_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    try:
        (home / ".ash").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    monkeypatch.setenv("HOME", str(home))
    resource = "https://mcp.example.test/rpc"
    bundle = _bundle(resource)
    store = MCPOAuthTokenStore("remote")

    with pytest.raises(MCPOAuthError, match="linked MCP OAuth credential path"):
        store.save(bundle)
    assert not (outside / "mcp-oauth").exists()

    staged = MCPOAuthTokenStore("remote", tmp_path / "staged")
    staged.save(bundle)
    redirected_directory = outside / "mcp-oauth"
    redirected_directory.mkdir()
    redirected = redirected_directory / "remote.json"
    redirected.write_bytes(staged.path.read_bytes())

    with pytest.raises(MCPOAuthError, match="linked MCP OAuth credential path"):
        store.load(resource)
    with pytest.raises(MCPOAuthError, match="linked MCP OAuth credential path"):
        store.remove()
    assert redirected.exists()
    assert store.credential_state(resource) == "invalid"


def test_oauth_store_rejects_symlinked_trusted_anchor_ancestor(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    linked = base / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    store = MCPOAuthTokenStore("remote", linked / "tokens")

    with pytest.raises(MCPOAuthError, match="linked MCP OAuth credential path"):
        store.save(_bundle("https://mcp.example.test/rpc"))
    assert not (outside / "tokens").exists()


def test_oauth_store_creates_missing_custom_parent_directories(
    tmp_path: Path,
) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore(
        "remote",
        tmp_path / "new" / "nested" / "tokens",
    )

    store.save(_bundle(resource))

    assert store.load(resource) == _bundle(resource)
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700


def test_oauth_store_save_stays_on_open_directory_after_visible_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")
    store.save(_bundle(resource))
    assert store.remove() is True

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_record = outside / store.path.name
    outside_record.write_bytes(b"outside-record-must-not-change")
    _redirect_visible_store_after_open(store, outside, monkeypatch)

    store.save(_bundle(resource))

    assert outside_record.read_bytes() == b"outside-record-must-not-change"
    displaced_record = tmp_path / "tokens-displaced" / store.path.name
    assert json.loads(displaced_record.read_text(encoding="utf-8"))["tokens"][
        "access_token"
    ] == "old-access"


def test_oauth_store_load_stays_on_open_directory_after_visible_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")
    store.save(_bundle(resource))
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_record = outside / store.path.name
    outside_record.write_bytes(
        store.path.read_bytes().replace(b"old-access", b"outside-access")
    )
    _redirect_visible_store_after_open(store, outside, monkeypatch)

    loaded = store.load(resource)

    assert loaded == _bundle(resource)
    assert b"outside-access" in outside_record.read_bytes()


def test_oauth_store_remove_stays_on_open_directory_after_visible_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")
    store.save(_bundle(resource))
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_record = outside / store.path.name
    outside_record.write_bytes(b"outside-record-must-survive")
    _redirect_visible_store_after_open(store, outside, monkeypatch)

    assert store.remove() is True

    assert outside_record.read_bytes() == b"outside-record-must-survive"
    assert not (tmp_path / "tokens-displaced" / store.path.name).exists()


def test_oauth_store_fails_closed_when_secure_store_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")
    store.save(_bundle(resource))
    before = store.path.read_bytes()

    def forbidden_open(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("unsupported private store used a pathname fallback")

    with monkeypatch.context() as isolated:
        isolated.setattr(
            private_store,
            "secure_private_store_available",
            lambda: False,
        )
        isolated.setattr(private_store.os, "open", forbidden_open)
        with pytest.raises(
            MCPOAuthError,
            match="secure MCP OAuth credential persistence is unavailable",
        ):
            store.save(_bundle(resource))
        with pytest.raises(
            MCPOAuthError,
            match="secure MCP OAuth credential persistence is unavailable",
        ):
            store.load(resource)
        with pytest.raises(
            MCPOAuthError,
            match="secure MCP OAuth credential persistence is unavailable",
        ):
            store.remove()
        assert store.credential_state(resource) == "unavailable"

    assert store.path.read_bytes() == before


def test_oauth_store_cleans_staged_file_after_failed_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")

    def fail_write(descriptor: int, payload: bytes) -> None:
        raise private_store.PrivateStoreError("forced private-store write failure")

    monkeypatch.setattr(private_store, "_write_all", fail_write)
    with pytest.raises(MCPOAuthError, match="forced private-store write failure"):
        store.save(_bundle(resource))

    assert list(store.directory.iterdir()) == []


def test_oauth_store_preserves_preexisting_temp_on_name_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")
    store.directory.mkdir()
    monkeypatch.setattr(private_store.secrets, "token_hex", lambda length: "collision")
    temporary = store.directory / f".{store.path.name}.collision.tmp"
    temporary.write_bytes(b"preexisting temporary entry")

    with pytest.raises(MCPOAuthError, match="unable to write"):
        store.save(_bundle(resource))

    assert temporary.read_bytes() == b"preexisting temporary entry"


def test_secure_store_capability_requires_fstat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(private_store.os, "fstat", raising=False)

    assert private_store.secure_private_store_available() is False


def test_oauth_store_runtime_unsupported_is_reported_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")
    store.save(_bundle(resource))

    def unsupported_open(*args: Any, **kwargs: Any) -> int:
        raise OSError(errno.ENOTSUP, "unsupported")

    monkeypatch.setattr(
        private_store,
        "secure_private_store_available",
        lambda: True,
    )
    monkeypatch.setattr(private_store.os, "open", unsupported_open)

    assert store.credential_state(resource) == "unavailable"


def test_oauth_store_read_io_failure_is_reported_without_path_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")
    store.save(_bundle(resource))

    def fail_read(*args: Any, **kwargs: Any) -> bytes:
        raise OSError("forced read failure")

    monkeypatch.setattr(private_store.os, "read", fail_read)

    with pytest.raises(MCPOAuthError, match="unable to read"):
        store.load(resource)
    assert store.credential_state(resource) == "invalid"


def test_oauth_credential_state_reports_health_without_secrets(tmp_path: Path) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")

    assert store.credential_state(resource) == "missing"

    store.save(_bundle(resource, expired=True))
    assert store.credential_state(resource) == "expired"

    store.save(_bundle(resource))
    assert store.credential_state(resource) == "usable"

    record = json.loads(store.path.read_text(encoding="utf-8"))
    record["tokens"]["access_token"] = 123
    store.path.write_text(json.dumps(record), encoding="utf-8")
    assert store.credential_state(resource) == "invalid"


def test_oauth_store_rejects_coerced_credential_types(tmp_path: Path) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")
    store.save(_bundle(resource))
    record = json.loads(store.path.read_text(encoding="utf-8"))
    record["tokens"]["access_token"] = 123
    store.path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(MCPOAuthError, match="invalid OAuth credential record"):
        store.load(resource)


def test_oauth_store_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")
    store.save(_bundle(resource))
    raw = store.path.read_text(encoding="utf-8")
    raw = raw.replace(
        '"access_token":"old-access"',
        '"access_token":"first","access_token":"old-access"',
    )
    store.path.write_text(raw, encoding="utf-8")

    with pytest.raises(MCPOAuthError, match="invalid OAuth credential record"):
        store.load(resource)


def test_oauth_discovery_url_builders_preserve_resource_identity() -> None:
    resource = canonical_resource_uri("HTTPS://MCP.Example.Test/team/mcp?tenant=one")

    assert resource == "https://mcp.example.test/team/mcp?tenant=one"
    assert canonical_resource_uri("https://MCP.Example.Test") == (
        "https://mcp.example.test"
    )
    with pytest.raises(MCPOAuthError, match="fragments"):
        canonical_resource_uri("https://mcp.example.test/rpc#invalid")
    assert protected_resource_metadata_urls(resource) == (
        "https://mcp.example.test/.well-known/oauth-protected-resource/team/mcp",
        "https://mcp.example.test/.well-known/oauth-protected-resource",
    )
    assert authorization_metadata_urls("https://auth.example.test/issuer") == (
        "https://auth.example.test/.well-known/oauth-authorization-server/issuer",
        "https://auth.example.test/.well-known/openid-configuration/issuer",
        "https://auth.example.test/issuer/.well-known/openid-configuration",
    )


def test_bearer_challenge_parser_ignores_other_auth_schemes() -> None:
    challenge = (
        'Basic realm="scope=wrong", Bearer '
        'resource_metadata="https://mcp.example.test/oauth", '
        'scope="files:read files:write", error="insufficient_scope", '
        'Digest realm="resource_metadata=wrong"'
    )

    assert bearer_challenge_parameters(challenge) == {
        "resource_metadata": "https://mcp.example.test/oauth",
        "scope": "files:read files:write",
        "error": "insufficient_scope",
    }
    assert protected_resource_metadata_urls(
        "https://mcp.example.test/rpc", challenge
    ) == ("https://mcp.example.test/oauth",)


@pytest.mark.asyncio
async def test_oauth_discovery_tries_semantically_invalid_metadata_fallbacks() -> None:
    resource = "https://mcp.example.test/tenant/rpc"
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url == httpx.URL(resource):
            return httpx.Response(404)
        if request.url == httpx.URL(
            "https://mcp.example.test/.well-known/oauth-protected-resource/tenant/rpc"
        ):
            return httpx.Response(200, json={})
        if request.url == httpx.URL(
            "https://mcp.example.test/.well-known/oauth-protected-resource"
        ):
            return httpx.Response(
                200,
                json={
                    "resource": resource,
                    "authorization_servers": ["https://auth.example.test/tenant"],
                },
            )
        if request.url == httpx.URL(
            "https://auth.example.test/.well-known/oauth-authorization-server/tenant"
        ):
            return httpx.Response(200, json={"issuer": "wrong"})
        if request.url == httpx.URL(
            "https://auth.example.test/.well-known/openid-configuration/tenant"
        ):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.example.test/tenant",
                    "authorization_endpoint": "https://auth.example.test/authorize",
                    "token_endpoint": "https://auth.example.test/token",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    discovery = await discover_oauth(http, resource)

    assert discovery.issuer == "https://auth.example.test/tenant"
    assert requested[-2:] == [
        "https://auth.example.test/.well-known/oauth-authorization-server/tenant",
        "https://auth.example.test/.well-known/openid-configuration/tenant",
    ]
    await http.aclose()


@pytest.mark.asyncio
async def test_runtime_requires_explicit_login_without_opening_a_browser(
    tmp_path: Path,
) -> None:
    session = MCPOAuthSession(
        "protected",
        "https://mcp.example.test/rpc",
        store=MCPOAuthTokenStore("protected", tmp_path / "tokens"),
    )

    with pytest.raises(MCPAuthorizationRequired, match=r"ash mcp login protected"):
        await session.authorization_header()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
async def test_explicit_oauth_login_rejects_non_finite_timeout(timeout: float) -> None:
    with pytest.raises(MCPOAuthError, match="timeout must be positive and finite"):
        await authorize_mcp_server(
            "protected",
            "https://mcp.example.test/rpc",
            timeout_seconds=timeout,
        )


@pytest.mark.asyncio
async def test_full_oauth_flow_discovers_registers_uses_pkce_and_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = "https://mcp.example.test/rpc"
    metadata_url = "https://mcp.example.test/oauth-resource"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")
    observed: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL(resource):
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer resource_metadata="{metadata_url}", '
                        'scope="challenge:read"'
                    )
                },
            )
        if request.url == httpx.URL(metadata_url):
            return httpx.Response(
                200,
                json={
                    "resource": resource,
                    "authorization_servers": ["https://auth.example.test"],
                    "scopes_supported": ["fallback:scope"],
                },
            )
        if request.url == httpx.URL(
            "https://auth.example.test/.well-known/oauth-authorization-server"
        ):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.example.test",
                    "authorization_endpoint": "https://auth.example.test/authorize",
                    "token_endpoint": "https://auth.example.test/token",
                    "registration_endpoint": "https://auth.example.test/register",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if request.url == httpx.URL("https://auth.example.test/register"):
            observed["registration"] = json.loads(request.content)
            return httpx.Response(201, json={"client_id": "dynamic-client"})
        if request.url == httpx.URL("https://auth.example.test/token"):
            token_form = parse_qs(request.content.decode("ascii"))
            observed["token_form"] = token_form
            verifier = token_form["code_verifier"][0]
            expected = (
                __import__("base64")
                .urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            assert expected == observed["authorization"]["code_challenge"][0]
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "challenge:read",
                },
            )
        raise AssertionError(
            f"unexpected OAuth request: {request.method} {request.url}"
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    callback_handler: dict[str, Any] = {}

    class FakeSocket:
        def getsockname(self) -> tuple[str, int]:
            return ("127.0.0.1", 43123)

    class FakeServer:
        sockets = [FakeSocket()]

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def start_server(handler: Any, host: str, port: int) -> FakeServer:
        assert host == "127.0.0.1"
        assert port == 0
        callback_handler["handler"] = handler
        return FakeServer()

    monkeypatch.setattr(asyncio, "start_server", start_server)

    def opener(url: str) -> bool:
        query = parse_qs(urlparse(url).query)
        observed["authorization"] = query
        redirect = urlparse(query["redirect_uri"][0])

        async def callback() -> None:
            reader = asyncio.StreamReader()
            target = (
                redirect.path
                + "?"
                + urlencode({"code": "authorization-code", "state": query["state"][0]})
            )
            reader.feed_data(
                f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode()
            )
            reader.feed_eof()

            class Writer:
                def write(self, data: bytes) -> None:
                    observed["callback_response"] = data

                async def drain(self) -> None:
                    return None

                def close(self) -> None:
                    return None

                async def wait_closed(self) -> None:
                    return None

            await callback_handler["handler"](reader, Writer())

        asyncio.create_task(callback())
        return True

    bundle = await authorize_mcp_server(
        "remote",
        resource,
        oauth_config={"scope": "configured:scope"},
        store=store,
        http_client=http,
        open_browser=opener,
        announce=lambda message: None,
        timeout_seconds=5,
    )
    await http.aclose()

    assert bundle.client.client_id == "dynamic-client"
    assert bundle.discovery.scopes == ("challenge:read",)
    assert observed["authorization"]["scope"] == ["challenge:read"]
    assert observed["authorization"]["resource"] == [resource]
    assert observed["registration"]["redirect_uris"][0].startswith("http://127.0.0.1:")
    assert observed["token_form"]["resource"] == [resource]
    assert store.load(resource).tokens.access_token == "access-token"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_explicit_step_up_scope_overrides_initial_challenge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = "https://mcp.example.test/rpc"
    observed: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL(resource):
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer scope="files:read", '
                        'resource_metadata="https://mcp.example.test/oauth"'
                    )
                },
            )
        if request.url == httpx.URL("https://mcp.example.test/oauth"):
            return httpx.Response(
                200,
                json={
                    "resource": resource,
                    "authorization_servers": ["https://auth.example.test"],
                },
            )
        if request.url == httpx.URL(
            "https://auth.example.test/.well-known/oauth-authorization-server"
        ):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.example.test",
                    "authorization_endpoint": "https://auth.example.test/authorize",
                    "token_endpoint": "https://auth.example.test/token",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if request.url == httpx.URL("https://auth.example.test/token"):
            return httpx.Response(
                200,
                json={"access_token": "token", "token_type": "Bearer"},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    callback_handler: dict[str, Any] = {}

    class FakeSocket:
        def getsockname(self) -> tuple[str, int]:
            return ("127.0.0.1", 43123)

    class FakeServer:
        sockets = [FakeSocket()]

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def start_server(handler: Any, host: str, port: int) -> FakeServer:
        callback_handler["handler"] = handler
        return FakeServer()

    monkeypatch.setattr(asyncio, "start_server", start_server)

    def opener(url: str) -> bool:
        query = parse_qs(urlparse(url).query)
        observed["scope"] = query["scope"]

        async def callback() -> None:
            reader = asyncio.StreamReader()
            reader.feed_data(
                (
                    "GET /callback?"
                    + urlencode({"code": "code", "state": query["state"][0]})
                    + " HTTP/1.1\r\n\r\n"
                ).encode()
            )
            reader.feed_eof()
            writer = type(
                "Writer",
                (),
                {
                    "write": lambda self, data: None,
                    "drain": lambda self: asyncio.sleep(0),
                    "close": lambda self: None,
                    "wait_closed": lambda self: asyncio.sleep(0),
                },
            )()
            await callback_handler["handler"](reader, writer)

        asyncio.create_task(callback())
        return True

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await authorize_mcp_server(
        "remote",
        resource,
        oauth_config={"client_id": "registered"},
        store=MCPOAuthTokenStore("remote", tmp_path / "tokens"),
        http_client=http,
        open_browser=opener,
        announce=lambda message: None,
        timeout_seconds=5,
        requested_scope="files:read files:write",
    )

    assert observed["scope"] == ["files:read files:write"]
    await http.aclose()


@pytest.mark.asyncio
async def test_oauth_session_refreshes_rotates_and_persists(tmp_path: Path) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")
    store.save(_bundle(resource, expired=True))

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("ascii"))
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["old-refresh"]
        assert form["resource"] == [resource]
        assert form["client_secret"] == ["client-secret"]
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "rotated-refresh",
                "token_type": "Bearer",
                "expires_in": 1200,
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = MCPOAuthSession("remote", resource, store=store, http_client=http)

    assert await session.authorization_header() == "Bearer new-access"
    persisted = store.load(resource)
    assert persisted is not None
    assert persisted.tokens.refresh_token == "rotated-refresh"
    assert persisted.tokens.expires_at > 0
    await http.aclose()


@pytest.mark.asyncio
async def test_oauth_refresh_surfaces_secure_persistence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")
    store.save(_bundle(resource, expired=True))
    refresh_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_count
        refresh_count += 1
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "token_type": "Bearer",
                "expires_in": 1200,
            },
        )

    capability_calls = 0

    def available_once() -> bool:
        nonlocal capability_calls
        capability_calls += 1
        return capability_calls == 1

    monkeypatch.setattr(
        private_store,
        "secure_private_store_available",
        available_once,
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = MCPOAuthSession("remote", resource, store=store, http_client=http)

    with pytest.raises(
        MCPOAuthStoreUnavailable,
        match="secure MCP OAuth credential persistence is unavailable",
    ):
        await session.authorization_header()

    assert refresh_count == 1
    monkeypatch.setattr(
        private_store,
        "secure_private_store_available",
        lambda: True,
    )
    assert store.load(resource) == _bundle(resource, expired=True)
    await http.aclose()


@pytest.mark.asyncio
async def test_oauth_refresh_uses_rotated_configured_client_secret(
    tmp_path: Path,
) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")
    store.save(_bundle(resource, expired=True))

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("ascii"))
        assert form["client_id"] == ["client-id"]
        assert form["client_secret"] == ["rotated-secret"]
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "token_type": "Bearer",
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = MCPOAuthSession(
        "remote",
        resource,
        oauth_config={"client_id": "client-id", "client_secret": "rotated-secret"},
        store=store,
        http_client=http,
    )

    assert await session.authorization_header() == "Bearer new-access"
    persisted = store.load(resource)
    assert persisted is not None
    assert persisted.client.client_secret == "rotated-secret"
    await http.aclose()


@pytest.mark.asyncio
async def test_concurrent_401_refreshes_rotate_token_once(tmp_path: Path) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")
    store.save(_bundle(resource))
    refresh_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_count
        refresh_count += 1
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "token_type": "Bearer",
                "expires_in": 1200,
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = MCPOAuthSession("remote", resource, store=store, http_client=http)

    headers = await asyncio.gather(
        session.authorization_header(
            force_refresh=True, rejected_access_token="old-access"
        ),
        session.authorization_header(
            force_refresh=True, rejected_access_token="old-access"
        ),
    )

    assert headers == ["Bearer new-access", "Bearer new-access"]
    assert refresh_count == 1
    await http.aclose()


@pytest.mark.asyncio
async def test_oauth_token_response_is_bounded_while_streaming(tmp_path: Path) -> None:
    resource = "https://mcp.example.test/rpc"
    store = MCPOAuthTokenStore("remote", tmp_path / "tokens")
    store.save(_bundle(resource, expired=True))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b'{"access_token":"' + (b"x" * 1_000_001) + b'"}',
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = MCPOAuthSession("remote", resource, store=store, http_client=http)

    with pytest.raises(MCPAuthorizationRequired, match="token refresh failed"):
        await session.authorization_header()
    await http.aclose()


@pytest.mark.asyncio
async def test_mcp_client_retries_one_401_with_refreshed_oauth() -> None:
    class FakeOAuth:
        def __init__(self) -> None:
            self.http_client = None
            self.calls: list[bool] = []
            self.refreshed = False

        async def authorization_header(
            self,
            *,
            force_refresh: bool = False,
            rejected_access_token: str = "",
        ) -> str:
            self.calls.append(force_refresh)
            if force_refresh:
                assert rejected_access_token == "stale"
            if force_refresh:
                self.refreshed = True
            return "Bearer fresh" if self.refreshed else "Bearer stale"

    oauth = FakeOAuth()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers.get("Authorization", "")
        seen.append(token)
        payload = json.loads(request.content)
        if token == "Bearer stale":
            return httpx.Response(401)
        if "id" not in payload:
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                },
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
            auth="oauth",
        ),
        http_client=http,
        oauth_session=oauth,  # type: ignore[arg-type]
    )

    await client.connect()
    await client.disconnect()
    await http.aclose()

    assert seen.count("Bearer stale") == 2
    assert "Bearer fresh" in seen
    assert oauth.calls == [False, False, True, False]


@pytest.mark.asyncio
async def test_mcp_tool_call_401_is_not_reposted_after_oauth_refresh() -> None:
    class FakeOAuth:
        def __init__(self) -> None:
            self.http_client = None
            self.token = "fresh"
            self.calls: list[bool] = []

        async def authorization_header(
            self,
            *,
            force_refresh: bool = False,
            rejected_access_token: str = "",
        ) -> str:
            del rejected_access_token
            self.calls.append(force_refresh)
            return f"Bearer {self.token}"

    oauth = FakeOAuth()
    tool_posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tool_posts
        payload = json.loads(request.content)
        if payload["method"] == "ping":
            return httpx.Response(401)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                    },
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        tool_posts += 1
        return httpx.Response(401)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="remote",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
            auth="oauth",
        ),
        http_client=http,
        oauth_session=oauth,  # type: ignore[arg-type]
    )
    await client.connect()
    oauth.token = "stale"
    try:
        with pytest.raises(MCPAuthorizationRequired, match="not replayed"):
            await client.call_tool("write", {})
        assert tool_posts == 1
        assert oauth.calls == [False, False, False, False]
    finally:
        await client.disconnect()
        await http.aclose()


@pytest.mark.asyncio
async def test_mcp_client_reports_insufficient_scope_without_interaction() -> None:
    class FakeOAuth:
        http_client = None

        async def authorization_header(
            self,
            *,
            force_refresh: bool = False,
            rejected_access_token: str = "",
        ) -> str:
            return "Bearer token"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={
                "WWW-Authenticate": (
                    'Bearer error="insufficient_scope", scope="files:read files:write"'
                )
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = MCPClient(
        MCPServerConfig(
            name="protected",
            command="",
            args=[],
            env={},
            transport="http",
            url="https://mcp.example.test/rpc",
            auth="oauth",
        ),
        http_client=http,
        oauth_session=FakeOAuth(),  # type: ignore[arg-type]
    )

    with pytest.raises(MCPAuthorizationRequired, match="files:read files:write"):
        await client.connect()
    await client.disconnect()
    await http.aclose()
