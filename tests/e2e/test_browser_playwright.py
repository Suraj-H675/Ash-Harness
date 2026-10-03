from __future__ import annotations

import asyncio
import json
import os
import re

import pytest

from ash.config import AshConfig
from ash.core.loop import AshLoop
from ash.core.session import SessionStore
from ash.providers.base import ProviderABC, StreamChunk
from ash.safety.guard import SafetyGuard
import ash.tools.browser as browser_module
from ash.tools.browser import BrowserSession, build_browser_tools, inspect_cdp_source
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
async def test_real_chromium_allows_exact_configured_loopback_origin_only() -> None:
    app_requests: list[bytes] = []
    neighbor_requests: list[bytes] = []

    async def app_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            app_requests.append(header)
            body = (
                b"<main><button onclick=\"document.querySelector('output').textContent="
                b"'local-ok'\">Run local app</button><output></output></main>"
            )
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Content-Type: text/html\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def neighbor_handler(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        neighbor_requests.append(await reader.read(64 * 1024))
        writer.close()
        await writer.wait_closed()

    app = await asyncio.start_server(app_handler, "127.0.0.1", 0)
    neighbor = await asyncio.start_server(neighbor_handler, "127.0.0.1", 0)
    app_port = int(app.sockets[0].getsockname()[1])
    neighbor_port = int(neighbor.sockets[0].getsockname()[1])
    session = BrowserSession(
        timeout_seconds=10,
        allowed_local_origins=(f"http://127.0.0.1:{app_port}",),
    )
    try:
        snapshot = await session.navigate(
            f"http://127.0.0.1:{app_port}/",
            "domcontentloaded",
        )
        action = re.search(r"\[([^]]+)] button 'Run local app'", snapshot)
        assert action is not None

        clicked = await session.click(action.group(1))

        assert "local-ok" in clicked
        with pytest.raises(ValueError):
            await session.navigate(
                f"http://127.0.0.1:{neighbor_port}/",
                "domcontentloaded",
            )
    finally:
        await session.close()
        app.close()
        neighbor.close()
        await app.wait_closed()
        await neighbor.wait_closed()

    assert app_requests
    assert neighbor_requests == []


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
        name_match = re.search(r"\[([^]]+)] input 'Name'", initial)
        greet_match = re.search(r"\[([^]]+)] button 'Greet'", initial)
        password_match = re.search(r"\[([^]]+)] input 'Password'", initial)
        assert name_match is not None
        assert greet_match is not None
        assert password_match is not None
        assert "must-not-leak" not in initial

        typed = await session.type_text(
            name_match.group(1),
            "Ash",
            submit=False,
            clear=True,
        )
        fresh_greet = re.search(r"\[([^]]+)] button 'Greet'", typed)
        assert fresh_greet is not None
        clicked = await session.click(fresh_greet.group(1))
        assert "Hello Ash" in clicked

        fresh_password = re.search(r"\[([^]]+)] input 'Password'", clicked)
        assert fresh_password is not None
        with pytest.raises(ValueError, match="password"):
            await session.type_text(
                fresh_password.group(1),
                "secret",
                submit=False,
                clear=True,
            )

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
async def test_real_chromium_structured_interactions_cover_modern_ui_flow() -> None:
    session = BrowserSession(timeout_seconds=15)
    page = await session.ensure_started()
    try:
        await page.set_content(
            """
            <main>
              <label>Flavor
                <select aria-label="Flavor">
                  <option value="one">One</option>
                  <option value="two">Two</option>
                </select>
              </label>
              <label>Command
                <input aria-label="Command"
                  onkeydown="if(event.key==='Enter') document.querySelector('#key').textContent='Key:Enter'">
              </label>
              <div role="button" aria-label="Hover target"
                onmouseenter="document.querySelector('#hover').textContent='Hovered'">Hover</div>
              <div role="button" aria-label="Drag source" draggable="true"
                ondragstart="event.dataTransfer.setData('text/plain','ash')">Drag source</div>
              <div role="button" aria-label="Drop target"
                ondragover="event.preventDefault()"
                ondrop="event.preventDefault(); document.querySelector('#drop').textContent='Dropped:' + event.dataTransfer.getData('text/plain')">Drop target</div>
              <button onclick="setTimeout(() => document.querySelector('#async').textContent='Ready', 100)">Start async</button>
              <output id="key"></output>
              <output id="hover"></output>
              <output id="drop"></output>
              <output id="async"></output>
            </main>
            """
        )

        initial = await session.snapshot()
        flavor = re.search(r"\[([^]]+)] select 'Flavor'", initial)
        assert flavor is not None
        selected = await session.select(flavor.group(1), ["two"])
        assert await page.locator('select[aria-label="Flavor"]').input_value() == "two"

        command = re.search(r"\[([^]]+)] input 'Command'", selected)
        assert command is not None
        typed = await session.type_text(
            command.group(1),
            "build",
            submit=False,
            clear=True,
        )
        command = re.search(r"\[([^]]+)] input 'Command'", typed)
        assert command is not None
        pressed = await session.press_key("Enter", ref=command.group(1))
        assert "Key:Enter" in pressed

        hover = re.search(r"\[([^]]+)] button 'Hover target'", pressed)
        assert hover is not None
        hovered = await session.hover(hover.group(1))
        assert "Hovered" in hovered

        source = re.search(r"\[([^]]+)] button 'Drag source'", hovered)
        target = re.search(r"\[([^]]+)] button 'Drop target'", hovered)
        assert source is not None
        assert target is not None
        dragged = await session.drag(source.group(1), target.group(1))
        assert "Dropped:ash" in dragged

        start = re.search(r"\[([^]]+)] button 'Start async'", dragged)
        assert start is not None
        await session.click(start.group(1))
        waited = await session.wait_for(
            condition="text",
            ref=None,
            value="Ready",
            load_state="domcontentloaded",
            timeout_seconds=2,
        )
        assert "Ready" in waited
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_real_chromium_coordinate_click_covers_visual_only_canvas() -> None:
    session = BrowserSession(timeout_seconds=15)
    page = await session.ensure_started()
    try:
        await page.set_content(
            """
            <main>
              <canvas id="target" width="240" height="120"
                style="display:block;border:1px solid black"></canvas>
              <output id="result"></output>
              <script>
                const canvas = document.querySelector('#target');
                const ctx = canvas.getContext('2d');
                ctx.fillStyle = '#ddd';
                ctx.fillRect(0, 0, canvas.width, canvas.height);
                ctx.fillStyle = '#111';
                ctx.font = '20px sans-serif';
                ctx.fillText('Visual target', 55, 65);
                canvas.addEventListener('click', () => {
                  document.querySelector('#result').textContent = 'Canvas clicked';
                });
              </script>
            </main>
            """
        )
        snapshot = await session.snapshot()
        assert "] canvas " not in snapshot
        screenshot = await session.screenshot(max_bytes=1_000_000)
        bounds = await page.locator("#target").bounding_box()
        assert bounds is not None

        clicked = await session.click_at(
            int(bounds["x"] + bounds["width"] / 2),
            int(bounds["y"] + bounds["height"] / 2),
            screenshot_sha256=screenshot.sha256,
        )

        assert "Canvas clicked" in clicked
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_real_chromium_dialogs_are_explicit_modal_state() -> None:
    session = BrowserSession(timeout_seconds=15)
    page = await session.ensure_started()
    try:
        await page.set_content(
            """
            <main>
              <button onclick="
                if (confirm('Proceed?')) {
                  const name = prompt('Name?');
                  document.querySelector('#result').textContent =
                    name === null ? 'Prompt cancelled' : 'Hello ' + name;
                } else {
                  document.querySelector('#result').textContent = 'Confirm cancelled';
                }
              ">Dialog flow</button>
              <output id="result"></output>
            </main>
            """
        )

        initial = await session.snapshot()
        trigger = re.search(r"\[([^]]+)] button 'Dialog flow'", initial)
        assert trigger is not None

        confirm_modal = await session.click(trigger.group(1))

        assert "Modal state:" in confirm_modal
        assert "confirm dialog" in confirm_modal
        assert "Proceed?" in confirm_modal
        assert await session.snapshot() == confirm_modal
        with pytest.raises(ValueError, match="pending dialog"):
            await session.press_key("Tab")
        with pytest.raises(ValueError, match="only valid for a prompt"):
            await session.handle_dialog(accept=True, prompt_text="wrong")

        prompt_modal = await session.handle_dialog(accept=True, prompt_text=None)

        assert "prompt dialog" in prompt_modal
        assert "Name?" in prompt_modal
        completed = await session.handle_dialog(accept=True, prompt_text="Ash")
        assert "Hello Ash" in completed

        trigger = re.search(r"\[([^]]+)] button 'Dialog flow'", completed)
        assert trigger is not None
        await session.click(trigger.group(1))
        dismissed = await session.handle_dialog(accept=False, prompt_text=None)
        assert "Confirm cancelled" in dismissed
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_real_chromium_iframe_controls_are_snapshot_scoped_and_actionable() -> None:
    session = BrowserSession(timeout_seconds=15)
    page = await session.ensure_started()
    try:
        await page.set_content(
            """
            <main>
              <button>Top button</button>
              <iframe id="visible-frame"></iframe>
              <iframe id="hidden-frame" style="display:none"></iframe>
            </main>
            """
        )
        await page.locator("#visible-frame").evaluate(
            "(element, html) => { element.srcdoc = html; }",
            """
            <main>
              <label>Frame name <input aria-label="Frame name"></label>
              <button onclick="
                document.querySelector('#result').textContent =
                  'Hello ' + document.querySelector('input').value
              ">Frame greet</button>
              <input type="password" value="frame-secret">
              <output id="result"></output>
            </main>
            """,
        )
        await page.locator("#hidden-frame").evaluate(
            """element => {
              element.srcdoc = '<button aria-label="Hidden frame button">Hidden</button>';
            }"""
        )
        await page.wait_for_timeout(150)

        initial = await session.snapshot()

        assert "Frame f1:" in initial
        assert "Hidden frame button" not in initial
        assert "frame-secret" not in initial
        frame_name = re.search(r"\[([^]]+)] input 'Frame name'", initial)
        frame_greet = re.search(r"\[([^]]+)] button 'Frame greet'", initial)
        assert frame_name is not None
        assert frame_greet is not None

        typed = await session.type_text(
            frame_name.group(1),
            "Ash",
            submit=False,
            clear=True,
        )
        fresh_greet = re.search(r"\[([^]]+)] button 'Frame greet'", typed)
        assert fresh_greet is not None

        clicked = await session.click(fresh_greet.group(1))

        assert "Hello Ash" in clicked
        assert "frame-secret" not in clicked
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_real_chromium_popup_tabs_are_explicitly_selectable() -> None:
    session = BrowserSession(timeout_seconds=15)
    page = await session.ensure_started()
    try:
        await page.set_content(
            """
            <title>first-tab</title>
            <main>
              <a href="about:blank" target="_blank">Open second tab</a>
            </main>
            """
        )
        initial = await session.snapshot()
        first_tab_match = re.search(r"^Tab: (\S+)$", initial, re.MULTILINE)
        link_match = re.search(r"\[([^]]+)] a 'Open second tab'", initial)
        assert first_tab_match is not None
        assert link_match is not None
        first_tab_id = first_tab_match.group(1)

        async with session._context.expect_page() as page_info:
            clicked = await session.click(link_match.group(1))
        second = await page_info.value
        await second.set_content("<title>second-tab</title><main>second</main>")

        assert f"Tab: {first_tab_id}" in clicked
        assert "Page: first-tab" in clicked

        tabs = json.loads(await session.list_tabs())["tabs"]
        assert len(tabs) == 2
        assert tabs[0]["tab_id"] == first_tab_id
        assert tabs[0]["active"] is True
        assert tabs[1]["active"] is False
        second_tab_id = tabs[1]["tab_id"]

        focused = await session.focus_tab(second_tab_id)
        assert f"Tab: {second_tab_id}" in focused
        assert "Page: second-tab" in focused

        remaining = json.loads(await session.close_tab(second_tab_id))["tabs"]
        assert remaining == [
            {
                "tab_id": first_tab_id,
                "active": True,
                "title": "first-tab",
                "url": "about:blank",
            }
        ]
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
        upload_match = re.search(r"\[([^]]+)] input 'Attachment'", initial)
        assert upload_match is not None

        await session.upload_file(
            upload_match.group(1),
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
        lambda url, _allowed_domains, _allowed_local_origins=(): url,
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
            allowed_local_origins: tuple[str, ...] = (),
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
                allowed_local_origins=allowed_local_origins,
                timeout_seconds=timeout_seconds,
                resolver=resolve,
                connector=connect,
            )

    monkeypatch.setattr(browser_module, "BrowserPolicyProxy", TestBrowserPolicyProxy)
    monkeypatch.setattr(
        browser_module,
        "_validate_browser_url",
        lambda url, _allowed_domains, _allowed_local_origins=(): url,
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
async def test_real_chromium_ash_owned_profile_persists_auth_state(tmp_path) -> None:
    profile = tmp_path / "ash-browser-profile"
    first = BrowserSession(
        timeout_seconds=15,
        profile_path=profile,
    )
    try:
        await first.ensure_started()
        assert first._context is not None
        await first._context.add_cookies(
            [
                {
                    "name": "ash_persistent_login",
                    "value": "present",
                    "url": "https://example.com/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                    "expires": 2_000_000_000,
                }
            ]
        )
    finally:
        await first.close()

    second = BrowserSession(
        timeout_seconds=15,
        profile_path=profile,
    )
    try:
        await second.ensure_started()
        assert second._context is not None
        copied = await second._context.cookies("https://example.com/")
        assert [(item["name"], item["value"]) for item in copied] == [
            ("ash_persistent_login", "present")
        ]
        assert await second.reset_persistent_profile() is True
    finally:
        await second.close()

    third = BrowserSession(
        timeout_seconds=15,
        profile_path=profile,
    )
    try:
        await third.ensure_started()
        assert third._context is not None
        assert await third._context.cookies("https://example.com/") == []
    finally:
        await third.close()


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
            [
                {
                    "name": "ash_login_probe",
                    "value": "present",
                    "url": "https://example.com/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                },
                {
                    "name": "unrelated_login_probe",
                    "value": "must-not-copy",
                    "url": "https://other.example/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                },
            ]
        )

        inventory = await inspect_cdp_source(
            f"http://127.0.0.1:{port}",
            timeout_seconds=15,
        )
        assert len(inventory["contexts"]) == 1
        assert inventory["contexts"][0]["tabs"][0]["title"] == "owner-alive"

        session = BrowserSession(
            timeout_seconds=15,
            allowed_domains=["example.com"],
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
        unrelated = await session._context.cookies("https://other.example/")
        assert unrelated == []

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
            allowed_web_domains=["example.com"],
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
        assert connected["storage_state_domains"] == ["example.com"]
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
