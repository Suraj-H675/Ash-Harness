"""Bounded, loopback-only HTTP transport for existing signed Crew event dispatch."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ash.crew_events import MAX_CREW_EVENT_BYTES

_HEADER_LIMIT = 8192
_HEADER_TIMEOUT_SECONDS = 5
_BODY_TIMEOUT_SECONDS = 5
_MAX_CLIENTS = 8
_REQUEST_PATH = "/crew/event"


@dataclass(frozen=True)
class CrewIngressReceipt:
    run_id: str
    session_id: str
    request_id: str | None
    analysis_session_id: str | None = None


class CrewIngressRefusal(Exception):
    """A request cannot be dispatched, with no private runtime details exposed."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _headers(raw: bytes, *, port: int) -> int:
    """Parse only the fixed POST shape; no chunking, keepalive, or proxy headers."""

    try:
        text = raw.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise CrewIngressRefusal(400, "bad_headers") from exc
    lines = text.split("\r\n")
    if not lines or lines[0] != f"POST {_REQUEST_PATH} HTTP/1.1":
        raise CrewIngressRefusal(404, "unsupported_request")
    entries: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            raise CrewIngressRefusal(400, "bad_headers")
        name, value = line.split(":", 1)
        normalized = name.lower()
        if (
            not name.isascii()
            or not name
            or any(not (c.isalnum() or c == "-") for c in name)
            or normalized in entries
            or any(ord(char) < 32 and char != "\t" or ord(char) == 127
                   for char in value)
        ):
            raise CrewIngressRefusal(400, "bad_headers")
        entries[normalized] = value.strip(" \t")
    required = {"host", "content-type", "content-length"}
    optional = {"accept", "user-agent", "accept-encoding", "connection"}
    if not required.issubset(entries) or set(entries) - required - optional:
        raise CrewIngressRefusal(400, "unsupported_headers")
    if entries["host"] != f"127.0.0.1:{port}":
        raise CrewIngressRefusal(400, "invalid_host")
    if entries["content-type"].lower() != "application/json":
        raise CrewIngressRefusal(415, "unsupported_content_type")
    length = entries["content-length"]
    if not length.isdecimal() or length.startswith("0") or len(length) > 6:
        raise CrewIngressRefusal(400, "invalid_content_length")
    size = int(length)
    if not 1 <= size <= MAX_CREW_EVENT_BYTES:
        raise CrewIngressRefusal(413, "body_too_large")
    return size


async def _respond(
    writer: asyncio.StreamWriter, status: int, payload: dict[str, Any],
) -> None:
    reasons = {
        200: "OK", 400: "Bad Request", 403: "Forbidden",
        404: "Not Found", 409: "Conflict", 413: "Content Too Large",
        415: "Unsupported Media Type", 422: "Unprocessable Content",
        429: "Too Many Requests", 500: "Internal Server Error",
        503: "Service Unavailable",
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
    writer.write(
        f"HTTP/1.1 {status} {reasons[status]}\r\n"
        "Content-Type: application/json\r\n"
        "Cache-Control: no-store\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "Connection: close\r\n"
        f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
    )
    try:
        await writer.drain()
    except OSError:
        # Disconnects never reverse or retry a previously claimed model event.
        pass


async def serve_crew_ingress(
    process: Callable[[bytes], Awaitable[CrewIngressReceipt]],
    *,
    port: int,
    stop: asyncio.Event | None = None,
    ready: Callable[[int], None] | None = None,
) -> None:
    """Serve signed envelopes on IPv4 loopback; never queue an uncommitted event.

    A 200 response means model processing finished, not merely that the HTTP
    request was accepted. A failed connection/response may have an unknown
    outcome; the caller must inspect durable Crew history before retrying.
    """

    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Crew ingress port is invalid")
    in_flight = False
    active_clients = 0

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        nonlocal in_flight, active_clients
        if active_clients >= _MAX_CLIENTS:
            try:
                await _respond(
                    writer, 429, {"status": "refused", "code": "too_many_connections"},
                )
            finally:
                writer.close()
            return
        active_clients += 1
        try:
            try:
                raw = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"), _HEADER_TIMEOUT_SECONDS,
                )
                if len(raw) > _HEADER_LIMIT:
                    raise CrewIngressRefusal(413, "headers_too_large")
                actual_port = writer.get_extra_info("sockname")[1]
                size = _headers(raw[:-4], port=actual_port)
                envelope = await asyncio.wait_for(
                    reader.readexactly(size), _BODY_TIMEOUT_SECONDS,
                )
            except (
                asyncio.TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
            ) as exc:
                raise CrewIngressRefusal(400, "incomplete_request") from exc
            if in_flight:
                await _respond(writer, 429, {"status": "refused", "code": "busy"})
                return
            in_flight = True
            try:
                receipt = await process(envelope)
                await _respond(writer, 200, {
                    "status": "completed",
                    "run_id": receipt.run_id,
                    "session_id": receipt.session_id,
                    "request_id": receipt.request_id,
                    "analysis_session_id": receipt.analysis_session_id,
                    "note": "no_source_changes_or_approvals_implied",
                })
            finally:
                in_flight = False
        except CrewIngressRefusal as exc:
            await _respond(writer, exc.status, {
                "status": (
                    "unknown" if exc.code == "dispatch_outcome_unknown"
                    else "refused"
                ),
                "code": exc.code,
                "note": "inspect_crew_history_before_retry",
            })
        except Exception:
            # No exception details, model content, keys or event bytes go over HTTP.
            await _respond(writer, 500, {
                "status": "unknown", "code": "dispatch_outcome_unknown",
                "note": "inspect_crew_history_before_retry",
            })
        finally:
            active_clients -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    server = await asyncio.start_server(
        handler, host="127.0.0.1", port=port, limit=_HEADER_LIMIT,
        backlog=8, reuse_address=False,
    )
    async with server:
        bound_port = server.sockets[0].getsockname()[1]  # type: ignore[index]
        if ready is not None:
            ready(bound_port)
        if stop is None:
            await server.serve_forever()
        else:
            await stop.wait()
