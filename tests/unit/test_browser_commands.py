from __future__ import annotations

from types import SimpleNamespace

import pytest

from ash.cli import _handle_browser_command


class _FakeBrowserLoop:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.status = {
            "backend": "managed",
            "cdp_url": "",
            "reuse_storage_state": False,
            "profile": "ephemeral",
            "started": False,
        }

    def browser_runtime_status(self) -> dict[str, object]:
        return dict(self.status)

    async def configure_browser_runtime(
        self,
        *,
        cdp_url: str | None,
        reuse_storage_state: bool = False,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "cdp_url": cdp_url,
                "reuse_storage_state": reuse_storage_state,
            }
        )
        self.status = {
            "backend": "cdp" if cdp_url else "managed",
            "cdp_url": cdp_url or "",
            "reuse_storage_state": reuse_storage_state,
            "profile": "ephemeral",
            "started": True,
        }
        return dict(self.status)


@pytest.mark.asyncio
async def test_browser_command_connect_defaults_to_loopback_without_mutating_config() -> None:
    loop = _FakeBrowserLoop()
    config = SimpleNamespace(
        browser_cdp_url="",
        browser_cdp_reuse_storage_state=False,
    )

    rendered = await _handle_browser_command(loop, config, ["connect"])

    assert loop.calls == [
        {
            "cdp_url": "http://127.0.0.1:9222",
            "reuse_storage_state": False,
        }
    ]
    assert "Browser backend: attached via CDP" in rendered
    assert "http://127.0.0.1:9222" in rendered
    assert config.browser_cdp_url == ""


@pytest.mark.asyncio
async def test_browser_command_connect_reuse_is_explicit_and_disconnects_to_managed() -> None:
    loop = _FakeBrowserLoop()
    config = SimpleNamespace(
        browser_cdp_url="http://127.0.0.1:9333",
        browser_cdp_reuse_storage_state=True,
    )

    connected = await _handle_browser_command(
        loop,
        config,
        ["connect", "--reuse-storage-state"],
    )
    disconnected = await _handle_browser_command(loop, config, ["disconnect"])

    assert loop.calls == [
        {
            "cdp_url": "http://127.0.0.1:9333",
            "reuse_storage_state": True,
        },
        {"cdp_url": None, "reuse_storage_state": False},
    ]
    assert "Storage-state reuse: enabled" in connected
    assert "Browser backend: managed Chromium" in disconnected
    assert config.browser_cdp_url == "http://127.0.0.1:9333"
    assert config.browser_cdp_reuse_storage_state is True


@pytest.mark.asyncio
async def test_browser_command_rejects_unknown_actions_and_extra_disconnect_args() -> None:
    loop = _FakeBrowserLoop()
    config = SimpleNamespace(
        browser_cdp_url="",
        browser_cdp_reuse_storage_state=False,
    )

    with pytest.raises(ValueError, match="Usage: /browser"):
        await _handle_browser_command(loop, config, ["wat"])
    with pytest.raises(ValueError, match="Usage: /browser"):
        await _handle_browser_command(loop, config, ["disconnect", "extra"])


@pytest.mark.asyncio
async def test_browser_status_sanitizes_endpoint_for_terminal_output() -> None:
    loop = _FakeBrowserLoop()
    loop.status = {
        "backend": "cdp",
        "cdp_url": "http://127.0.0.1:9222/\x1b[2J",
        "reuse_storage_state": False,
        "profile": "isolated",
        "started": True,
    }
    config = SimpleNamespace(
        browser_cdp_url="",
        browser_cdp_reuse_storage_state=False,
    )

    rendered = await _handle_browser_command(loop, config, ["status"])

    assert "\x1b[2J" not in rendered
    assert "\\x1b[2J" in rendered
