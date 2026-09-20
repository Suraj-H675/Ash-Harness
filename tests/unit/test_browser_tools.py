from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ash.safety.guard import SafetyGuard
from ash.safety.policy import PermissionPolicy, PolicyAction
from ash.tools.browser import (
    MAX_BROWSER_TABS,
    MAX_SNAPSHOT_CHARS,
    BrowserSession,
    BrowserUnavailableError,
    BrowserBackTool,
    BrowserCloseTabTool,
    BrowserUploadTool,
    BrowserDownloadTool,
    BrowserScreenshotTool,
    BrowserClickTool,
    BrowserFocusTabTool,
    BrowserNavigateTool,
    BrowserOpenTabTool,
    BrowserScrollTool,
    BrowserSnapshotTool,
    BrowserTabsTool,
    BrowserTypeTool,
    _redact_browser_url,
    _validate_browser_url,
    _validate_cdp_url,
    build_browser_tools,
)
from ash.tools.browser_proxy import BrowserProxyError


class FakeBrowserSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.closed = 0

    async def navigate(self, url: str, wait_until: str) -> str:
        self.calls.append(("navigate", (url, wait_until)))
        return "navigation snapshot"

    async def snapshot(self) -> str:
        self.calls.append(("snapshot", None))
        return "current snapshot"

    async def list_tabs(self) -> str:
        self.calls.append(("tabs", None))
        return '{"tabs":[]}'

    async def open_tab(self, url: str, wait_until: str) -> str:
        self.calls.append(("open_tab", (url, wait_until)))
        return "new tab snapshot"

    async def focus_tab(self, tab_id: str) -> str:
        self.calls.append(("focus_tab", tab_id))
        return "focused tab snapshot"

    async def close_tab(self, tab_id: str) -> str:
        self.calls.append(("close_tab", tab_id))
        return '{"tabs":[]}'

    async def click(self, ref: str) -> str:
        self.calls.append(("click", ref))
        return "click snapshot"

    async def type_text(
        self,
        ref: str,
        text: str,
        *,
        submit: bool,
        clear: bool,
    ) -> str:
        self.calls.append(("type", (ref, text, submit, clear)))
        return "type snapshot"

    async def scroll(self, direction: str, amount: int) -> str:
        self.calls.append(("scroll", (direction, amount)))
        return "scroll snapshot"

    async def back(self) -> str:
        self.calls.append(("back", None))
        return "back snapshot"

    async def screenshot(self, *, max_bytes: int):
        self.calls.append(("screenshot", max_bytes))

        class Screenshot:
            media_type = "image/png"
            data = "cG5nLWRhdGE="
            sha256 = "a" * 64

        return Screenshot()

    async def upload_file(
        self,
        ref: str,
        file_path: str,
        *,
        safety_guard,
        max_bytes: int,
    ) -> str:
        assert safety_guard is not None
        self.calls.append(("upload", (ref, file_path, max_bytes)))
        return "upload snapshot"

    async def download_file(
        self,
        ref: str,
        file_path: str,
        *,
        safety_guard,
        max_bytes: int,
        overwrite: bool,
    ) -> str:
        assert safety_guard is not None
        self.calls.append(("download", (ref, file_path, max_bytes, overwrite)))
        return "download snapshot"

    async def close(self) -> None:
        self.closed += 1


class _UploadLocator:
    async def get_attribute(self, name: str) -> str | None:
        return "file" if name == "type" else None

    async def click(self, *, timeout: int) -> None:
        assert timeout == 1_000


class _UploadChooser:
    def __init__(self) -> None:
        self.files: Any = None

    async def set_files(self, files: Any) -> None:
        self.files = files


class _UploadChooserContext:
    def __init__(self, chooser: _UploadChooser) -> None:
        self.value = self._resolve_chooser(chooser)

    async def _resolve_chooser(self, chooser: _UploadChooser) -> _UploadChooser:
        return chooser

    async def __aenter__(self) -> "_UploadChooserContext":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _UploadPage:
    def __init__(self, chooser: _UploadChooser) -> None:
        self.chooser = chooser

    def expect_file_chooser(self, *, timeout: int) -> _UploadChooserContext:
        assert timeout == 1_000
        return _UploadChooserContext(self.chooser)


def _upload_session(chooser: _UploadChooser) -> BrowserSession:
    session = BrowserSession(timeout_seconds=1)
    session._session_token = "deadbeef"
    session.ensure_started = AsyncMock(  # type: ignore[method-assign]
        return_value=_UploadPage(chooser)
    )
    session._locator = AsyncMock(  # type: ignore[method-assign]
        return_value=_UploadLocator()
    )
    session._settle = AsyncMock()  # type: ignore[method-assign]
    session.snapshot = AsyncMock(return_value="snapshot")  # type: ignore[method-assign]
    return session


@pytest.mark.asyncio
async def test_browser_tools_dispatch_validated_actions_and_close(tmp_path) -> None:
    guard = SafetyGuard(tmp_path)
    session = FakeBrowserSession()
    tools = [
        BrowserNavigateTool(guard, session),  # type: ignore[arg-type]
        BrowserSnapshotTool(guard, session),  # type: ignore[arg-type]
        BrowserTabsTool(guard, session),  # type: ignore[arg-type]
        BrowserOpenTabTool(guard, session),  # type: ignore[arg-type]
        BrowserFocusTabTool(guard, session),  # type: ignore[arg-type]
        BrowserCloseTabTool(guard, session),  # type: ignore[arg-type]
        BrowserClickTool(guard, session),  # type: ignore[arg-type]
        BrowserTypeTool(guard, session),  # type: ignore[arg-type]
        BrowserScrollTool(guard, session),  # type: ignore[arg-type]
        BrowserBackTool(guard, session),  # type: ignore[arg-type]
        BrowserScreenshotTool(guard, session),  # type: ignore[arg-type]
        BrowserUploadTool(guard, session),  # type: ignore[arg-type]
        BrowserDownloadTool(guard, session),  # type: ignore[arg-type]
    ]

    results = [
        await tools[0].run(url="https://example.com", wait_until="load"),
        await tools[1].run(),
        await tools[2].run(),
        await tools[3].run(url="https://example.org", wait_until="domcontentloaded"),
        await tools[4].run(tab_id="tdeadbeef-2"),
        await tools[5].run(tab_id="tdeadbeef-2"),
        await tools[6].run(ref="tdeadbeef-1:s1:e2"),
        await tools[7].run(
            ref="tdeadbeef-1:s1:e3",
            text="hello",
            submit=True,
            clear=False,
        ),
        await tools[8].run(direction="up", amount=250),
        await tools[9].run(),
        await tools[10].run(max_bytes=1_000_000),
        await tools[11].run(
            ref="tdeadbeef-1:s1:e4",
            file_path="docs/report.pdf",
            max_bytes=2_000_000,
        ),
        await tools[12].run(
            ref="tdeadbeef-1:s1:e5",
            file_path="downloads/report.pdf",
            max_bytes=4_000_000,
        ),
    ]
    await tools[0].aclose()

    assert all(result.success for result in results)
    assert session.calls == [
        ("navigate", ("https://example.com", "load")),
        ("snapshot", None),
        ("tabs", None),
        ("open_tab", ("https://example.org", "domcontentloaded")),
        ("focus_tab", "tdeadbeef-2"),
        ("close_tab", "tdeadbeef-2"),
        ("click", "tdeadbeef-1:s1:e2"),
        ("type", ("tdeadbeef-1:s1:e3", "hello", True, False)),
        ("scroll", ("up", 250)),
        ("back", None),
        ("screenshot", 1_000_000),
        ("upload", ("tdeadbeef-1:s1:e4", "docs/report.pdf", 2_000_000)),
        (
            "download",
            ("tdeadbeef-1:s1:e5", "downloads/report.pdf", 4_000_000, False),
        ),
    ]
    assert session.closed == 1
    assert results[10].images[0]["sha256"] == "a" * 64
    assert results[10].image_blocks[0]["data"] == "cG5nLWRhdGE="


