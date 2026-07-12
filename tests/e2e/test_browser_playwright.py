from __future__ import annotations

import os

import pytest

from ash.tools.browser import BrowserSession


pytestmark = pytest.mark.skipif(
    os.environ.get("ASH_RUN_BROWSER_TESTS") != "1",
    reason="set ASH_RUN_BROWSER_TESTS=1 after installing ash-ai[browser] and Chromium",
)


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
        assert "[e1] input 'Name'" in initial
        assert "[e2] button 'Greet'" in initial
        assert "must-not-leak" not in initial

        await session.type_text("e1", "Ash", submit=False, clear=True)
        clicked = await session.click("e2")
        assert "Hello Ash" in clicked

        with pytest.raises(ValueError, match="password"):
            await session.type_text("e3", "secret", submit=False, clear=True)

        blocked = await page.evaluate(
            """async () => {
              try { await fetch('http://127.0.0.1/private'); return false; }
              catch (_) { return true; }
            }"""
        )
        assert blocked is True
    finally:
        await session.close()
