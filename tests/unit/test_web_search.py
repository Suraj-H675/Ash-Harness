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
async def test_web_search_rejects_blank_queries(tmp_path) -> None:
    tool = WebSearchTool(SafetyGuard(tmp_path))

    with pytest.raises(ValueError, match="cannot be blank"):
        await tool.run(query="   ")


def test_web_search_requires_interactive_approval() -> None:
    decision = PermissionPolicy("interactive").evaluate(
        "web_search", {"query": "latest release"}
    )

    assert decision.action == PolicyAction.ASK