@pytest.mark.asyncio
async def test_browser_session_tab_handles_are_stable_and_selection_is_explicit() -> None:
    class FakePage:
        def __init__(self, title: str, url: str) -> None:
            self._title = title
            self.url = url
            self.closed = False
            self.front_calls = 0
            self.wait_for_load_state = AsyncMock()

        def is_closed(self) -> bool:
            return self.closed

        async def title(self) -> str:
            return self._title

        async def bring_to_front(self) -> None:
            self.front_calls += 1

        async def close(self) -> None:
            self.closed = True

    first = FakePage("First", "https://first.example/")
    second = FakePage("Second", "https://second.example/")

    class FakeContext:
        pages = [first, second]

    session = BrowserSession(timeout_seconds=1)
    session._session_token = "deadbeef"
    session._context = FakeContext()
    session._page = first
    session.snapshot = AsyncMock(return_value="snapshot")  # type: ignore[method-assign]

    first_listing = json.loads(await session.list_tabs())
    second_listing = json.loads(await session.list_tabs())

    assert first_listing == second_listing
    assert first_listing["tabs"] == [
        {
            "tab_id": "tdeadbeef-1",
            "active": True,
            "title": "First",
            "url": "https://first.example/",
        },
        {
            "tab_id": "tdeadbeef-2",
            "active": False,
            "title": "Second",
            "url": "https://second.example/",
        },
    ]

    await session.focus_tab("tdeadbeef-2")
    assert session._page is second
    assert second.front_calls == 1

    await session._settle(first)
    assert session._page is first

    closed = json.loads(await session.close_tab("tdeadbeef-2"))
    assert closed["tabs"] == [
        {
            "tab_id": "tdeadbeef-1",
            "active": True,
            "title": "First",
            "url": "https://first.example/",
        }
    ]
    with pytest.raises(ValueError, match="stale or missing"):
        await session.focus_tab("tdeadbeef-2")
    with pytest.raises(ValueError, match="last live tab"):
        await session.close_tab("tdeadbeef-1")


@pytest.mark.asyncio
async def test_browser_open_tab_failure_restores_previous_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        def __init__(self, *, fail: bool = False) -> None:
            self.url = "about:blank"
            self.closed = False
            self.fail = fail

        def is_closed(self) -> bool:
            return self.closed

        async def goto(self, url: str, **kwargs: Any) -> None:
            del kwargs
            self.url = url
            if self.fail:
                raise RuntimeError("navigation failed")

        async def close(self) -> None:
            self.closed = True

    first = FakePage()
    created = FakePage(fail=True)

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [first]

        async def new_page(self) -> FakePage:
            self.pages.append(created)
            return created

    monkeypatch.setattr(
        "ash.tools.browser._validate_browser_url",
        lambda url, _allowed_domains: url,
    )
    session = BrowserSession(timeout_seconds=1)
    session._context = FakeContext()
    session._page = first

    with pytest.raises(RuntimeError, match="navigation failed"):
        await session.open_tab("https://example.com", "load")

    assert created.closed is True
    assert session._page is first
    assert list(session._tab_pages.values()) == [first]


@pytest.mark.asyncio
async def test_browser_open_tab_enforces_live_tab_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        url = "about:blank"

        def is_closed(self) -> bool:
            return False

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [FakePage() for _ in range(32)]
            self.new_page = AsyncMock()

    monkeypatch.setattr(
        "ash.tools.browser._validate_browser_url",
        lambda url, _allowed_domains: url,
    )
    context = FakeContext()
    session = BrowserSession(timeout_seconds=1)
    session._context = context
    session._page = context.pages[0]

    with pytest.raises(ValueError, match="tab limit reached"):
        await session.open_tab("https://example.com", "load")

    context.new_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_popup_admission_closes_overflow_tab() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.closed = False
            self.url = "about:blank"

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

    pages = [FakePage() for _ in range(MAX_BROWSER_TABS)]
    overflow = FakePage()

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [*pages, overflow]

    session = BrowserSession(timeout_seconds=1)
    session._context = FakeContext()
    session._page = pages[0]

    await session._admit_page(overflow)

    assert overflow.closed is True
    assert len(session._live_pages()) == MAX_BROWSER_TABS


@pytest.mark.asyncio
async def test_browser_cancelled_tab_listing_still_finishes_popup_admission() -> None:
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class FakePage:
        def __init__(self, *, slow_close: bool = False) -> None:
            self.closed = False
            self.url = "about:blank"
            self.slow_close = slow_close

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            if self.slow_close:
                close_started.set()
                await release_close.wait()
            self.closed = True

    pages = [FakePage() for _ in range(MAX_BROWSER_TABS)]
    overflow = FakePage(slow_close=True)

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [*pages, overflow]

    session = BrowserSession(timeout_seconds=1)
    session._context = FakeContext()
    session._page = pages[0]
    session._on_page_created(overflow)

    listing = asyncio.create_task(session.list_tabs())
    await close_started.wait()
    listing.cancel()
    release_close.set()

    with pytest.raises(asyncio.CancelledError):
        await listing

    assert overflow.closed is True
    assert len(session._live_pages()) == MAX_BROWSER_TABS
    assert not session._page_tasks


