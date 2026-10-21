from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import AsyncIterator

import httpx
import pytest

from ash.safety.guard import SafetyGuard
from ash.safety.policy import PermissionPolicy, PolicyAction
from ash.tools.web import (
    WebFetchTool,
    _PinnedPublicTransport,
    _resolve_public_addresses,
    _resolve_public_addresses_with_timeout,
    _validate_public_url,
)


class _SyntheticStream(httpx.AsyncByteStream):
    def __init__(self, *, drip: bool = False) -> None:
        self.started = asyncio.Event()
        self.closed = False
        self.drip = drip

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            self.started.set()
            yield b"x"
            if self.drip:
                await asyncio.sleep(0)
            else:
                await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def guard(tmp_path):
    return SafetyGuard(tmp_path)


async def _allow_public_dns(
    hostname: str,
    *,
    timeout_seconds: float,
) -> tuple[str, ...]:
    del hostname, timeout_seconds
    return ("93.184.216.34",)


@pytest.mark.asyncio
async def test_web_fetch_returns_bounded_html_text(monkeypatch, guard) -> None:
    monkeypatch.setattr(
        "ash.tools.web._resolve_public_addresses_with_timeout",
        _allow_public_dns,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("Mozilla/5.0")
        assert request.headers["user-agent"].endswith("Ash-WebFetch/0.1")
        assert request.headers["accept-language"] == "en-US,en;q=0.9"
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body><h1>Hello</h1><script>bad()</script><p>World</p></body></html>",
        )

    tool = WebFetchTool(guard, transport=httpx.MockTransport(handler))
    result = await tool.run(url="https://example.com/page", max_chars=10)

    assert result.success is True
    assert "Hello" in result.output
    assert "bad" not in result.output
    assert result.truncated is True
    assert result.citations == [
        {
            "title": "",
            "url": "https://example.com/page",
            "status_code": 200,
            "content_type": "text/html",
        }
    ]


@pytest.mark.asyncio
async def test_web_fetch_prefers_readable_main_content_over_site_chrome(
    monkeypatch,
    guard,
) -> None:
    monkeypatch.setattr(
        "ash.tools.web._resolve_public_addresses_with_timeout",
        _allow_public_dns,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                "<html><head><title>Site title</title></head><body>"
                "<nav>Docs Products Pricing Login</nav>"
                "<main><h1>Install Ash</h1><p>Run the installer.</p>"
                "<article><p>Then verify the CLI.</p></article></main>"
                "<aside>Related marketing links</aside>"
                "<footer>Copyright and legal links</footer>"
                "</body></html>"
            ),
        )

    result = await WebFetchTool(
        guard,
        transport=httpx.MockTransport(handler),
    ).run(url="https://example.com/docs")

    assert result.success is True
    assert "Install Ash" in result.output
    assert "Run the installer." in result.output
    assert "Then verify the CLI." in result.output
    assert "Docs Products Pricing Login" not in result.output
    assert "Related marketing links" not in result.output
    assert "Copyright and legal links" not in result.output
    assert "Site title" not in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("content_length", ["invalid", "-1"])
async def test_web_fetch_rejects_invalid_content_length(
    monkeypatch,
    guard,
    content_length: str,
) -> None:
    monkeypatch.setattr(
        "ash.tools.web._resolve_public_addresses_with_timeout",
        _allow_public_dns,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "text/plain",
                "content-length": content_length,
            },
            content=b"ok",
        )

    result = await WebFetchTool(
        guard,
        transport=httpx.MockTransport(handler),
    ).run(url="https://example.com/page")

    assert result.success is False
    assert "invalid Content-Length" in (result.error or "")


