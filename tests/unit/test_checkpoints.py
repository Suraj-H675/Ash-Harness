import pytest

from core.checkpoints import (
    FileCheckpointMiddleware,
    diff_latest_checkpoint,
    undo_latest_checkpoint,
)
from core.session import SessionStore
from safety.guard import SafetyGuard
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
    result = await tool.run(**arguments)
    await middleware.after_tool("whole_edit", arguments, result)

    assert diff_latest_checkpoint(store, guard, session.session_id) == (
        f"Binary file changed: {path}"
    )