@pytest.mark.asyncio
async def test_browser_tab_mutations_are_serialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.closed = False
            self.url = "about:blank"

        def is_closed(self) -> bool:
            return self.closed

        async def goto(self, url: str, **kwargs: Any) -> None:
            del kwargs
            self.url = url

        async def close(self) -> None:
            await asyncio.sleep(0)
            self.closed = True

    class FakeContext:
        def __init__(self, pages: list[FakePage]) -> None:
            self.pages = pages

        async def new_page(self) -> FakePage:
            await asyncio.sleep(0)
            page = FakePage()
            self.pages.append(page)
            return page

    monkeypatch.setattr(
        "ash.tools.browser._validate_browser_url",
        lambda url, _allowed_domains: url,
    )
    pages = [FakePage() for _ in range(MAX_BROWSER_TABS - 1)]
    context = FakeContext(pages)
    session = BrowserSession(timeout_seconds=1)
    session._context = context
    session._page = pages[0]
    session.snapshot = AsyncMock(return_value="snapshot")  # type: ignore[method-assign]

    opened = await asyncio.gather(
        session.open_tab("https://one.example", "load"),
        session.open_tab("https://two.example", "load"),
        return_exceptions=True,
    )

    assert sum(result == "snapshot" for result in opened) == 1
    assert sum(isinstance(result, ValueError) for result in opened) == 1
    assert len(session._live_pages()) == MAX_BROWSER_TABS

    first = FakePage()
    second = FakePage()
    context = FakeContext([first, second])
    session = BrowserSession(timeout_seconds=1)
    session._session_token = "deadbeef"
    session._context = context
    session._page = first
    first_id = session._remember_tab(first)
    second_id = session._remember_tab(second)
    session._list_tabs_unlocked = AsyncMock(  # type: ignore[method-assign]
        return_value='{"tabs":[]}'
    )

    closed = await asyncio.gather(
        session.close_tab(first_id),
        session.close_tab(second_id),
        return_exceptions=True,
    )

    assert sum(result == '{"tabs":[]}' for result in closed) == 1
    assert sum(isinstance(result, ValueError) for result in closed) == 1
    assert len(session._live_pages()) == 1


@pytest.mark.asyncio
async def test_browser_open_tab_cancellation_rolls_back_new_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.closed = False
            self.url = "about:blank"

        def is_closed(self) -> bool:
            return self.closed

        async def goto(self, url: str, **kwargs: Any) -> None:
            del kwargs
            self.url = url

        async def close(self) -> None:
            self.closed = True

    previous = FakePage()
    created = FakePage()

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [previous]

        async def new_page(self) -> FakePage:
            self.pages.append(created)
            return created

    monkeypatch.setattr(
        "ash.tools.browser._validate_browser_url",
        lambda url, _allowed_domains: url,
    )
    session = BrowserSession(timeout_seconds=1)
    session._context = FakeContext()
    session._page = previous
    session.snapshot = AsyncMock(  # type: ignore[method-assign]
        side_effect=asyncio.CancelledError
    )

    with pytest.raises(asyncio.CancelledError):
        await session.open_tab("https://example.com", "load")

    assert created.closed is True
    assert session._page is previous


@pytest.mark.asyncio
async def test_browser_open_tab_cancellation_during_creation_closes_created_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.closed = False
            self.url = "about:blank"

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

    previous = FakePage()
    created = FakePage()
    created_event = asyncio.Event()
    release_event = asyncio.Event()

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [previous]

        async def new_page(self) -> FakePage:
            self.pages.append(created)
            created_event.set()
            await release_event.wait()
            return created

    monkeypatch.setattr(
        "ash.tools.browser._validate_browser_url",
        lambda url, _allowed_domains: url,
    )
    session = BrowserSession(timeout_seconds=1)
    session._context = FakeContext()
    session._page = previous

    task = asyncio.create_task(session.open_tab("https://example.com", "load"))
    await created_event.wait()
    task.cancel()
    release_event.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert created.closed is True
    assert session._page is previous
    assert session._live_pages() == [previous]


@pytest.mark.asyncio
async def test_browser_open_tab_preserves_navigation_error_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        def __init__(self, *, fail_navigation: bool = False) -> None:
            self.closed = False
            self.url = "about:blank"
            self.fail_navigation = fail_navigation

        def is_closed(self) -> bool:
            return self.closed

        async def goto(self, url: str, **kwargs: Any) -> None:
            del kwargs
            self.url = url
            if self.fail_navigation:
                raise ValueError("navigation failed")

        async def close(self) -> None:
            raise RuntimeError("close failed")

    previous = FakePage()
    created = FakePage(fail_navigation=True)

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [previous]

        async def new_page(self) -> FakePage:
            self.pages.append(created)
            return created

    monkeypatch.setattr(
        "ash.tools.browser._validate_browser_url",
        lambda url, _allowed_domains: url,
    )
    session = BrowserSession(timeout_seconds=1)
    session._context = FakeContext()
    session._page = previous

    with pytest.raises(ValueError, match="navigation failed"):
        await session.open_tab("https://example.com", "load")

    assert session._page is None
    assert session._context is None


def test_browser_tab_ids_do_not_alias_across_sessions() -> None:
    class FakePage:
        def is_closed(self) -> bool:
            return False

    old_page = FakePage()
    new_page = FakePage()
    old_session = BrowserSession(timeout_seconds=1)
    old_session._session_token = "deadbeef"
    old_session._context = type("Context", (), {"pages": [old_page]})()
    old_session._page = old_page
    old_id = old_session._remember_tab(old_page)

    new_session = BrowserSession(timeout_seconds=1)
    new_session._session_token = "feedface"
    new_session._context = type("Context", (), {"pages": [new_page]})()
    new_session._page = new_page
    new_session._remember_tab(new_page)

    with pytest.raises(ValueError, match="stale or missing"):
        new_session._resolve_tab(old_id)


@pytest.mark.asyncio
async def test_closed_browser_session_cannot_be_restarted() -> None:
    session = BrowserSession(timeout_seconds=1)
    tool = BrowserSnapshotTool(
        SafetyGuard(Path.cwd()),
        session,
    )

    await session.close()

    with pytest.raises(BrowserUnavailableError, match="session is closed"):
        await session.ensure_started()
    result = await tool.run()
    assert result.success is False
    assert result.error == "browser session is closed"