@pytest.mark.asyncio
async def test_web_fetch_validates_redirect_targets(monkeypatch, guard) -> None:
    async def allow_example_only(
        hostname: str,
        *,
        timeout_seconds: float,
    ) -> tuple[str, ...]:
        del timeout_seconds
        if hostname == "example.com":
            return ("93.184.216.34",)
        return _resolve_public_addresses(hostname)

    monkeypatch.setattr(
        "ash.tools.web._resolve_public_addresses_with_timeout",
        allow_example_only,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    tool = WebFetchTool(guard, transport=httpx.MockTransport(handler))
    result = await tool.run(url="https://example.com")

    assert result.success is False
    assert "non-public" in (result.error or "")


@pytest.mark.asyncio
async def test_web_fetch_redacts_signed_redirect_url_from_output_and_citation(
    monkeypatch,
    guard,
) -> None:
    monkeypatch.setattr(
        "ash.tools.web._resolve_public_addresses_with_timeout",
        _allow_public_dns,
    )
    marker = "redirect-signature-marker"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={
                    "location": (
                        "https://example.com/final?"
                        f"X-Amz-Signature={marker}&view=complete"
                    )
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="done",
        )

    tool = WebFetchTool(guard, transport=httpx.MockTransport(handler))
    result = await tool.run(url="https://example.com/start")

    assert result.success is True
    assert marker not in result.output
    assert "URL (sanitized):" in result.output
    assert "X-Amz-Signature=[REDACTED]" in result.output
    assert "view=complete" in result.output
    assert result.citations is not None
    assert marker not in result.citations[0]["url"]
    assert "X-Amz-Signature=[REDACTED]" in result.citations[0]["url"]
    assert result.citations[0]["url_is_sanitized"] is True


@pytest.mark.asyncio
async def test_web_fetch_marks_redacted_citation_url_as_sanitized(
    monkeypatch,
    guard,
) -> None:
    monkeypatch.setattr(
        "ash.tools.web._resolve_public_addresses_with_timeout",
        _allow_public_dns,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="done",
        )

    tool = WebFetchTool(guard, transport=httpx.MockTransport(handler))
    result = await tool.run(url="https://example.com/?code=200&view=complete")

    assert result.success is True
    assert "URL (sanitized):" in result.output
    assert "code=[REDACTED]" in result.output
    assert result.citations[0]["url_is_sanitized"] is True
    assert result.citations[0]["url"].endswith(
        "?code=[REDACTED]&view=complete"
    )


@pytest.mark.asyncio
async def test_web_fetch_redacts_signed_url_from_http_error(monkeypatch, guard) -> None:
    monkeypatch.setattr(
        "ash.tools.web._resolve_public_addresses_with_timeout",
        _allow_public_dns,
    )
    marker = "error-signature-marker"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={
                    "location": (
                        "https://example.com/final?"
                        f"sig={marker}&view=complete"
                    )
                },
            )
        return httpx.Response(
            500,
            headers={"content-type": "text/plain"},
            text="failed",
        )

    tool = WebFetchTool(guard, transport=httpx.MockTransport(handler))
    result = await tool.run(url="https://example.com/start")

    assert result.success is False
    assert result.error is not None
    assert marker not in result.error
    assert "sig=[REDACTED]" in result.error


@pytest.mark.asyncio
async def test_web_fetch_enforces_allowed_domains(monkeypatch, guard) -> None:
    monkeypatch.setattr(
        "ash.tools.web._resolve_public_addresses_with_timeout",
        _allow_public_dns,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="allowed",
        )

    tool = WebFetchTool(
        guard,
        transport=httpx.MockTransport(handler),
        allowed_domains=["example.com", "*.docs.example"],
    )
    allowed_exact = await tool.run(url="https://example.com/page")
    allowed_wildcard = await tool.run(url="https://api.docs.example/page")
    blocked = await tool.run(url="https://blocked.example/page")

    assert allowed_exact.success is True
    assert allowed_wildcard.success is True
    assert blocked.success is False
    assert "allowed_web_domains" in (blocked.error or "")


@pytest.mark.asyncio
async def test_web_fetch_rejects_redirect_outside_allowed_domains(
    monkeypatch,
    guard,
) -> None:
    monkeypatch.setattr(
        "ash.tools.web._resolve_public_addresses_with_timeout",
        _allow_public_dns,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://other.example/final"},
        )

    tool = WebFetchTool(
        guard,
        transport=httpx.MockTransport(handler),
        allowed_domains=["example.com"],
    )
    result = await tool.run(url="https://example.com/start")

    assert result.success is False
    assert "allowed_web_domains" in (result.error or "")


