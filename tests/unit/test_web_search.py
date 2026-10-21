from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from ash.safety.guard import SafetyGuard
from ash.safety.policy import PermissionPolicy, PolicyAction
from ash.tools.web_search import WebSearchTool


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


@pytest.mark.asyncio
async def test_brave_search_is_bounded_filtered_and_emits_provenance(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.search.brave.com"
        assert request.url.params["q"] == "current MCP specification"
        assert request.url.params["count"] == "3"
        assert request.url.params["freshness"] == "pw"
        assert request.url.params["safesearch"] == "moderate"
        assert request.url.params["text_decorations"] == "false"
        assert request.headers["x-subscription-token"] == "brave-test-key"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "MCP",
                            "url": "https://docs.example/mcp",
                            "description": "Protocol docs",
                            "page_age": "2 days ago",
                        },
                        {
                            "title": "Blocked",
                            "url": "https://blocked.example/mcp",
                            "description": "Not allowed",
                        },
                        {
                            "title": "Unsafe",
                            "url": "javascript:alert(1)",
                            "description": "Invalid URL",
                        },
                    ]
                }
            },
        )

    events: list[dict] = []
    tool = WebSearchTool(
        SafetyGuard(tmp_path),
        allowed_domains=["docs.example"],
        transport=httpx.MockTransport(handler),
    )
    tool.set_event_sink(events.append)

    result = await tool.run(
        query=" current MCP specification ",
        limit=3,
        freshness="week",
    )
    payload = json.loads(result.output)

    assert result.success is True
    assert payload == {
        "provider": "brave",
        "query": "current MCP specification",
        "results": [
            {
                "published_at": "2 days ago",
                "snippet": "Protocol docs",
                "title": "MCP",
                "url": "https://docs.example/mcp",
            }
        ],
    }
    assert events == [
        {
            "type": "web.search.completed",
            "provider": "brave",
            "query": "current MCP specification",
            "result_count": 1,
        }
    ]
    assert result.citations == [
        {
            "title": "MCP",
            "url": "https://docs.example/mcp",
            "snippet_sha256": __import__("hashlib").sha256(
                b"Protocol docs"
            ).hexdigest(),
            "published_at": "2 days ago",
            "provider": "brave",
        }
    ]


@pytest.mark.asyncio
async def test_auto_search_falls_back_to_tavily_after_brave_rate_limit(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-test-key")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.host)
        if request.url.host == "api.search.brave.com":
            return httpx.Response(429, json={"error": "contains-secret-details"})
        assert request.url.host == "api.tavily.com"
        assert request.headers["authorization"] == "Bearer tavily-test-key"
        body = json.loads(request.content)
        assert body["query"] == "release news"
        assert body["max_results"] == 2
        assert body["time_range"] == "day"
        assert body["include_raw_content"] is False
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Release",
                        "url": "https://example.com/release",
                        "content": "A new release.",
                        "published_date": "2026-07-12",
                    }
                ]
            },
        )

    tool = WebSearchTool(
        SafetyGuard(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    result = await tool.run(query="release news", limit=2, freshness="day")
    payload = json.loads(result.output)

    assert result.success is True
    assert payload["provider"] == "tavily"
    assert requests == ["api.search.brave.com", "api.tavily.com"]
    assert "contains-secret-details" not in result.output


@pytest.mark.asyncio
async def test_web_search_provider_uses_public_dns_pinned_transport(
    tmp_path,
    monkeypatch,
) -> None:
    from ash.tools import web_search

    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-key")
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
            assert request.url.host == "api.search.brave.com"
            assert request.headers["x-subscription-token"] == "brave-test-key"
            assert request.extensions["timeout"] == {
                "connect": 12.0,
                "read": 12.0,
                "write": 12.0,
                "pool": 12.0,
            }
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"web": {"results": []}},
                request=request,
            )

        return httpx.MockTransport(handler)

    monkeypatch.setattr(
        web_search,
        "_resolve_public_addresses_with_timeout",
        resolved,
    )
    monkeypatch.setattr(web_search, "_PinnedPublicTransport", pinned_transport)

    result = await WebSearchTool(
        SafetyGuard(tmp_path),
        provider="brave",
        timeout=12,
    ).run(query="anything")

    assert result.success is True
    assert observed == {
        "hostname": "api.search.brave.com",
        "timeout": 5.0,
        "addresses": ("93.184.216.34",),
    }


