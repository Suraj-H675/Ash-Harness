from __future__ import annotations

import asyncio
import os

import pytest

from ash.config import AshConfig
from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.safety.guard import SafetyGuard
import ash.tools.browser as browser_module
from ash.tools.browser import BrowserSession, build_browser_tools
from ash.tools.browser_proxy import BrowserPolicyProxy
from ash.tools.web import _resolve_public_addresses


pytestmark = pytest.mark.skipif(
    os.environ.get("ASH_RUN_BROWSER_TESTS") != "1",
    reason="set ASH_RUN_BROWSER_TESTS=1 after installing ash-ai[browser] and Chromium",
)


class _BrowserE2EProvider(ProviderABC):
    model_name = "browser-e2e"

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def stream_chat(self, messages, temperature=0.0, tools=None):
        yield StreamChunk(content="ok", is_done=True)


@pytest.mark.asyncio
async def test_real_chromium_snapshot_fill_click_and_private_fetch_block() -> None:
    session = BrowserSession(timeout_seconds=15)
    page = await session.ensure_started()
    try:
        await page.set_content(
            """
            <main>
              <label>Name <input aria-label="Name"></label>
              <button onclick="document.querySelector('output').textContent =
                'Hello ' + document.querySelector('input').value">Greet</button>
              <input type="password" aria-label="Password" value="must-not-leak">
              <output></output>
            </main>
            """
        )
        initial = await session.snapshot()
        assert "[e1] input 'Name'" in initial
        assert "[e2] button 'Greet'" in initial
        assert "must-not-leak" not in initial

        await session.type_text("e1", "Ash", submit=False, clear=True)
        clicked = await session.click("e2")
        assert "Hello Ash" in clicked

        with pytest.raises(ValueError, match="password"):
            await session.type_text("e3", "secret", submit=False, clear=True)

        blocked = await page.evaluate(
            """async () => {
              try { await fetch('http://127.0.0.1/private'); return false; }
              catch (_) { return true; }
            }"""
        )
        assert blocked is True
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_real_chromium_upload_uses_approved_in_memory_payload(tmp_path) -> None:
    session = BrowserSession(timeout_seconds=15)
    approved = tmp_path / "approved.txt"
    approved.write_bytes(b"approved-content")
    page = await session.ensure_started()
    try:
        await page.set_content(
            """
            <main>
              <label>Attachment <input type="file" aria-label="Attachment"></label>
            </main>
            """
        )
        initial = await session.snapshot()
        assert "[e1] input 'Attachment'" in initial

        await session.upload_file(
            "e1",
            str(approved),
            safety_guard=SafetyGuard(tmp_path),
            max_bytes=1_000,
        )
        observed = await page.evaluate(
            """async () => {
              const file = document.querySelector('input[type=file]').files[0];
              return {
                name: file.name,
                type: file.type,
                content: await file.text(),
              };
            }"""
        )
        assert observed == {
            "name": "approved.txt",
            "type": "text/plain",
            "content": "approved-content",
        }
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_real_chromium_cannot_bypass_proxy_for_loopback_or_link_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[bytes] = []

    async def victim_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        requests.append(await reader.read(64 * 1024))
        writer.close()
        await writer.wait_closed()

    victim = await asyncio.start_server(victim_handler, "127.0.0.1", 0)
    victim_port = int(victim.sockets[0].getsockname()[1])
    monkeypatch.setattr(
        browser_module,
        "_validate_browser_url",
        lambda url, _allowed_domains: url,
    )
    session = BrowserSession(timeout_seconds=10)
    try:
        page = await session.ensure_started()
        loopback_response = await page.goto(
            f"http://127.0.0.1:{victim_port}/secret",
            wait_until="commit",
            timeout=5_000,
        )
        link_local_response = await page.goto(
            "http://169.254.169.254/metadata",
            wait_until="commit",
            timeout=5_000,
        )
        websocket_result = await page.evaluate(
            f"""async () => new Promise(resolve => {{
              const socket = new WebSocket('ws://127.0.0.1:{victim_port}/socket');
              socket.onopen = () => resolve('opened');
              socket.onerror = () => resolve('blocked');
              setTimeout(() => resolve('timeout'), 3_000);
            }})"""
        )
    finally:
        await session.close()
        victim.close()
        await victim.wait_closed()

    assert loopback_response is not None
    assert loopback_response.status == 403
    assert link_local_response is not None
    assert link_local_response.status == 403
    assert websocket_result == "blocked"
    assert requests == []


