from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
import uvicorn
from playwright.async_api import async_playwright

from ash.sdk import AshEvent
from ash.server.http import HTTPApprovalBroker, create_app


pytestmark = pytest.mark.skipif(
    os.environ.get("ASH_RUN_BROWSER_TESTS") != "1",
    reason=(
        "set ASH_RUN_BROWSER_TESTS=1 after installing the browser and server "
        "optional dependencies plus Chromium"
    ),
)


class _SessionSummary:
    def __init__(self, session_id: str, title: str) -> None:
        self.session_id = session_id
        self.title = title

    def model_dump(self, *, mode: str = "json") -> dict[str, Any]:
        del mode
        return {
            "session_id": self.session_id,
            "title": self.title,
            "project_path": "/workspace",
            "created_at": "2026-10-03T00:00:00+00:00",
            "updated_at": "2026-10-03T00:00:00+00:00",
            "message_count": 2,
            "model": "fake/model",
            "parent_session_id": None,
            "root_session_id": self.session_id,
            "fork_message_count": None,
            "branch_name": "",
            "depth": 0,
            "context_summary": "",
        }


class _RemoteUIClient:
    def __init__(self) -> None:
        self.config = SimpleNamespace(model="fake/model")
        self.loop = SimpleNamespace(
            current_session=SimpleNamespace(session_id="session-1"),
            permission_policy=SimpleNamespace(
                mode=SimpleNamespace(value="interactive")
            ),
            project_root=Path("/workspace"),
            _last_context_tokens=0,
            last_turn_usage={
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cache_hit_rate": 0.0,
                "cost_usd": 0.0,
            },
        )
        self.turn_started = asyncio.Event()
        self.release_turn = asyncio.Event()
        self.steering: str | None = None
        self.stream_session_id: str | None = None

    def sessions(self, *, query: str = "", limit: int = 20) -> list[_SessionSummary]:
        del limit
        sessions = [
            _SessionSummary("session-1", "Main session"),
            _SessionSummary("session-2", "Review session"),
        ]
        query_cf = query.casefold()
        return [
            item
            for item in sessions
            if not query_cf
            or query_cf in item.title.casefold()
            or query_cf in item.session_id.casefold()
        ]

    def session_messages(
        self,
        session_id: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, str]]:
        del limit
        label = "main history" if session_id == "session-1" else "review history"
        return [
            {
                "role": "user",
                "content": label,
                "timestamp": "2026-10-03T00:00:00+00:00",
            },
            {
                "role": "assistant",
                "content": f"{label} reply",
                "timestamp": "2026-10-03T00:00:01+00:00",
            },
        ]

    async def resume(self, session_id: str) -> str:
        if session_id not in {"session-1", "session-2"}:
            raise KeyError(f"Session not found: {session_id}")
        self.loop.current_session = SimpleNamespace(session_id=session_id)
        return session_id

    async def new_session(self) -> str:
        self.loop.current_session = SimpleNamespace(session_id="session-new")
        return "session-new"

    async def steer(self, text: str) -> int:
        self.steering = text
        self.release_turn.set()
        return 1

    async def stream_prompt(
        self,
        text: str,
        *,
        session_id: str | None = None,
    ) -> AsyncIterator[AshEvent]:
        del text
        target = session_id or self.loop.current_session.session_id
        self.stream_session_id = target
        self.turn_started.set()
        yield AshEvent("assistant.delta", {"text": "hello remote"}, session_id=target)
        await self.release_turn.wait()
        yield AshEvent(
            "turn.completed",
            {"response": "hello remote"},
            session_id=target,
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_remote_control_ui_uses_real_app_for_sessions_stream_steer_and_approval() -> None:
    client = _RemoteUIClient()
    approval_broker = HTTPApprovalBroker(timeout_seconds=10)
    approval_task = asyncio.create_task(
        approval_broker.request("write_file", {"path": "README.md"})
    )
    await asyncio.sleep(0)
    app = create_app(
        client,  # type: ignore[arg-type]
        bearer_token="0123456789abcdef",
        requests_per_minute=120,
        approval_broker=approval_broker,
    )

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="critical",
            access_log=False,
            lifespan="on",
        )
    )
    server_task = asyncio.create_task(
        server.serve(sockets=[listener]),
        name="ash-remote-ui-e2e-server",
    )
    for _ in range(200):
        if server.started:
            break
        if server_task.done():
            await server_task
        await asyncio.sleep(0.01)
    assert server.started

    browser = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1100, "height": 760})
            await page.goto(f"http://127.0.0.1:{port}/ui")
            assert await page.title() == "Ash Remote"

            await page.locator("#token").fill("0123456789abcdef")
            await page.locator("#connect").click()
            await page.wait_for_function(
                "() => document.querySelector('#connection-state').textContent === 'Connected'"
            )
            await page.wait_for_function(
                "() => document.querySelector('#transcript').textContent.includes('main history')"
            )
            options = await page.locator("#sessions option").all_text_contents()
            assert "Review session — session-2" in options

            await page.locator("#sessions").select_option("session-2")
            await page.wait_for_function(
                "() => document.querySelector('#transcript').textContent.includes('review history')"
            )
            transcript = await page.locator("#transcript").inner_text()
            assert "main history" not in transcript

            await page.wait_for_function(
                "() => document.querySelector('#approvals').textContent.includes('write_file')"
            )
            await page.get_by_role("button", name="Approve").click()
            assert await asyncio.wait_for(approval_task, timeout=2) is True

            await page.locator("#prompt").fill("hello")
            await page.locator("#send").click()
            await asyncio.wait_for(client.turn_started.wait(), timeout=2)
            await page.wait_for_function(
                "() => document.querySelector('#send').textContent === 'Steer'"
            )
            await page.locator("#prompt").fill("focus on tests")
            await page.locator("#send").click()
            await page.wait_for_function(
                "() => document.querySelector('#transcript').textContent.includes('hello remote')"
            )

            assert client.stream_session_id == "session-2"
            assert client.steering == "focus on tests"
    finally:
        if browser is not None:
            await browser.close()
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=5)
        finally:
            listener.close()
            await approval_broker.close()
            if not approval_task.done():
                approval_task.cancel()
                await asyncio.gather(approval_task, return_exceptions=True)
