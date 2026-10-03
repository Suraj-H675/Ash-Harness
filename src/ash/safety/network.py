"""Small shared network-policy helpers."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from urllib.parse import urlsplit


def normalize_loopback_origins(origins: Iterable[str]) -> tuple[str, ...]:
    """Normalize exact HTTP(S) loopback origins."""

    normalized = {_normalize_loopback_origin(origin) for origin in origins}
    return tuple(sorted(normalized))


def loopback_url_allowed(url: str, origins: tuple[str, ...]) -> bool:
    """Return whether an HTTP(S)/WS(S) URL matches an allowed loopback origin."""

    if not origins:
        return False
    try:
        parsed = urlsplit(url)
        if parsed.username is not None or parsed.password is not None:
            return False
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https", "ws", "wss"}:
            return False
        origin_scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
        hostname = _normalize_loopback_hostname(parsed.hostname)
        port = parsed.port or (443 if origin_scheme == "https" else 80)
    except ValueError:
        return False
    return _render_origin(origin_scheme, hostname, port) in origins


def loopback_target_allowed(
    hostname: str,
    port: int,
    origins: tuple[str, ...],
    *,
    scheme: str | None = None,
) -> bool:
    """Return whether a proxy host/port target is covered by the origin list."""

    try:
        normalized_host = _normalize_loopback_hostname(hostname)
    except ValueError:
        return False
    if scheme is not None:
        origin_scheme = {"ws": "http", "wss": "https"}.get(
            scheme.casefold(), scheme.casefold()
        )
        if origin_scheme not in {"http", "https"}:
            return False
        return _render_origin(origin_scheme, normalized_host, port) in origins
    suffix = f":{port}"
    return any(
        origin.endswith(suffix)
        and urlsplit(origin).hostname == normalized_host
        for origin in origins
    )


def _normalize_loopback_origin(raw: str) -> str:
    value = raw.strip()
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"}:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError
        hostname = _normalize_loopback_hostname(parsed.hostname)
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError(
            "browser_allowed_local_origins entries must be exact HTTP(S) "
            "loopback origins such as http://localhost:3000"
        ) from exc
    return _render_origin(scheme, hostname, port)


def _normalize_loopback_hostname(hostname: str | None) -> str:
    if not hostname:
        raise ValueError("loopback origin requires a hostname")
    normalized = hostname.casefold().rstrip(".")
    if normalized == "localhost":
        return normalized
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise ValueError("loopback origin must use localhost or a loopback IP") from exc
    if not address.is_loopback:
        raise ValueError("loopback origin must use localhost or a loopback IP")
    return address.compressed


def _render_origin(scheme: str, hostname: str, port: int) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{scheme}://{host}:{port}"