@pytest.mark.asyncio
async def test_real_chromium_proxy_blocks_redirect_and_subresource_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    victim_requests: list[bytes] = []
    origin_requests: list[bytes] = []

    async def victim_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        victim_requests.append(await reader.read(64 * 1024))
        writer.close()
        await writer.wait_closed()

    victim = await asyncio.start_server(victim_handler, "127.0.0.1", 0)
    victim_port = int(victim.sockets[0].getsockname()[1])

    async def origin_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            origin_requests.append(header)
            path = header.split(b" ", 2)[1]
            if path == b"/redirect":
                response = (
                    b"HTTP/1.1 302 Found\r\n"
                    + f"Location: http://127.0.0.1:{victim_port}/redirected\r\n".encode(
                        "ascii"
                    )
                    + b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            else:
                body = (
                    "<script>"
                    "const image = new Image();"
                    "image.onload = () => document.body.dataset.result = 'unexpected';"
                    "image.onerror = () => document.body.dataset.result = 'blocked';"
                    f"image.src = 'http://127.0.0.1:{victim_port}/subresource';"
                    "</script>"
                ).encode("ascii")
                response = (
                    b"HTTP/1.1 200 OK\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode("ascii")
                    + b"Content-Type: text/html\r\nConnection: close\r\n\r\n"
                    + body
                )
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    origin = await asyncio.start_server(origin_handler, "127.0.0.1", 0)
    origin_port = int(origin.sockets[0].getsockname()[1])

    class TestBrowserPolicyProxy(BrowserPolicyProxy):
        def __init__(
            self,
            allowed_domains: tuple[str, ...],
            *,
            timeout_seconds: float,
        ) -> None:
            def resolve(hostname: str) -> tuple[str, ...]:
                if hostname == "public.example":
                    return ("93.184.216.34",)
                return _resolve_public_addresses(hostname)

            async def connect(address: str, _port: int):
                assert address == "93.184.216.34"
                return await asyncio.open_connection("127.0.0.1", origin_port)

            super().__init__(
                allowed_domains,
                timeout_seconds=timeout_seconds,
                resolver=resolve,
                connector=connect,
            )

    monkeypatch.setattr(browser_module, "BrowserPolicyProxy", TestBrowserPolicyProxy)
    monkeypatch.setattr(
        browser_module,
        "_validate_browser_url",
        lambda url, _allowed_domains: url,
    )
    session = BrowserSession(timeout_seconds=10)
    try:
        page = await session.ensure_started()
        redirect_response = await page.goto(
            f"http://public.example:{origin_port}/redirect",
            wait_until="commit",
            timeout=5_000,
        )
        page_response = await page.goto(
            f"http://public.example:{origin_port}/page",
            wait_until="domcontentloaded",
            timeout=5_000,
        )
        await page.wait_for_function(
            "document.body.dataset.result === 'blocked'",
            timeout=5_000,
        )
    finally:
        await session.close()
        origin.close()
        await origin.wait_closed()
        victim.close()
        await victim.wait_closed()

    assert redirect_response is not None
    assert redirect_response.status == 403
    assert page_response is not None
    assert page_response.status == 200
    assert len(origin_requests) >= 2
    assert victim_requests == []


@pytest.mark.asyncio
async def test_real_chromium_cdp_attach_uses_isolated_context_and_preserves_owner(tmp_path) -> None:
    import socket
    from playwright.async_api import async_playwright

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    owner_playwright = await async_playwright().start()
    owner_context = None
    session = None
    try:
        owner_context = await owner_playwright.chromium.launch_persistent_context(
            user_data_dir=str(tmp_path / "owner-profile"),
            headless=True,
            args=[f"--remote-debugging-port={port}"],
        )
        owner_page = (
            owner_context.pages[0]
            if owner_context.pages
            else await owner_context.new_page()
        )
        await owner_page.set_content("<title>owner-alive</title><main>owner</main>")
        await owner_context.add_cookies(
            [{
                "name": "ash_login_probe",
                "value": "present",
                "url": "https://example.com/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }]
        )

        session = BrowserSession(
            timeout_seconds=15,
            cdp_url=f"http://127.0.0.1:{port}",
            cdp_reuse_storage_state=True,
        )
        ash_page = await session.ensure_started()
        assert ash_page is not owner_page
        assert session._context is not owner_context
        copied = await session._context.cookies("https://example.com/")
        assert [(item["name"], item["value"]) for item in copied] == [
            ("ash_login_probe", "present")
        ]

        await session.close()
        session = None
        assert await owner_page.title() == "owner-alive"
    finally:
        if session is not None:
            await session.close()
        if owner_context is not None:
            await owner_context.close()
        await owner_playwright.stop()


@pytest.mark.asyncio
async def test_runtime_browser_switch_attaches_and_disconnects_without_owning_source_browser(
    tmp_path,
) -> None:
    import socket
    from playwright.async_api import async_playwright

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    owner_playwright = await async_playwright().start()
    owner_context = None
    loop = None
    try:
        owner_context = await owner_playwright.chromium.launch_persistent_context(
            user_data_dir=str(tmp_path / "runtime-owner-profile"),
            headless=True,
            args=[f"--remote-debugging-port={port}"],
        )
        owner_page = (
            owner_context.pages[0]
            if owner_context.pages
            else await owner_context.new_page()
        )
        await owner_page.set_content("<title>runtime-owner</title><main>owner</main>")
        await owner_context.add_cookies(
            [{
                "name": "ash_runtime_probe",
                "value": "present",
                "url": "https://example.com/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }]
        )

        guard = SafetyGuard(tmp_path)
        config = AshConfig(
            workspace_root=tmp_path,
            db_directory=tmp_path / "db",
            memory_backend="off",
            browser_timeout_seconds=15,
        )
        browser_tools = {
            tool.name: tool
            for tool in build_browser_tools(
                guard,
                timeout_seconds=15,
            )
        }
        loop = AshLoop(
            SessionStore(tmp_path / "runtime-browser.db"),
            _BrowserE2EProvider(),
            guard,
            object(),
            tmp_path,
            tools=browser_tools,
            config=config,
        )

        connected = await loop.configure_browser_runtime(
            cdp_url=f"http://127.0.0.1:{port}",
            reuse_storage_state=True,
        )
        attached = loop.tools["browser_navigate"].session
        copied = await attached._context.cookies("https://example.com/")

        assert connected["backend"] == "cdp"
        assert connected["reuse_storage_state"] is True
        assert [(item["name"], item["value"]) for item in copied] == [
            ("ash_runtime_probe", "present")
        ]
        assert await owner_page.title() == "runtime-owner"

        disconnected = await loop.configure_browser_runtime(cdp_url=None)

        assert disconnected["backend"] == "managed"
        assert loop.tools["browser_navigate"].session.cdp_url == ""
        assert await owner_page.title() == "runtime-owner"
    finally:
        if loop is not None:
            await loop.aclose()
        if owner_context is not None:
            await owner_context.close()
        await owner_playwright.stop()
