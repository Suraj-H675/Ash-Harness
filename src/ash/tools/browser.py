"""Playwright-backed browser automation with bounded model-facing snapshots."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel, Field, field_validator

from ash.core.redaction import redact_text
from ash.safe_io import validate_unlinked_directory_path
from ash.safety.environment import build_scrubbed_environment
from ash.safety.guard import SafetyGuard
from ash.safety.scoped_io import atomic_write_scoped_bytes
from ash.tools.base import BaseTool, ToolResult, count_output_tokens
from ash.tools.browser_proxy import BrowserPolicyProxy
from ash.tools.web import _normalize_allowed_domains, _validate_public_url


MAX_SNAPSHOT_CHARS = 30_000
MAX_INTERACTIVE_ELEMENTS = 150
MAX_CDP_STORAGE_STATE_BYTES = 4 * 1024 * 1024
MAX_BROWSER_TABS = 32
TAB_ID_PATTERN = r"t[0-9a-f]{8}-[1-9][0-9]{0,8}"
BROWSER_TOOL_NAMES = frozenset(
    {
        "browser_navigate",
        "browser_snapshot",
        "browser_tabs",
        "browser_open_tab",
        "browser_focus_tab",
        "browser_close_tab",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_back",
        "browser_screenshot",
        "browser_upload",
        "browser_download",
    }
)
ELEMENT_REF = re.compile(
    rf"^(?P<tab_id>{TAB_ID_PATTERN}):s(?P<snapshot>[1-9][0-9]{{0,8}}):"
    r"e[1-9][0-9]{0,3}$"
)
TAB_ID = re.compile(rf"^{TAB_ID_PATTERN}$")
SENSITIVE_BROWSER_URL_FIELDS = frozenset(
    {
        "access_token",
        "apikey",
        "api_key",
        "auth",
        "authorization",
        "authorization_code",
        "client_secret",
        "code",
        "code_verifier",
        "credential",
        "credentials",
        "id_token",
        "jwt",
        "key",
        "oauth_code",
        "password",
        "refresh_token",
        "saml_response",
        "secret",
        "session",
        "session_id",
        "state",
        "ticket",
        "token",
    }
)
INTERACTIVE_SELECTOR = ",".join(
    (
        "a[href]",
        "button",
        "input:not([type=hidden])",
        "textarea",
        "select",
        "summary",
        "[role=button]",
        "[role=link]",
        "[role=checkbox]",
        "[role=radio]",
        "[role=tab]",
        "[contenteditable=true]",
    )
)


class BrowserUnavailableError(RuntimeError):
    """The optional runtime or its pinned browser binary is unavailable."""


class BrowserScreenshot:
    """A bounded canonical screenshot payload."""

    def __init__(self, *, media_type: str, data: str, sha256: str) -> None:
        self.media_type = media_type
        self.data = data
        self.sha256 = sha256


class BrowserSession:
    """One isolated browser context shared by the runtime's browser tools."""

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_seconds: float = 30.0,
        allowed_domains: list[str] | tuple[str, ...] | None = None,
        profile_path: Path | None = None,
        cdp_url: str | None = None,
        cdp_reuse_storage_state: bool = False,
    ) -> None:
        if not 1.0 <= timeout_seconds <= 120.0:
            raise ValueError("browser timeout must be between 1 and 120 seconds")
        self.headless = headless
        self._timeout_seconds = timeout_seconds
        self.timeout_ms = int(timeout_seconds * 1000)
        self.allowed_domains = _normalize_allowed_domains(allowed_domains or ())
        normalized_cdp = _validate_cdp_url(cdp_url) if cdp_url else ""
        if normalized_cdp and profile_path is not None:
            raise ValueError("browser CDP attachment cannot use an Ash persistent profile")
        if cdp_reuse_storage_state and not normalized_cdp:
            raise ValueError("browser_cdp_reuse_storage_state requires browser_cdp_url")
        self.cdp_url = normalized_cdp
        self.cdp_reuse_storage_state = bool(cdp_reuse_storage_state)
        self.profile_path = (
            validate_unlinked_directory_path(
                profile_path, label="browser profile directory"
            )
            if profile_path is not None
            else None
        )
        self._lock = asyncio.Lock()
        self._tab_lock = asyncio.Lock()
        self._closed = False
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._tab_pages: dict[str, Any] = {}
        self._snapshot_versions: dict[str, int] = {}
        self._session_token = secrets.token_hex(4)
        self._next_tab_id = 1
        self._page_tasks: set[asyncio.Task[None]] = set()
        self._proxy: BrowserPolicyProxy | None = None

    @property
    def is_started(self) -> bool:
        page = self._page
        return page is not None and not page.is_closed()

    async def ensure_started(self) -> Any:
        async with self._lock:
            if self._closed:
                raise BrowserUnavailableError("browser session is closed")
            if self._page is not None and not self._page.is_closed():
                self._remember_tab(self._page)
                return self._page
            if self._context is not None:
                self._page = self._latest_page()
                if self._page is not None and not self._page.is_closed():
                    self._remember_tab(self._page)
                    return self._page
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                from ash.install import pipx_install_command

                raise BrowserUnavailableError(
                    f"Run `{pipx_install_command('browser')}`, then "
                    "`ash setup browser` to enable browser tools."
                ) from exc
            try:
                if any(
                    resource is not None
                    for resource in (
                        self._proxy,
                        self._playwright,
                        self._browser,
                        self._context,
                    )
                ):
                    cleanup_task = asyncio.create_task(
                        self._close_unlocked(),
                        name="ash-browser-startup-cleanup",
                    )
                    await _settle_browser_cleanup_task(cleanup_task)
                if self.profile_path is not None:
                    validate_unlinked_directory_path(
                        self.profile_path, label="browser profile directory"
                    )
                self._proxy = BrowserPolicyProxy(
                    self.allowed_domains,
                    timeout_seconds=self._timeout_seconds,
                )
                await self._proxy.start()
                proxy_settings: Any = self._proxy.playwright_settings
                self._playwright = await async_playwright().start()
                if self.profile_path is not None:
                    self.profile_path.mkdir(mode=0o700, parents=True, exist_ok=True)
                    validate_unlinked_directory_path(
                        self.profile_path, label="browser profile directory"
                    )
                    if os.name != "nt":
                        self.profile_path.chmod(0o700)
                browser_environment_names = {
                    "PLAYWRIGHT_BROWSERS_PATH",
                    "PLAYWRIGHT_NODEJS_PATH",
                }
                if not self.headless:
                    browser_environment_names.update(
                        {
                            "DISPLAY",
                            "WAYLAND_DISPLAY",
                            "XAUTHORITY",
                            "DBUS_SESSION_BUS_ADDRESS",
                        }
                    )
                browser_environment: dict[str, str | float | bool] = dict(
                    build_scrubbed_environment(browser_environment_names)
                )
                if self.cdp_url:
                    self._browser = await self._playwright.chromium.connect_over_cdp(
                        self.cdp_url,
                        timeout=self.timeout_ms,
                    )
                    storage_state: dict[str, Any] | None = None
                    if self.cdp_reuse_storage_state:
                        contexts = list(self._browser.contexts)
                        if not contexts:
                            raise BrowserUnavailableError(
                                "CDP browser has no default context to copy storage state from"
                            )
                        try:
                            raw_storage_state: Any = await asyncio.wait_for(
                                contexts[0].storage_state(),
                                timeout=self._timeout_seconds,
                            )
                        except asyncio.TimeoutError as exc:
                            raise BrowserUnavailableError(
                                "CDP browser storage-state copy timed out"
                            ) from exc
                        if not isinstance(raw_storage_state, dict):
                            raise BrowserUnavailableError(
                                "CDP browser returned invalid storage state"
                            )
                        storage_state = raw_storage_state
                        _validate_cdp_storage_state(storage_state)
                    context_kwargs: dict[str, Any] = {
                        "accept_downloads": True,
                        "service_workers": "block",
                        "viewport": {"width": 1280, "height": 800},
                        "proxy": proxy_settings,
                    }
                    if storage_state is not None:
                        context_kwargs["storage_state"] = storage_state
                    self._context = await self._browser.new_context(**context_kwargs)
                elif self.profile_path is not None:
                    self._context = await (
                        self._playwright.chromium.launch_persistent_context(
                            user_data_dir=str(self.profile_path),
                            headless=self.headless,
                            env=browser_environment,
                            proxy=proxy_settings,
                            accept_downloads=True,
                            service_workers="block",
                            viewport={"width": 1280, "height": 800},
                        )
                    )
                else:
                    self._browser = await self._playwright.chromium.launch(
                        headless=self.headless,
                        env=browser_environment,
                        proxy=proxy_settings,
                    )
                    self._context = await self._browser.new_context(
                        accept_downloads=True,
                        service_workers="block",
                        viewport={"width": 1280, "height": 800},
                    )
                self._context.set_default_timeout(self.timeout_ms)
                self._context.set_default_navigation_timeout(self.timeout_ms)
                await self._context.route("**/*", self._route_request)
                await self._context.route_web_socket("**/*", self._route_websocket)
                on_event = getattr(self._context, "on", None)
                if callable(on_event):
                    on_event("page", self._on_page_created)
                self._page = await self._context.new_page()
                self._remember_tab(self._page)
                return self._page
            except asyncio.CancelledError:
                cleanup_task = asyncio.create_task(
                    self._close_unlocked(),
                    name="ash-browser-startup-cleanup",
                )
                await _settle_browser_cleanup_task(cleanup_task)
                raise
            except Exception as exc:
                cleanup_task = asyncio.create_task(
                    self._close_unlocked(),
                    name="ash-browser-startup-cleanup",
                )
                await _settle_browser_cleanup_task(cleanup_task)
                message = redact_text(str(exc))[:500]
                if "Executable doesn't exist" in message:
                    raise BrowserUnavailableError(
                        "Chromium is not installed; run `ash setup browser`."
                    ) from exc
                raise BrowserUnavailableError(
                    f"Could not start the browser session: {message}"
                ) from exc

    async def _route_request(self, route: Any, request: Any) -> None:
        try:
            await asyncio.to_thread(
                _validate_browser_url, request.url, self.allowed_domains
            )
        except ValueError:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _route_websocket(self, websocket: Any) -> None:
        try:
            await asyncio.to_thread(
                _validate_browser_url, websocket.url, self.allowed_domains
            )
        except ValueError:
            await websocket.close(code=1008, reason="Blocked by Ash network policy")
            return
        websocket.connect_to_server()

    async def navigate(self, url: str, wait_until: str) -> str:
        validated = await asyncio.to_thread(
            _validate_browser_url, url, self.allowed_domains
        )
        page = await self.ensure_started()
        await page.goto(validated, wait_until=wait_until, timeout=self.timeout_ms)
        return await self.snapshot()

    async def snapshot(self) -> str:
        page = await self.ensure_started()
        tab_id = self._remember_tab(page)
        snapshot_version = self._snapshot_versions.get(tab_id, 0) + 1
        self._snapshot_versions[tab_id] = snapshot_version
        ref_prefix = f"{tab_id}:s{snapshot_version}"
        title = redact_text(_single_line(str(await page.title())))[:200]
        elements = await page.eval_on_selector_all(
            INTERACTIVE_SELECTOR,
            """(nodes, options) => {
              let refIndex = 0;
              return nodes.flatMap((node) => {
                if (refIndex >= options.maxItems) return [];
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                if (style.visibility === 'hidden' || style.display === 'none' ||
                    rect.width <= 0 || rect.height <= 0) return [];
                refIndex += 1;
                const ref = options.refPrefix + ':e' + refIndex;
                node.setAttribute('data-ash-ref', ref);
                const role = node.getAttribute('role') || node.tagName.toLowerCase();
                const text = node.getAttribute('aria-label') ||
                    node.getAttribute('alt') || node.getAttribute('placeholder') ||
                    node.innerText || node.getAttribute('title') || '';
                return [{ref, role, text: text.replace(/\\s+/g, ' ').trim(),
                    disabled: Boolean(node.disabled) || node.getAttribute('aria-disabled') === 'true'}];
              });
            }""",
            {"maxItems": MAX_INTERACTIVE_ELEMENTS, "refPrefix": ref_prefix},
        )
        aria = await page.aria_snapshot(timeout=self.timeout_ms)
        password_values = await page.eval_on_selector_all(
            'input[type="password"]',
            """nodes => nodes.slice(0, 100).map(node => String(node.value || '').slice(0, 10000)).filter(Boolean)""",
        )
        aria = _redact_literals(aria, password_values)
        lines = [
            f"Tab: {tab_id}",
            f"Page: {title}",
            f"URL: {_redact_browser_url(str(page.url))}",
            "",
            "Interactive elements:",
        ]
        for item in elements:
            label = _single_line(str(item.get("text", "")))[:200]
            disabled = " disabled" if item.get("disabled") else ""
            lines.append(
                f"[{item.get('ref', '')}] {item.get('role', 'element')}{disabled} "
                f"{label!r}"
            )
        if not elements:
            lines.append("(none)")
        lines.extend(("", "ARIA snapshot:", aria))
        return _truncate_snapshot("\n".join(lines))

    async def screenshot(self, *, max_bytes: int) -> "BrowserScreenshot":
        page = await self.ensure_started()
        payload = await page.screenshot(type="png", full_page=False)
        if len(payload) > max_bytes:
            raise ValueError(
                f"browser screenshot exceeds {max_bytes} bytes; try scrolling or "
                "capturing a smaller page"
            )
        return BrowserScreenshot(
            media_type="image/png",
            data=base64.b64encode(payload).decode("ascii"),
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    async def upload_file(
        self,
        ref: str,
        file_path: str,
        *,
        safety_guard: SafetyGuard,
        max_bytes: int,
    ) -> str:
        from ash.commands.attachments import _reject_sensitive
        from ash.safety.scoped_io import read_scoped_bytes

        locator = await self._locator(ref)
        input_type = (await locator.get_attribute("type") or "").casefold()
        if input_type != "file":
            raise ValueError("browser_upload target must be a file input")
        page = await self.ensure_started()
        validated_path, payload = await asyncio.to_thread(
            read_scoped_bytes,
            file_path,
            safety_guard,
        )
        if len(payload) > max_bytes:
            raise ValueError(f"upload exceeds {max_bytes} bytes; choose a smaller file")
        await asyncio.to_thread(
            _reject_sensitive,
            validated_path,
            safety_guard.project_root,
        )
        mime_type = (
            mimetypes.guess_type(validated_path.name, strict=False)[0]
            or "application/octet-stream"
        )
        file_payload = {
            "name": validated_path.name,
            "mimeType": mime_type,
            "buffer": payload,
        }
        async with page.expect_file_chooser(
            timeout=self.timeout_ms,
        ) as chooser_info:
            await locator.click(timeout=self.timeout_ms)
        file_chooser = await chooser_info.value
        await file_chooser.set_files(file_payload)
        await self._settle(page)
        return await self.snapshot()

    async def download_file(
        self,
        ref: str,
        file_path: str,
        *,
        safety_guard: SafetyGuard,
        max_bytes: int,
        overwrite: bool,
    ) -> str:
        """Save one bounded browser download into the trusted workspace."""

        target = await asyncio.to_thread(
            safety_guard.validate_mutation_path,
            file_path,
        )
        page = await self.ensure_started()
        locator = await self._locator(ref)
        async with page.expect_download(timeout=self.timeout_ms) as download_info:
            await locator.click(timeout=self.timeout_ms)
        download = await download_info.value
        failure = await download.failure()
        if failure:
            raise ValueError(f"browser download failed: {failure}")
        temporary_path = await download.path()
        if temporary_path is None:
            raise ValueError("browser download did not produce a local file")
        payload = await asyncio.to_thread(
            _read_download_payload,
            Path(temporary_path),
            max_bytes,
        )
        await asyncio.to_thread(
            atomic_write_scoped_bytes,
            target,
            payload,
            safety_guard,
            overwrite=overwrite,
        )
        await self._settle(page)
        snapshot = await self.snapshot()
        return f"Downloaded {len(payload)} bytes to {target}\n\n{snapshot}"

    async def click(self, ref: str) -> str:
        page = await self.ensure_started()
        locator = await self._locator(ref)
        await locator.click(timeout=self.timeout_ms)
        await self._settle(page)
        return await self.snapshot()

    async def type_text(
        self,
        ref: str,
        text: str,
        *,
        submit: bool,
        clear: bool,
    ) -> str:
        page = await self.ensure_started()
        locator = await self._locator(ref)
        input_type = (await locator.get_attribute("type") or "").casefold()
        if input_type == "password":
            raise ValueError("browser_type refuses password fields")
        if clear:
            await locator.fill(text, timeout=self.timeout_ms)
        else:
            await locator.press_sequentially(text, timeout=self.timeout_ms)
        if submit:
            await locator.press("Enter", timeout=self.timeout_ms)
            await self._settle(page)
        return await self.snapshot()

    async def scroll(self, direction: str, amount: int) -> str:
        page = await self.ensure_started()
        delta = amount if direction == "down" else -amount
        await page.mouse.wheel(0, delta)
        await page.wait_for_timeout(150)
        return await self.snapshot()

    async def back(self) -> str:
        page = await self.ensure_started()
        await page.go_back(wait_until="domcontentloaded", timeout=self.timeout_ms)
        return await self.snapshot()

    async def list_tabs(self) -> str:
        await self.ensure_started()
        await self._drain_page_tasks()
        async with self._tab_lock:
            await self._enforce_tab_limit_unlocked()
            return await self._list_tabs_unlocked()

    async def _list_tabs_unlocked(self) -> str:
        pages = self._live_pages()
        self._prune_tab_pages(pages)
        if self._page is None or self._page.is_closed() or not any(
            self._page is page for page in pages
        ):
            self._page = pages[-1] if pages else None
        tabs: list[dict[str, Any]] = []
        for page in pages:
            tab_id = self._remember_tab(page)
            try:
                title = _single_line(str(await page.title()))[:200]
            except Exception:
                if page.is_closed():
                    continue
                title = ""
            tabs.append(
                {
                    "tab_id": tab_id,
                    "active": page is self._page,
                    "title": redact_text(title),
                    "url": _redact_browser_url(str(page.url))[:2048],
                }
            )
        await self._enforce_tab_limit_unlocked()
        live_pages = self._live_pages()
        tabs = [
            item
            for item in tabs
            if any(
                self._tab_pages.get(str(item["tab_id"])) is page
                for page in live_pages
            )
        ]
        if self._page is None or self._page.is_closed() or not any(
            self._page is page for page in live_pages
        ):
            self._page = live_pages[-1] if live_pages else None
        for item in tabs:
            item["active"] = self._tab_pages.get(str(item["tab_id"])) is self._page
        total = len(live_pages)
        truncated = len(tabs) < total
        payload = {"tabs": tabs, "total": total, "truncated": truncated}
        output = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        while len(output) > MAX_SNAPSHOT_CHARS and len(tabs) > 1:
            removable = next(
                (
                    index
                    for index in range(len(tabs) - 1, -1, -1)
                    if not bool(tabs[index]["active"])
                ),
                len(tabs) - 1,
            )
            tabs.pop(removable)
            truncated = True
            payload = {"tabs": tabs, "total": total, "truncated": truncated}
            output = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(output) > MAX_SNAPSHOT_CHARS:
            raise ValueError("browser tab listing exceeds the bounded output limit")
        return output

    async def open_tab(self, url: str, wait_until: str) -> str:
        validated = await asyncio.to_thread(
            _validate_browser_url, url, self.allowed_domains
        )
        await self.ensure_started()
        async with self._tab_lock:
            if self._context is None:
                raise BrowserUnavailableError("browser context is unavailable")
            await self._enforce_tab_limit_unlocked()
            if len(self._live_pages()) >= MAX_BROWSER_TABS:
                raise ValueError(
                    f"browser tab limit reached ({MAX_BROWSER_TABS}); close a tab first"
                )
            previous = self._page
            page_task = asyncio.create_task(
                self._context.new_page(),
                name="ash-browser-open-tab-create",
            )
            page, page_error, creation_interrupted = await _settle_browser_value_task(
                page_task
            )
            if page_error is not None:
                if creation_interrupted:
                    raise asyncio.CancelledError
                raise page_error
            if page is None:
                raise BrowserUnavailableError("browser did not create a new tab")
            self._remember_tab(page)
            self._page = page
            if creation_interrupted:
                await self._rollback_open_tab(page, previous)
                raise asyncio.CancelledError
            try:
                await page.goto(
                    validated,
                    wait_until=wait_until,
                    timeout=self.timeout_ms,
                )
                return await self.snapshot()
            except BaseException as primary:
                cleanup_error, cleanup_interrupted = await self._rollback_open_tab(
                    page, previous
                )
                if cleanup_error is not None:
                    primary.add_note(f"browser tab rollback failed: {cleanup_error}")
                if cleanup_interrupted:
                    primary.add_note("browser tab rollback was interrupted")
                raise

    async def _rollback_open_tab(
        self,
        page: Any,
        previous: Any | None,
    ) -> tuple[BaseException | None, bool]:
        cleanup_task = asyncio.create_task(
            page.close(),
            name="ash-browser-open-tab-cleanup",
        )
        cleanup_error, interrupted = await _settle_browser_cleanup_task(cleanup_task)
        if cleanup_error is not None or not page.is_closed():
            teardown_task = asyncio.create_task(
                self._close_unlocked(),
                name="ash-browser-open-tab-teardown",
            )
            teardown_error, teardown_interrupted = await _settle_browser_cleanup_task(
                teardown_task
            )
            interrupted = interrupted or teardown_interrupted
            error = cleanup_error or BrowserUnavailableError(
                "browser could not close a failed new tab"
            )
            if teardown_error is not None:
                error.add_note(f"browser session teardown failed: {teardown_error}")
            return error, interrupted
        self._prune_tab_pages()
        if previous is not None and not previous.is_closed():
            self._page = previous
        else:
            self._page = self._latest_page()
        return None, interrupted

    async def focus_tab(self, tab_id: str) -> str:
        await self.ensure_started()
        await self._drain_page_tasks()
        async with self._tab_lock:
            page = self._resolve_tab(tab_id)
            await page.bring_to_front()
            self._page = page
            return await self.snapshot()

    async def close_tab(self, tab_id: str) -> str:
        await self.ensure_started()
        await self._drain_page_tasks()
        async with self._tab_lock:
            page = self._resolve_tab(tab_id)
            pages = self._live_pages()
            if len(pages) <= 1:
                raise ValueError("browser_close_tab refuses to close the last live tab")
            was_active = page is self._page
            await page.close()
            self._prune_tab_pages()
            if was_active:
                self._page = self._latest_page()
            return await self._list_tabs_unlocked()

    async def _locator(self, ref: str) -> Any:
        match = ELEMENT_REF.fullmatch(ref)
        if match is None:
            raise ValueError(
                "browser element ref must look like <tab-id>:s<snapshot>:e1"
            )
        page = await self.ensure_started()
        active_tab_id = self._remember_tab(page)
        if match.group("tab_id") != active_tab_id:
            raise ValueError(
                f"browser element {ref!r} belongs to another tab; "
                "focus that tab and take a new snapshot"
            )
        snapshot_version = self._snapshot_versions.get(active_tab_id)
        if snapshot_version is None or int(match.group("snapshot")) != snapshot_version:
            raise ValueError(
                f"browser element {ref!r} is stale; take a new snapshot"
            )
        locator = page.locator(f'[data-ash-ref="{ref}"]')
        count = await locator.count()
        if count != 1:
            raise ValueError(
                f"browser element {ref!r} is stale or missing; take a new snapshot"
            )
        return locator

    async def _settle(self, page: Any) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=2000)
        except Exception:
            pass
        await self._drain_page_tasks()
        if not page.is_closed():
            self._page = page
            self._remember_tab(page)
        else:
            self._page = self._latest_page()
            if self._page is not None:
                self._remember_tab(self._page)
        self._prune_tab_pages()

    def _on_page_created(self, page: Any) -> None:
        task = asyncio.create_task(
            self._admit_page(page),
            name="ash-browser-page-admission",
        )
        self._page_tasks.add(task)
        task.add_done_callback(self._page_task_done)

    def _page_task_done(self, task: asyncio.Task[None]) -> None:
        self._page_tasks.discard(task)
        try:
            task.result()
        except BaseException:
            pass

    async def _drain_page_tasks(self) -> None:
        interrupted = False
        while True:
            pending = [task for task in self._page_tasks if not task.done()]
            if not pending:
                break
            waiter = asyncio.gather(*pending, return_exceptions=True)
            while not waiter.done():
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError:
                    interrupted = True
                    current = asyncio.current_task()
                    if current is not None:
                        current.uncancel()
                except BaseException:
                    break
        if interrupted:
            raise asyncio.CancelledError

    async def _admit_page(self, page: Any) -> None:
        async with self._tab_lock:
            if page.is_closed():
                return
            self._remember_tab(page)
            try:
                await self._enforce_tab_limit_unlocked()
            except BrowserUnavailableError:
                if not page.is_closed():
                    try:
                        await page.close()
                    except Exception:
                        pass
                self._prune_tab_pages()

    async def _enforce_tab_limit_unlocked(self) -> None:
        pages = self._live_pages()
        if len(pages) <= MAX_BROWSER_TABS:
            self._prune_tab_pages(pages)
            return
        keep = pages[:MAX_BROWSER_TABS]
        if (
            self._page is not None
            and any(self._page is page for page in pages)
            and not any(self._page is page for page in keep)
        ):
            keep[-1] = self._page
        overflow = [
            page
            for page in pages
            if not any(page is kept_page for kept_page in keep)
        ]
        for page in overflow:
            try:
                await page.close()
            except Exception:
                pass
        remaining = self._live_pages()
        self._prune_tab_pages(remaining)
        if len(remaining) > MAX_BROWSER_TABS:
            raise BrowserUnavailableError(
                "browser could not enforce the live-tab resource limit"
            )

    def _live_pages(self) -> list[Any]:
        if self._context is None:
            return []
        return [page for page in self._context.pages if not page.is_closed()]

    def _remember_tab(self, page: Any) -> str:
        for tab_id, known_page in self._tab_pages.items():
            if known_page is page:
                return tab_id
        tab_id = f"t{self._session_token}-{self._next_tab_id}"
        self._next_tab_id += 1
        self._tab_pages[tab_id] = page
        return tab_id

    def _prune_tab_pages(self, pages: list[Any] | None = None) -> None:
        live = pages if pages is not None else self._live_pages()
        self._tab_pages = {
            tab_id: page
            for tab_id, page in self._tab_pages.items()
            if any(page is live_page for live_page in live)
        }
        self._snapshot_versions = {
            tab_id: version
            for tab_id, version in self._snapshot_versions.items()
            if tab_id in self._tab_pages
        }

    def _resolve_tab(self, tab_id: str) -> Any:
        if not TAB_ID.fullmatch(tab_id):
            raise ValueError("browser tab id must look like t1234abcd-1")
        self._prune_tab_pages()
        page = self._tab_pages.get(tab_id)
        if page is None or page.is_closed():
            raise ValueError(
                f"browser tab {tab_id!r} is stale or missing; list tabs again"
            )
        return page

    def _latest_page(self) -> Any:
        live = self._live_pages()
        if live:
            return live[-1]
        return self._page

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            cleanup_task = asyncio.create_task(
                self._close_unlocked(),
                name="ash-browser-close",
            )
            cleanup_error, interrupted = await _settle_browser_cleanup_task(
                cleanup_task
            )
            if cleanup_error is not None:
                raise cleanup_error
            if interrupted:
                raise asyncio.CancelledError

    async def _close_unlocked(self) -> None:
        page_tasks = list(self._page_tasks)
        for task in page_tasks:
            task.cancel()
        if page_tasks:
            await asyncio.gather(*page_tasks, return_exceptions=True)
        self._page_tasks.clear()
        for resource in (self._context, self._browser):
            if resource is not None:
                try:
                    await resource.close()
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        if self._proxy is not None:
            try:
                await self._proxy.close()
            except Exception:
                pass
        self._page = None
        self._tab_pages.clear()
        self._snapshot_versions.clear()
        self._context = None
        self._browser = None
        self._playwright = None
        self._proxy = None


