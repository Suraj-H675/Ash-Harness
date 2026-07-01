import pytest
from datetime import datetime, timezone

from core.checkpoints import (
    FileCheckpointMiddleware,
    diff_latest_checkpoint,
    rewind_session_with_files,
    undo_latest_checkpoint,
)
from core.session import Message, SessionStore
from safety.guard import SafetyGuard
from tools.base import ToolResult
from tools.filesystem import WholeEditTool


@pytest.mark.asyncio
async def test_checkpoint_undo_and_conflict_detection(tmp_path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("before")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))

    def context():
        return session.session_id, "turn-1"

    guard = SafetyGuard(tmp_path)
    middleware = FileCheckpointMiddleware(store, guard, context)
    arguments = {"file_path": "file.txt", "content": "after"}
    tool = WholeEditTool(guard)
    await middleware.before_tool("whole_edit", arguments, tool)
    result = await tool.run(**arguments)
    await middleware.after_tool("whole_edit", arguments, result)
    assert path.read_text() == "after"
    assert undo_latest_checkpoint(store, guard, session.session_id) == [path]
    assert path.read_text() == "before"

    def context2():
        return session.session_id, "turn-2"

    middleware = FileCheckpointMiddleware(store, guard, context2)
    await middleware.before_tool("whole_edit", arguments, tool)
    result = await tool.run(**arguments)
    await middleware.after_tool("whole_edit", arguments, result)
    path.write_text("user change")
    with pytest.raises(RuntimeError, match="changed after"):
        undo_latest_checkpoint(store, guard, session.session_id)
    assert path.read_text() == "user change"


