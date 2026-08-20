import pytest
from datetime import datetime, timezone

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