async def _settle_browser_cleanup_task(
    task: asyncio.Task[None],
) -> tuple[BaseException | None, bool]:
    """Settle browser cleanup despite repeated cancellation of its caller."""

    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
        except BaseException:
            break
    try:
        task.result()
    except BaseException as exc:
        return exc, interrupted
    return None, interrupted


async def _settle_browser_value_task(
    task: asyncio.Task[Any],
) -> tuple[Any | None, BaseException | None, bool]:
    """Settle one value-producing browser task despite caller cancellation."""

    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
        except BaseException:
            break
    try:
        return task.result(), None, interrupted
    except BaseException as exc:
        return None, exc, interrupted


def _validate_cdp_url(url: str) -> str:
    normalized = url.strip()
    if not normalized:
        raise ValueError("browser CDP URL must not be empty")
    try:
        parsed = urlparse(normalized)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("browser CDP URL has an invalid port") from exc
    if parsed.port == 0:
        raise ValueError("browser CDP URL port must be between 1 and 65535")
    if parsed.scheme.casefold() not in {"http", "https", "ws", "wss"}:
        raise ValueError("browser CDP URL must use http, https, ws, or wss")
    if parsed.username or parsed.password:
        raise ValueError("browser CDP URL cannot contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("browser CDP URL cannot contain query parameters or fragments")
    host = (parsed.hostname or "").casefold()
    if not host:
        raise ValueError("browser CDP URL must include a host")
    if host != "localhost":
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("browser CDP URL must target loopback") from exc
        if not address.is_loopback:
            raise ValueError("browser CDP URL must target loopback")
    return normalized


def _validate_cdp_storage_state(state: dict[str, Any]) -> None:
    try:
        encoded = json.dumps(
            state,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise BrowserUnavailableError("CDP browser returned invalid storage state") from exc
    if len(encoded) > MAX_CDP_STORAGE_STATE_BYTES:
        raise BrowserUnavailableError(
            f"CDP storage state exceeded {MAX_CDP_STORAGE_STATE_BYTES} bytes"
        )


def _validate_browser_url(url: str, allowed_domains: tuple[str, ...]) -> str:
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise ValueError("Browser URLs cannot contain embedded credentials")
    if parsed.scheme in {"ws", "wss"}:
        equivalent = parsed._replace(
            scheme="https" if parsed.scheme == "wss" else "http"
        )
        _validate_public_url(urlunparse(equivalent), allowed_domains=allowed_domains)
        return url
    return _validate_public_url(url, allowed_domains=allowed_domains)


def _redact_browser_url(url: str) -> str:
    parsed = urlparse(url)

    def redact_component(value: str) -> str:
        if not value or "=" not in value:
            return redact_text(value)
        pairs = parse_qsl(value, keep_blank_values=True)
        redacted_pairs = []
        for name, field_value in pairs:
            normalized = re.sub(
                r"(?<=[a-z0-9])(?=[A-Z])",
                "_",
                name,
            ).casefold().replace("-", "_")
            sensitive = (
                normalized in SENSITIVE_BROWSER_URL_FIELDS
                or normalized.endswith("_token")
                or normalized.endswith("_secret")
                or normalized.endswith("_password")
                or normalized.endswith("_api_key")
            )
            redacted_pairs.append(
                (
                    name,
                    (
                        "[REDACTED]"
                        if sensitive and field_value
                        else redact_text(field_value)
                    ),
                )
            )
        return urlencode(redacted_pairs, safe="[]")

    netloc = parsed.netloc
    if parsed.username is not None or parsed.password is not None:
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
    fragment = parsed.fragment
    if "?" in fragment:
        fragment_path, fragment_query = fragment.split("?", 1)
        fragment = (
            f"{redact_text(fragment_path)}?{redact_component(fragment_query)}"
        )
    else:
        fragment = redact_component(fragment)
    return urlunparse(
        parsed._replace(
            netloc=netloc,
            path=redact_text(parsed.path),
            params=redact_text(parsed.params),
            query=redact_component(parsed.query),
            fragment=fragment,
        )
    )


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _redact_literals(value: str, secrets: list[Any]) -> str:
    redacted = value
    normalized = sorted(
        {str(secret) for secret in secrets if isinstance(secret, str) and secret},
        key=len,
        reverse=True,
    )
    for secret in normalized:
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _truncate_snapshot(value: str) -> str:
    if len(value) <= MAX_SNAPSHOT_CHARS:
        return value
    half = (MAX_SNAPSHOT_CHARS - 45) // 2
    return value[:half] + "\n[browser snapshot truncated]\n" + value[-half:]


def _read_download_payload(path: Path, max_bytes: int) -> bytes:
    """Read a Playwright temporary download without exceeding its byte cap."""

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"browser download cannot be inspected: {exc}") from exc
    if size > max_bytes:
        raise ValueError(
            f"browser download exceeds {max_bytes} bytes; choose a smaller file"
        )
    try:
        with path.open("rb") as handle:
            payload = handle.read(max_bytes + 1)
    except OSError as exc:
        raise ValueError(f"browser download cannot be read: {exc}") from exc
    if len(payload) > max_bytes:
        raise ValueError(
            f"browser download exceeds {max_bytes} bytes; choose a smaller file"
        )
    return payload


class NavigateArgs(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = (
        "domcontentloaded"
    )


class TabArgs(BaseModel):
    tab_id: str = Field(..., min_length=11, max_length=19)

    @field_validator("tab_id")
    @classmethod
    def validate_tab_id(cls, value: str) -> str:
        if not TAB_ID.fullmatch(value):
            raise ValueError("browser tab id must look like t1234abcd-1")
        return value


class ElementArgs(BaseModel):
    ref: str = Field(..., min_length=17, max_length=40)

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        if not ELEMENT_REF.fullmatch(value):
            raise ValueError("browser element ref must look like t1234abcd-1:s1:e1")
        return value


class TypeArgs(ElementArgs):
    text: str = Field(..., max_length=10_000)
    submit: bool = False
    clear: bool = True


class ScrollArgs(BaseModel):
    direction: Literal["up", "down"] = "down"
    amount: int = Field(600, ge=50, le=5000)


class EmptyArgs(BaseModel):
    pass


class ScreenshotArgs(BaseModel):
    max_bytes: int = Field(
        2_000_000,
        ge=10_000,
        le=5_000_000,
        description="Maximum PNG payload accepted from the browser.",
    )


class UploadArgs(ElementArgs):
    file_path: str = Field(..., min_length=1, max_length=2048)
    max_bytes: int = Field(
        5_000_000,
        ge=1,
        le=20_000_000,
        description="Maximum upload payload accepted from the workspace.",
    )


class DownloadArgs(ElementArgs):
    file_path: str = Field(..., min_length=1, max_length=2048)
    max_bytes: int = Field(
        20_000_000,
        ge=1,
        le=20_000_000,
        description="Maximum download payload written to the workspace.",
    )
    overwrite: bool = False


class _BrowserTool(BaseTool):
    def __init__(self, safety_guard: SafetyGuard, session: BrowserSession) -> None:
        super().__init__(safety_guard)
        self.session = session

    async def aclose(self) -> None:
        await self.session.close()

    async def _result(self, operation: Any) -> ToolResult:
        try:
            output = await operation
        except (BrowserUnavailableError, ValueError) as exc:
            return ToolResult(success=False, output="", error=redact_text(str(exc)))
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error="browser action failed: " + redact_text(str(exc))[:500],
            )
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
            truncated=(
                "[browser snapshot truncated]" in output
                or output.startswith('{"tabs":')
                and '"truncated":true' in output
            ),
        )