@pytest.mark.asyncio
async def test_web_fetch_returns_network_failures_as_tool_results(
    monkeypatch,
    guard,
) -> None:
    monkeypatch.setattr(
        "ash.tools.web._resolve_public_addresses_with_timeout",
        _allow_public_dns,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    tool = WebFetchTool(guard, transport=httpx.MockTransport(handler))
    result = await tool.run(url="https://example.com/unavailable")

    assert result.success is False
    assert result.output == ""
    assert result.error == "connection refused"


def test_web_fetch_rejects_private_and_non_http_hosts(monkeypatch) -> None:
    with monkeypatch.context() as mp:
        mp.setattr("ash.tools.web._ensure_public_host", lambda hostname: None)
        assert "https://example.com" == _validate_public_url("https://example.com")
    with pytest.raises(ValueError, match="Only http"):
        _validate_public_url("file:///etc/passwd")
    with pytest.raises(ValueError, match="non-public"):
        _validate_public_url("http://127.0.0.1")

    def fake_getaddrinfo(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="non-public"):
        _validate_public_url("https://private.example")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "expected_error"),
    [
        ("https://@example.com/path", "URLs cannot contain embedded credentials"),
        ("https://:@example.com/path", "URLs cannot contain embedded credentials"),
        ("https://example.com:invalid/path", "Invalid HTTP(S) URL"),
        ("https://example.com:99999/path", "Invalid HTTP(S) URL"),
        ("https://exa\nmple.com/path", "Invalid HTTP(S) URL"),
    ],
)
async def test_web_fetch_rejects_malformed_authorities_before_dns_or_dispatch(
    monkeypatch, guard, url: str, expected_error: str
) -> None:
    requests: list[httpx.Request] = []
    lookups: list[str] = []

    async def lookup(hostname: str, *, timeout_seconds: float) -> tuple[str, ...]:
        del timeout_seconds
        lookups.append(hostname)
        return ("93.184.216.34",)

    monkeypatch.setattr(
        "ash.tools.web._resolve_public_addresses_with_timeout", lookup
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    tool = WebFetchTool(
        guard,
        transport=httpx.MockTransport(handler),
    )
    result = await tool.run(url=url)

    assert result.success is False
    assert result.output == ""
    assert result.error == expected_error
    assert lookups == []
    assert requests == []


@pytest.mark.asyncio
async def test_web_fetch_accepts_at_sign_outside_url_authority(
    monkeypatch, guard
) -> None:
    monkeypatch.setattr(
        "ash.tools.web._resolve_public_addresses_with_timeout",
        _allow_public_dns,
    )
    url = "https://example.com/team@work?contact=user@example.com"
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, text="ok")

    result = await WebFetchTool(
        guard, transport=httpx.MockTransport(handler)
    ).run(url=url)

    assert result.success is True
    assert requests == [url]


@pytest.mark.parametrize("literal", ["100.64.0.1", "100.127.255.254"])
def test_web_fetch_rejects_shared_address_space(literal: str) -> None:
    with pytest.raises(ValueError, match="non-public"):
        _resolve_public_addresses(literal)


@pytest.mark.asyncio
async def test_public_dns_resolution_timeout_does_not_block_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()

    def stalled_resolver(hostname: str) -> tuple[str, ...]:
        del hostname
        release.wait(1)
        return ("93.184.216.34",)

    monkeypatch.setattr("ash.tools.web._resolve_public_addresses", stalled_resolver)
    try:
        with pytest.raises(ValueError, match="DNS resolution timed out"):
            await _resolve_public_addresses_with_timeout(
                "stalled.example", timeout_seconds=0.01
            )
    finally:
        release.set()


@pytest.mark.asyncio
async def test_public_dns_stalled_resolvers_have_bounded_thread_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.tools import web

    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(web, "_DNS_RESOLVER_SLOTS", slots)
    release = threading.Event()
    both_started = threading.Event()
    entered = 0
    lock = threading.Lock()

    def stalled_resolver(hostname: str) -> tuple[str, ...]:
        nonlocal entered
        del hostname
        with lock:
            entered += 1
            if entered == 2:
                both_started.set()
        release.wait(1)
        return ("93.184.216.34",)

    monkeypatch.setattr(web, "_resolve_public_addresses", stalled_resolver)
    first = asyncio.create_task(
        _resolve_public_addresses_with_timeout("first.example", timeout_seconds=0.05)
    )
    second = asyncio.create_task(
        _resolve_public_addresses_with_timeout("second.example", timeout_seconds=0.05)
    )
    try:
        assert await asyncio.to_thread(both_started.wait, 0.5)
        with pytest.raises(ValueError, match="DNS resolver capacity exhausted"):
            await _resolve_public_addresses_with_timeout(
                "third.example", timeout_seconds=0.05
            )
        assert entered == 2
        for task in (first, second):
            with pytest.raises(ValueError, match="DNS resolution timed out"):
                await task
    finally:
        release.set()

    for _ in range(2):
        assert await asyncio.to_thread(slots.acquire, timeout=1.0)
    slots.release()
    slots.release()
    assert await _resolve_public_addresses_with_timeout(
        "recovered.example", timeout_seconds=0.5
    ) == ("93.184.216.34",)


