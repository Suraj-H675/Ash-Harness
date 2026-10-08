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
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, Field, field_validator, model_validator

from ash.core.redaction import redact_text, redact_url
from ash.safe_io import (
    read_bounded_bytes,
    remove_anchored_path,
    validate_unlinked_directory_path,
)
from ash.safety.environment import build_scrubbed_environment
from ash.safety.guard import SafetyGuard
from ash.safety.network import loopback_url_allowed, normalize_loopback_origins
from ash.safety.scoped_io import atomic_write_scoped_bytes
from ash.tools.base import BaseTool, ToolResult, count_output_tokens
from ash.tools.browser_proxy import BrowserPolicyProxy
from ash.tools.web import _host_allowed, _normalize_allowed_domains, _validate_public_url
from ash.ui.safe_text import terminal_safe_text


MAX_SNAPSHOT_CHARS = 30_000
MAX_INTERACTIVE_ELEMENTS = 150
MAX_CDP_STORAGE_STATE_BYTES = 4 * 1024 * 1024
MAX_CDP_SOURCE_TABS = 64
MAX_BROWSER_TABS = 32
MAX_BROWSER_FRAMES = 32
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
        "browser_click_at",
        "browser_type",
        "browser_press",
        "browser_hover",
        "browser_select",
        "browser_drag",
        "browser_wait",
        "browser_dialog",
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
        allowed_local_origins: list[str] | tuple[str, ...] | None = None,
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
        self.allowed_local_origins = normalize_loopback_origins(
            allowed_local_origins or ()
        )
        normalized_cdp = _validate_cdp_url(cdp_url) if cdp_url else ""
        if normalized_cdp and profile_path is not None:
            raise ValueError("browser CDP attachment cannot use an Ash persistent profile")
        if cdp_reuse_storage_state and not normalized_cdp:
            raise ValueError("browser_cdp_reuse_storage_state requires browser_cdp_url")
        if cdp_reuse_storage_state and not self.allowed_domains:
            raise ValueError(
                "browser_cdp_reuse_storage_state requires non-empty "
                "allowed_web_domains to scope imported authentication state"
            )
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
        self._cleanup_failed = False
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._tab_pages: dict[str, Any] = {}
        self._snapshot_versions: dict[str, int] = {}
        self._snapshot_ref_scopes: dict[str, Any] = {}
        self._snapshot_task: asyncio.Task[str] | None = None
        self._session_token = secrets.token_hex(4)
        self._next_tab_id = 1
        self._page_tasks: set[asyncio.Task[None]] = set()
        self._page_admission_dirty = False
        self._proxy: BrowserPolicyProxy | None = None
        self._pending_dialog: Any | None = None
        self._dialog_page: Any | None = None
        self._dialog_action_task: asyncio.Task[Any] | None = None
        self._dialog_queue: asyncio.Queue[Any] | None = None
        self._dialog_handler: Callable[[Any], None] | None = None

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
                    self._close_unlocked(cancel_snapshot=False),
                    name="ash-browser-startup-cleanup",
                )
                cleanup_error, cleanup_interrupted = (
                    await _settle_browser_cleanup_task(cleanup_task)
                )
                if cleanup_interrupted:
                    cancellation = asyncio.CancelledError()
                    if cleanup_error is not None:
                        cancellation.add_note(
                            "browser session cleanup failed while restart was "
                            "being cancelled: "
                            + _redact_browser_text(str(cleanup_error))[:500]
                        )
                    raise cancellation from cleanup_error
                if cleanup_error is not None:
                    raise cleanup_error
            try:
                if self.profile_path is not None:
                    validate_unlinked_directory_path(
                        self.profile_path, label="browser profile directory"
                    )
                self._proxy = BrowserPolicyProxy(
                    self.allowed_domains,
                    allowed_local_origins=self.allowed_local_origins,
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
                        _validate_cdp_storage_state(raw_storage_state)
                        storage_state = _filter_cdp_storage_state(
                            raw_storage_state,
                            self.allowed_domains,
                        )
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
            except asyncio.CancelledError as primary_error:
                cleanup_task = asyncio.create_task(
                    self._close_unlocked(cancel_snapshot=False),
                    name="ash-browser-startup-cleanup",
                )
                cleanup_error, _ = await _settle_browser_cleanup_task(cleanup_task)
                if cleanup_error is not None:
                    primary_error.add_note(
                        "browser session cleanup failed after startup cancellation: "
                        + _redact_browser_text(str(cleanup_error))[:500]
                    )
                raise
            except Exception as exc:
                cleanup_task = asyncio.create_task(
                    self._close_unlocked(cancel_snapshot=False),
                    name="ash-browser-startup-cleanup",
                )
                cleanup_error, cleanup_interrupted = (
                    await _settle_browser_cleanup_task(cleanup_task)
                )
                if cleanup_interrupted:
                    cancellation = asyncio.CancelledError()
                    cancellation.add_note(
                        "browser startup failed before cancellation: "
                        + _redact_browser_text(str(exc))[:500]
                    )
                    if cleanup_error is not None:
                        cancellation.add_note(
                            "browser session cleanup also failed: "
                            + _redact_browser_text(str(cleanup_error))[:500]
                        )
                    raise cancellation from exc
                message = _redact_browser_text(str(exc))[:500]
                if "Executable doesn't exist" in message:
                    error = BrowserUnavailableError(
                        "Chromium is not installed; run `ash setup browser`."
                    )
                else:
                    error = BrowserUnavailableError(
                        f"Could not start the browser session: {message}"
                    )
                if cleanup_error is not None:
                    error.add_note(
                        "browser session cleanup failed: "
                        + _redact_browser_text(str(cleanup_error))[:500]
                    )
                raise error from exc

    async def _route_request(self, route: Any, request: Any) -> None:
        try:
            await asyncio.to_thread(
                _validate_browser_url,
                request.url,
                self.allowed_domains,
                self.allowed_local_origins,
            )
        except ValueError:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _route_websocket(self, websocket: Any) -> None:
        try:
            await asyncio.to_thread(
                _validate_browser_url,
                websocket.url,
                self.allowed_domains,
                self.allowed_local_origins,
            )
        except ValueError:
            await websocket.close(code=1008, reason="Blocked by Ash network policy")
            return
        websocket.connect_to_server()

    async def navigate(self, url: str, wait_until: str) -> str:
        validated = await asyncio.to_thread(
            _validate_browser_url,
            url,
            self.allowed_domains,
            self.allowed_local_origins,
        )
        page = await self.ensure_started()
        modal = await self._run_dialog_aware_action(
            page,
            lambda: page.goto(
                validated,
                wait_until=wait_until,
                timeout=self.timeout_ms,
            ),
            label="navigate",
        )
        if modal is not None:
            return modal
        return await self.snapshot()

    async def snapshot(self) -> str:
        if self._pending_dialog is not None:
            return self._render_dialog_state()
        task = self._snapshot_task
        if task is None or task.done():
            task = asyncio.create_task(
                self._snapshot_once(),
                name="ash-browser-snapshot",
            )
            self._snapshot_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._snapshot_task is task and task.done():
                self._snapshot_task = None

    async def _snapshot_once(self) -> str:
        page = await self.ensure_started()
        tab_id = self._remember_tab(page)
        snapshot_version = self._snapshot_versions.get(tab_id, 0) + 1
        self._snapshot_versions[tab_id] = snapshot_version
        ref_prefix = f"{tab_id}:s{snapshot_version}"
        for ref in tuple(self._snapshot_ref_scopes):
            if ref.startswith(f"{tab_id}:"):
                self._snapshot_ref_scopes.pop(ref, None)
        title = _redact_browser_text(_single_line(str(await page.title())))[:200]
        scopes: list[tuple[str | None, Any]] = [(None, page)]
        page_frames = list(getattr(page, "frames", ()) or ())
        child_frames = page_frames[1:MAX_BROWSER_FRAMES]
        scopes.extend(
            (f"f{index}", frame)
            for index, frame in enumerate(child_frames, start=1)
        )
        frames_truncated = len(page_frames) > MAX_BROWSER_FRAMES
        element_groups: list[tuple[str | None, str, list[dict[str, Any]]]] = []
        aria_groups: list[tuple[str | None, str, str]] = []
        ref_index = 0
        for frame_label, scope in scopes:
            if frame_label is not None and not await self._frame_is_visible(scope):
                continue
            try:
                frame_url = _redact_browser_url(str(getattr(scope, "url", page.url)))
                elements = await scope.eval_on_selector_all(
                    INTERACTIVE_SELECTOR,
                    """(nodes, options) => {
              let refIndex = options.startIndex;
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
                    {
                        "maxItems": MAX_INTERACTIVE_ELEMENTS,
                        "refPrefix": ref_prefix,
                        "startIndex": ref_index,
                    },
                )
                ref_index += len(elements)
                for item in elements:
                    ref = str(item.get("ref", ""))
                    if ref:
                        self._snapshot_ref_scopes[ref] = scope
                element_groups.append((frame_label, frame_url, elements))
                password_values = await scope.eval_on_selector_all(
                    'input[type="password"]',
                    """nodes => nodes.slice(0, 100).map(node => String(node.value || '').slice(0, 10000)).filter(Boolean)""",
                )
                if frame_label is None:
                    aria = await page.aria_snapshot(timeout=self.timeout_ms)
                else:
                    aria = await scope.locator("html").aria_snapshot(
                        timeout=self.timeout_ms
                    )
                aria_groups.append(
                    (
                        frame_label,
                        frame_url,
                        _redact_browser_text(
                            _redact_literals(str(aria), password_values)
                        ),
                    )
                )
            except Exception:
                if frame_label is None:
                    raise
                continue
        lines = [
            f"Tab: {tab_id}",
            f"Page: {title}",
            f"URL: {_redact_browser_url(str(page.url))}",
            "",
            "Interactive elements:",
        ]
        rendered_elements = 0
        for frame_label, frame_url, elements in element_groups:
            if frame_label is not None:
                lines.extend(("", f"Frame {frame_label}: {frame_url[:2048]}"))
            for item in elements:
                label = _redact_browser_text(
                    _single_line(str(item.get("text", "")))
                )[:200]
                disabled = " disabled" if item.get("disabled") else ""
                lines.append(
                    f"[{item.get('ref', '')}] {item.get('role', 'element')}{disabled} "
                    f"{label!r}"
                )
                rendered_elements += 1
        if not rendered_elements:
            lines.append("(none)")
        if frames_truncated:
            lines.extend(("", f"[frame list truncated at {MAX_BROWSER_FRAMES}]"))
        for frame_label, frame_url, aria in aria_groups:
            if frame_label is None:
                lines.extend(("", "ARIA snapshot:", aria))
            else:
                lines.extend(
                    (
                        "",
                        f"Frame {frame_label} ARIA snapshot: {frame_url[:2048]}",
                        aria,
                    )
                )
        return _truncate_snapshot("\n".join(lines))

    async def _frame_is_visible(self, frame: Any) -> bool:
        current = frame
        while getattr(current, "parent_frame", None) is not None:
            try:
                frame_element = await current.frame_element()
                if not await frame_element.is_visible():
                    return False
            except Exception:
                return False
            current = current.parent_frame
        return True

    async def screenshot(self, *, max_bytes: int) -> "BrowserScreenshot":
        self._ensure_no_pending_dialog("take a screenshot")
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

        self._ensure_no_pending_dialog("upload a file")
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

        self._ensure_no_pending_dialog("download a file")
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
        self._ensure_no_pending_dialog("click another element")
        page = await self.ensure_started()
        locator = await self._locator(ref)
        modal = await self._run_dialog_aware_action(
            page,
            lambda: locator.click(timeout=self.timeout_ms),
            label="click",
        )
        if modal is not None:
            return modal
        await self._settle(page)
        return await self.snapshot()

    async def click_at(
        self,
        x: int,
        y: int,
        *,
        screenshot_sha256: str,
    ) -> str:
        self._ensure_no_pending_dialog("click another coordinate")
        page = await self.ensure_started()
        viewport = getattr(page, "viewport_size", None)
        if (
            not isinstance(viewport, dict)
            or not isinstance(viewport.get("width"), int)
            or not isinstance(viewport.get("height"), int)
        ):
            raise ValueError("browser coordinate click requires a known viewport")
        width = int(viewport["width"])
        height = int(viewport["height"])
        if not 0 <= x < width or not 0 <= y < height:
            raise ValueError(
                f"browser coordinate ({x}, {y}) is outside viewport {width}x{height}"
            )
        current = await page.screenshot(type="png", full_page=False)
        current_sha256 = hashlib.sha256(current).hexdigest()
        if not secrets.compare_digest(current_sha256, screenshot_sha256):
            raise ValueError(
                "browser screenshot is stale; take a fresh browser_screenshot "
                "before using browser_click_at"
            )
        modal = await self._run_dialog_aware_action(
            page,
            lambda: page.mouse.click(x, y),
            label="coordinate-click",
        )
        if modal is not None:
            return modal
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
        self._ensure_no_pending_dialog("type into another element")
        page = await self.ensure_started()
        locator = await self._locator(ref)
        input_type = (await locator.get_attribute("type") or "").casefold()
        if input_type == "password":
            raise ValueError("browser_type refuses password fields")

        async def type_action() -> None:
            if clear:
                await locator.fill(text, timeout=self.timeout_ms)
            else:
                await locator.press_sequentially(text, timeout=self.timeout_ms)
            if submit:
                await locator.press("Enter", timeout=self.timeout_ms)

        modal = await self._run_dialog_aware_action(
            page,
            type_action,
            label="type",
        )
        if modal is not None:
            return modal
        if submit:
            await self._settle(page)
        return await self.snapshot()

    async def press_key(self, key: str, *, ref: str | None = None) -> str:
        self._ensure_no_pending_dialog("press another key")
        page = await self.ensure_started()
        if ref is None:
            async def action() -> Any:
                return await page.keyboard.press(key)
        else:
            locator = await self._locator(ref)

            async def action() -> Any:
                return await locator.press(key, timeout=self.timeout_ms)
        modal = await self._run_dialog_aware_action(
            page,
            action,
            label="press",
        )
        if modal is not None:
            return modal
        await self._settle(page)
        return await self.snapshot()

    async def hover(self, ref: str) -> str:
        self._ensure_no_pending_dialog("hover another element")
        page = await self.ensure_started()
        locator = await self._locator(ref)
        modal = await self._run_dialog_aware_action(
            page,
            lambda: locator.hover(timeout=self.timeout_ms),
            label="hover",
        )
        if modal is not None:
            return modal
        await self._settle(page)
        return await self.snapshot()

    async def select(self, ref: str, values: list[str]) -> str:
        self._ensure_no_pending_dialog("select another value")
        page = await self.ensure_started()
        locator = await self._locator(ref)
        modal = await self._run_dialog_aware_action(
            page,
            lambda: locator.select_option(values, timeout=self.timeout_ms),
            label="select",
        )
        if modal is not None:
            return modal
        await self._settle(page)
        return await self.snapshot()

    async def drag(self, source_ref: str, target_ref: str) -> str:
        self._ensure_no_pending_dialog("drag another element")
        page = await self.ensure_started()
        source = await self._locator(source_ref)
        target = await self._locator(target_ref)
        modal = await self._run_dialog_aware_action(
            page,
            lambda: source.drag_to(target, timeout=self.timeout_ms),
            label="drag",
        )
        if modal is not None:
            return modal
        await self._settle(page)
        return await self.snapshot()

    async def wait_for(
        self,
        *,
        condition: str,
        ref: str | None,
        value: str | None,
        load_state: str,
        timeout_seconds: float,
    ) -> str:
        self._ensure_no_pending_dialog("wait for another page condition")
        page = await self.ensure_started()
        timeout_ms = min(self.timeout_ms, max(100, int(timeout_seconds * 1000)))
        if condition == "ref_visible":
            assert ref is not None
            locator = await self._locator(ref)
            await locator.wait_for(state="visible", timeout=timeout_ms)
        elif condition == "text":
            assert value is not None
            await page.get_by_text(value, exact=False).first.wait_for(
                state="visible",
                timeout=timeout_ms,
            )
        elif condition == "url_contains":
            assert value is not None
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_ms / 1000
            while value not in str(page.url):
                if loop.time() >= deadline:
                    raise ValueError(
                        f"browser wait timed out before URL contained {value!r}"
                    )
                await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))
        elif condition == "load":
            await page.wait_for_load_state(load_state, timeout=timeout_ms)
        else:  # pragma: no cover - validated by WaitArgs
            raise ValueError(f"unsupported browser wait condition: {condition}")
        await self._settle(page)
        return await self.snapshot()

    async def scroll(self, direction: str, amount: int) -> str:
        self._ensure_no_pending_dialog("scroll the page")
        page = await self.ensure_started()
        delta = amount if direction == "down" else -amount
        async def scroll_action() -> None:
            await page.mouse.wheel(0, delta)
            await page.wait_for_timeout(150)

        modal = await self._run_dialog_aware_action(
            page,
            scroll_action,
            label="scroll",
        )
        if modal is not None:
            return modal
        return await self.snapshot()

    async def back(self) -> str:
        self._ensure_no_pending_dialog("navigate back")
        page = await self.ensure_started()
        modal = await self._run_dialog_aware_action(
            page,
            lambda: page.go_back(
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            ),
            label="back",
        )
        if modal is not None:
            return modal
        return await self.snapshot()

    async def handle_dialog(self, *, accept: bool, prompt_text: str | None) -> str:
        dialog = self._pending_dialog
        page = self._dialog_page
        action_task = self._dialog_action_task
        queue = self._dialog_queue
        if (
            dialog is None
            or page is None
            or action_task is None
            or queue is None
        ):
            raise ValueError("browser has no pending dialog")
        dialog_type = str(getattr(dialog, "type", "dialog"))
        if prompt_text is not None and dialog_type != "prompt":
            raise ValueError("prompt_text is only valid for a prompt dialog")
        try:
            if accept:
                if prompt_text is None:
                    await dialog.accept()
                else:
                    await dialog.accept(prompt_text=prompt_text)
            else:
                await dialog.dismiss()
        except Exception:
            raise
        self._pending_dialog = None
        modal = await self._wait_for_action_or_dialog(
            page,
            action_task,
            queue,
        )
        if modal is not None:
            return modal
        self._clear_dialog_action_state()
        await self._settle(page)
        return await self.snapshot()

    def _ensure_no_pending_dialog(self, operation: str) -> None:
        if self._pending_dialog is None:
            return
        raise ValueError(
            f"browser has a pending dialog; use browser_dialog before you {operation}"
        )

    def _render_dialog_state(self) -> str:
        dialog = self._pending_dialog
        if dialog is None:
            raise ValueError("browser has no pending dialog")
        dialog_type = _single_line(str(getattr(dialog, "type", "dialog")))[:32]
        message = _redact_browser_text(
            _single_line(str(getattr(dialog, "message", "")))
        )[:1_000]
        return (
            "Modal state:\n"
            f"- {dialog_type} dialog with message {message!r}\n"
            "- Use browser_dialog to accept or dismiss it before other browser actions."
        )

    async def _run_dialog_aware_action(
        self,
        page: Any,
        action: Callable[[], Coroutine[Any, Any, Any]],
        *,
        label: str,
    ) -> str | None:
        self._ensure_no_pending_dialog(f"run browser action {label!r}")
        on_event = getattr(page, "on", None)
        remove_listener = getattr(page, "remove_listener", None)
        if not callable(on_event) or not callable(remove_listener):
            await action()
            return None

        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)

        def capture_dialog(dialog: Any) -> None:
            if queue.empty():
                queue.put_nowait(dialog)

        on_event("dialog", capture_dialog)
        action_task: asyncio.Task[Any] = asyncio.create_task(
            action(),
            name=f"ash-browser-{label}-action",
        )
        try:
            modal = await self._wait_for_action_or_dialog(
                page,
                action_task,
                queue,
            )
        except BaseException:
            remove_listener("dialog", capture_dialog)
            if not action_task.done():
                action_task.cancel()
                await asyncio.gather(action_task, return_exceptions=True)
            raise
        if modal is None:
            remove_listener("dialog", capture_dialog)
            return None
        self._dialog_page = page
        self._dialog_action_task = action_task
        self._dialog_queue = queue
        self._dialog_handler = capture_dialog
        return modal

    async def _wait_for_action_or_dialog(
        self,
        page: Any,
        action_task: asyncio.Task[Any],
        queue: asyncio.Queue[Any],
    ) -> str | None:
        dialog_task = asyncio.create_task(
            queue.get(),
            name="ash-browser-dialog-wait",
        )
        try:
            done, _ = await asyncio.wait(
                {action_task, dialog_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if action_task in done:
                await action_task
                return None
            dialog = dialog_task.result()
            self._pending_dialog = dialog
            self._dialog_page = page
            return self._render_dialog_state()
        finally:
            if not dialog_task.done():
                dialog_task.cancel()
                await asyncio.gather(dialog_task, return_exceptions=True)

    def _clear_dialog_action_state(self) -> None:
        page = self._dialog_page
        handler = self._dialog_handler
        if page is not None and handler is not None:
            remove_listener = getattr(page, "remove_listener", None)
            if callable(remove_listener):
                remove_listener("dialog", handler)
        self._pending_dialog = None
        self._dialog_page = None
        self._dialog_action_task = None
        self._dialog_queue = None
        self._dialog_handler = None

    async def list_tabs(self) -> str:
        self._ensure_no_pending_dialog("list tabs")
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
                title = _redact_browser_text(
                    _single_line(str(await page.title()))
                )[:200]
            except Exception:
                if page.is_closed():
                    continue
                title = ""
            tabs.append(
                {
                    "tab_id": tab_id,
                    "active": page is self._page,
                    "title": title,
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
        self._ensure_no_pending_dialog("open another tab")
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
                modal = await self._run_dialog_aware_action(
                    page,
                    lambda: page.goto(
                        validated,
                        wait_until=wait_until,
                        timeout=self.timeout_ms,
                    ),
                    label="open-tab-navigate",
                )
                if modal is not None:
                    return modal
                return await self.snapshot()
            except BaseException as primary:
                if self._pending_dialog is not None and self._dialog_page is page:
                    raise
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
        self._ensure_no_pending_dialog("focus another tab")
        await self.ensure_started()
        await self._drain_page_tasks()
        async with self._tab_lock:
            page = self._resolve_tab(tab_id)
            await page.bring_to_front()
            self._page = page
            return await self.snapshot()

    async def close_tab(self, tab_id: str) -> str:
        self._ensure_no_pending_dialog("close another tab")
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
        scope = self._snapshot_ref_scopes.get(ref)
        if scope is None:
            raise ValueError(
                f"browser element {ref!r} is stale or missing; take a new snapshot"
            )
        locator = scope.locator(f'[data-ash-ref="{ref}"]')
        try:
            count = await locator.count()
        except Exception as exc:
            raise ValueError(
                f"browser element {ref!r} is stale or missing; take a new snapshot"
            ) from exc
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
        del page
        self._page_admission_dirty = True
        if any(not task.done() for task in self._page_tasks):
            return
        task = asyncio.create_task(
            self._drain_page_admissions(),
            name="ash-browser-page-admission",
        )
        self._page_tasks.add(task)
        task.add_done_callback(self._page_task_done)

    async def _drain_page_admissions(self) -> None:
        while self._page_admission_dirty:
            self._page_admission_dirty = False
            async with self._tab_lock:
                try:
                    await self._enforce_tab_limit_unlocked()
                except BrowserUnavailableError:
                    pass
                for page in self._live_pages()[:MAX_BROWSER_TABS]:
                    self._remember_tab(page)
                self._prune_tab_pages()

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
            if self._closed and not self._cleanup_failed:
                return
            self._closed = True
            cleanup_task = asyncio.create_task(
                self._close_unlocked(),
                name="ash-browser-close",
            )
            cleanup_error, interrupted = await _settle_browser_cleanup_task(
                cleanup_task
            )
            if interrupted:
                cancellation = asyncio.CancelledError()
                if cleanup_error is not None:
                    cancellation.add_note(
                        "browser session cleanup failed while cancellation was pending: "
                        + _redact_browser_text(str(cleanup_error))[:500]
                    )
                self._cleanup_failed = cleanup_error is not None
                raise cancellation from cleanup_error
            if cleanup_error is not None:
                self._cleanup_failed = True
                raise cleanup_error
            self._cleanup_failed = False

    async def reset_for_session(self) -> None:
        """Drop live browsing state while preserving configured persistence policy."""

        async with self._lock:
            if self._closed:
                raise BrowserUnavailableError("browser session is closed")
            cleanup_task = asyncio.create_task(
                self._close_unlocked(),
                name="ash-browser-session-reset",
            )
            cleanup_error, interrupted = await _settle_browser_cleanup_task(
                cleanup_task
            )
            if interrupted:
                cancellation = asyncio.CancelledError()
                if cleanup_error is not None:
                    cancellation.add_note(
                        "browser session reset cleanup failed while cancellation was "
                        "pending: "
                        + _redact_browser_text(str(cleanup_error))[:500]
                    )
                raise cancellation from cleanup_error
            if cleanup_error is not None:
                self._cleanup_failed = True
                raise cleanup_error
            self._cleanup_failed = False
            self._session_token = secrets.token_hex(4)
            self._next_tab_id = 1

    async def reset_persistent_profile(self) -> bool:
        if self.profile_path is None:
            raise ValueError("browser session does not use an Ash persistent profile")
        async with self._lock:
            if self._closed:
                raise BrowserUnavailableError("browser session is closed")
            cleanup_task = asyncio.create_task(
                self._close_unlocked(),
                name="ash-browser-profile-reset-close",
            )
            cleanup_error, interrupted = await _settle_browser_cleanup_task(
                cleanup_task
            )
            if interrupted:
                cancellation = asyncio.CancelledError()
                if cleanup_error is not None:
                    cancellation.add_note(
                        "browser cleanup failed while profile reset was cancelled: "
                        + _redact_browser_text(str(cleanup_error))[:500]
                    )
                raise cancellation from cleanup_error
            if cleanup_error is not None:
                raise cleanup_error
            return await asyncio.to_thread(
                remove_anchored_path,
                self.profile_path,
                trusted_root=self.profile_path.parent,
                label="Ash browser profile",
            )

    async def _close_unlocked(self, *, cancel_snapshot: bool = True) -> None:
        cleanup_failures: list[tuple[str, BaseException]] = []
        pending_dialog = self._pending_dialog
        if pending_dialog is not None:
            try:
                await pending_dialog.dismiss()
            except BaseException as exc:
                cleanup_failures.append(("browser dialog", exc))
        dialog_action_task = self._dialog_action_task
        self._clear_dialog_action_state()
        if (
            dialog_action_task is not None
            and dialog_action_task is not asyncio.current_task()
            and not dialog_action_task.done()
        ):
            dialog_action_task.cancel()
            await asyncio.gather(dialog_action_task, return_exceptions=True)
        snapshot_task = self._snapshot_task
        current_task = asyncio.current_task()
        if (
            cancel_snapshot
            and
            snapshot_task is not None
            and snapshot_task is not current_task
            and not snapshot_task.done()
        ):
            snapshot_task.cancel()
            await asyncio.gather(snapshot_task, return_exceptions=True)
        if cancel_snapshot and snapshot_task is not current_task:
            self._snapshot_task = None
        page_tasks = list(self._page_tasks)
        for task in page_tasks:
            task.cancel()
        if page_tasks:
            await asyncio.gather(*page_tasks, return_exceptions=True)
        self._page_tasks.clear()
        self._page_admission_dirty = False
        context = self._context
        if context is not None:
            try:
                await context.close()
            except BaseException as exc:
                cleanup_failures.append(("browser context", exc))
            else:
                if self._context is context:
                    self._context = None
        browser = self._browser
        if browser is not None:
            try:
                await browser.close()
            except BaseException as exc:
                cleanup_failures.append(("browser process", exc))
            else:
                if self._browser is browser:
                    self._browser = None
        playwright = self._playwright
        if playwright is not None:
            try:
                await playwright.stop()
            except BaseException as exc:
                cleanup_failures.append(("Playwright runtime", exc))
            else:
                if self._playwright is playwright:
                    self._playwright = None
        proxy = self._proxy
        if proxy is not None:
            try:
                await proxy.close()
            except BaseException as exc:
                cleanup_failures.append(("browser policy proxy", exc))
            else:
                if self._proxy is proxy:
                    self._proxy = None
        self._page = None
        self._tab_pages.clear()
        self._snapshot_versions.clear()
        self._snapshot_ref_scopes.clear()
        if cleanup_failures:
            label, primary = cleanup_failures[0]
            error = BrowserUnavailableError(
                "browser session cleanup failed: "
                f"{label}: {_redact_browser_text(str(primary))[:500]}"
            )
            for extra_label, extra in cleanup_failures[1:]:
                error.add_note(
                    f"additional {extra_label} cleanup failure: "
                    + _redact_browser_text(str(extra))[:500]
                )
            raise error from primary


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


def _filter_cdp_storage_state(
    state: dict[str, Any],
    allowed_domains: tuple[str, ...],
) -> dict[str, Any]:
    cookies = state.get("cookies", [])
    origins = state.get("origins", [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise BrowserUnavailableError("CDP browser returned invalid storage state")

    filtered_cookies: list[dict[str, Any]] = []
    for item in cookies:
        if not isinstance(item, dict):
            raise BrowserUnavailableError("CDP browser returned invalid storage state")
        domain = item.get("domain")
        if not isinstance(domain, str) or not domain.strip():
            raise BrowserUnavailableError("CDP browser returned invalid storage state")
        cookie_host = domain.strip().casefold().lstrip(".").rstrip(".")
        if cookie_host and _host_allowed(cookie_host, allowed_domains):
            filtered_cookies.append(dict(item))

    filtered_origins: list[dict[str, Any]] = []
    for item in origins:
        if not isinstance(item, dict):
            raise BrowserUnavailableError("CDP browser returned invalid storage state")
        origin = item.get("origin")
        local_storage = item.get("localStorage", [])
        if not isinstance(origin, str) or not isinstance(local_storage, list):
            raise BrowserUnavailableError("CDP browser returned invalid storage state")
        parsed = urlparse(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or not _host_allowed(parsed.hostname, allowed_domains)
        ):
            continue
        normalized_local_storage: list[dict[str, str]] = []
        for entry in local_storage:
            if not isinstance(entry, dict):
                raise BrowserUnavailableError("CDP browser returned invalid storage state")
            name = entry.get("name")
            value = entry.get("value")
            if not isinstance(name, str) or not isinstance(value, str):
                raise BrowserUnavailableError("CDP browser returned invalid storage state")
            normalized_local_storage.append({"name": name, "value": value})
        filtered_origins.append(
            {
                "origin": origin,
                "localStorage": normalized_local_storage,
            }
        )

    return {
        "cookies": filtered_cookies,
        "origins": filtered_origins,
    }


def _validate_browser_url(
    url: str,
    allowed_domains: tuple[str, ...],
    allowed_local_origins: tuple[str, ...] = (),
) -> str:
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise ValueError("Browser URLs cannot contain embedded credentials")
    if loopback_url_allowed(url, allowed_local_origins):
        return url
    if parsed.scheme in {"ws", "wss"}:
        equivalent = parsed._replace(
            scheme="https" if parsed.scheme == "wss" else "http"
        )
        _validate_public_url(urlunparse(equivalent), allowed_domains=allowed_domains)
        return url
    return _validate_public_url(url, allowed_domains=allowed_domains)


def _redact_browser_url(url: str) -> str:
    return redact_url(url)


def _redact_browser_text(value: str) -> str:
    """Redact generic secrets plus secret-bearing browser URL fields."""

    return redact_text(value)


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
        return read_bounded_bytes(
            path,
            max_bytes,
            label="browser download",
        )
    except (OSError, ValueError) as exc:
        if "exceeds" in str(exc):
            raise ValueError(
                f"browser download exceeds {max_bytes} bytes; choose a smaller file"
            ) from exc
        raise ValueError(f"browser download cannot be read: {exc}") from exc


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


class CoordinateClickArgs(BaseModel):
    x: int = Field(..., ge=0, le=100_000)
    y: int = Field(..., ge=0, le=100_000)
    screenshot_sha256: str = Field(..., min_length=64, max_length=64)

    @field_validator("screenshot_sha256")
    @classmethod
    def validate_screenshot_sha256(cls, value: str) -> str:
        normalized = value.casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("browser screenshot sha256 must be 64 hexadecimal characters")
        return normalized


class TypeArgs(ElementArgs):
    text: str = Field(..., max_length=10_000)
    submit: bool = False
    clear: bool = True


_KEY_MODIFIERS = frozenset({"Alt", "Control", "Meta", "Shift"})
_NAMED_KEYS = frozenset(
    {
        "Enter",
        "Tab",
        "Escape",
        "Space",
        "Backspace",
        "Delete",
        "Insert",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        *(f"F{index}" for index in range(1, 13)),
    }
)


class PressArgs(BaseModel):
    key: str = Field(..., min_length=1, max_length=64)
    ref: str | None = Field(None, min_length=17, max_length=40)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        parts = value.split("+")
        if not parts or any(not part for part in parts):
            raise ValueError("browser key chord is invalid")
        modifiers = parts[:-1]
        final = parts[-1]
        if len(set(modifiers)) != len(modifiers) or any(
            modifier not in _KEY_MODIFIERS for modifier in modifiers
        ):
            raise ValueError("browser key chord has an invalid modifier")
        if final not in _NAMED_KEYS and not (
            len(final) == 1 and final.isascii() and final.isprintable()
        ):
            raise ValueError("browser key chord has an unsupported key")
        return value

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str | None) -> str | None:
        if value is not None and not ELEMENT_REF.fullmatch(value):
            raise ValueError("browser element ref must look like t1234abcd-1:s1:e1")
        return value


class SelectArgs(ElementArgs):
    values: list[str] = Field(..., min_length=1, max_length=20)

    @field_validator("values")
    @classmethod
    def validate_values(cls, values: list[str]) -> list[str]:
        if any(len(value.encode("utf-8")) > 1_000 for value in values):
            raise ValueError("browser select values must be at most 1000 UTF-8 bytes")
        return values


class DragArgs(BaseModel):
    source_ref: str = Field(..., min_length=17, max_length=40)
    target_ref: str = Field(..., min_length=17, max_length=40)

    @field_validator("source_ref", "target_ref")
    @classmethod
    def validate_refs(cls, value: str) -> str:
        if not ELEMENT_REF.fullmatch(value):
            raise ValueError("browser element ref must look like t1234abcd-1:s1:e1")
        return value


class WaitArgs(BaseModel):
    condition: Literal["ref_visible", "text", "url_contains", "load"]
    ref: str | None = Field(None, min_length=17, max_length=40)
    value: str | None = Field(None, max_length=2_048)
    load_state: Literal["domcontentloaded", "load", "networkidle"] = "domcontentloaded"
    timeout_seconds: float = Field(10.0, ge=0.1, le=30.0)

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str | None) -> str | None:
        if value is not None and not ELEMENT_REF.fullmatch(value):
            raise ValueError("browser element ref must look like t1234abcd-1:s1:e1")
        return value

    @model_validator(mode="after")
    def validate_condition_arguments(self) -> "WaitArgs":
        if self.condition == "ref_visible":
            if self.ref is None or self.value is not None:
                raise ValueError("ref_visible wait requires ref and no value")
        elif self.condition in {"text", "url_contains"}:
            if self.ref is not None or self.value is None or not self.value.strip():
                raise ValueError(f"{self.condition} wait requires a non-empty value")
        elif self.ref is not None or self.value is not None:
            raise ValueError("load wait does not accept ref or value")
        return self


class DialogArgs(BaseModel):
    accept: bool
    prompt_text: str | None = Field(None, max_length=10_000)

    @field_validator("prompt_text")
    @classmethod
    def validate_prompt_text(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > 10_000:
            raise ValueError("browser dialog prompt text exceeds 10000 UTF-8 bytes")
        return value

    @model_validator(mode="after")
    def validate_action(self) -> "DialogArgs":
        if not self.accept and self.prompt_text is not None:
            raise ValueError("browser dialog prompt text requires accept=true")
        return self


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
            return ToolResult(
                success=False,
                output="",
                error=_redact_browser_text(str(exc)),
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error="browser action failed: " + _redact_browser_text(str(exc))[:500],
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


class BrowserClickAtTool(_BrowserTool):
    name = "browser_click_at"
    description = (
        "Fallback for visual-only browser controls: click viewport coordinates from "
        "a specific browser_screenshot. The viewport is re-captured first and the "
        "action fails if the screenshot SHA-256 is stale. Prefer browser_click refs "
        "whenever an actionable ref exists."
    )
    args_schema = CoordinateClickArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = CoordinateClickArgs(**kwargs)
        return await self._result(
            self.session.click_at(
                args.x,
                args.y,
                screenshot_sha256=args.screenshot_sha256,
            )
        )


class BrowserTypeTool(_BrowserTool):
    name = "browser_type"
    description = "Fill or type into a referenced non-password browser control, optionally submit, and return the updated snapshot."
    args_schema = TypeArgs
    sensitive_argument_fields = frozenset({"text"})

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


class BrowserPressTool(_BrowserTool):
    name = "browser_press"
    description = (
        "Press one bounded keyboard key/chord on the current page or a referenced "
        "element and return the updated snapshot."
    )
    args_schema = PressArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = PressArgs(**kwargs)
        return await self._result(self.session.press_key(args.key, ref=args.ref))


class BrowserHoverTool(_BrowserTool):
    name = "browser_hover"
    description = (
        "Hover one element reference from the latest browser snapshot and return "
        "the updated snapshot."
    )
    args_schema = ElementArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = ElementArgs(**kwargs)
        return await self._result(self.session.hover(args.ref))


class BrowserSelectTool(_BrowserTool):
    name = "browser_select"
    description = (
        "Select one or more option values in a referenced select control and "
        "return the updated snapshot."
    )
    args_schema = SelectArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = SelectArgs(**kwargs)
        return await self._result(self.session.select(args.ref, args.values))


class BrowserDragTool(_BrowserTool):
    name = "browser_drag"
    description = (
        "Drag one referenced browser element onto another referenced element from "
        "the same current snapshot and return the updated snapshot."
    )
    args_schema = DragArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = DragArgs(**kwargs)
        return await self._result(self.session.drag(args.source_ref, args.target_ref))


class BrowserWaitTool(_BrowserTool):
    name = "browser_wait"
    description = (
        "Wait up to 30 seconds for a referenced element, visible text, URL "
        "substring, or page load state, then return a fresh snapshot."
    )
    args_schema = WaitArgs

    async def run(self, **kwargs: Any) -> ToolResult:
        args = WaitArgs(**kwargs)
        return await self._result(
            self.session.wait_for(
                condition=args.condition,
                ref=args.ref,
                value=args.value,
                load_state=args.load_state,
                timeout_seconds=args.timeout_seconds,
            )
        )


class BrowserDialogTool(_BrowserTool):
    name = "browser_dialog"
    description = (
        "Handle the currently pending alert, confirm, or prompt dialog by accepting "
        "or dismissing it; prompt text is allowed only when accepting a prompt."
    )
    args_schema = DialogArgs
    sensitive_argument_fields = frozenset({"prompt_text"})

    async def run(self, **kwargs: Any) -> ToolResult:
        args = DialogArgs(**kwargs)
        return await self._result(
            self.session.handle_dialog(
                accept=args.accept,
                prompt_text=args.prompt_text,
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
            return ToolResult(
                success=False,
                output="",
                error=_redact_browser_text(str(exc)),
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=(
                    "browser screenshot failed: "
                    + _redact_browser_text(str(exc))[:500]
                ),
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
    allowed_local_origins: list[str] | tuple[str, ...] | None = None,
    profile_path: Path | None = None,
    cdp_url: str | None = None,
    cdp_reuse_storage_state: bool = False,
) -> list[BaseTool]:
    session = BrowserSession(
        headless=headless,
        timeout_seconds=timeout_seconds,
        allowed_domains=allowed_domains,
        allowed_local_origins=allowed_local_origins,
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
        BrowserClickAtTool(safety_guard, session),
        BrowserTypeTool(safety_guard, session),
        BrowserPressTool(safety_guard, session),
        BrowserHoverTool(safety_guard, session),
        BrowserSelectTool(safety_guard, session),
        BrowserDragTool(safety_guard, session),
        BrowserWaitTool(safety_guard, session),
        BrowserDialogTool(safety_guard, session),
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


async def inspect_cdp_source(
    cdp_url: str,
    *,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Inspect one loopback CDP browser without taking ownership of its tabs."""

    if not 1.0 <= timeout_seconds <= 120.0:
        raise ValueError("browser timeout must be between 1 and 120 seconds")
    endpoint = _validate_cdp_url(cdp_url)
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        from ash.install import pipx_install_command

        raise BrowserUnavailableError(
            f"Run `{pipx_install_command('browser')}`, then "
            "`ash setup browser` to enable browser tools."
        ) from exc
    playwright: Any | None = None
    browser: Any | None = None
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.connect_over_cdp(
            endpoint,
            timeout=int(timeout_seconds * 1000),
        )
        contexts = list(browser.contexts)
        contexts_truncated = False
        rendered: list[dict[str, Any]] = []
        for context_index, context in enumerate(contexts):
            pages = list(context.pages)
            pages_truncated = len(pages) > MAX_CDP_SOURCE_TABS
            tabs: list[dict[str, str]] = []
            for page in pages[:MAX_CDP_SOURCE_TABS]:
                try:
                    title = terminal_safe_text(
                        _redact_browser_text(_single_line(str(await page.title())))[:200],
                        single_line=True,
                    )
                except Exception:
                    title = "(unavailable)"
                tabs.append(
                    {
                        "title": title,
                        "url": terminal_safe_text(
                            _redact_browser_url(str(page.url)),
                            single_line=True,
                        )[:2048],
                    }
                )
            rendered.append(
                {
                    "index": context_index,
                    "tabs": tabs,
                    "tabs_truncated": pages_truncated,
                }
            )
        return {
            "endpoint": endpoint,
            "contexts": rendered,
            "contexts_truncated": contexts_truncated,
        }
    finally:
        if browser is not None:
            await browser.close()
        if playwright is not None:
            await playwright.stop()