class BrowserNavigateTool(_BrowserTool):
    name = "browser_navigate"
    description = "Navigate the isolated browser to a public HTTP(S) URL and return a bounded page snapshot."
    args_schema = NavigateArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = NavigateArgs(**kwargs)
        return await self._result(self.session.navigate(args.url, args.wait_until))


class BrowserSnapshotTool(_BrowserTool):
    name = "browser_snapshot"
    description = "Read the current page's bounded ARIA snapshot and interactive element references."
    args_schema = EmptyArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        EmptyArgs(**kwargs)
        return await self._result(self.session.snapshot())


class BrowserTabsTool(_BrowserTool):
    name = "browser_tabs"
    description = (
        "List live tabs in the isolated browser with stable session-local tab IDs "
        "and the currently active tab."
    )
    args_schema = EmptyArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        EmptyArgs(**kwargs)
        return await self._result(self.session.list_tabs())


class BrowserOpenTabTool(_BrowserTool):
    name = "browser_open_tab"
    description = (
        "Open a public HTTP(S) URL in a new isolated-browser tab, make it active, "
        "and return its bounded snapshot."
    )
    args_schema = NavigateArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = NavigateArgs(**kwargs)
        return await self._result(self.session.open_tab(args.url, args.wait_until))


class BrowserFocusTabTool(_BrowserTool):
    name = "browser_focus_tab"
    description = (
        "Focus one live isolated-browser tab by its stable tab ID and return its "
        "bounded snapshot."
    )
    args_schema = TabArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = TabArgs(**kwargs)
        return await self._result(self.session.focus_tab(args.tab_id))


