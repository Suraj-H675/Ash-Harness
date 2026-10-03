from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from ash.core.session import Message, SessionStore
from ash.safety.guard import SafetyGuard
from ash.tools.sessions import SearchSessionsTool


@pytest.mark.asyncio
async def test_search_sessions_tool_returns_bounded_current_workspace_history(
    tmp_path,
) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    store = SessionStore(tmp_path / "sessions.db")
    current = store.create_session(str(project))
    external = store.create_session(str(other))
    timestamp = datetime.now(timezone.utc)
    store.save_message(
        current.session_id,
        Message(
            role="assistant",
            content="Fixed reconnect needle\x1b[2J without replaying writes.",
            timestamp=timestamp,
        ),
    )
    store.save_message(
        external.session_id,
        Message(
            role="assistant",
            content="reconnect needle from another workspace",
            timestamp=timestamp,
        ),
    )
    tool = SearchSessionsTool(SafetyGuard(project), store, project)

    result = await tool.run(query="reconnect needle", limit=5)

    assert result.success is True
    payload = json.loads(result.output)
    assert [item["session_id"] for item in payload["matches"]] == [
        current.session_id
    ]
    assert "\x1b" not in payload["matches"][0]["excerpt"]
    assert "\\x1b[2J" in payload["matches"][0]["excerpt"]


@pytest.mark.asyncio
async def test_search_sessions_tool_rejects_blank_query(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    tool = SearchSessionsTool(
        SafetyGuard(project),
        SessionStore(tmp_path / "sessions.db"),
        project,
    )

    with pytest.raises(ValueError, match="cannot be blank"):
        await tool.run(query="   ")
