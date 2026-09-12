from __future__ import annotations

import asyncio
import contextlib

import pytest

from ash.tools.browser_proxy import BrowserPolicyProxy


def _port(server: asyncio.AbstractServer) -> int:
    sockets = getattr(server, "sockets", ())
    assert sockets
    address = sockets[0].getsockname()
    assert isinstance(address, tuple)
    return int(address[1])


async def _proxy_request(proxy: BrowserPolicyProxy, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection(
        "127.0.0.1",
        int(proxy.server_url.rsplit(":", 1)[1]),
    )
    try:
        writer.write(request)
        await writer.drain()
        return await reader.read()
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_browser_proxy_forwards_public_http_to_vetted_numeric_address() -> None:
    requests: list[bytes] = []

    async def origin_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            body = b"permitted response"
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    origin = await asyncio.start_server(origin_handler, "127.0.0.1", 0)
    origin_port = _port(origin)

    def resolve(hostname: str) -> tuple[str, ...]:
        assert hostname == "public.example"
        return ("93.184.216.34",)

    async def connect(address: str, port: int):
        assert address == "93.184.216.34"
        assert port == origin_port
        return await asyncio.open_connection("127.0.0.1", origin_port)

    proxy = BrowserPolicyProxy(
        (),
        timeout_seconds=1,
        resolver=resolve,
        connector=connect,
    )
    await proxy.start()
    try:
        response = await _proxy_request(
            proxy,
            (
                f"GET http://public.example:{origin_port}/hello?x=1 HTTP/1.1\r\n"
                "Host: public.example\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii"),
        )
    finally:
        await proxy.close()
        origin.close()
        await origin.wait_closed()

    assert b"HTTP/1.1 200 OK" in response
    assert response.endswith(b"permitted response")
    assert requests
    assert requests[0].startswith(b"GET /hello?x=1 HTTP/1.1\r\n")
    assert b"Host: public.example\r\n" in requests[0]


@pytest.mark.asyncio
async def test_browser_proxy_rejects_mixed_public_and_non_global_dns_results() -> None:
    connector_called = False

    def resolve(_hostname: str) -> tuple[str, ...]:
        return ("93.184.216.34", "100.64.0.1")

    async def connect(_address: str, _port: int):
        nonlocal connector_called
        connector_called = True
        raise AssertionError("a mixed DNS result must be rejected before connect")

    proxy = BrowserPolicyProxy(
        (),
        timeout_seconds=1,
        resolver=resolve,
        connector=connect,
    )
    await proxy.start()
    try:
        response = await _proxy_request(
            proxy,
            b"GET http://public.example/private HTTP/1.1\r\n"
            b"Host: public.example\r\n"
            b"\r\n",
        )
    finally:
        await proxy.close()

    assert b"403 Forbidden" in response
    assert connector_called is False


@pytest.mark.asyncio
async def test_browser_proxy_tunnels_connect_without_tls_termination() -> None:
    received = asyncio.Event()

    async def origin_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            assert await reader.readexactly(4) == b"ping"
            received.set()
            writer.write(b"pong")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    origin = await asyncio.start_server(origin_handler, "127.0.0.1", 0)
    origin_port = _port(origin)

    def resolve(_hostname: str) -> tuple[str, ...]:
        return ("93.184.216.34",)

    async def connect(address: str, port: int):
        assert address == "93.184.216.34"
        assert port == 443
        return await asyncio.open_connection("127.0.0.1", origin_port)

    proxy = BrowserPolicyProxy(
        (),
        timeout_seconds=1,
        resolver=resolve,
        connector=connect,
    )
    await proxy.start()
    reader, writer = await asyncio.open_connection(
        "127.0.0.1",
        int(proxy.server_url.rsplit(":", 1)[1]),
    )
    try:
        writer.write(
            b"CONNECT public.example:443 HTTP/1.1\r\n"
            b"Host: public.example:443\r\n\r\n"
        )
        await writer.drain()
        assert b"200 Connection Established" in await reader.readuntil(b"\r\n\r\n")
        writer.write(b"ping")
        await writer.drain()
        assert await reader.readexactly(4) == b"pong"
        await asyncio.wait_for(received.wait(), timeout=1)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        await proxy.close()
        origin.close()
        await origin.wait_closed()


@pytest.mark.asyncio
async def test_browser_proxy_forwards_plain_websocket_upgrade() -> None:
    requests: list[bytes] = []

    async def origin_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n\r\n"
                b"\x81\x04pong"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    origin = await asyncio.start_server(origin_handler, "127.0.0.1", 0)
    origin_port = _port(origin)

    def resolve(_hostname: str) -> tuple[str, ...]:
        return ("93.184.216.34",)

    async def connect(address: str, port: int):
        assert address == "93.184.216.34"
        assert port == origin_port
        return await asyncio.open_connection("127.0.0.1", origin_port)

    proxy = BrowserPolicyProxy(
        (),
        timeout_seconds=1,
        resolver=resolve,
        connector=connect,
    )
    await proxy.start()
    reader, writer = await asyncio.open_connection(
        "127.0.0.1",
        int(proxy.server_url.rsplit(":", 1)[1]),
    )
    try:
        writer.write(
            (
                f"GET ws://public.example:{origin_port}/socket HTTP/1.1\r\n"
                "Host: public.example\r\n"
                "Connection: Upgrade\r\n"
                "Upgrade: websocket\r\n\r\n"
            ).encode("ascii")
        )
        await writer.drain()
        assert b"101 Switching Protocols" in await reader.readuntil(b"\r\n\r\n")
        assert await reader.readexactly(6) == b"\x81\x04pong"
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        await proxy.close()
        origin.close()
        await origin.wait_closed()

    assert requests
    assert requests[0].startswith(b"GET /socket HTTP/1.1\r\n")


@pytest.mark.asyncio
async def test_browser_proxy_enforces_allowlist_before_resolution() -> None:
    resolved = False

    def resolve(_hostname: str) -> tuple[str, ...]:
        nonlocal resolved
        resolved = True
        return ("93.184.216.34",)

    proxy = BrowserPolicyProxy(
        ("allowed.example",),
        timeout_seconds=1,
        resolver=resolve,
    )
    await proxy.start()
    try:
        response = await _proxy_request(
            proxy,
            b"GET http://blocked.example/ HTTP/1.1\r\n"
            b"Host: blocked.example\r\n\r\n",
        )
    finally:
        await proxy.close()

    assert b"403 Forbidden" in response
    assert resolved is False


@pytest.mark.asyncio
async def test_browser_proxy_close_settles_accepted_connections() -> None:
    proxy = BrowserPolicyProxy((), timeout_seconds=1)
    await proxy.start()
    _reader, writer = await asyncio.open_connection(
        "127.0.0.1",
        int(proxy.server_url.rsplit(":", 1)[1]),
    )
    try:
        writer.write(b"GET http://public.example/")
        await writer.drain()
        await asyncio.sleep(0)
        assert proxy._connection_tasks
        await proxy.close()
        assert not proxy._connection_tasks
        assert not proxy._writers
        assert writer.is_closing()
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        await proxy.close()


@pytest.mark.asyncio
async def test_browser_proxy_settings_force_loopback_through_proxy() -> None:
    proxy = BrowserPolicyProxy((), timeout_seconds=1)
    await proxy.start()
    try:
        settings = proxy.playwright_settings
    finally:
        await proxy.close()

    assert settings["server"].startswith("http://127.0.0.1:")
    assert settings["bypass"] == "<-loopback>"