@pytest.mark.asyncio
async def test_browser_element_refs_are_bound_to_their_tab() -> None:
    class FakeLocator:
        async def count(self) -> int:
            return 1

    class FakePage:
        def __init__(self) -> None:
            self.closed = False

        def is_closed(self) -> bool:
            return self.closed

        def locator(self, _selector: str) -> FakeLocator:
            return FakeLocator()

    first = FakePage()
    second = FakePage()
    session = BrowserSession(timeout_seconds=1)
    session._session_token = "deadbeef"
    session._context = type("Context", (), {"pages": [first, second]})()
    first_id = session._remember_tab(first)
    second_id = session._remember_tab(second)
    session._snapshot_versions[first_id] = 1
    session._snapshot_versions[second_id] = 1
    session._page = second

    with pytest.raises(ValueError, match="belongs to another tab"):
        await session._locator(f"{first_id}:s1:e1")

    locator = await session._locator(f"{second_id}:s1:e1")
    assert isinstance(locator, FakeLocator)

    session._snapshot_versions[second_id] = 2
    with pytest.raises(ValueError, match="is stale"):
        await session._locator(f"{second_id}:s1:e1")


@pytest.mark.asyncio
async def test_browser_snapshot_redacts_page_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        url = "https://example.com/"

        def is_closed(self) -> bool:
            return False

        async def title(self) -> str:
            return "Dashboard title-secret"

        async def eval_on_selector_all(self, selector: str, *_args: Any) -> list[Any]:
            del selector
            return []

        async def aria_snapshot(self, **_kwargs: Any) -> str:
            return "main"

    monkeypatch.setattr(
        "ash.tools.browser.redact_text",
        lambda value: value.replace("title-secret", "[REDACTED]"),
    )
    page = FakePage()
    session = BrowserSession(timeout_seconds=1)
    session._session_token = "deadbeef"
    session._context = type("Context", (), {"pages": [page]})()
    session._page = page

    snapshot = await session.snapshot()

    assert "title-secret" not in snapshot
    assert "Page: Dashboard [REDACTED]" in snapshot


@pytest.mark.asyncio
async def test_browser_tab_listing_recovers_active_close_race_and_is_bounded() -> None:
    class FakePage:
        def __init__(
            self,
            title: str,
            url: str,
            *,
            close_during_title: bool = False,
        ) -> None:
            self._title = title
            self.url = url
            self.closed = False
            self.close_during_title = close_during_title

        def is_closed(self) -> bool:
            return self.closed

        async def close(self) -> None:
            self.closed = True

        async def title(self) -> str:
            if self.close_during_title:
                self.closed = True
            return self._title

    closing = FakePage(
        "closing",
        "https://closing.example/",
        close_during_title=True,
    )
    backup = FakePage("backup", "https://backup.example/")
    session = BrowserSession(timeout_seconds=1)
    session._session_token = "deadbeef"
    session._context = type("Context", (), {"pages": [closing, backup]})()
    session._page = closing

    raced = json.loads(await session.list_tabs())

    assert raced["tabs"] == [
        {
            "tab_id": "tdeadbeef-2",
            "active": True,
            "title": "backup",
            "url": "https://backup.example/",
        }
    ]

    popup = FakePage("popup", "https://popup.example/")

    class MutatingPage(FakePage):
        def __init__(self, context: Any) -> None:
            super().__init__("first", "https://first.example/")
            self._context = context
            self._mutated = False

        async def title(self) -> str:
            if not self._mutated:
                self._mutated = True
                self._context.pages.append(popup)
            return await super().title()

    context = type("Context", (), {"pages": []})()
    first = MutatingPage(context)
    context.pages = [
        first,
        *[
            FakePage(f"page-{index}", f"https://{index}.example/")
            for index in range(MAX_BROWSER_TABS - 1)
        ],
    ]
    session = BrowserSession(timeout_seconds=1)
    session._session_token = "cafebabe"
    session._context = context
    session._page = first

    popup_race = json.loads(await session.list_tabs())

    assert popup.closed is True
    assert popup_race["total"] == MAX_BROWSER_TABS
    assert len(popup_race["tabs"]) == MAX_BROWSER_TABS
    assert popup_race["truncated"] is False

    pages = [
        FakePage(
            "x" * 200,
            "https://example.com/?" + ("a" * 2_000),
        )
        for _ in range(MAX_BROWSER_TABS)
    ]
    session = BrowserSession(timeout_seconds=1)
    session._session_token = "feedface"
    session._context = type("Context", (), {"pages": pages})()
    session._page = pages[0]

    output = await session.list_tabs()
    bounded = json.loads(output)

    assert len(output) <= MAX_SNAPSHOT_CHARS
    assert bounded["total"] == MAX_BROWSER_TABS
    assert bounded["truncated"] is True
    assert len(bounded["tabs"]) < MAX_BROWSER_TABS
    assert sum(bool(item["active"]) for item in bounded["tabs"]) == 1


@pytest.mark.asyncio
async def test_browser_download_writes_bounded_payload_atomically(tmp_path: Path) -> None:
    from ash.tools.browser import _read_download_payload

    source = tmp_path / "playwright-download.tmp"
    source.write_bytes(b"downloaded content")
    assert _read_download_payload(source, 100) == b"downloaded content"

    oversized = tmp_path / "oversized.tmp"
    oversized.write_bytes(b"0123456789")
    with pytest.raises(ValueError, match="exceeds 5 bytes"):
        _read_download_payload(oversized, 5)


@pytest.mark.asyncio
async def test_browser_session_download_uses_workspace_scope_and_no_overwrite(
    tmp_path: Path,
) -> None:
    class FakeDownload:
        async def failure(self):
            return None

        async def path(self):
            return str(source)

    class DownloadContext:
        def __init__(self):
            self.value = self._download()

        async def _download(self):
            return FakeDownload()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class FakeLocator:
        async def click(self, *, timeout):
            assert timeout == 1_000

    class FakePage:
        def expect_download(self, *, timeout):
            assert timeout == 1_000
            return DownloadContext()

    source = tmp_path / "playwright-download.tmp"
    source.write_bytes(b"safe payload")
    session = BrowserSession(timeout_seconds=1)
    guard = SafetyGuard(tmp_path)
    session.ensure_started = AsyncMock(return_value=FakePage())  # type: ignore[method-assign]
    session._locator = AsyncMock(return_value=FakeLocator())  # type: ignore[method-assign]
    session._settle = AsyncMock()  # type: ignore[method-assign]
    session.snapshot = AsyncMock(return_value="snapshot")  # type: ignore[method-assign]

    result = await session.download_file(
        "e1",
        "downloads/report.txt",
        safety_guard=guard,
        max_bytes=100,
        overwrite=False,
    )

    assert (tmp_path / "downloads/report.txt").read_bytes() == b"safe payload"
    assert "Downloaded 12 bytes" in result
    await session.close()