@pytest.mark.asyncio
async def test_web_search_provider_dns_policy_fails_closed_before_dispatch(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-key")

    async def rejected(
        hostname: str,
        *,
        timeout_seconds: float,
    ) -> tuple[str, ...]:
        del timeout_seconds
        raise ValueError(f"Refusing non-public provider endpoint {hostname}")

    monkeypatch.setattr(
        "ash.tools.web_search._resolve_public_addresses_with_timeout",
        rejected,
    )

    from ash.tools import web_search

    constructed: list[bool] = []

    def unexpected_transport(*, pinned_addresses: tuple[str, ...]):
        del pinned_addresses
        constructed.append(True)
        raise AssertionError("transport must not be constructed after DNS rejection")

    monkeypatch.setattr(web_search, "_PinnedPublicTransport", unexpected_transport)

    result = await WebSearchTool(
        SafetyGuard(tmp_path),
        provider="brave",
    ).run(query="anything")

    assert result.success is False
    assert "public-network validation" in (result.error or "")
    assert "brave-test-key" not in (result.error or "")
    assert constructed == []


@pytest.mark.asyncio
async def test_web_search_reports_dns_capacity_as_resource_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.tools.web import _DNSResolverCapacityExhausted

    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-fake-key")

    async def exhausted(hostname: str, *, timeout_seconds: float) -> tuple[str, ...]:
        del hostname, timeout_seconds
        raise _DNSResolverCapacityExhausted("Public DNS resolver capacity exhausted")

    monkeypatch.setattr(
        "ash.tools.web_search._resolve_public_addresses_with_timeout", exhausted
    )
    result = await WebSearchTool(
        SafetyGuard(tmp_path), provider="brave"
    ).run(query="example")

    assert result.success is False
    assert result.error == (
        "web search failed: brave public DNS resolver capacity exhausted"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_kind", ["aggregate", "resolver"])
async def test_web_search_dns_deadlines_fail_before_transport_construction(
    tmp_path,
    monkeypatch,
    timeout_kind: str,
) -> None:
    from ash.tools import web_search
    from ash.tools.web import _DNSResolutionTimeout

    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-timeout-secret")
    constructed: list[bool] = []

    async def stalled_dns(
        hostname: str,
        *,
        timeout_seconds: float,
    ) -> tuple[str, ...]:
        del hostname, timeout_seconds
        if timeout_kind == "resolver":
            raise _DNSResolutionTimeout(
                "DNS resolution timed out for host provider.example"
            )
        await asyncio.Event().wait()
        return ("93.184.216.34",)

    def unexpected_transport(*, pinned_addresses: tuple[str, ...]):
        del pinned_addresses
        constructed.append(True)
        raise AssertionError("transport must not be constructed before DNS completes")

    monkeypatch.setattr(web_search, "_resolve_public_addresses_with_timeout", stalled_dns)
    monkeypatch.setattr(web_search, "_PinnedPublicTransport", unexpected_transport)
    tool = WebSearchTool(SafetyGuard(tmp_path), provider="brave", timeout=1)
    if timeout_kind == "aggregate":
        tool.timeout = 0.01

    result = await asyncio.wait_for(tool.run(query="search-secret-marker"), timeout=0.2)

    assert result.success is False
    assert result.output == ""
    assert result.error == "web search failed: brave timed out"
    assert "search-secret-marker" not in (result.error or "")
    assert "brave-timeout-secret" not in (result.error or "")
    assert "api.search.brave.com" not in (result.error or "")
    assert "provider.example" not in (result.error or "")
    assert constructed == []


@pytest.mark.asyncio
async def test_web_search_times_out_drip_stream_with_safe_error_and_cleanup(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-timeout-secret")
    stream = _SyntheticStream(drip=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=stream,
            request=request,
        )

    tool = WebSearchTool(
        SafetyGuard(tmp_path),
        provider="brave",
        transport=httpx.MockTransport(handler),
        timeout=1,
    )
    tool.timeout = 0.01
    result = await asyncio.wait_for(
        tool.run(query="search-secret-marker"),
        timeout=0.2,
    )

    assert stream.started.is_set()
    assert stream.closed is True
    assert result.success is False
    assert result.output == ""
    assert result.citations == []
    assert result.error == "web search failed: brave timed out"
    assert "search-secret-marker" not in (result.error or "")
    assert "brave-timeout-secret" not in (result.error or "")
    assert "api.search.brave.com" not in (result.error or "")


@pytest.mark.asyncio
async def test_auto_search_falls_back_after_timed_out_provider_and_closes_stream(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-fake-secret")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-fake-secret")
    stream = _SyntheticStream(drip=True)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.host)
        if request.url.host == "api.search.brave.com":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=stream,
                request=request,
            )
        assert request.url.host == "api.tavily.com"
        assert stream.closed is True
        return httpx.Response(
            200,
            json={"results": [{"title": "Fallback", "url": "https://example.com"}]},
            request=request,
        )

    tool = WebSearchTool(
        SafetyGuard(tmp_path),
        transport=httpx.MockTransport(handler),
        timeout=1,
    )
    tool.timeout = 0.01
    result = await asyncio.wait_for(tool.run(query="fallback test"), timeout=0.3)

    assert result.success is True
    assert json.loads(result.output)["provider"] == "tavily"
    assert requests == ["api.search.brave.com", "api.tavily.com"]
    assert stream.closed is True
    assert "brave-fake-secret" not in result.output
    assert "tavily-fake-secret" not in result.output


@pytest.mark.asyncio
async def test_web_search_caller_cancellation_propagates_and_closes_stream(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-key")
    stream = _SyntheticStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=stream,
            request=request,
        )

    tool = WebSearchTool(
        SafetyGuard(tmp_path),
        provider="brave",
        transport=httpx.MockTransport(handler),
        timeout=120,
    )
    task = asyncio.create_task(tool.run(query="anything"))
    await asyncio.wait_for(stream.started.wait(), timeout=0.2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed is True


@pytest.mark.asyncio
async def test_web_search_budget_bounds_sequential_pinned_address_attempts(
    monkeypatch,
) -> None:
    from ash.tools import web_search
    from ash.tools.web import _PinnedPublicTransport
    from ash.tools.web_search import WebSearchBackendError, _request_json

    addresses = tuple(f"93.184.216.{value}" for value in range(1, 41))
    attempts = 0

    class FailingTransport(httpx.AsyncBaseTransport):
        closed = False

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(0.005)
            raise httpx.ConnectTimeout("synthetic connect timeout", request=request)

        async def aclose(self) -> None:
            self.closed = True

    failing_transport = FailingTransport()
    pinned_transports: list[_PinnedPublicTransport] = []

    class TrackingPinnedTransport(_PinnedPublicTransport):
        def __init__(self, *, pinned_addresses: tuple[str, ...]) -> None:
            super().__init__(pinned_addresses=pinned_addresses)
            self._transport = failing_transport
            pinned_transports.append(self)

    async def resolved(
        hostname: str,
        *,
        timeout_seconds: float,
    ) -> tuple[str, ...]:
        del hostname, timeout_seconds
        return addresses

    monkeypatch.setattr(web_search, "_resolve_public_addresses_with_timeout", resolved)
    monkeypatch.setattr(web_search, "_PinnedPublicTransport", TrackingPinnedTransport)

    with pytest.raises(WebSearchBackendError, match="brave timed out"):
        await asyncio.wait_for(
            _request_json(
                "brave",
                "GET",
                "https://api.search.brave.com/search",
                timeout=0.02,
                transport=None,
                headers={"x-subscription-token": "synthetic-secret"},
            ),
            timeout=0.2,
        )

    assert 1 <= attempts < len(addresses)
    assert len(pinned_transports) == 1
    assert failing_transport.closed is True


@pytest.mark.asyncio
async def test_explicit_provider_does_not_fallback_and_missing_keys_are_actionable(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "available-but-not-selected")
    tool = WebSearchTool(SafetyGuard(tmp_path), provider="brave")

    result = await tool.run(query="anything")

    assert result.success is False
    assert result.output == ""
    assert "BRAVE_SEARCH_API_KEY" in (result.error or "")
    assert "TAVILY" not in (result.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_length", "expected"),
    [
        ("2000001", "larger than 2 MB"),
        ("invalid", "invalid Content-Length"),
    ],
)
async def test_web_search_rejects_invalid_provider_response_lengths(
    tmp_path,
    monkeypatch,
    content_length: str,
    expected: str,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-length": content_length,
            },
            content=b"{}",
        )

    tool = WebSearchTool(
        SafetyGuard(tmp_path),
        provider="brave",
        transport=httpx.MockTransport(handler),
    )

    result = await tool.run(query="anything")

    assert result.success is False
    assert expected in (result.error or "")


@pytest.mark.asyncio
async def test_web_search_rejects_duplicate_json_keys(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"web":{"results":[]},"web":{"results":[{"title":"x"}]}}',
        )

    tool = WebSearchTool(
        SafetyGuard(tmp_path),
        provider="brave",
        transport=httpx.MockTransport(handler),
    )

    result = await tool.run(query="anything")

    assert result.success is False
    assert "invalid JSON" in (result.error or "")


