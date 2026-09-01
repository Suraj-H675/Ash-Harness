from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ash.safety.guard import SafetyGuard
from ash.safety.policy import PermissionPolicy, PolicyAction
from ash.tools.browser import (
    BrowserSession,
    BrowserBackTool,
    BrowserUploadTool,
    BrowserDownloadTool,
    BrowserScreenshotTool,
    BrowserClickTool,
    BrowserNavigateTool,
    BrowserScrollTool,
    BrowserSnapshotTool,
    BrowserTypeTool,
    _validate_browser_url,
    build_browser_tools,
)


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


@pytest.mark.asyncio
async def test_browser_tools_dispatch_validated_actions_and_close(tmp_path) -> None:
    guard = SafetyGuard(tmp_path)
    session = FakeBrowserSession()
    tools = [
        BrowserNavigateTool(guard, session),  # type: ignore[arg-type]
        BrowserSnapshotTool(guard, session),  # type: ignore[arg-type]
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
        await tools[2].run(ref="e2"),
        await tools[3].run(ref="e3", text="hello", submit=True, clear=False),
        await tools[4].run(direction="up", amount=250),
        await tools[5].run(),
        await tools[6].run(max_bytes=1_000_000),
        await tools[7].run(
            ref="e4",
            file_path="docs/report.pdf",
            max_bytes=2_000_000,
        ),
        await tools[8].run(
            ref="e5",
            file_path="downloads/report.pdf",
            max_bytes=4_000_000,
        ),
    ]
    await tools[0].aclose()

    assert all(result.success for result in results)
    assert session.calls == [
        ("navigate", ("https://example.com", "load")),
        ("snapshot", None),
        ("click", "e2"),
        ("type", ("e3", "hello", True, False)),
        ("scroll", ("up", 250)),
        ("back", None),
        ("screenshot", 1_000_000),
        ("upload", ("e4", "docs/report.pdf", 2_000_000)),
        ("download", ("e5", "downloads/report.pdf", 4_000_000, False)),
    ]
    assert session.closed == 1
    assert results[6].images[0]["sha256"] == "a" * 64
    assert results[6].image_blocks[0]["data"] == "cG5nLWRhdGE="


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


def test_browser_tools_share_one_lazy_session_and_permissions(tmp_path) -> None:
    tools = build_browser_tools(SafetyGuard(tmp_path))

    assert len({id(tool.session) for tool in tools}) == 1
    assert (
        PermissionPolicy("interactive").evaluate("browser_snapshot", {}).action
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

    result = await tool.run(ref="e1")

    assert result.success is False
    assert "stale or missing" in (result.error or "")


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
    await session.close()
