from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ash.safety.guard import SafetyGuard
from ash.safety.policy import PermissionPolicy, PolicyAction
from ash.tools.browser import (
    BrowserSession,
    BrowserUnavailableError,
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