@pytest.mark.asyncio
async def test_web_search_redacts_signed_result_urls_and_text_secrets(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-key")
    signature = "signed-search-marker"
    provider_secret = "sk-proj-" + "A" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": f"title token={provider_secret}",
                            "url": (
                                "https://docs.example/page?"
                                f"X-Amz-Signature={signature}&view=complete"
                            ),
                            "description": f"snippet token={provider_secret}",
                            "page_age": f"date token={provider_secret}",
                        }
                    ]
                }
            },
        )

    tool = WebSearchTool(
        SafetyGuard(tmp_path),
        provider="brave",
        transport=httpx.MockTransport(handler),
    )
    result = await tool.run(query="anything")
    payload = json.loads(result.output)

    assert result.success is True
    serialized = json.dumps(payload)
    assert provider_secret not in serialized
    assert signature not in serialized
    assert "[REDACTED]" in payload["results"][0]["url"]
    assert "view=complete" in payload["results"][0]["url"]
    assert result.citations is not None
    assert signature not in result.citations[0]["url"]


@pytest.mark.asyncio
async def test_web_search_redacts_exact_configured_provider_credential_from_results(
    tmp_path,
    monkeypatch,
) -> None:
    api_key = "tiny-k"
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", api_key)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-subscription-token"] == api_key
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": f"provider echoed {api_key}",
                            "url": "https://example.com/result",
                            "description": f"snippet echoed {api_key}",
                            "page_age": f"published with {api_key}",
                        }
                    ]
                }
            },
        )

    tool = WebSearchTool(
        SafetyGuard(tmp_path),
        provider="brave",
        transport=httpx.MockTransport(handler),
    )

    result = await tool.run(query="anything")

    assert result.success is True
    assert api_key not in result.output
    assert result.citations is not None
    assert api_key not in json.dumps(result.citations)
    assert "[REDACTED]" in result.output