@pytest.mark.asyncio
async def test_browser_session_upload_retains_approved_bytes_after_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.safety import scoped_io

    approved = tmp_path / "approved.txt"
    approved.write_bytes(b"approved-content")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-upload-sentinel"
    outside.write_bytes(b"outside-content")
    try:
        probe = tmp_path / "symlink-probe"
        probe.symlink_to(outside)
        probe.unlink()
    except OSError as exc:
        outside.unlink(missing_ok=True)
        pytest.skip(f"symlinks are unavailable: {exc}")

    read_complete = threading.Event()
    release_read = threading.Event()
    real_read = scoped_io.read_scoped_bytes

    def gated_read(path: str | Path, guard: SafetyGuard, **kwargs: Any):
        result = real_read(path, guard, **kwargs)
        read_complete.set()
        if not release_read.wait(5):
            raise AssertionError("upload read was not released")
        return result

    monkeypatch.setattr(scoped_io, "read_scoped_bytes", gated_read)
    chooser = _UploadChooser()
    session = _upload_session(chooser)
    upload = asyncio.create_task(
        session.upload_file(
            "e1",
            str(approved),
            safety_guard=SafetyGuard(tmp_path),
            max_bytes=100,
        )
    )

    try:
        assert await asyncio.to_thread(read_complete.wait, 5)
        displaced = tmp_path / "approved.original.txt"
        approved.rename(displaced)
        approved.symlink_to(outside)
        release_read.set()
        assert await upload == "snapshot"
    finally:
        release_read.set()
        await asyncio.gather(upload, return_exceptions=True)
        approved.unlink(missing_ok=True)
        outside.unlink(missing_ok=True)

    assert isinstance(chooser.files, dict)
    assert not isinstance(chooser.files, (str, Path))
    assert chooser.files["name"] == "approved.txt"
    assert chooser.files["mimeType"] == "text/plain"
    assert chooser.files["buffer"] == b"approved-content"
    assert chooser.files["buffer"] != b"outside-content"


@pytest.mark.asyncio
async def test_browser_session_upload_does_not_follow_sensitive_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ash.safety import scoped_io

    approved = tmp_path / "notes.txt"
    approved.write_bytes(b"approved-notes")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-id-ed25519"
    outside.write_bytes(b"outside-sensitive-sentinel")
    try:
        probe = tmp_path / "symlink-probe"
        probe.symlink_to(outside)
        probe.unlink()
    except OSError as exc:
        outside.unlink(missing_ok=True)
        pytest.skip(f"symlinks are unavailable: {exc}")

    real_read = scoped_io.read_scoped_bytes

    def read_then_swap(path: str | Path, guard: SafetyGuard, **kwargs: Any):
        result = real_read(path, guard, **kwargs)
        target = Path(path)
        displaced = target.with_name("notes.original.txt")
        target.rename(displaced)
        target.symlink_to(outside)
        return result

    monkeypatch.setattr(scoped_io, "read_scoped_bytes", read_then_swap)
    chooser = _UploadChooser()
    session = _upload_session(chooser)

    try:
        assert (
            await session.upload_file(
                "e1",
                str(approved),
                safety_guard=SafetyGuard(tmp_path),
                max_bytes=100,
            )
            == "snapshot"
        )
    finally:
        approved.unlink(missing_ok=True)
        outside.unlink(missing_ok=True)

    assert isinstance(chooser.files, dict)
    assert chooser.files["name"] == "notes.txt"
    assert chooser.files["buffer"] == b"approved-notes"
    assert chooser.files["buffer"] != b"outside-sensitive-sentinel"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("relative_path", "expected_name", "expected_mime"),
    (
        ("nested/report.txt", "report.txt", "text/plain"),
        ("payload.unknown", "payload.unknown", "application/octet-stream"),
        ("no_extension", "no_extension", "application/octet-stream"),
    ),
)
async def test_browser_session_upload_uses_basename_and_filename_mime(
    tmp_path: Path,
    relative_path: str,
    expected_name: str,
    expected_mime: str,
) -> None:
    source = tmp_path / relative_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"approved")
    chooser = _UploadChooser()
    session = _upload_session(chooser)

    assert (
        await session.upload_file(
            "e1",
            str(source),
            safety_guard=SafetyGuard(tmp_path),
            max_bytes=100,
        )
        == "snapshot"
    )

    assert chooser.files == {
        "name": expected_name,
        "mimeType": expected_mime,
        "buffer": b"approved",
    }


@pytest.mark.asyncio
async def test_browser_session_upload_preserves_size_limit_and_sensitive_rejection(
    tmp_path: Path,
) -> None:
    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"0123456789")
    chooser = _UploadChooser()
    session = _upload_session(chooser)

    with pytest.raises(ValueError, match="upload exceeds 5 bytes"):
        await session.upload_file(
            "e1",
            str(oversized),
            safety_guard=SafetyGuard(tmp_path),
            max_bytes=5,
        )
    assert chooser.files is None

    sensitive = tmp_path / ".env"
    sensitive.write_text("TOKEN=not-a-real-secret", encoding="utf-8")
    with pytest.raises(ValueError, match="Refusing to attach sensitive path"):
        await session.upload_file(
            "e1",
            str(sensitive),
            safety_guard=SafetyGuard(tmp_path),
            max_bytes=100,
        )
    assert chooser.files is None


def test_browser_url_policy_blocks_private_non_http_and_disallowed_hosts(
    monkeypatch,
) -> None:
    with pytest.raises(ValueError, match="non-public"):
        _validate_browser_url("http://127.0.0.1/private", ())
    with pytest.raises(ValueError, match="Only http"):
        _validate_browser_url("file:///etc/passwd", ())
    with pytest.raises(ValueError, match="embedded credentials"):
        _validate_browser_url("https://user:secret@example.com", ())

    monkeypatch.setattr("ash.tools.web._ensure_public_host", lambda hostname: None)
    assert (
        _validate_browser_url("wss://api.example.com/socket", ("*.example.com",))
        == "wss://api.example.com/socket"
    )
    with pytest.raises(ValueError, match="allowed_web_domains"):
        _validate_browser_url("https://blocked.example/page", ("docs.example",))