class BrowserCloseTabTool(_BrowserTool):
    name = "browser_close_tab"
    description = (
        "Close one live isolated-browser tab by its stable tab ID; the final live "
        "tab is protected from closure."
    )
    args_schema = TabArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = TabArgs(**kwargs)
        return await self._result(self.session.close_tab(args.tab_id))


class BrowserClickTool(_BrowserTool):
    name = "browser_click"
    description = "Click one element reference from the latest browser snapshot and return the updated snapshot."
    args_schema = ElementArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ElementArgs(**kwargs)
        return await self._result(self.session.click(args.ref))


class BrowserTypeTool(_BrowserTool):
    name = "browser_type"
    description = "Fill or type into a referenced non-password browser control, optionally submit, and return the updated snapshot."
    args_schema = TypeArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = TypeArgs(**kwargs)
        return await self._result(
            self.session.type_text(
                args.ref,
                args.text,
                submit=args.submit,
                clear=args.clear,
            )
        )


class BrowserScrollTool(_BrowserTool):
    name = "browser_scroll"
    description = (
        "Scroll the current browser page up or down and return the updated snapshot."
    )
    args_schema = ScrollArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ScrollArgs(**kwargs)
        return await self._result(self.session.scroll(args.direction, args.amount))


class BrowserBackTool(_BrowserTool):
    name = "browser_back"
    description = "Navigate the isolated browser back one history entry and return the updated snapshot."
    args_schema = EmptyArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        EmptyArgs(**kwargs)
        return await self._result(self.session.back())


