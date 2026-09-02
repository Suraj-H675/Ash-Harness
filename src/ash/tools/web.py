"""Network fetch tools with SSRF and size protections."""

from __future__ import annotations

import ipaddress
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel, Field

from ash.tools.base import BaseTool, ToolResult, count_output_tokens


MAX_FETCH_BYTES = 1_000_000
DEFAULT_MAX_CHARS = 20_000
MAX_REDIRECTS = 5
TEXT_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/rss+xml",
)


class WebFetchArgs(BaseModel):
    url: str = Field(..., min_length=1, description="HTTP or HTTPS URL to fetch.")
    max_chars: int = Field(
        DEFAULT_MAX_CHARS,
        ge=1,
        le=100_000,
        description="Maximum response characters returned to the model.",
    )


class WebFetchTool(BaseTool):
    name = "web_fetch"
    description = (
        "Fetch a public HTTP(S) page with redirect, private-network, content-type, "
        "and size protections. Requires normal network permission approval."
    )
    args_schema = WebFetchArgs

    def __init__(
        self,
        safety_guard,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        allowed_domains: list[str] | tuple[str, ...] | None = None,
    ):
        super().__init__(safety_guard)
        self._transport = transport
        self._allowed_domains = _normalize_allowed_domains(allowed_domains or ())

    async def run(self, **kwargs: Any) -> ToolResult:
        args = WebFetchArgs(**kwargs)
        try:
            final_url, status_code, content_type, body = await _fetch_public_text(
                args.url,
                transport=self._transport,
                allowed_domains=self._allowed_domains,
            )
        except (ValueError, httpx.HTTPError) as exc:
            return ToolResult(success=False, output="", error=str(exc))
        text = _html_to_text(body) if "html" in content_type else body
        truncated = len(text) > args.max_chars
        if truncated:
            text = text[: args.max_chars] + "\n[web_fetch output truncated]"
        output = "\n".join(
            (
                f"URL: {final_url}",
                f"Status: {status_code}",
                f"Content-Type: {content_type or 'unknown'}",
                "",
                text.strip(),
            )
        ).strip()
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
            truncated=truncated,
            citations=[
                {
                    "title": "",
                    "url": final_url,
                    "status_code": status_code,
                    "content_type": content_type or "unknown",
                }
            ],
        )


async def _fetch_public_text(
    raw_url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    allowed_domains: tuple[str, ...] = (),
) -> tuple[str, int, str, str]:
    url = _validate_public_url(raw_url, allowed_domains=allowed_domains)
    async with httpx.AsyncClient(
        timeout=10.0,
        transport=transport,
        headers={
            "User-Agent": "ash-web-fetch/0.1",
            "Accept": "text/*,application/json,application/xml;q=0.9,*/*;q=0.1",
        },
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Redirect response did not include Location")
                    url = _validate_public_url(
                        urljoin(str(response.url), location),
                        allowed_domains=allowed_domains,
                    )
                    continue
                if response.status_code >= 400:
                    raise ValueError(
                        f"HTTP {response.status_code} while fetching {url}"
                    )
                content_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if content_type and not content_type.startswith(TEXT_CONTENT_TYPES):
                    raise ValueError(f"Unsupported content type: {content_type}")
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > MAX_FETCH_BYTES:
                    raise ValueError("Response is larger than 1 MB")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_FETCH_BYTES:
                        raise ValueError("Response is larger than 1 MB")
                    chunks.append(chunk)
                data = b"".join(chunks)
                encoding = response.encoding or "utf-8"
                return (
                    str(response.url),
                    response.status_code,
                    content_type,
                    data.decode(encoding, errors="replace"),
                )
        raise ValueError(f"Too many redirects while fetching {raw_url}")


def _validate_public_url(
    raw_url: str,
    *,
    allowed_domains: tuple[str, ...] = (),
) -> str:
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs are supported")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")
    if allowed_domains and not _host_allowed(parsed.hostname, allowed_domains):
        raise ValueError(f"Host {parsed.hostname!r} is not in allowed_web_domains")
    _ensure_public_host(parsed.hostname)
    return raw_url


def _normalize_allowed_domains(
    domains: list[str] | tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                domain.strip().casefold().rstrip(".")
                for domain in domains
                if domain.strip()
            }
        )
    )


def _host_allowed(hostname: str, allowed_domains: tuple[str, ...]) -> bool:
    host = hostname.casefold().rstrip(".")
    for domain in allowed_domains:
        if domain.startswith("*."):
            suffix = domain[2:]
            if host.endswith(f".{suffix}") and host != suffix:
                return True
        elif host == domain:
            return True
    return False


def _ensure_public_host(hostname: str) -> None:
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        if hostname.casefold() == "localhost":
            raise ValueError("Refusing to fetch localhost")
        try:
            infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"Could not resolve host {hostname!r}: {exc}") from exc
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    for address in addresses:
        if any(
            (
                address.is_private,
                address.is_loopback,
                address.is_link_local,
                address.is_multicast,
                address.is_reserved,
                address.is_unspecified,
            )
        ):
            raise ValueError(f"Refusing to fetch non-public address: {address}")


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        if tag in {"p", "br", "div", "section", "article", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if tag in {"p", "div", "section", "article", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            stripped = " ".join(data.split())
            if stripped:
                self.parts.append(stripped + " ")

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


def _html_to_text(raw: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(raw)
    return parser.text()
