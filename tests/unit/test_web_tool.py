from __future__ import annotations

import socket

import httpx
import pytest

from ash.safety.guard import SafetyGuard
from ash.safety.policy import PermissionPolicy, PolicyAction
from ash.tools.web import WebFetchTool, _validate_public_url


@pytest.fixture
def guard(tmp_path):
    return SafetyGuard(tmp_path)


@pytest.mark.asyncio
async def test_web_fetch_returns_bounded_html_text(monkeypatch, guard) -> None:
    monkeypatch.setattr("ash.tools.web._ensure_public_host", lambda hostname: None)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("ash-web-fetch")
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


@pytest.mark.asyncio
async def test_web_fetch_validates_redirect_targets(monkeypatch, guard) -> None:
    from ash.tools import web

    original = web._ensure_public_host

    def allow_example_only(hostname: str) -> None:
        if hostname == "example.com":
            return None
        return original(hostname)

    monkeypatch.setattr("ash.tools.web._ensure_public_host", allow_example_only)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    tool = WebFetchTool(guard, transport=httpx.MockTransport(handler))
    result = await tool.run(url="https://example.com")

    assert result.success is False
    assert "non-public" in (result.error or "")


@pytest.mark.asyncio
async def test_web_fetch_enforces_allowed_domains(monkeypatch, guard) -> None:
    monkeypatch.setattr("ash.tools.web._ensure_public_host", lambda hostname: None)

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
    monkeypatch.setattr("ash.tools.web._ensure_public_host", lambda hostname: None)

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


def test_web_fetch_requires_approval_in_interactive_policy() -> None:
    decision = PermissionPolicy("interactive").evaluate(
        "web_fetch", {"url": "https://example.com"}
    )
    assert decision.action == PolicyAction.ASK
