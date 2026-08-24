"""Playwright-backed browser automation with bounded model-facing snapshots."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, Field, field_validator

from ash.core.redaction import redact_text
from ash.safety.environment import build_scrubbed_environment
from ash.safety.guard import SafetyGuard
from ash.tools.base import BaseTool, ToolResult, count_output_tokens
from ash.tools.web import _normalize_allowed_domains, _validate_public_url


MAX_SNAPSHOT_CHARS = 30_000
MAX_INTERACTIVE_ELEMENTS = 150
ELEMENT_REF = re.compile(r"^e[1-9][0-9]{0,3}$")
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
    ) -> None:
        if not 1.0 <= timeout_seconds <= 120.0:
            raise ValueError("browser timeout must be between 1 and 120 seconds")
        self.headless = headless
        self.timeout_ms = int(timeout_seconds * 1000)
        self.allowed_domains = _normalize_allowed_domains(allowed_domains or ())
        self._lock = asyncio.Lock()
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None

    async def ensure_started(self) -> Any:
        async with self._lock:
            if self._page is not None and not self._page.is_closed():
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
                self._playwright = await async_playwright().start()
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
                self._browser = await self._playwright.chromium.launch(
                    headless=self.headless,
                    env=browser_environment,
                )
                self._context = await self._browser.new_context(
                    accept_downloads=False,
                    service_workers="block",
                    viewport={"width": 1280, "height": 800},
                )
                self._context.set_default_timeout(self.timeout_ms)
                self._context.set_default_navigation_timeout(self.timeout_ms)
                await self._context.route("**/*", self._route_request)
                await self._context.route_web_socket("**/*", self._route_websocket)
                self._page = await self._context.new_page()
                return self._page
            except Exception as exc:
                await self._close_unlocked()
                message = redact_text(str(exc))[:500]
                if "Executable doesn't exist" in message:
                    raise BrowserUnavailableError(
                        "Chromium is not installed; run `ash setup browser`."
                    ) from exc
                raise BrowserUnavailableError(
                    f"Could not start the isolated browser: {message}"
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
        self._page = self._latest_page()
        return await self.snapshot()

    async def snapshot(self) -> str:
        page = await self.ensure_started()
        self._page = self._latest_page()
        page = self._page
        title = await page.title()
        elements = await page.eval_on_selector_all(
            INTERACTIVE_SELECTOR,
            """(nodes, maxItems) => {
              let refIndex = 0;
              return nodes.flatMap((node) => {
                if (refIndex >= maxItems) return [];
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                if (style.visibility === 'hidden' || style.display === 'none' ||
                    rect.width <= 0 || rect.height <= 0) return [];
                refIndex += 1;
                const ref = `e${refIndex}`;
                node.setAttribute('data-ash-ref', ref);
                const role = node.getAttribute('role') || node.tagName.toLowerCase();
                const text = node.getAttribute('aria-label') ||
                    node.getAttribute('alt') || node.getAttribute('placeholder') ||
                    node.innerText || node.getAttribute('title') || '';
                return [{ref, role, text: text.replace(/\\s+/g, ' ').trim(),
                    disabled: Boolean(node.disabled) || node.getAttribute('aria-disabled') === 'true'}];
              });
            }""",
            MAX_INTERACTIVE_ELEMENTS,
        )
        aria = await page.aria_snapshot(timeout=self.timeout_ms)
        password_values = await page.eval_on_selector_all(
            'input[type="password"]',
            """nodes => nodes.slice(0, 100).map(node => String(node.value || '').slice(0, 10000)).filter(Boolean)""",
        )
        aria = _redact_literals(aria, password_values)
        lines = [f"Page: {title}", f"URL: {page.url}", "", "Interactive elements:"]
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
        self._page = self._latest_page()
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

    async def _locator(self, ref: str) -> Any:
        if not ELEMENT_REF.fullmatch(ref):
            raise ValueError("browser element ref must look like e1")
        page = await self.ensure_started()
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
            return
        self._page = self._latest_page()

    def _latest_page(self) -> Any:
        if self._context is not None:
            live = [page for page in self._context.pages if not page.is_closed()]
            if live:
                return live[-1]
        return self._page

    async def close(self) -> None:
        async with self._lock:
            await self._close_unlocked()

    async def _close_unlocked(self) -> None:
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
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None


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


class NavigateArgs(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = (
        "domcontentloaded"
    )


class ElementArgs(BaseModel):
    ref: str = Field(..., min_length=2, max_length=5)

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        if not ELEMENT_REF.fullmatch(value):
            raise ValueError("browser element ref must look like e1")
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
            truncated="[browser snapshot truncated]" in output,
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


def build_browser_tools(
    safety_guard: SafetyGuard,
    *,
    headless: bool = True,
    timeout_seconds: float = 30.0,
    allowed_domains: list[str] | tuple[str, ...] | None = None,
) -> list[BaseTool]:
    session = BrowserSession(
        headless=headless,
        timeout_seconds=timeout_seconds,
        allowed_domains=allowed_domains,
    )
    return [
        BrowserNavigateTool(safety_guard, session),
        BrowserSnapshotTool(safety_guard, session),
        BrowserClickTool(safety_guard, session),
        BrowserTypeTool(safety_guard, session),
        BrowserScrollTool(safety_guard, session),
        BrowserBackTool(safety_guard, session),
        BrowserScreenshotTool(safety_guard, session),
    ]