@pytest.mark.asyncio
async def test_web_search_drops_embedded_credential_result_urls(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "unsafe",
                            "url": "https://user:secret@example.com/private",
                            "description": "must be dropped",
                        },
                        {
                            "title": "safe",
                            "url": "https://example.com/public",
                            "description": "kept",
                        },
                    ]
                }
            },
        )

    tool = WebSearchTool(
        SafetyGuard(tmp_path),
        provider="brave",
        transport=httpx.MockTransport(handler),
    )
    result = await tool.run(query="anything", limit=2)
    payload = json.loads(result.output)

    assert result.success is True
    assert [item["title"] for item in payload["results"]] == ["safe"]
    assert "secret" not in result.output


@pytest.mark.asyncio
async def test_web_search_discards_malformed_provider_urls_without_losing_valid_hits(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-key")
    bad_urls = [
        "https://[invalid/path",
        "https://example.com:bad/path",
        "https://example.com:99999/path",
        "https://exa\nmple.com/path",
        "https://@example.com/private",
    ]
    valid_url = "https://example.com/team@work"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"title": "unsafe", "url": url, "description": "unusable"}
                        for url in bad_urls
                    ]
                    + [{"title": "safe", "url": valid_url, "description": "useful"}]
                }
            },
        )

    result = await WebSearchTool(
        SafetyGuard(tmp_path),
        provider="brave",
        transport=httpx.MockTransport(handler),
    ).run(query="anything", limit=6)

    assert result.success is True
    assert json.loads(result.output)["results"] == [
        {
            "title": "safe",
            "url": valid_url,
            "snippet": "useful",
            "published_at": "",
        }
    ]
    assert result.citations is not None
    assert [citation["url"] for citation in result.citations] == [valid_url]


@pytest.mark.asyncio
async def test_web_search_bounds_provider_result_fields(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "t" * 2_000,
                            "url": "https://example.com/result",
                            "description": "s" * 20_000,
                            "page_age": "d" * 1_000,
                        },
                        {
                            "title": "oversized-url",
                            "url": "https://example.com/" + ("x" * 5_000),
                            "description": "must be dropped",
                        },
                    ]
                }
            },
        )

    tool = WebSearchTool(
        SafetyGuard(tmp_path),
        provider="brave",
        transport=httpx.MockTransport(handler),
    )
    result = await tool.run(query="anything", limit=2)
    payload = json.loads(result.output)

    assert result.success is True
    assert len(payload["results"]) == 1
    hit = payload["results"][0]
    assert len(hit["title"]) == 500
    assert len(hit["snippet"]) == 4000
    assert len(hit["published_at"]) == 128


@pytest.mark.asyncio
async def test_web_search_normalizes_nullable_provider_fields(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Result",
                            "url": "https://example.com/result",
                            "description": None,
                            "page_age": None,
                        }
                    ]
                }
            },
        )

    result = await WebSearchTool(
        SafetyGuard(tmp_path),
        provider="brave",
        transport=httpx.MockTransport(handler),
    ).run(query="anything")
    payload = json.loads(result.output)

    assert result.success is True
    assert payload["results"][0]["snippet"] == ""
    assert payload["results"][0]["published_at"] == ""


@pytest.mark.asyncio
async def test_web_search_rejects_blank_queries(tmp_path) -> None:
    tool = WebSearchTool(SafetyGuard(tmp_path))

    with pytest.raises(ValueError, match="cannot be blank"):
        await tool.run(query="   ")


def test_web_search_requires_interactive_approval() -> None:
    decision = PermissionPolicy("interactive").evaluate(
        "web_search", {"query": "latest release"}
    )

    assert decision.action == PolicyAction.ASK