class BrowserScreenshotTool(_BrowserTool):
    name = "browser_screenshot"
    description = (
        "Capture a bounded PNG screenshot of the current browser page for "
        "vision-capable models."
    )
    args_schema = ScreenshotArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ScreenshotArgs(**kwargs)
        try:
            image = await self.session.screenshot(max_bytes=args.max_bytes)
            output = (
                f'<attachment kind="image" path="browser-screenshot" '
                f'media_type="{image.media_type}" sha256="{image.sha256}" />'
            )
        except (BrowserUnavailableError, ValueError) as exc:
            return ToolResult(success=False, output="", error=redact_text(str(exc)))
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error="browser screenshot failed: " + redact_text(str(exc))[:500],
            )
        return ToolResult(
            success=True,
            output=output,
            token_count=count_output_tokens(output),
            images=[
                {
                    "path": "browser-screenshot",
                    "media_type": image.media_type,
                    "sha256": image.sha256,
                }
            ],
            image_blocks=[
                {
                    "type": "image",
                    "media_type": image.media_type,
                    "data": image.data,
                }
            ],
        )


class BrowserUploadTool(_BrowserTool):
    name = "browser_upload"
    description = (
        "Upload one bounded workspace file through a referenced file input and "
        "return the updated snapshot."
    )
    args_schema = UploadArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = UploadArgs(**kwargs)
        return await self._result(
            self.session.upload_file(
                args.ref,
                args.file_path,
                safety_guard=self.safety_guard,
                max_bytes=args.max_bytes,
            )
        )


