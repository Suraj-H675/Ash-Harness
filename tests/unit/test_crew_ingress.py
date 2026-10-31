"""Ingress tests use loopback sockets only, with synthetic signed payloads."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import pytest

from ash.crew_ingress import (
    CrewIngressReceipt,
    CrewIngressRefusal,
    _headers,
    serve_crew_ingress,
)


def _request(port: int, body: bytes, *, extra: bytes = b"") -> bytes:
    return (
        b"POST /crew/event HTTP/1.1\r\n"
        + f"Host: 127.0.0.1:{port}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + b"User-Agent: local-test\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + extra + b"\r\n" + body
    )


async def _send(port: int, raw: bytes) -> tuple[int, dict[str, object]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(raw)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(-1), timeout=4)
    finally:
        writer.close()
        await writer.wait_closed()
    head, body = data.split(b"\r\n\r\n", 1)
    return int(head.split(b"\r\n", 1)[0].split()[1]), json.loads(body)


async def _with_listener(
    process: Callable[[bytes], Awaitable[CrewIngressReceipt]],
    action: Callable[[int], Awaitable[None]],
) -> None:
    stop = asyncio.Event()
    ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(serve_crew_ingress(
        process, port=0, stop=stop, ready=ready.set_result,
    ))
    try:
        port = await asyncio.wait_for(ready, timeout=3)
        await action(port)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=3)


@pytest.mark.asyncio
async def test_loopback_ingress_sends_sanitized_receipt_after_processing() -> None:
    seen: list[bytes] = []

    async def dispatch(data: bytes) -> CrewIngressReceipt:
        seen.append(data)
        return CrewIngressReceipt("run-1", "session-1", "request-1")

    async def action(port: int) -> None:
        code, body = await _send(port, _request(port, b'{"signed":true}'))
        assert code == 200
        assert body == {
            "status": "completed", "run_id": "run-1",
            "session_id": "session-1", "request_id": "request-1",
            "analysis_session_id": None,
            "note": "no_source_changes_or_approvals_implied",
        }

    await _with_listener(dispatch, action)
    assert seen == [b'{"signed":true}']


@pytest.mark.parametrize(("extra", "expected"), [
    (b"Transfer-Encoding: chunked\r\n", 400),
    (b"Content-Length: 2\r\n", 400),
    (b"Expect: 100-continue\r\n", 400),
    (b"Content-Encoding: gzip\r\n", 400),
    (b"Host: evil.example\r\n", 400),
])
@pytest.mark.asyncio
async def test_loopback_ingress_rejects_smuggling_and_extra_authority(
    extra: bytes, expected: int,
) -> None:
    called = False

    async def dispatch(data: bytes) -> CrewIngressReceipt:
        nonlocal called
        called = True
        return CrewIngressReceipt("run", "session", "request")

    async def action(port: int) -> None:
        code, body = await _send(port, _request(port, b"x", extra=extra))
        assert code == expected
        assert body["status"] == "refused"

    await _with_listener(dispatch, action)
    assert not called


@pytest.mark.asyncio
async def test_loopback_ingress_denies_large_body_and_non_post_requests() -> None:
    async def dispatch(data: bytes) -> CrewIngressReceipt:
        raise AssertionError("Request must be rejected before dispatch")

    async def action(port: int) -> None:
        code, _ = await _send(port, _request(port, b"x" * (16 * 1024 + 1)))
        assert code == 413
        code, _ = await _send(
            port,
            f"GET /crew/event HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Content-Length: 1\r\nContent-Type: application/json\r\n\r\nx".encode(),
        )
        assert code == 404
        code, _ = await _send(
            port,
            _request(port, b"x").replace(
                b"Content-Type: application/json",
                b"Content-Type: text/plain",
            ),
        )
        assert code == 415

    await _with_listener(dispatch, action)


@pytest.mark.asyncio
async def test_loopback_ingress_busy_refusal_and_single_logical_dispatch() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    count = 0

    async def dispatch(data: bytes) -> CrewIngressReceipt:
        nonlocal count
        count += 1
        entered.set()
        await release.wait()
        return CrewIngressReceipt("run", "session", "request")

    async def action(port: int) -> None:
        first = asyncio.create_task(_send(port, _request(port, b"first")))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            code, body = await _send(port, _request(port, b"second"))
            assert code == 429
            assert body["code"] == "busy"
        finally:
            release.set()
        assert (await first)[0] == 200

    await _with_listener(dispatch, action)
    assert count == 1


@pytest.mark.asyncio
async def test_loopback_ingress_discloses_no_private_exception_text() -> None:
    async def dispatch(data: bytes) -> CrewIngressReceipt:
        raise RuntimeError("SECRET-CREDENTIAL-FROM-MODEL-ENV")

    async def action(port: int) -> None:
        code, response = await _send(port, _request(port, b"safe"))
        assert code == 500
        assert response == {
            "status": "unknown", "code": "dispatch_outcome_unknown",
            "note": "inspect_crew_history_before_retry",
        }
        assert "SECRET" not in json.dumps(response)

    await _with_listener(dispatch, action)


@pytest.mark.asyncio
async def test_loopback_ingress_reports_signed_rejection_without_dispatch() -> None:
    async def dispatch(data: bytes) -> CrewIngressReceipt:
        raise CrewIngressRefusal(403, "invalid_signature_or_event")

    async def action(port: int) -> None:
        code, response = await _send(port, _request(port, b"wrong-signature"))
        assert code == 403
        assert response["code"] == "invalid_signature_or_event"
        assert "inspect_crew_history_before_retry" == response["note"]

    await _with_listener(dispatch, action)


def test_ingress_header_parser_rejects_malformed_host_and_length() -> None:
    good = (
        b"POST /crew/event HTTP/1.1\r\n"
        b"Host: 127.0.0.1:8781\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 12"
    )
    assert _headers(good, port=8781) == 12
    for bad in (
        good.replace(b"127.0.0.1:8781", b"localhost:8781"),
        good.replace(b"Content-Length: 12", b"Content-Length: 012"),
        good.replace(b"Content-Length: 12", b"Content-Length: -1"),
        good.replace(b"Content-Length: 12", b"Content-Length: 12\nX: bad"),
        good.replace(b"Content-Length: 12", b"Content-Length: 12\x00"),
    ):
        with pytest.raises(CrewIngressRefusal):
            _headers(bad, port=8781)


@pytest.mark.asyncio
async def test_loopback_ingress_caps_open_connections_before_any_dispatch() -> None:
    async def dispatch(data: bytes) -> CrewIngressReceipt:
        raise AssertionError("No validated event was sent")

    async def action(port: int) -> None:
        sockets: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
        try:
            for _ in range(8):
                sockets.append(await asyncio.open_connection("127.0.0.1", port))
            # All eight accepted connections are waiting for bounded headers.
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                data = await asyncio.wait_for(reader.read(-1), timeout=2)
                assert b"429 Too Many Requests" in data
                assert b"too_many_connections" in data
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            for _, writer in sockets:
                writer.close()
                await writer.wait_closed()

    await _with_listener(dispatch, action)


@pytest.mark.asyncio
async def test_ingress_unknown_post_dispatch_refusal_is_never_reported_as_denial() -> None:
    async def dispatch(data: bytes) -> CrewIngressReceipt:
        raise CrewIngressRefusal(500, "dispatch_outcome_unknown")

    async def action(port: int) -> None:
        code, response = await _send(port, _request(port, b"signed-but-ambiguous"))
        assert code == 500
        assert response == {
            "status": "unknown",
            "code": "dispatch_outcome_unknown",
            "note": "inspect_crew_history_before_retry",
        }

    await _with_listener(dispatch, action)