def test_browser_url_output_redacts_oauth_query_and_fragment_credentials() -> None:
    redacted = _redact_browser_url(
        "https://login.example/callback?"
        "code=authorization-value&next=%2Fhome&api_key=provider-secret"
        "#access_token=browser-token&section=profile"
    )

    assert "authorization-value" not in redacted
    assert "provider-secret" not in redacted
    assert "browser-token" not in redacted
    assert "code=[REDACTED]" in redacted
    assert "api_key=[REDACTED]" in redacted
    assert "access_token=[REDACTED]" in redacted
    assert "next=%2Fhome" in redacted
    assert "section=profile" in redacted

    spa = _redact_browser_url(
        "https://user:password@login.example/app"
        "#/callback?code=spa-code&view=complete"
    )
    assert "user:password@" not in spa
    assert "#/callback?code=[REDACTED]&view=complete" in spa

    camel_case = _redact_browser_url(
        "https://login.example/callback?"
        "accessToken=browser-token&clientSecret=client-secret&"
        "apikey=plain-api-key&APIKey=upper-api-key&view=complete"
    )
    assert "browser-token" not in camel_case
    assert "client-secret" not in camel_case
    assert "plain-api-key" not in camel_case
    assert "upper-api-key" not in camel_case
    assert "accessToken=[REDACTED]" in camel_case
    assert "clientSecret=[REDACTED]" in camel_case
    assert "apikey=[REDACTED]" in camel_case
    assert "APIKey=[REDACTED]" in camel_case
    assert "view=complete" in camel_case


def test_browser_tools_share_one_lazy_session_and_permissions(tmp_path) -> None:
    tools = build_browser_tools(SafetyGuard(tmp_path))

    assert len({id(tool.session) for tool in tools}) == 1
    assert {tool.name for tool in tools} >= {
        "browser_tabs",
        "browser_open_tab",
        "browser_focus_tab",
        "browser_close_tab",
    }
    assert (
        PermissionPolicy("interactive").evaluate("browser_snapshot", {}).action
        == PolicyAction.ALLOW
    )
    assert (
        PermissionPolicy("interactive").evaluate("browser_tabs", {}).action
        == PolicyAction.ALLOW
    )
    assert (
        PermissionPolicy("interactive")
        .evaluate("browser_navigate", {"url": "https://example.com"})
        .action
        == PolicyAction.ASK
    )
    assert (
        PermissionPolicy("interactive")
        .evaluate("browser_focus_tab", {"tab_id": "tdeadbeef-2"})
        .action
        == PolicyAction.ASK
    )
    assert (
        PermissionPolicy("interactive")
        .evaluate(
            "browser_download",
            {"ref": "e1", "file_path": "downloads/file.bin"},
        )
        .action
        == PolicyAction.ASK
    )


@pytest.mark.asyncio
async def test_browser_tool_reports_stale_refs_without_raising(tmp_path) -> None:
    class StaleSession(FakeBrowserSession):
        async def click(self, ref: str) -> str:
            raise ValueError("stale or missing")

    tool = BrowserClickTool(
        SafetyGuard(tmp_path),
        StaleSession(),  # type: ignore[arg-type]
    )

    result = await tool.run(ref="tdeadbeef-1:s1:e1")

    assert result.success is False
    assert "stale or missing" in (result.error or "")


@pytest.mark.asyncio
async def test_browser_tabs_tool_propagates_json_truncation_metadata(tmp_path) -> None:
    class TruncatedSession(FakeBrowserSession):
        async def list_tabs(self) -> str:
            return '{"tabs":[],"total":32,"truncated":true}'

    tool = BrowserTabsTool(
        SafetyGuard(tmp_path),
        TruncatedSession(),  # type: ignore[arg-type]
    )

    result = await tool.run()

    assert result.success is True
    assert result.truncated is True


def test_browser_cdp_url_is_loopback_only_and_credential_free() -> None:
    assert _validate_cdp_url("http://127.0.0.1:9222") == "http://127.0.0.1:9222"
    assert _validate_cdp_url("ws://[::1]:9222/devtools/browser/id") == (
        "ws://[::1]:9222/devtools/browser/id"
    )
    assert _validate_cdp_url("https://localhost:9443") == "https://localhost:9443"

    for value in (
        "https://example.com:9222",
        "http://10.0.0.2:9222",
        "http://user:secret@127.0.0.1:9222",
        "http://127.0.0.1:9222/?token=secret",
        "http://127.0.0.1:9222/#fragment",
        "file:///tmp/chrome",
        "http://127.0.0.1:0",
    ):
        with pytest.raises(ValueError):
            _validate_cdp_url(value)


def test_browser_cdp_rejects_persistent_profile_and_orphaned_storage_reuse(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="persistent profile"):
        BrowserSession(
            cdp_url="http://127.0.0.1:9222",
            profile_path=tmp_path / "profile",
        )
    with pytest.raises(ValueError, match="requires browser_cdp_url"):
        BrowserSession(cdp_reuse_storage_state=True)


@pytest.mark.asyncio
async def test_browser_cdp_attach_failure_cleans_started_resources() -> None:
    proxy = MagicMock()
    proxy.start = AsyncMock()
    proxy.close = AsyncMock()
    proxy.playwright_settings = {"server": "http://127.0.0.1:1", "bypass": "<-loopback>"}
    playwright = MagicMock()
    playwright.chromium.connect_over_cdp = AsyncMock(side_effect=RuntimeError("attach failed"))
    playwright.stop = AsyncMock()
    factory = MagicMock()
    factory.start = AsyncMock(return_value=playwright)
    session = BrowserSession(cdp_url="http://127.0.0.1:9222")

    with patch("ash.tools.browser.BrowserPolicyProxy", return_value=proxy):
        with patch("playwright.async_api.async_playwright", return_value=factory):
            with pytest.raises(BrowserUnavailableError, match="browser session"):
                await session.ensure_started()

    proxy.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()
    assert session._proxy is None
    assert session._playwright is None
    assert session._browser is None
    assert session._context is None