@pytest.mark.asyncio
async def test_web_fetch_dns_timeout_fails_before_transport_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    guard,
) -> None:
    from ash.tools import web

    dispatched = False

    async def timed_out(
        hostname: str,
        *,
        timeout_seconds: float,
    ) -> tuple[str, ...]:
        del timeout_seconds
        raise web._DNSResolutionTimeout(
            f"DNS resolution timed out for host {hostname!r}"
        )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal dispatched
        dispatched = True
        return httpx.Response(200, text="must not dispatch", request=request)

    monkeypatch.setattr(
        "ash.tools.web._resolve_public_addresses_with_timeout",
        timed_out,
    )
    tool = WebFetchTool(guard, transport=httpx.MockTransport(handler))

    result = await tool.run(url="https://stalled.example/page")

    assert result.success is False
    assert result.error == "web_fetch timed out"
    assert dispatched is False


@pytest.mark.asyncio
async def test_web_fetch_reports_httpx_timeout_without_echoing_request_url(
    monkeypatch: pytest.MonkeyPatch,
    guard,
) -> None:
    monkeypatch.setattr(
        "ash.tools.web._resolve_public_addresses_with_timeout",
        _allow_public_dns,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(
            f"request to {request.url} timed out",
            request=request,
        )

    result = await WebFetchTool(
        guard,
        transport=httpx.MockTransport(handler),
    ).run(url="https://example.com/page?token=fetch-secret")

    assert result.success is False
    assert result.output == ""
    assert result.error == "web_fetch timed out"
    assert "fetch-secret" not in (result.error or "")
    assert "https://" not in (result.error or "")


@pytest.mark.asyncio
async def test_web_fetch_bounds_never_ending_response_and_closes_stream(
    monkeypatch: pytest.MonkeyPatch,
    guard,
) -> None:
    from ash.tools import web

    monkeypatch.setattr(web, "WEB_FETCH_TOTAL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(web, "_resolve_public_addresses_with_timeout", _allow_public_dns)
    stream = _SyntheticStream(drip=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            stream=stream,
            request=request,
        )

    tool = WebFetchTool(guard, transport=httpx.MockTransport(handler))
    result = await asyncio.wait_for(
        tool.run(url="https://example.com/page?token=fetch-secret"),
        timeout=0.2,
    )

    assert stream.started.is_set()
    assert stream.closed is True
    assert result.success is False
    assert result.output == ""
    assert result.citations == []
    assert result.error == "web_fetch timed out"
    assert "fetch-secret" not in (result.error or "")
    assert "https://" not in (result.error or "")


@pytest.mark.asyncio
async def test_web_fetch_redirect_dns_stall_uses_one_total_budget(
    monkeypatch: pytest.MonkeyPatch,
    guard,
) -> None:
    from ash.tools import web

    monkeypatch.setattr(web, "WEB_FETCH_TOTAL_TIMEOUT_SECONDS", 0.01)
    dispatched: list[str] = []

    async def resolve(hostname: str, *, timeout_seconds: float) -> tuple[str, ...]:
        del timeout_seconds
        if hostname == "redirect.example":
            await asyncio.Event().wait()
        return ("93.184.216.34",)

    def handler(request: httpx.Request) -> httpx.Response:
        dispatched.append(request.url.host)
        return httpx.Response(
            302,
            headers={"location": "https://redirect.example/final"},
            request=request,
        )

    monkeypatch.setattr(web, "_resolve_public_addresses_with_timeout", resolve)
    tool = WebFetchTool(guard, transport=httpx.MockTransport(handler))
    result = await asyncio.wait_for(
        tool.run(url="https://example.com/start?token=fetch-secret"),
        timeout=0.2,
    )

    assert result.success is False
    assert result.error == "web_fetch timed out"
    assert "fetch-secret" not in (result.error or "")
    assert "redirect.example" not in (result.error or "")
    assert dispatched == ["example.com"]


@pytest.mark.asyncio
async def test_web_fetch_caller_cancellation_propagates_and_closes_stream(
    monkeypatch: pytest.MonkeyPatch,
    guard,
) -> None:
    from ash.tools import web

    monkeypatch.setattr(web, "_resolve_public_addresses_with_timeout", _allow_public_dns)
    stream = _SyntheticStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            stream=stream,
            request=request,
        )

    tool = WebFetchTool(guard, transport=httpx.MockTransport(handler))
    task = asyncio.create_task(tool.run(url="https://example.com/page"))
    await asyncio.wait_for(stream.started.wait(), timeout=0.2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed is True


@pytest.mark.asyncio
async def test_web_fetch_connects_with_preflight_pinned_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.tools import web

    observed: dict[str, object] = {}

    async def resolved(
        hostname: str,
        *,
        timeout_seconds: float,
    ) -> tuple[str, ...]:
        observed["hostname"] = hostname
        observed["timeout"] = timeout_seconds
        return ("93.184.216.34",)

    def pinned_transport(*, pinned_addresses: tuple[str, ...]):
        observed["addresses"] = pinned_addresses

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                text="pinned",
                request=request,
            )

        return httpx.MockTransport(handler)

    monkeypatch.setattr(
        "ash.tools.web._resolve_public_addresses_with_timeout",
        resolved,
    )
    monkeypatch.setattr(web, "_PinnedPublicTransport", pinned_transport)

    final_url, status, content_type, body = await web._fetch_public_text(
        "https://example.com/page"
    )

    assert final_url == "https://example.com/page"
    assert status == 200
    assert content_type == "text/plain"
    assert body == "pinned"
    assert observed == {
        "hostname": "example.com",
        "timeout": web.DNS_TIMEOUT_SECONDS,
        "addresses": ("93.184.216.34",),
    }


@pytest.mark.asyncio
async def test_pinned_transport_reuses_preflight_addresses_without_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _PinnedPublicTransport(pinned_addresses=("93.184.216.34",))
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(204)

    transport._transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "ash.tools.web._resolve_public_addresses",
        lambda hostname: (_ for _ in ()).throw(AssertionError(f"unexpected DNS: {hostname}")),
    )
    try:
        response = await transport.handle_async_request(
            httpx.Request("POST", "https://example.com/hook")
        )
    finally:
        await transport.aclose()

    assert response.status_code == 204
    assert len(observed) == 1
    assert observed[0].url.host == "93.184.216.34"
    assert observed[0].headers["host"] == "example.com"