class BrowserDownloadTool(_BrowserTool):
    name = "browser_download"
    description = (
        "Save one bounded browser download to a workspace path; existing files "
        "are preserved unless overwrite is explicitly requested."
    )
    args_schema = DownloadArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = DownloadArgs(**kwargs)
        return await self._result(
            self.session.download_file(
                args.ref,
                args.file_path,
                safety_guard=self.safety_guard,
                max_bytes=args.max_bytes,
                overwrite=args.overwrite,
            )
        )


def build_browser_tools(
    safety_guard: SafetyGuard,
    *,
    headless: bool = True,
    timeout_seconds: float = 30.0,
    allowed_domains: list[str] | tuple[str, ...] | None = None,
    profile_path: Path | None = None,
    cdp_url: str | None = None,
    cdp_reuse_storage_state: bool = False,
) -> list[BaseTool]:
    session = BrowserSession(
        headless=headless,
        timeout_seconds=timeout_seconds,
        allowed_domains=allowed_domains,
        profile_path=profile_path,
        cdp_url=cdp_url,
        cdp_reuse_storage_state=cdp_reuse_storage_state,
    )
    return [
        BrowserNavigateTool(safety_guard, session),
        BrowserSnapshotTool(safety_guard, session),
        BrowserTabsTool(safety_guard, session),
        BrowserOpenTabTool(safety_guard, session),
        BrowserFocusTabTool(safety_guard, session),
        BrowserCloseTabTool(safety_guard, session),
        BrowserClickTool(safety_guard, session),
        BrowserTypeTool(safety_guard, session),
        BrowserScrollTool(safety_guard, session),
        BrowserBackTool(safety_guard, session),
        BrowserScreenshotTool(safety_guard, session),
        BrowserUploadTool(safety_guard, session),
        BrowserDownloadTool(safety_guard, session),
    ]


def browser_session_from_tools(
    tools: Mapping[str, BaseTool],
) -> BrowserSession | None:
    """Return the single shared browser session owned by one complete tool family."""

    sessions: dict[int, BrowserSession] = {}
    for name in BROWSER_TOOL_NAMES:
        tool = tools.get(name)
        if tool is None:
            return None
        session = getattr(tool, "session", None)
        if not isinstance(session, BrowserSession):
            return None
        sessions[id(session)] = session
    if len(sessions) != 1:
        return None
    return next(iter(sessions.values()))
