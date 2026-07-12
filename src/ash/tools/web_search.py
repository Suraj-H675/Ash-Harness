"""Provider-neutral web search with bounded, normalized source results."""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from threading import RLock
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, field_validator

from ash.core.redaction import redact_text
from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult, count_output_tokens
from ash.tools.web import _host_allowed, _normalize_allowed_domains


BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
MAX_SEARCH_RESPONSE_BYTES = 2_000_000
WEB_SEARCH_PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
Freshness = Literal["any", "day", "week", "month", "year"]


@dataclass(frozen=True)
class WebSearchHit:
    title: str
    url: str
    snippet: str
    published_at: str = ""


class WebSearchBackendError(RuntimeError):
    """A safe provider failure suitable for returning to the model."""


class WebSearchProvider(ABC):
    """One normalized search backend with a cheap credential probe."""

    name: str
    credential_env: str

    def available(self) -> bool:
        return bool(os.environ.get(self.credential_env, "").strip())

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        limit: int,
        freshness: Freshness,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> list[WebSearchHit]:
        """Return normalized search hits or raise ``WebSearchBackendError``."""


class BraveWebSearchProvider(WebSearchProvider):
    name = "brave"
    credential_env = "BRAVE_SEARCH_API_KEY"

    async def search(
        self,
        query: str,
        *,
        limit: int,
        freshness: Freshness,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> list[WebSearchHit]:
        api_key = os.environ.get(self.credential_env, "").strip()
        if not api_key:
            raise WebSearchBackendError(f"{self.credential_env} is not configured")
        params: dict[str, Any] = {
            "q": query,
            "count": limit,
            "safesearch": "moderate",
        }
        if freshness != "any":
            params["freshness"] = {
                "day": "pd",
                "week": "pw",
                "month": "pm",
                "year": "py",
            }[freshness]
        payload = await _request_json(
            self.name,
            "GET",
            BRAVE_SEARCH_URL,
            timeout=timeout,
            transport=transport,
            params=params,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
            },
        )
        web = payload.get("web", {})
        raw_results = web.get("results", []) if isinstance(web, dict) else []
        if not isinstance(raw_results, list):
            raise WebSearchBackendError("brave returned an invalid result list")
        return [
            WebSearchHit(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("description", "")),
                published_at=str(item.get("page_age", "")),
            )
            for item in raw_results[:limit]
            if isinstance(item, dict)
        ]