@pytest.mark.asyncio
async def test_checkpoint_diff_renders_latest_turn(tmp_path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("before\n")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    middleware = FileCheckpointMiddleware(
        store, guard, lambda: (session.session_id, "t1")
    )
    arguments = {"file_path": "file.txt", "content": "after\n"}
    tool = WholeEditTool(guard)

    await middleware.before_tool("whole_edit", arguments, tool)
    result = await tool.run(**arguments)
    await middleware.after_tool("whole_edit", arguments, result)

    rendered = diff_latest_checkpoint(store, guard, session.session_id)

    assert "--- a/file.txt" in rendered
    assert "+++ b/file.txt" in rendered
    assert "-before" in rendered
    assert "+after" in rendered


@pytest.mark.asyncio
async def test_checkpoint_diff_refuses_current_file_conflict(tmp_path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("before\n")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    middleware = FileCheckpointMiddleware(
        store, guard, lambda: (session.session_id, "t1")
    )
    arguments = {"file_path": "file.txt", "content": "after\n"}
    tool = WholeEditTool(guard)

    await middleware.before_tool("whole_edit", arguments, tool)
    result = await tool.run(**arguments)
    await middleware.after_tool("whole_edit", arguments, result)
    path.write_text("user edit\n")

    with pytest.raises(RuntimeError, match="files changed after Ash's edit"):
        diff_latest_checkpoint(store, guard, session.session_id)


@pytest.mark.asyncio
async def test_checkpoint_diff_reports_binary_change(tmp_path) -> None:
    path = tmp_path / "data.bin"
    path.write_bytes(b"\x00before")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    middleware = FileCheckpointMiddleware(
        store, guard, lambda: (session.session_id, "t1")
    )
    arguments = {"file_path": "data.bin", "content": "\x00after"}
    tool = WholeEditTool(guard)

    await middleware.before_tool("whole_edit", arguments, tool)
    path.write_bytes(b"\x00after")
    await middleware.after_tool(
        "whole_edit",
        arguments,
        ToolResult(success=True, output="written"),
    )

    assert diff_latest_checkpoint(store, guard, session.session_id) == (
        f"Binary file changed: {path}"
    )


async def _record_edit_turn(
    store: SessionStore,
    guard: SafetyGuard,
    session_id: str,
    turn_id: str,
    path: str,
    content: str,
) -> None:
    store.start_turn(session_id, turn_id, content)
    store.save_message(
        session_id,
        Message(role="user", content=content, timestamp=datetime.now(timezone.utc)),
        turn_id=turn_id,
    )
    middleware = FileCheckpointMiddleware(store, guard, lambda: (session_id, turn_id))
    arguments = {"file_path": path, "content": content}
    tool = WholeEditTool(guard)
    await middleware.before_tool("whole_edit", arguments, tool)
    result = await tool.run(**arguments)
    await middleware.after_tool("whole_edit", arguments, result)
    store.save_message(
        session_id,
        Message(role="assistant", content="done", timestamp=datetime.now(timezone.utc)),
        turn_id=turn_id,
    )
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": 0.01,
        "estimated_prompt_tokens": 0,
        "estimated_completion_tokens": 0,
        "estimated_cost_usd": 0.0,
    }
    store.save_turn_usage(turn_id, usage)
    store.save_session_token_stats(session_id, 10, 2, 0.01)
    store.complete_turn(turn_id)


@pytest.mark.asyncio
async def test_combined_rewind_restores_multiple_turns_and_usage(tmp_path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("version-0")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    await _record_edit_turn(
        store, guard, session.session_id, "turn-1", "file.txt", "version-1"
    )
    await _record_edit_turn(
        store, guard, session.session_id, "turn-2", "file.txt", "version-2"
    )

    rewound, restored = rewind_session_with_files(store, guard, session.session_id, 2)

    assert path.read_text() == "version-1"
    assert restored == [path]
    assert [message.content for message in rewound.messages] == ["version-1", "done"]
    usage = store.get_session_usage(session.session_id)
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 2
    assert usage.cost_usd == pytest.approx(0.01)

    empty, restored = rewind_session_with_files(store, guard, session.session_id, 0)
    assert path.read_text() == "version-0"
    assert restored == [path]
    assert empty.messages == []
    assert store.get_session_usage(session.session_id).total_tokens == 0


@pytest.mark.asyncio
async def test_combined_rewind_conflict_leaves_transcript_and_files_unchanged(
    tmp_path,
) -> None:
    path = tmp_path / "file.txt"
    path.write_text("before")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    await _record_edit_turn(
        store, guard, session.session_id, "turn-1", "file.txt", "after"
    )
    path.write_text("user-change")

    with pytest.raises(RuntimeError, match="files changed after Ash's edit"):
        rewind_session_with_files(store, guard, session.session_id, 0)

    assert path.read_text() == "user-change"
    assert len(store.load_session(session.session_id).messages) == 2
    assert store.get_session_usage(session.session_id).total_tokens == 12
    assert store.latest_file_checkpoints(session.session_id)


@pytest.mark.asyncio
async def test_combined_rewind_rolls_files_forward_when_database_rewind_fails(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "file.txt"
    path.write_text("before")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    await _record_edit_turn(
        store, guard, session.session_id, "turn-1", "file.txt", "after"
    )

    def fail(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "rewind_session", fail)
    with pytest.raises(RuntimeError, match="database unavailable"):
        rewind_session_with_files(store, guard, session.session_id, 0)

    assert path.read_text() == "after"


def test_combined_rewind_requires_complete_turn_boundaries_and_mapping(
    tmp_path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    now = datetime.now(timezone.utc)
    store.start_turn(session.session_id, "turn-1", "work")
    for content in ("user", "assistant"):
        store.save_message(
            session.session_id,
            Message(role="user", content=content, timestamp=now),
            turn_id="turn-1",
        )

    with pytest.raises(ValueError, match="splits an Ash turn"):
        rewind_session_with_files(store, SafetyGuard(tmp_path), session.session_id, 1)

    legacy = store.create_session(str(tmp_path))
    store.save_message(
        legacy.session_id,
        Message(role="user", content="legacy", timestamp=now),
    )
    with pytest.raises(ValueError, match="legacy messages"):
        rewind_session_with_files(store, SafetyGuard(tmp_path), legacy.session_id, 0)
