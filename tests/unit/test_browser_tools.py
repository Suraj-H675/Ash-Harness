from __future__ import annotations

from typing import Any

import pytest

from safety.guard import SafetyGuard
from safety.policy import PermissionPolicy, PolicyAction
from tools.browser import (
    BrowserBackTool,
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
    ]

    results = [
        await tools[0].run(url="https://example.com", wait_until="load"),
        await tools[1].run(),
        await tools[2].run(ref="e2"),
        await tools[3].run(ref="e3", text="hello", submit=True, clear=False),
        await tools[4].run(direction="up", amount=250),
        await tools[5].run(),
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
    ]
    assert session.closed == 1


def test_browser_url_policy_blocks_private_non_http_and_disallowed_hosts(
    monkeypatch,
) -> None:
    with pytest.raises(ValueError, match="non-public"):
        _validate_browser_url("http://127.0.0.1/private", ())
    with pytest.raises(ValueError, match="Only http"):
        _validate_browser_url("file:///etc/passwd", ())
    with pytest.raises(ValueError, match="embedded credentials"):
        _validate_browser_url("https://user:secret@example.com", ())

    monkeypatch.setattr("tools.web._ensure_public_host", lambda hostname: None)
    assert (
        _validate_browser_url("wss://api.example.com/socket", ("*.example.com",))
        == "wss://api.example.com/socket"
    )
    with pytest.raises(ValueError, match="allowed_web_domains"):
        _validate_browser_url("https://blocked.example/page", ("docs.example",))


def test_browser_tools_share_one_lazy_session_and_permissions(tmp_path) -> None:
    tools = build_browser_tools(SafetyGuard(tmp_path))

    assert len({id(tool.session) for tool in tools}) == 1
    assert PermissionPolicy("interactive").evaluate(
        "browser_snapshot", {}
    ).action == PolicyAction.ALLOW
    assert PermissionPolicy("interactive").evaluate(
        "browser_navigate", {"url": "https://example.com"}
    ).action == PolicyAction.ASK


@pytest.mark.asyncio
async def test_browser_tool_reports_stale_refs_without_raising(tmp_path) -> None:
    class StaleSession(FakeBrowserSession):
        async def click(self, ref: str) -> str:
            raise ValueError("stale or missing")

    tool = BrowserClickTool(
        SafetyGuard(tmp_path), StaleSession()  # type: ignore[arg-type]
    )

    result = await tool.run(ref="e1")

    assert result.success is False
    assert "stale or missing" in (result.error or "")