class TavilyWebSearchProvider(WebSearchProvider):
    name = "tavily"
    credential_env = "TAVILY_API_KEY"

    async def search(
        self,
        query: str,
        *,
        limit: int,
        freshness: Freshness,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> list[WebSearchHit]:
        api_key = os.environ.get(self.credential_env, "").strip()
        if not api_key:
            raise WebSearchBackendError(f"{self.credential_env} is not configured")
        body: dict[str, Any] = {
            "query": query,
            "max_results": limit,
            "search_depth": "basic",
            "topic": "general",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        if freshness != "any":
            body["time_range"] = freshness
        payload = await _request_json(
            self.name,
            "POST",
            TAVILY_SEARCH_URL,
            timeout=timeout,
            transport=transport,
            json=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        raw_results = payload.get("results", [])
        if not isinstance(raw_results, list):
            raise WebSearchBackendError("tavily returned an invalid result list")
        return [
            WebSearchHit(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("content", "")),
                published_at=str(item.get("published_date", "")),
            )
            for item in raw_results[:limit]
            if isinstance(item, dict)
        ]


async def _request_json(
    provider: str,
    method: str,
    url: str,
    *,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None,
    **kwargs: Any,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            async with client.stream(method, url, **kwargs) as response:
                if response.status_code == 401:
                    raise WebSearchBackendError(
                        f"{provider} rejected its configured credential"
                    )
                if response.status_code == 429:
                    raise WebSearchBackendError(f"{provider} is rate limited")
                if response.status_code >= 400:
                    raise WebSearchBackendError(
                        f"{provider} returned HTTP {response.status_code}"
                    )
                content_type = response.headers.get("content-type", "").casefold()
                if content_type and "json" not in content_type:
                    raise WebSearchBackendError(
                        f"{provider} returned a non-JSON content type"
                    )
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_length = int(content_length)
                    except ValueError as exc:
                        raise WebSearchBackendError(
                            f"{provider} returned an invalid Content-Length"
                        ) from exc
                    if declared_length < 0:
                        raise WebSearchBackendError(
                            f"{provider} returned an invalid Content-Length"
                        )
                    if declared_length > MAX_SEARCH_RESPONSE_BYTES:
                        raise WebSearchBackendError(
                            f"{provider} response is larger than 2 MB"
                        )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_SEARCH_RESPONSE_BYTES:
                        raise WebSearchBackendError(
                            f"{provider} response is larger than 2 MB"
                        )
                    chunks.append(chunk)
                raw_response = b"".join(chunks)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        reason = (
            "timed out" if isinstance(exc, httpx.TimeoutException) else "is unreachable"
        )
        raise WebSearchBackendError(f"{provider} {reason}") from exc
    try:
        payload = json.loads(raw_response)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebSearchBackendError(f"{provider} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise WebSearchBackendError(f"{provider} returned a non-object response")
    return payload


class WebSearchRegistry:
    """Thread-safe search provider registry with deterministic auto selection."""

    def __init__(self) -> None:
        self._providers: dict[str, WebSearchProvider] = {}
        self._lock = RLock()

    def register(self, provider: WebSearchProvider, *, replace: bool = False) -> None:
        name = provider.name.strip().casefold()
        if not WEB_SEARCH_PROVIDER_NAME.fullmatch(name):
            raise ValueError(
                "web search provider name must be path-safe lowercase text"
            )
        with self._lock:
            if name in self._providers and not replace:
                raise ValueError(f"web search provider {name!r} is already registered")
            self._providers[name] = provider

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._providers))

    def resolve(self, selection: str) -> list[WebSearchProvider]:
        normalized = selection.strip().casefold()
        with self._lock:
            providers = dict(self._providers)
        if normalized != "auto":
            provider = providers.get(normalized)
            if provider is None:
                raise WebSearchBackendError(
                    f"unknown web search provider {normalized!r}; available: "
                    + ", ".join(sorted(providers))
                )
            if not provider.available():
                raise WebSearchBackendError(
                    f"{provider.credential_env} is required for web search provider "
                    f"{provider.name!r}"
                )
            return [provider]
        preferred = ("brave", "tavily")
        candidates = [providers[name] for name in preferred if name in providers]
        candidates.extend(
            providers[name] for name in sorted(providers) if name not in preferred
        )
        available = [provider for provider in candidates if provider.available()]
        if not available:
            raise WebSearchBackendError(
                "web search needs BRAVE_SEARCH_API_KEY or TAVILY_API_KEY in ~/.ash/.env"
            )
        return available


def create_default_web_search_registry() -> WebSearchRegistry:
    registry = WebSearchRegistry()
    registry.register(BraveWebSearchProvider())
    registry.register(TavilyWebSearchProvider())
    return registry


class WebSearchArgs(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    limit: int = Field(8, ge=1, le=20)
    freshness: Freshness = "any"

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("web search query cannot be blank")
        return normalized


class WebSearchTool(BaseTool):
    name = "web_search"
    description = (
        "Search the live public web through a configured search provider and return "
        "bounded source titles, URLs, snippets, dates, and provider provenance."
    )
    args_schema = WebSearchArgs

    def __init__(
        self,
        safety_guard: SafetyGuard,
        *,
        provider: str = "auto",
        timeout: float = 20.0,
        allowed_domains: list[str] | tuple[str, ...] | None = None,
        registry: WebSearchRegistry | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(safety_guard)
        if not 1.0 <= timeout <= 120.0:
            raise ValueError("web search timeout must be between 1 and 120 seconds")
        self.provider = provider.strip().casefold()
        self.timeout = timeout
        self._allowed_domains = _normalize_allowed_domains(allowed_domains or ())
        self._registry = registry or create_default_web_search_registry()
        self._transport = transport

    async def run(self, **kwargs: Any) -> ToolResult:
        args = WebSearchArgs(**kwargs)
        try:
            providers = self._registry.resolve(self.provider)
        except WebSearchBackendError as exc:
            return ToolResult(success=False, output="", error=str(exc))
        errors: list[str] = []
        for provider in providers:
            try:
                raw_hits = await provider.search(
                    args.query,
                    limit=args.limit,
                    freshness=args.freshness,
                    timeout=self.timeout,
                    transport=self._transport,
                )
            except WebSearchBackendError as exc:
                errors.append(str(exc))
                continue
            hits = [
                hit
                for hit in raw_hits
                if _result_url_allowed(hit.url, self._allowed_domains)
            ][: args.limit]
            output = json.dumps(
                {
                    "provider": provider.name,
                    "query": redact_text(args.query),
                    "results": [asdict(hit) for hit in hits],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            self.emit_event(
                {
                    "type": "web.search.completed",
                    "provider": provider.name,
                    "query": redact_text(args.query),
                    "result_count": len(hits),
                }
            )
            return ToolResult(
                success=True,
                output=output,
                token_count=count_output_tokens(output),
            )
        return ToolResult(
            success=False,
            output="",
            error="web search failed: " + "; ".join(errors),
        )


def _result_url_allowed(url: str, allowed_domains: tuple[str, ...]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    return not allowed_domains or _host_allowed(parsed.hostname, allowed_domains)
