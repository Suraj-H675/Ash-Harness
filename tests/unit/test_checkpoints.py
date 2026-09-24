import os

import pytest
from datetime import datetime, timezone
from pathlib import Path

from ash.core.checkpoints import (
    FileCheckpointMiddleware,
    diff_latest_checkpoint,
    recover_interrupted_turns,
    rewind_session_with_files,
    undo_latest_checkpoint,
)
from ash.core.session import Message, SessionStore, ToolCallRecord
from ash.safety.guard import SafetyGuard
from ash.tools.base import ToolResult
from ash.tools.filesystem import WholeEditTool


@pytest.mark.asyncio
async def test_checkpoint_read_rejects_oversized_file_before_edit(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("ash.core.checkpoints.MAX_CHECKPOINT_BYTES", 8)
    path = tmp_path / "large.txt"
    path.write_bytes(b"x" * 9)
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    middleware = FileCheckpointMiddleware(
        store, guard, lambda: (session.session_id, "turn-1")
    )

    with pytest.raises(ValueError, match="over 8 bytes"):
        await middleware.before_tool(
            "whole_edit",
            {"file_path": "large.txt", "content": "replacement"},
            WholeEditTool(guard),
        )

    assert path.read_bytes() == b"x" * 9


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
async def test_failed_partial_edit_does_not_poison_later_turn_checkpoint_undo(
    tmp_path,
) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("before-a", encoding="utf-8")
    b.write_text("before-b", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    call_id = "call-a"

    def context():
        return session.session_id, "turn-1", call_id

    middleware = FileCheckpointMiddleware(store, guard, context)
    tool = WholeEditTool(guard)

    failed_arguments = {"file_path": "a.txt", "content": "partial-a"}
    await middleware.before_tool("whole_edit", failed_arguments, tool)
    a.write_text("partial-a", encoding="utf-8")
    await middleware.after_tool(
        "whole_edit",
        failed_arguments,
        ToolResult(success=False, output="", error="simulated tool failure"),
    )

    call_id = "call-b"
    successful_arguments = {"file_path": "b.txt", "content": "after-b"}
    await middleware.before_tool("whole_edit", successful_arguments, tool)
    result = await tool.run(**successful_arguments)
    await middleware.after_tool("whole_edit", successful_arguments, result)

    rows = store.latest_file_checkpoints(session.session_id)
    assert len(rows) == 2
    assert all(row["after_sha256"] is not None for row in rows)
    assert set(undo_latest_checkpoint(store, guard, session.session_id)) == {a, b}
    assert a.read_text(encoding="utf-8") == "before-a"
    assert b.read_text(encoding="utf-8") == "before-b"


@pytest.mark.asyncio
async def test_failed_tool_checkpoint_finalization_error_preserves_original_context(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.core.checkpoints as checkpoints

    path = tmp_path / "a.txt"
    path.write_text("before", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    middleware = FileCheckpointMiddleware(
        store,
        guard,
        lambda: (session.session_id, "turn-1", "call-a"),
    )
    arguments = {"file_path": "a.txt", "content": "after"}
    await middleware.before_tool("whole_edit", arguments, WholeEditTool(guard))

    def fail_digest(_path, _guard):
        raise OSError("simulated checkpoint digest failure")

    monkeypatch.setattr(checkpoints, "_digest", fail_digest)
    result = ToolResult(success=False, output="", error="original tool failure")

    with pytest.raises(
        RuntimeError,
        match=(
            "original tool failure; checkpoint finalization failed: "
            "simulated checkpoint digest failure"
        ),
    ):
        await middleware.after_tool("whole_edit", arguments, result)

    rows = store.file_checkpoints_for_call(session.session_id, "turn-1", "call-a")
    assert len(rows) == 1
    assert rows[0]["after_sha256"] is None


@pytest.mark.asyncio
async def test_checkpoint_undo_removes_created_file_and_restores_mode(tmp_path) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_text("before", encoding="utf-8")
    existing.chmod(0o640)
    created = tmp_path / "created.txt"
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    middleware = FileCheckpointMiddleware(
        store, guard, lambda: (session.session_id, "turn-1", "call-1")
    )
    tool = WholeEditTool(guard)

    for path, content in ((existing, "after"), (created, "new content")):
        arguments = {"file_path": path.name, "content": content}
        await middleware.before_tool("whole_edit", arguments, tool)
        result = await tool.run(**arguments)
        await middleware.after_tool("whole_edit", arguments, result)

    existing.chmod(0o600)
    restored = undo_latest_checkpoint(store, guard, session.session_id)

    assert set(restored) == {existing, created}
    assert existing.read_text(encoding="utf-8") == "before"
    if os.name == "posix":
        assert existing.stat().st_mode & 0o777 == 0o640
    assert not created.exists()


@pytest.mark.asyncio
async def test_checkpoint_undo_rolls_files_forward_when_restore_fails(
    tmp_path, monkeypatch
) -> None:
    import ash.core.checkpoints as checkpoints

    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    tool = WholeEditTool(guard)
    for name in ("a.txt", "b.txt"):
        path = tmp_path / name
        path.write_text(f"before-{name}")
        middleware = FileCheckpointMiddleware(
            store,
            guard,
            lambda name=name: (session.session_id, "turn-1", f"call-{name}"),
        )
        arguments = {"file_path": name, "content": f"after-{name}"}
        await middleware.before_tool("whole_edit", arguments, tool)
        result = await tool.run(**arguments)
        await middleware.after_tool("whole_edit", arguments, result)

    original_restore = checkpoints.restore_scoped_file
    restore_calls = 0

    def fail_second_restore(path, content, guard, **kwargs):
        nonlocal restore_calls
        restore_calls += 1
        if restore_calls == 2:
            raise OSError("injected second-file failure")
        original_restore(path, content, guard, **kwargs)

    monkeypatch.setattr(checkpoints, "restore_scoped_file", fail_second_restore)

    with pytest.raises(OSError, match="second-file failure"):
        undo_latest_checkpoint(store, guard, session.session_id)

    assert (tmp_path / "a.txt").read_text() == "after-a.txt"
    assert (tmp_path / "b.txt").read_text() == "after-b.txt"
    assert len(store.latest_file_checkpoints(session.session_id)) == 2


@pytest.mark.asyncio
async def test_checkpoint_undo_rolls_files_forward_when_database_mark_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "file.txt"
    path.write_text("before", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    middleware = FileCheckpointMiddleware(
        store,
        guard,
        lambda: (session.session_id, "turn-1", "call-1"),
    )
    arguments = {"file_path": "file.txt", "content": "after"}
    tool = WholeEditTool(guard)
    await middleware.before_tool("whole_edit", arguments, tool)
    result = await tool.run(**arguments)
    await middleware.after_tool("whole_edit", arguments, result)
    assert path.read_text(encoding="utf-8") == "after"

    def fail_mark(*args, **kwargs):
        raise RuntimeError("checkpoint database unavailable")

    monkeypatch.setattr(store, "mark_file_checkpoints_restored", fail_mark)

    with pytest.raises(RuntimeError, match="checkpoint database unavailable"):
        undo_latest_checkpoint(store, guard, session.session_id)

    assert path.read_text(encoding="utf-8") == "after"
    assert store.latest_file_checkpoints(session.session_id)


@pytest.mark.asyncio
async def test_checkpoint_undo_reports_primary_and_scoped_rollback_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.core.checkpoints as checkpoints
    from ash.safety.scoped_io import ScopedIOError

    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    tool = WholeEditTool(guard)
    for name in ("a.txt", "b.txt"):
        path = tmp_path / name
        path.write_text(f"before-{name}", encoding="utf-8")
        middleware = FileCheckpointMiddleware(
            store,
            guard,
            lambda name=name: (session.session_id, "turn-1", f"call-{name}"),
        )
        arguments = {"file_path": name, "content": f"after-{name}"}
        await middleware.before_tool("whole_edit", arguments, tool)
        result = await tool.run(**arguments)
        await middleware.after_tool("whole_edit", arguments, result)

    original_restore = checkpoints.restore_scoped_file
    restore_calls = 0

    def fail_primary_then_rollback(path, content, guard, **kwargs):
        nonlocal restore_calls
        restore_calls += 1
        if restore_calls == 2:
            raise OSError("primary restore failure")
        if restore_calls == 3:
            raise ScopedIOError("rollback scoped failure")
        return original_restore(path, content, guard, **kwargs)

    monkeypatch.setattr(
        checkpoints,
        "restore_scoped_file",
        fail_primary_then_rollback,
    )

    with pytest.raises(
        RuntimeError,
        match=(
            "Undo failed \\(primary restore failure\\) and file rollback was incomplete:"
        ),
    ) as exc_info:
        undo_latest_checkpoint(store, guard, session.session_id)

    assert "rollback scoped failure" in str(exc_info.value)


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


@pytest.mark.asyncio
async def test_turn_diff_and_undo_collapse_repeated_edits_to_one_path(tmp_path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("version-0\n")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    tool = WholeEditTool(guard)

    for call_id, content in (("call-1", "version-1\n"), ("call-2", "version-2\n")):
        middleware = FileCheckpointMiddleware(
            store,
            guard,
            lambda call_id=call_id: (session.session_id, "turn-1", call_id),
        )
        arguments = {"file_path": "file.txt", "content": content}
        await middleware.before_tool("whole_edit", arguments, tool)
        result = await tool.run(**arguments)
        await middleware.after_tool("whole_edit", arguments, result)

    rendered = diff_latest_checkpoint(store, guard, session.session_id)
    assert "-version-0" in rendered
    assert "+version-2" in rendered
    assert "version-1" not in rendered
    assert undo_latest_checkpoint(store, guard, session.session_id) == [path]
    assert path.read_text() == "version-0\n"


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


def _tool_record(
    call_id: str,
    tool_name: str,
    *,
    executed: bool,
) -> ToolCallRecord:
    return ToolCallRecord(
        call_id=call_id,
        tool_name=tool_name,
        arguments={},
        approved=True,
        executed=executed,
        dispatched=not executed,
        result="done" if executed else None,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_startup_recovery_compensates_only_the_pending_file_call(
    tmp_path,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first-before")
    second.write_text("second-before")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-1", "edit files")
    guard = SafetyGuard(tmp_path)
    tool = WholeEditTool(guard)

    for call_id, path, content, executed in (
        ("call-complete", "first.txt", "first-after", True),
        ("call-pending", "second.txt", "second-after", False),
    ):
        store.save_tool_call(
            session.session_id,
            _tool_record(call_id, "whole_edit", executed=executed),
            turn_id="turn-1",
        )
        middleware = FileCheckpointMiddleware(
            store,
            guard,
            lambda call_id=call_id: (session.session_id, "turn-1", call_id),
        )
        arguments = {"file_path": path, "content": content}
        await middleware.before_tool("whole_edit", arguments, tool)
        result = await tool.run(**arguments)
        await middleware.after_tool("whole_edit", arguments, result)

    summary = recover_interrupted_turns(store, guard, session.session_id)

    assert first.read_text() == "first-after"
    assert second.read_text() == "second-before"
    assert summary.interrupted_turns == 1
    assert summary.compensated_calls == 1
    assert summary.compensated_files == (second,)
    assert summary.needs_attention is False
    recovered = store.load_session(session.session_id)
    pending = next(
        call for call in recovered.tool_calls if call.call_id == "call-pending"
    )
    assert pending.executed is True
    assert "rolled back" in (pending.error or "")
    report = store.interrupted_recovery_reports(session.session_id)[0]
    assert report["status"] == "compensated"
    assert report["compensated_calls"] == ["call-pending"]
    assert (
        recover_interrupted_turns(store, guard, session.session_id).interrupted_turns
        == 0
    )


@pytest.mark.asyncio
async def test_startup_recovery_parent_swap_refuses_restore_without_escape(
    tmp_path, monkeypatch
) -> None:
    """A parent swap after hashing cannot redirect recovery outside the workspace."""

    import ash.core.checkpoints as checkpoints

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = workspace / "victim"
    parent.mkdir()
    target = parent / "target.txt"
    target.write_text("before", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_target = outside / "target.txt"
    outside_target.write_text("outside", encoding="utf-8")

    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(workspace))
    store.start_turn(session.session_id, "turn-1", "edit")
    guard = SafetyGuard(workspace)
    store.save_tool_call(
        session.session_id,
        _tool_record("call-pending", "whole_edit", executed=False),
        turn_id="turn-1",
    )
    middleware = FileCheckpointMiddleware(
        store,
        guard,
        lambda: (session.session_id, "turn-1", "call-pending"),
    )
    arguments = {"file_path": "victim/target.txt", "content": "after"}
    tool = WholeEditTool(guard)
    await middleware.before_tool("whole_edit", arguments, tool)
    result = await tool.run(**arguments)
    await middleware.after_tool("whole_edit", arguments, result)
    assert target.read_text(encoding="utf-8") == "after"

    original_digest = checkpoints._digest
    swapped = False
    displaced_parent = workspace / "victim-displaced"

    def swap_parent_after_hash(path: Path, guard: SafetyGuard) -> str:
        nonlocal swapped
        digest = original_digest(path, guard)
        if path == target and not swapped:
            swapped = True
            parent.rename(displaced_parent)
            parent.symlink_to(outside, target_is_directory=True)
        return digest

    monkeypatch.setattr(checkpoints, "_digest", swap_parent_after_hash)
    try:
        summary = recover_interrupted_turns(store, guard, session.session_id)
    finally:
        if parent.is_symlink():
            parent.unlink()
        if displaced_parent.exists():
            displaced_parent.rename(parent)

    assert swapped is True
    assert summary.needs_attention is True
    assert summary.unresolved_files == (target,)
    assert outside_target.read_text(encoding="utf-8") == "outside"
    assert target.read_text(encoding="utf-8") == "after"


@pytest.mark.asyncio
async def test_startup_recovery_refuses_changed_incomplete_file_checkpoint(
    tmp_path,
) -> None:
    path = tmp_path / "file.txt"
    path.write_text("before")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-1", "edit")
    store.save_tool_call(
        session.session_id,
        _tool_record("call-pending", "whole_edit", executed=False),
        turn_id="turn-1",
    )
    guard = SafetyGuard(tmp_path)
    middleware = FileCheckpointMiddleware(
        store,
        guard,
        lambda: (session.session_id, "turn-1", "call-pending"),
    )
    await middleware.before_tool(
        "whole_edit",
        {"file_path": "file.txt", "content": "after"},
        WholeEditTool(guard),
    )
    path.write_text("unknown-state")

    summary = recover_interrupted_turns(store, guard, session.session_id)

    assert path.read_text() == "unknown-state"
    assert summary.needs_attention is True
    assert summary.unresolved_files == (path,)
    report = store.interrupted_recovery_reports(session.session_id)[0]
    assert report["status"] == "needs_attention"
    assert report["unresolved_files"] == ["file.txt"]


def test_startup_recovery_flags_non_file_tool_outcome_as_unknown(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-1", "run command")
    store.save_tool_call(
        session.session_id,
        _tool_record("call-command", "run_command", executed=False),
        turn_id="turn-1",
    )

    summary = recover_interrupted_turns(
        store, SafetyGuard(tmp_path), session.session_id
    )

    assert summary.needs_attention is True
    assert summary.unknown_calls == ("run_command (call-command)",)
    recovered = store.load_session(session.session_id).tool_calls[0]
    assert recovered.executed is True
    assert "outcome is unknown" in (recovered.error or "")


def test_recovery_marks_an_unstarted_approved_intent_as_not_run(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-1", "pending approval")
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id="call-unstarted",
            tool_name="run_command",
            arguments={"command_line": "build"},
            approved=True,
            executed=False,
            dispatched=False,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id="turn-1",
    )

    summary = recover_interrupted_turns(
        store, SafetyGuard(tmp_path), session.session_id
    )

    assert summary.needs_attention is False
    assert summary.unknown_calls == ()
    assert summary.recovered_calls[0].dispatched is False
    assert summary.recovered_calls[0].ambiguous is False
    recovered = store.load_session(session.session_id).tool_calls[0]
    assert recovered.executed is False
    assert recovered.dispatched is False
    assert "was not run" in (recovered.error or "")
    assert (
        recover_interrupted_turns(
            store, SafetyGuard(tmp_path), session.session_id
        ).interrupted_turns
        == 0
    )


def test_recovery_ignores_terminal_middleware_skipped_call(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-1", "skip tool")
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id="call-skipped",
            tool_name="run_command",
            arguments={"command_line": "build"},
            approved=True,
            executed=False,
            dispatched=False,
            result="skipped by middleware",
            error=None,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id="turn-1",
    )

    assert store.pending_tool_calls(session.session_id, "turn-1") == []

    summary = recover_interrupted_turns(
        store, SafetyGuard(tmp_path), session.session_id
    )

    assert summary.recovered_calls == ()
    recovered = store.load_session(session.session_id).tool_calls[0]
    assert recovered.executed is False
    assert recovered.dispatched is False
    assert recovered.result == "skipped by middleware"
    assert recovered.error is None


def test_recovery_processes_interrupted_turn_with_pending_dispatched_call(
    tmp_path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-interrupted-after-dispatch"
    store.start_turn(session.session_id, turn_id, "run command")
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id="call-command",
            tool_name="run_command",
            arguments={"command_line": "build"},
            approved=True,
            executed=False,
            dispatched=True,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id=turn_id,
    )
    store.interrupt_turn(turn_id)

    summary = recover_interrupted_turns(
        store, SafetyGuard(tmp_path), session.session_id
    )

    assert summary.interrupted_turns == 1
    assert summary.needs_attention is True
    assert summary.unknown_calls == ("run_command (call-command)",)
    recovered = store.load_session(session.session_id).tool_calls[0]
    assert recovered.executed is True
    assert recovered.dispatched is True
    assert "outcome is unknown" in (recovered.error or "")
    reports = store.interrupted_recovery_reports(session.session_id)
    assert reports[0]["turn_id"] == turn_id
    assert reports[0]["status"] == "needs_attention"
    assert (
        recover_interrupted_turns(
            store, SafetyGuard(tmp_path), session.session_id
        ).interrupted_turns
        == 0
    )


def test_recovery_ignores_interrupted_turn_without_pending_work(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-cleanly-interrupted"
    store.start_turn(session.session_id, turn_id, "failed before tools")
    store.interrupt_turn(turn_id)

    summary = recover_interrupted_turns(
        store, SafetyGuard(tmp_path), session.session_id
    )

    assert summary.interrupted_turns == 0
    assert summary.recovered_calls == ()
    assert store.interrupted_recovery_reports(session.session_id) == []


def test_recovery_processes_interrupted_turn_with_incomplete_checkpoint_only(
    tmp_path,
) -> None:
    path = tmp_path / "file.txt"
    path.write_text("before", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-interrupted-checkpoint-only"
    call_id = "call-edit"
    store.start_turn(session.session_id, turn_id, "edit file")
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id=call_id,
            tool_name="whole_edit",
            arguments={"file_path": "file.txt", "content": "after"},
            approved=True,
            executed=True,
            dispatched=True,
            result="tool failed after dispatch",
            error="checkpoint finalization failed",
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id=turn_id,
    )
    store.save_file_checkpoint(
        session.session_id,
        turn_id,
        "whole_edit",
        str(path),
        existed=True,
        before_content=b"before",
        before_mode=path.stat().st_mode,
        call_id=call_id,
    )
    store.interrupt_turn(turn_id)

    summary = recover_interrupted_turns(
        store, SafetyGuard(tmp_path), session.session_id
    )

    assert summary.interrupted_turns == 1
    checkpoints = store.file_checkpoints_for_turns(session.session_id, [turn_id])
    assert checkpoints == []
    reports = store.interrupted_recovery_reports(session.session_id)
    assert reports[0]["turn_id"] == turn_id
    assert reports[0]["status"] == "compensated"
    assert (
        recover_interrupted_turns(
            store, SafetyGuard(tmp_path), session.session_id
        ).interrupted_turns
        == 0
    )


def test_recovery_reconstructs_missing_terminal_tool_result_message(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-terminal-missing-result"
    call_id = "call-terminal"
    store.start_turn(session.session_id, turn_id, "run command")
    store.save_message(
        session.session_id,
        Message(
            role="assistant",
            content="",
            timestamp=datetime.now(timezone.utc),
            metadata={
                "tool_calls": [
                    {
                        "call_id": call_id,
                        "name": "run_command",
                        "arguments": {"command_line": "build"},
                    }
                ]
            },
        ),
        turn_id=turn_id,
    )
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id=call_id,
            tool_name="run_command",
            arguments={"command_line": "build"},
            approved=True,
            executed=True,
            dispatched=True,
            result="build complete",
            error=None,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id=turn_id,
    )
    store.interrupt_turn(turn_id)

    assert [
        str(row["turn_id"]) for row in store.recoverable_turns(session.session_id)
    ] == [turn_id]

    summary = recover_interrupted_turns(
        store, SafetyGuard(tmp_path), session.session_id
    )

    assert summary.interrupted_turns == 1
    assert len(summary.recovered_calls) == 1
    recovered_call = summary.recovered_calls[0]
    assert recovered_call.call_id == call_id
    assert recovered_call.success is True
    assert recovered_call.output == "build complete"
    assert recovered_call.dispatched is True
    assert recovered_call.ambiguous is False
    loaded = store.load_session(session.session_id)
    durable = loaded.tool_calls[0]
    assert durable.executed is True
    assert durable.dispatched is True
    assert durable.result == "build complete"
    assert durable.error is None
    tool_messages = [message for message in loaded.messages if message.role == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].metadata["call_id"] == call_id
    assert '"success": true' in tool_messages[0].content
    assert '"output": "build complete"' in tool_messages[0].content
    assert '"replayed": false' in tool_messages[0].content
    audit = store.list_audit_logs(session.session_id)[-1]
    assert audit.details["success"] is True
    assert audit.details["output"] == "build complete"
    assert (
        recover_interrupted_turns(
            store, SafetyGuard(tmp_path), session.session_id
        ).interrupted_turns
        == 0
    )


def test_recovery_preserves_terminal_unknown_outcome_classification(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-terminal-unknown"
    call_id = "call-unknown"
    store.start_turn(session.session_id, turn_id, "run command")
    store.save_message(
        session.session_id,
        Message(
            role="assistant",
            content="",
            timestamp=datetime.now(timezone.utc),
            metadata={
                "tool_calls": [
                    {
                        "call_id": call_id,
                        "name": "run_command",
                        "arguments": {"command_line": "build"},
                    }
                ]
            },
        ),
        turn_id=turn_id,
    )
    ambiguous_error = (
        "Tool outcome is ambiguous; its side effect may have occurred. "
        "Ash did not retry the operation: transport lost terminal acknowledgement"
    )
    store.save_tool_call(
        session.session_id,
        ToolCallRecord(
            call_id=call_id,
            tool_name="run_command",
            arguments={"command_line": "build"},
            approved=True,
            executed=True,
            dispatched=True,
            result="",
            error=ambiguous_error,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id=turn_id,
    )
    store.interrupt_turn(turn_id)

    summary = recover_interrupted_turns(
        store, SafetyGuard(tmp_path), session.session_id
    )

    assert summary.interrupted_turns == 1
    assert summary.needs_attention is True
    assert summary.unknown_calls == ("run_command (call-unknown)",)
    assert len(summary.recovered_calls) == 1
    recovered = summary.recovered_calls[0]
    assert recovered.ambiguous is True
    assert recovered.dispatched is True
    assert recovered.success is False
    assert recovered.output == ""
    loaded = store.load_session(session.session_id)
    durable = loaded.tool_calls[0]
    assert durable.result == ""
    assert durable.error == ambiguous_error
    tool_messages = [message for message in loaded.messages if message.role == "tool"]
    assert len(tool_messages) == 1
    assert '"ambiguous": true' in tool_messages[0].content
    assert '"replayed": false' in tool_messages[0].content
    reports = store.interrupted_recovery_reports(session.session_id)
    assert reports[0]["status"] == "needs_attention"
