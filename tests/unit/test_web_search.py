from __future__ import annotations

import json

import httpx
import pytest

from ash.safety.guard import SafetyGuard
from ash.safety.policy import PermissionPolicy, PolicyAction
from ash.tools.web_search import WebSearchTool


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
async def test_web_search_rejects_blank_queries(tmp_path) -> None:
    tool = WebSearchTool(SafetyGuard(tmp_path))

    with pytest.raises(ValueError, match="cannot be blank"):
        await tool.run(query="   ")


def test_web_search_requires_interactive_approval() -> None:
    decision = PermissionPolicy("interactive").evaluate(
        "web_search", {"query": "latest release"}
    )

    assert decision.action == PolicyAction.ASK
