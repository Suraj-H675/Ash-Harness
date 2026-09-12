"""Browser-scoped HTTP proxy with Ash's public-network policy at connect time."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from ash.tools.web import _host_allowed, _resolve_public_addresses


MAX_PROXY_HEADER_BYTES = 64 * 1024
PROXY_READ_CHUNK_BYTES = 64 * 1024
LOOPBACK_PROXY_BYPASS_RULE = "<-loopback>"


class BrowserProxyError(RuntimeError):
    """The browser policy proxy could not be started or used safely."""


class _MalformedProxyRequest(Exception):
    pass


class _RejectedProxyTarget(Exception):
    pass


class _UpstreamConnectionError(Exception):
    pass


@dataclass(frozen=True)
class _ProxyTarget:
    hostname: str
    port: int
    scheme: str | None = None
    path: str | None = None


Resolver = Callable[[str], Sequence[str]]
Connector = Callable[
    [str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
]


class BrowserPolicyProxy:
    """A loopback-only, browser-scoped policy-enforcing HTTP proxy.

    The proxy never forwards a hostname to an upstream connector.  It resolves
    the requested hostname, rejects any result that is not globally reachable,
    and connects to the accepted numeric address directly.  HTTPS and WSS
    traffic remains end-to-end encrypted because CONNECT payloads are tunneled
    without TLS termination.
    """

    def __init__(
        self,
        allowed_domains: tuple[str, ...],
        *,
        timeout_seconds: float,
        resolver: Resolver | None = None,
        connector: Connector | None = None,
    ) -> None:
        self.allowed_domains = allowed_domains
        self.timeout_seconds = timeout_seconds
        self._resolver = resolver or _resolve_public_addresses
        self._connector = connector or self._connect_numeric
        self._server: asyncio.AbstractServer | None = None
        self._connection_tasks: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._closed = False

    async def start(self) -> None:
        """Start listening before Chromium is launched."""

        if self._server is not None:
            return
        self._closed = False
        server: asyncio.AbstractServer | None = None
        try:
            server = await asyncio.start_server(
                self._handle_client,
                host="127.0.0.1",
                port=0,
                family=socket.AF_INET,
                limit=MAX_PROXY_HEADER_BYTES,
            )
            sockets = getattr(server, "sockets", None)
            if not isinstance(sockets, (list, tuple)) or not sockets:
                raise BrowserProxyError("browser policy proxy did not expose a listener")
            self._server = server
        except BrowserProxyError:
            if server is not None:
                server.close()
                await server.wait_closed()
            raise
        except (OSError, RuntimeError) as exc:
            if server is not None:
                server.close()
                await server.wait_closed()
            raise BrowserProxyError(
                "browser policy proxy could not start"
            ) from exc
        except BaseException:
            if server is not None:
                server.close()
                await server.wait_closed()
            raise

    @property
    def server_url(self) -> str:
        server = self._server
        sockets = getattr(server, "sockets", None) if server is not None else None
        if not isinstance(sockets, (list, tuple)) or not sockets:
            raise BrowserProxyError("browser policy proxy is not running")
        address = sockets[0].getsockname()
        if not isinstance(address, tuple) or len(address) < 2:
            raise BrowserProxyError("browser policy proxy has an invalid listener")
        return f"http://127.0.0.1:{int(address[1])}"

    @property
    def playwright_settings(self) -> dict[str, str]:
        """Return explicit settings with Chromium's implicit loopback bypass removed."""

        return {
            "server": self.server_url,
            "bypass": LOOPBACK_PROXY_BYPASS_RULE,
        }

    async def close(self) -> None:
        """Stop the listener and settle every accepted connection."""

        server = self._server
        self._server = None
        self._closed = True
        if server is not None:
            server.close()

        for writer in tuple(self._writers):
            writer.close()

        while self._connection_tasks:
            tasks = tuple(self._connection_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            await server.wait_closed()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._connection_tasks.add(current)
        self._writers.add(writer)
        try:
            if self._closed:
                return
            try:
                header = await reader.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                await self._send_error(writer, 400, "Bad Request")
                return
            if len(header) > MAX_PROXY_HEADER_BYTES:
                await self._send_error(writer, 431, "Request Header Fields Too Large")
                return
            try:
                method, target, version, header_lines = _parse_request_header(header)
            except _MalformedProxyRequest:
                await self._send_error(writer, 400, "Bad Request")
                return

            try:
                if method.upper() == b"CONNECT":
                    await self._handle_connect(target, reader, writer)
                else:
                    await self._handle_forward(
                        method,
                        target,
                        version,
                        header_lines,
                        reader,
                        writer,
                    )
            except _MalformedProxyRequest:
                await self._send_error(writer, 400, "Bad Request")
            except _RejectedProxyTarget:
                await self._send_error(writer, 403, "Forbidden")
            except _UpstreamConnectionError:
                await self._send_error(writer, 502, "Bad Gateway")
            except Exception:
                await self._send_error(writer, 502, "Bad Gateway")
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(OSError, RuntimeError):
                await writer.wait_closed()
            if current is not None:
                self._connection_tasks.discard(current)

    async def _handle_connect(
        self,
        raw_target: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        target = _parse_authority(raw_target)
        upstream_reader, upstream_writer = await self._open_upstream(target)
        try:
            writer.write(
                b"HTTP/1.1 200 Connection Established\r\n"
                b"Proxy-Agent: ash-browser-policy-proxy\r\n"
                b"\r\n"
            )
            await writer.drain()
            await _relay_bidirectionally(
                reader,
                writer,
                upstream_reader,
                upstream_writer,
            )
        finally:
            await _close_writer(upstream_writer)

    async def _handle_forward(
        self,
        method: bytes,
        raw_target: bytes,
        version: bytes,
        header_lines: list[bytes],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        target = _parse_forward_target(raw_target)
        upstream_reader, upstream_writer = await self._open_upstream(target)
        try:
            rewritten_header = _rewrite_forward_header(
                method,
                target,
                version,
                header_lines,
            )
            upstream_writer.write(rewritten_header)
            await upstream_writer.drain()
            await _relay_bidirectionally(
                reader,
                writer,
                upstream_reader,
                upstream_writer,
            )
        finally:
            await _close_writer(upstream_writer)

    async def _open_upstream(
        self,
        target: _ProxyTarget,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self.allowed_domains and not _host_allowed(
            target.hostname, self.allowed_domains
        ):
            raise _RejectedProxyTarget
        try:
            addresses = tuple(
                await asyncio.to_thread(self._resolver, target.hostname)
            )
        except (OSError, ValueError, TypeError):
            raise _RejectedProxyTarget from None
        if not addresses:
            raise _RejectedProxyTarget
        for raw_address in addresses:
            try:
                address = ipaddress.ip_address(raw_address)
            except ValueError:
                raise _RejectedProxyTarget from None
            if not address.is_global:
                raise _RejectedProxyTarget

        for raw_address in addresses:
            try:
                return await asyncio.wait_for(
                    self._connector(str(raw_address), target.port),
                    timeout=self.timeout_seconds,
                )
            except (OSError, asyncio.TimeoutError, ConnectionError):
                continue
        raise _UpstreamConnectionError

    async def _connect_numeric(
        self,
        address: str,
        port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return await asyncio.open_connection(
            address,
            port,
            family=family,
            flags=socket.AI_NUMERICHOST,
        )

    @staticmethod
    async def _send_error(
        writer: asyncio.StreamWriter,
        status: int,
        reason: str,
    ) -> None:
        if writer.is_closing():
            return
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n"
            "\r\n".encode("ascii")
        )
        with contextlib.suppress(OSError, RuntimeError):
            await writer.drain()


def _parse_request_header(
    header: bytes,
) -> tuple[bytes, bytes, bytes, list[bytes]]:
    lines = header.split(b"\r\n")
    if len(lines) < 3 or lines[-2:] != [b"", b""]:
        raise _MalformedProxyRequest
    request_line = lines[0].split(b" ", 2)
    if len(request_line) != 3 or not all(request_line):
        raise _MalformedProxyRequest
    method, target, version = request_line
    if not version.startswith(b"HTTP/"):
        raise _MalformedProxyRequest
    header_lines = lines[1:-2]
    for line in header_lines:
        if not line or b":" not in line:
            raise _MalformedProxyRequest
    return method, target, version, header_lines


def _parse_authority(raw_target: bytes) -> _ProxyTarget:
    try:
        authority = raw_target.decode("ascii")
        parsed = urlsplit(f"//{authority}")
        hostname = parsed.hostname
        port = parsed.port or 443
    except (UnicodeDecodeError, ValueError):
        raise _MalformedProxyRequest from None
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not 1 <= port <= 65535
    ):
        raise _MalformedProxyRequest
    return _ProxyTarget(hostname=hostname, port=port)


def _parse_forward_target(raw_target: bytes) -> _ProxyTarget:
    try:
        raw_url = raw_target.decode("ascii")
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
        port = parsed.port
    except (UnicodeDecodeError, ValueError):
        raise _MalformedProxyRequest from None
    if (
        parsed.scheme not in {"http", "ws"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise _MalformedProxyRequest
    if port is None:
        port = 80
    if not 1 <= port <= 65535:
        raise _MalformedProxyRequest
    path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return _ProxyTarget(hostname=hostname, port=port, scheme=parsed.scheme, path=path)


def _rewrite_forward_header(
    method: bytes,
    target: _ProxyTarget,
    version: bytes,
    header_lines: list[bytes],
) -> bytes:
    assert target.path is not None
    connection_values = _header_values(header_lines, b"connection")
    upgrade_values = _header_values(header_lines, b"upgrade")
    websocket = (
        _has_token(connection_values, b"upgrade")
        and any(value.lower() == b"websocket" for value in upgrade_values)
    )
    rewritten = [b" ".join((method, target.path.encode("ascii"), version))]
    for line in header_lines:
        name, _, _value = line.partition(b":")
        lowered = name.strip().lower()
        if lowered in {b"proxy-connection", b"proxy-authorization"}:
            continue
        if lowered == b"connection" and not websocket:
            continue
        if lowered == b"keep-alive" and not websocket:
            continue
        rewritten.append(line)
    if not websocket:
        rewritten.append(b"Connection: close")
    return b"\r\n".join((*rewritten, b"", b""))


def _header_values(header_lines: list[bytes], name: bytes) -> list[bytes]:
    values: list[bytes] = []
    for line in header_lines:
        key, separator, value = line.partition(b":")
        if separator and key.strip().lower() == name:
            values.append(value.strip())
    return values


def _has_token(values: list[bytes], token: bytes) -> bool:
    return any(
        item.strip().lower() == token
        for value in values
        for item in value.split(b",")
    )


async def _relay_bidirectionally(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    async def pump(
        source: asyncio.StreamReader,
        destination: asyncio.StreamWriter,
    ) -> None:
        try:
            while chunk := await source.read(PROXY_READ_CHUNK_BYTES):
                destination.write(chunk)
                await destination.drain()
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        finally:
            if not destination.is_closing():
                with contextlib.suppress(OSError, RuntimeError):
                    destination.write_eof()

    tasks = (
        asyncio.create_task(
            pump(client_reader, upstream_writer),
            name="ash-browser-proxy-client-to-upstream",
        ),
        asyncio.create_task(
            pump(upstream_reader, client_writer),
            name="ash-browser-proxy-upstream-to-client",
        ),
    )
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError, RuntimeError):
        await writer.wait_closed()