@pytest.mark.asyncio
async def test_browser_cdp_uses_isolated_policy_context_and_optional_storage() -> None:
    class FakeContext:
        def set_default_timeout(self, value):
            pass

        def set_default_navigation_timeout(self, value):
            pass

        async def route(self, *args):
            return None

        async def route_web_socket(self, *args):
            return None

        async def new_page(self):
            return object()

        async def close(self):
            return None

    source_context = MagicMock()
    source_context.storage_state = AsyncMock(
        return_value={
            "cookies": [{"name": "session", "value": "ok", "domain": ".example.com", "path": "/", "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}],
            "origins": [],
        }
    )
    isolated = FakeContext()
    attached = MagicMock()
    attached.contexts = [source_context]
    attached.new_context = AsyncMock(return_value=isolated)
    attached.close = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.connect_over_cdp = AsyncMock(return_value=attached)
    playwright.chromium.launch = AsyncMock()
    playwright.stop = AsyncMock()
    factory = MagicMock()
    factory.start = AsyncMock(return_value=playwright)
    session = BrowserSession(
        cdp_url="http://127.0.0.1:9222",
        cdp_reuse_storage_state=True,
    )

    with patch("playwright.async_api.async_playwright", return_value=factory):
        await session.ensure_started()
        await session.close()

    kwargs = playwright.chromium.connect_over_cdp.await_args.kwargs
    assert kwargs["timeout"] == 30_000
    playwright.chromium.launch.assert_not_awaited()
    source_context.storage_state.assert_awaited_once_with()
    context_kwargs = attached.new_context.await_args.kwargs
    assert context_kwargs["service_workers"] == "block"
    assert context_kwargs["accept_downloads"] is True
    assert context_kwargs["storage_state"]["cookies"][0]["name"] == "session"
    assert context_kwargs["proxy"]["bypass"] == "<-loopback>"
    assert context_kwargs["proxy"]["server"].startswith("http://127.0.0.1:")
    attached.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_cdp_rejects_non_object_storage_state() -> None:
    source_context = MagicMock()
    source_context.storage_state = AsyncMock(return_value=["not", "an", "object"])
    attached = MagicMock()
    attached.contexts = [source_context]
    attached.close = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.connect_over_cdp = AsyncMock(return_value=attached)
    playwright.stop = AsyncMock()
    factory = MagicMock()
    factory.start = AsyncMock(return_value=playwright)
    session = BrowserSession(
        cdp_url="http://127.0.0.1:9222",
        cdp_reuse_storage_state=True,
    )

    with patch("playwright.async_api.async_playwright", return_value=factory):
        with pytest.raises(BrowserUnavailableError, match="invalid storage state"):
            await session.ensure_started()

    attached.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_cdp_storage_import_timeout_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker = asyncio.Event()
    source_context = MagicMock()

    async def blocked_storage_state():
        await blocker.wait()
        return {"cookies": [], "origins": []}

    source_context.storage_state = AsyncMock(side_effect=blocked_storage_state)
    attached = MagicMock()
    attached.contexts = [source_context]
    attached.close = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.connect_over_cdp = AsyncMock(return_value=attached)
    playwright.stop = AsyncMock()
    factory = MagicMock()
    factory.start = AsyncMock(return_value=playwright)
    session = BrowserSession(
        timeout_seconds=1,
        cdp_url="http://127.0.0.1:9222",
        cdp_reuse_storage_state=True,
    )

    with patch("playwright.async_api.async_playwright", return_value=factory):
        with pytest.raises(BrowserUnavailableError, match="storage-state copy timed out"):
            await session.ensure_started()

    attached.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_cdp_storage_import_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_context = MagicMock()
    source_context.storage_state = AsyncMock(
        return_value={"cookies": [], "origins": [{"origin": "https://example.com", "localStorage": [{"name": "x", "value": "y" * 100}]}]}
    )
    attached = MagicMock()
    attached.contexts = [source_context]
    attached.close = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.connect_over_cdp = AsyncMock(return_value=attached)
    playwright.stop = AsyncMock()
    factory = MagicMock()
    factory.start = AsyncMock(return_value=playwright)
    monkeypatch.setattr("ash.tools.browser.MAX_CDP_STORAGE_STATE_BYTES", 32)
    session = BrowserSession(
        cdp_url="http://127.0.0.1:9222",
        cdp_reuse_storage_state=True,
    )

    with patch("playwright.async_api.async_playwright", return_value=factory):
        with pytest.raises(BrowserUnavailableError, match="storage state exceeded"):
            await session.ensure_started()

    attached.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_profile_is_ephemeral_by_default() -> None:
    session = BrowserSession()

    class FakeContext:
        def set_default_timeout(self, value):
            pass

        def set_default_navigation_timeout(self, value):
            pass

        async def route(self, *args):
            return None

        async def route_web_socket(self, *args):
            return None

        async def new_page(self):
            return object()

    with patch("playwright.async_api.async_playwright") as playwright_factory:
        playwright = MagicMock()
        launch_result = MagicMock()
        launch_result.new_context = AsyncMock(return_value=FakeContext())
        playwright.chromium.launch = AsyncMock(return_value=launch_result)
        playwright.chromium.launch_persistent_context = AsyncMock(
            return_value=FakeContext()
        )
        playwright.stop = AsyncMock()
        playwright_factory.return_value.start = AsyncMock(return_value=playwright)

        await session.ensure_started()

    playwright.chromium.launch_persistent_context.assert_not_called()
    launch_kwargs = playwright.chromium.launch.await_args.kwargs
    assert launch_kwargs["proxy"]["bypass"] == "<-loopback>"
    assert launch_kwargs["proxy"]["server"].startswith("http://127.0.0.1:")
    kwargs = playwright.chromium.launch.return_value.new_context.await_args.kwargs
    assert "user_data_dir" not in kwargs
    assert kwargs["accept_downloads"] is True
    await session.close()


@pytest.mark.asyncio
async def test_browser_optin_profile_creates_private_directory(tmp_path: Path) -> None:
    profile = tmp_path / "state" / "browser-profile"
    session = BrowserSession(profile_path=profile)

    class FakeContext:
        def set_default_timeout(self, value):
            pass

        def set_default_navigation_timeout(self, value):
            pass

        async def route(self, *args):
            return None

        async def route_web_socket(self, *args):
            return None

        async def new_page(self):
            return object()

    with patch("playwright.async_api.async_playwright") as playwright_factory:
        playwright = MagicMock()
        launch_result = MagicMock()
        launch_result.new_context = AsyncMock(return_value=FakeContext())
        playwright.chromium.launch = AsyncMock(return_value=launch_result)
        playwright.chromium.launch_persistent_context = AsyncMock(
            return_value=FakeContext()
        )
        playwright.stop = AsyncMock()
        playwright_factory.return_value.start = AsyncMock(return_value=playwright)

        await session.ensure_started()

    assert profile.is_dir()
    if os.name != "nt":
        assert profile.stat().st_mode & 0o077 == 0
    kwargs = playwright.chromium.launch_persistent_context.await_args.kwargs
    assert kwargs["user_data_dir"] == str(profile)
    assert kwargs["proxy"]["bypass"] == "<-loopback>"
    assert kwargs["proxy"]["server"].startswith("http://127.0.0.1:")
    await session.close()


@pytest.mark.asyncio
async def test_browser_session_fails_closed_if_policy_proxy_cannot_start() -> None:
    session = BrowserSession()
    proxy = MagicMock()
    proxy.start = AsyncMock(
        side_effect=BrowserProxyError("browser policy proxy could not start")
    )
    proxy.close = AsyncMock()

    with patch("ash.tools.browser.BrowserPolicyProxy", return_value=proxy):
        with pytest.raises(BrowserUnavailableError, match="policy proxy"):
            await session.ensure_started()

    proxy.close.assert_awaited_once()
    assert session._page is None
    assert session._proxy is None


@pytest.mark.asyncio
async def test_browser_session_startup_cancellation_closes_started_proxy() -> None:
    class FakeProxy:
        instances: list["FakeProxy"] = []

        def __init__(self, *_args, **_kwargs) -> None:
            self.closed = False
            self.started = asyncio.Event()
            type(self).instances.append(self)

        async def start(self) -> None:
            self.started.set()

        @property
        def playwright_settings(self) -> dict[str, str]:
            return {"server": "http://127.0.0.1:1", "bypass": "<-loopback>"}

        async def close(self) -> None:
            self.closed = True

    playwright = MagicMock()
    playwright.chromium.launch = AsyncMock()
    playwright_factory = MagicMock()
    playwright_factory.start = AsyncMock(
        side_effect=asyncio.CancelledError()
    )
    session = BrowserSession()

    with patch("ash.tools.browser.BrowserPolicyProxy", FakeProxy):
        with patch("playwright.async_api.async_playwright", return_value=playwright_factory):
            startup = asyncio.create_task(session.ensure_started())
            await asyncio.sleep(0)
            await FakeProxy.instances[-1].started.wait()
            with pytest.raises(asyncio.CancelledError):
                await startup

    assert FakeProxy.instances[-1].closed is True
    assert session._proxy is None
    assert playwright.chromium.launch.await_count == 0


@pytest.mark.asyncio
async def test_browser_session_retires_proxy_before_restarting_after_page_closes() -> None:
    class FakeProxy:
        instances: list["FakeProxy"] = []

        def __init__(self, *_args, **_kwargs) -> None:
            self.closed = False
            type(self).instances.append(self)

        async def start(self) -> None:
            return None

        @property
        def playwright_settings(self) -> dict[str, str]:
            return {"server": "http://127.0.0.1:1", "bypass": "<-loopback>"}

        async def close(self) -> None:
            self.closed = True

    class FakePage:
        def __init__(self) -> None:
            self.closed = False

        def is_closed(self) -> bool:
            return self.closed

    class FakeContext:
        def __init__(self, page: FakePage) -> None:
            self.pages = [page]
            self.close_calls = 0

        def set_default_timeout(self, _value: int) -> None:
            return None

        def set_default_navigation_timeout(self, _value: int) -> None:
            return None

        async def route(self, *_args) -> None:
            return None

        async def route_web_socket(self, *_args) -> None:
            return None

        async def new_page(self) -> FakePage:
            return self.pages[0]

        async def close(self) -> None:
            self.close_calls += 1

    page_one = FakePage()
    page_two = FakePage()
    context_one = FakeContext(page_one)
    context_two = FakeContext(page_two)
    browser = MagicMock()
    browser.new_context = AsyncMock(side_effect=[context_one, context_two])
    playwright = MagicMock()
    playwright.chromium.launch = AsyncMock(return_value=browser)
    playwright.stop = AsyncMock()
    playwright_factory = MagicMock()
    playwright_factory.start = AsyncMock(return_value=playwright)
    session = BrowserSession()

    with patch("ash.tools.browser.BrowserPolicyProxy", FakeProxy):
        with patch("playwright.async_api.async_playwright", return_value=playwright_factory):
            first_page = await session.ensure_started()
            page_one.closed = True
            second_page = await session.ensure_started()
            await session.close()

    assert first_page is page_one
    assert second_page is page_two
    assert FakeProxy.instances[0].closed is True
    assert len(FakeProxy.instances) == 2
    assert context_one.close_calls == 1
    assert context_two.close_calls == 1


@pytest.mark.asyncio
async def test_browser_session_close_settles_proxy_after_repeated_cancellation() -> None:
    context_close_started = asyncio.Event()
    release_context_close = asyncio.Event()
    proxy_closed = asyncio.Event()

    class BlockingContext:
        async def close(self) -> None:
            context_close_started.set()
            await release_context_close.wait()

    class FakeProxy:
        async def close(self) -> None:
            assert release_context_close.is_set()
            proxy_closed.set()

    session = BrowserSession()
    session._context = BlockingContext()
    session._proxy = FakeProxy()  # type: ignore[assignment]
    closing = asyncio.create_task(session.close())

    await asyncio.wait_for(context_close_started.wait(), timeout=1)
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not proxy_closed.is_set()

    release_context_close.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert proxy_closed.is_set()
    assert session._proxy is None
    assert not any(
        task.get_name() == "ash-browser-close" and not task.done()
        for task in asyncio.all_tasks()
    )


def test_browser_profile_rejects_symlinked_state_path(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    profile = tmp_path / "state" / "browser-profile"
    profile.parent.mkdir()
    try:
        profile.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(ValueError, match="browser profile directory.*symlink or junction"):
        BrowserSession(profile_path=profile)


@pytest.mark.asyncio
async def test_browser_profile_revalidates_before_start(tmp_path: Path) -> None:
    profile = tmp_path / "state" / "browser-profile"
    session = BrowserSession(profile_path=profile)
    outside = tmp_path / "outside"
    outside.mkdir()
    profile.parent.mkdir()
    try:
        profile.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(
        BrowserUnavailableError,
        match="browser profile directory.*symlink or junction",
    ):
        await session.ensure_started()

    assert list(outside.iterdir()) == []