def test_web_fetch_accepts_global_ipv4_and_ipv6() -> None:
    assert _resolve_public_addresses("93.184.216.34") == ("93.184.216.34",)
    assert _resolve_public_addresses("2001:4860:4860::8888") == (
        "2001:4860:4860::8888",
    )


def test_web_fetch_rejects_mixed_public_and_non_global_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mixed_getaddrinfo(host, port, type=0):
        del host, port
        return [
            (socket.AF_INET, type or socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),
            (socket.AF_INET, type or socket.SOCK_STREAM, 0, "", ("100.64.0.1", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mixed_getaddrinfo)
    with pytest.raises(ValueError, match="non-public"):
        _resolve_public_addresses("mixed.example")


@pytest.mark.asyncio
async def test_web_fetch_pins_vetted_dns_result_against_rebinding(
    monkeypatch, guard
) -> None:
    from ash.tools import web

    original = socket.getaddrinfo
    resolutions = 0
    observed_addresses: list[tuple[str, ...]] = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        nonlocal resolutions
        hostname = host.decode() if isinstance(host, bytes) else host
        if hostname != "rebind.test":
            return original(host, port, *args, **kwargs)
        resolutions += 1
        address = "93.184.216.34" if resolutions == 1 else "127.0.0.1"
        socket_type = kwargs.get("type", socket.SOCK_STREAM)
        return [
            (
                socket.AF_INET,
                socket_type,
                socket.IPPROTO_TCP,
                "",
                (address, port or 0),
            )
        ]

    def pinned_transport(*, pinned_addresses: tuple[str, ...]):
        observed_addresses.append(pinned_addresses)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                text="vetted-public-endpoint",
                request=request,
            )

        return httpx.MockTransport(handler)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(web, "_PinnedPublicTransport", pinned_transport)

    _final_url, _status, _content_type, body = await web._fetch_public_text(
        "http://rebind.test/private"
    )

    assert body == "vetted-public-endpoint"
    assert resolutions == 1
    assert observed_addresses == [("93.184.216.34",)]


def test_web_fetch_rejects_embedded_url_credentials(monkeypatch) -> None:
    monkeypatch.setattr("ash.tools.web._ensure_public_host", lambda hostname: None)
    with pytest.raises(ValueError, match="embedded credentials"):
        _validate_public_url("https://alice:credential-value@example.com/private")


def test_web_fetch_requires_approval_in_interactive_policy() -> None:
    decision = PermissionPolicy("interactive").evaluate(
        "web_fetch", {"url": "https://example.com"}
    )
    assert decision.action == PolicyAction.ASK
