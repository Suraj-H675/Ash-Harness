import asyncio
import hashlib
import json
import os
import stat

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
from ash.safety.scoped_io import ScopedIOError, workspace_mutation_lock
from ash.tools.base import ToolResult
from ash.tools.filesystem import WholeEditTool
from ash.tools.patch import ApplyPatchTool


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
async def test_model_patch_preserves_multifile_checkpoints_and_undo(tmp_path) -> None:
    first = tmp_path / "first.txt"
    first.write_text("before\n", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    middleware = FileCheckpointMiddleware(
        store, guard, lambda: (session.session_id, "turn-1", "patch-call")
    )
    arguments = {
        "patch": """*** Begin Patch
*** Update File: first.txt
@@
-before
+after
*** Add File: second.txt
+created
*** End Patch"""
    }
    tool = ApplyPatchTool(guard)
    await middleware.before_tool("apply_patch", arguments, tool)
    result = await tool.run(**arguments)
    assert result.success, result.error
    await middleware.after_tool("apply_patch", arguments, result)
    assert first.read_text() == "after\n"
    assert (tmp_path / "second.txt").read_text() == "created\n"
    rows = store.file_checkpoints_for_call(session.session_id, "turn-1", "patch-call")
    assert len(rows) == 2
    assert all(row["after_sha256"] is not None for row in rows)
    assert set(undo_latest_checkpoint(store, guard, session.session_id)) == {
        first, tmp_path / "second.txt"
    }
    assert first.read_text() == "before\n"
    assert not (tmp_path / "second.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_with", ["undo", "recovery"])
async def test_stale_multifile_patch_never_checkpoints_external_bytes(
    tmp_path, finish_with: str
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first-before\n", encoding="utf-8")
    second.write_text("second-before\n", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-1", "patch both files")
    call_id = "patch-call"
    if finish_with == "recovery":
        store.save_tool_call(
            session.session_id,
            _tool_record(call_id, "apply_patch", executed=False),
            turn_id="turn-1",
        )
    guard = SafetyGuard(tmp_path)
    middleware = FileCheckpointMiddleware(
        store, guard, lambda: (session.session_id, "turn-1", call_id)
    )
    tool = ApplyPatchTool(guard)
    arguments = {
        "patch": (
            "diff --git a/first.txt b/first.txt\n"
            "--- a/first.txt\n"
            "+++ b/first.txt\n"
            "@@ -1 +1 @@\n"
            "-first-before\n"
            "+first-after\n"
            "diff --git a/second.txt b/second.txt\n"
            "--- a/second.txt\n"
            "+++ b/second.txt\n"
            "@@ -1 +1 @@\n"
            "-stale-second\n"
            "+patch-second\n"
        )
    }

    await middleware.before_tool("apply_patch", arguments, tool)
    second.write_text("external-change\n", encoding="utf-8")
    result = await tool.run(**arguments)
    assert result.success is False
    assert result.outcome.value == "completed"
    await middleware.after_tool("apply_patch", arguments, result)

    assert first.read_text(encoding="utf-8") == "first-before\n"
    assert second.read_text(encoding="utf-8") == "external-change\n"
    rows = store.file_checkpoints_for_call(
        session.session_id, "turn-1", call_id
    )
    second_row = next(row for row in rows if row["path"] == str(second))
    assert second_row["after_sha256"] is None

    if finish_with == "undo":
        with pytest.raises(RuntimeError):
            undo_latest_checkpoint(store, guard, session.session_id)
    else:
        summary = recover_interrupted_turns(store, guard, session.session_id)
        assert summary.needs_attention is True
        assert summary.compensated_calls == 0
        assert summary.compensated_files == ()
        assert summary.unresolved_files == (second,)

    assert first.read_text(encoding="utf-8") == "first-before\n"
    assert second.read_text(encoding="utf-8") == "external-change\n"


@pytest.mark.asyncio
async def test_successful_patch_does_not_checkpoint_divergent_concurrent_bytes(
    tmp_path,
) -> None:
    path = tmp_path / "file.txt"
    path.write_text("before\n", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    middleware = FileCheckpointMiddleware(
        store, guard, lambda: (session.session_id, "turn-1", "patch-call")
    )
    tool = ApplyPatchTool(guard)
    arguments = {
        "patch": (
            "diff --git a/file.txt b/file.txt\n"
            "--- a/file.txt\n"
            "+++ b/file.txt\n"
            "@@ -1 +1 @@\n"
            "-before\n"
            "+after\n"
        )
    }

    await middleware.before_tool("apply_patch", arguments, tool)
    result = await tool.run(**arguments)
    assert result.success is True
    path.write_text("external-change\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        await middleware.after_tool("apply_patch", arguments, result)

    row = store.file_checkpoints_for_call(
        session.session_id, "turn-1", "patch-call"
    )[0]
    assert row["after_sha256"] is None
    assert undo_latest_checkpoint(store, guard, session.session_id) == []
    assert path.read_text(encoding="utf-8") == "external-change\n"


@pytest.mark.asyncio
async def test_recovery_does_not_claim_rollback_when_checkpoint_preimage_is_present(
    tmp_path,
) -> None:
    path = tmp_path / "file.txt"
    path.write_text("before", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-1", "edit file")
    call_id = "call-pending"
    store.save_tool_call(
        session.session_id,
        _tool_record(call_id, "apply_patch", executed=False),
        turn_id="turn-1",
    )
    guard = SafetyGuard(tmp_path)
    middleware = FileCheckpointMiddleware(
        store, guard, lambda: (session.session_id, "turn-1", call_id)
    )
    await middleware.before_tool(
        "apply_patch",
        {"patch": "diff --git a/file.txt b/file.txt\n"},
        ApplyPatchTool(guard),
    )

    summary = recover_interrupted_turns(store, guard, session.session_id)

    assert path.read_text(encoding="utf-8") == "before"
    assert summary.compensated_calls == 0
    assert summary.compensated_files == ()
    assert summary.unknown_calls == ("apply_patch (call-pending)",)
    assert summary.needs_attention is True
    recovered = store.load_session(session.session_id).tool_calls[0]
    assert "preimage" in (recovered.error or "").lower()
    assert "no file restoration" in (recovered.error or "").lower()
    assert "rolled back" not in (recovered.error or "").lower()
    report = store.interrupted_recovery_reports(session.session_id)[0]
    assert report["status"] == "needs_attention"
    assert report["compensated_calls"] == []
    assert report["compensated_files"] == []


@pytest.mark.asyncio
async def test_checkpoint_undo_refuses_permission_only_user_change(tmp_path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("before", encoding="utf-8")
    path.chmod(0o640)
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    middleware = FileCheckpointMiddleware(
        store, guard, lambda: (session.session_id, "turn-1", "call-1")
    )
    arguments = {"file_path": "file.txt", "content": "after"}
    tool = WholeEditTool(guard)
    await middleware.before_tool("whole_edit", arguments, tool)
    result = await tool.run(**arguments)
    await middleware.after_tool("whole_edit", arguments, result)
    path.chmod(0o600)

    with pytest.raises(RuntimeError, match="permissions changed"):
        undo_latest_checkpoint(store, guard, session.session_id)

    assert path.read_text(encoding="utf-8") == "after"
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.latest_file_checkpoints(session.session_id)


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
async def test_multifile_checkpoint_preflight_is_atomic_after_later_file_rejection(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ash.core.checkpoints.MAX_CHECKPOINT_BYTES", 8)
    first = tmp_path / "a.txt"
    oversized = tmp_path / "b.txt"
    later = tmp_path / "c.txt"
    first.write_text("small", encoding="utf-8")
    oversized.write_text("too-large", encoding="utf-8")
    later.write_text("before-c", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    call_id = "call-patch"
    middleware = FileCheckpointMiddleware(
        store,
        guard,
        lambda: (session.session_id, "turn-1", call_id),
    )
    patch = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-small\n"
        "+after\n"
        "diff --git a/b.txt b/b.txt\n"
        "--- a/b.txt\n"
        "+++ b/b.txt\n"
        "@@ -1 +1 @@\n"
        "-too-large\n"
        "+after\n"
    )

    with pytest.raises(ValueError, match="over 8 bytes"):
        await middleware.before_tool(
            "apply_patch", {"patch": patch}, WholeEditTool(guard)
        )

    assert store.file_checkpoints_for_call(session.session_id, "turn-1", call_id) == []
    assert first.read_text(encoding="utf-8") == "small"
    assert oversized.read_text(encoding="utf-8") == "too-large"

    call_id = "call-later"
    arguments = {"file_path": "c.txt", "content": "after-c"}
    tool = WholeEditTool(guard)
    await middleware.before_tool("whole_edit", arguments, tool)
    result = await tool.run(**arguments)
    await middleware.after_tool("whole_edit", arguments, result)

    undo_latest_checkpoint(store, guard, session.session_id)

    assert later.read_text(encoding="utf-8") == "before-c"
    assert store.latest_file_checkpoints(session.session_id) == []


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

    monkeypatch.setattr(checkpoints, "_digest_and_mode", fail_digest)
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
async def test_checkpoint_rollback_preserves_later_edit_on_untouched_path(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.core.checkpoints as checkpoints

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

    rows = store.latest_file_checkpoints(session.session_id)
    first_path = Path(str(rows[0]["path"]))
    untouched_path = Path(str(rows[1]["path"]))
    original_restore = checkpoints.restore_scoped_file
    failed = False

    def fail_first_restore(path, content, file_guard, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            untouched_path.write_text("later-external-edit", encoding="utf-8")
            raise OSError("injected first restore failure")
        return original_restore(path, content, file_guard, **kwargs)

    monkeypatch.setattr(checkpoints, "restore_scoped_file", fail_first_restore)

    with pytest.raises(OSError, match="first restore failure"):
        undo_latest_checkpoint(store, guard, session.session_id)

    assert first_path.read_text(encoding="utf-8").startswith("after-")
    assert untouched_path.read_text(encoding="utf-8") == "later-external-edit"
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
async def test_checkpoint_undo_retries_after_process_stops_mid_restore(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.core.checkpoints as checkpoints

    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    tool = WholeEditTool(guard)
    paths = [tmp_path / "a.txt", tmp_path / "b.txt"]
    for index, path in enumerate(paths):
        path.write_text(f"before-{index}", encoding="utf-8")
        middleware = FileCheckpointMiddleware(
            store,
            guard,
            lambda index=index: (session.session_id, "turn-1", f"call-{index}"),
        )
        arguments = {"file_path": path.name, "content": f"after-{index}"}
        await middleware.before_tool("whole_edit", arguments, tool)
        result = await tool.run(**arguments)
        await middleware.after_tool("whole_edit", arguments, result)

    original_apply = checkpoints._apply_checkpoint_rows

    def stop_after_first_restore(rows, file_guard, originals, rollback_expected):
        original_apply(rows[:1], file_guard, originals, rollback_expected)
        raise SystemExit("simulated process stop after one restored file")

    with monkeypatch.context() as patch:
        patch.setattr(checkpoints, "_apply_checkpoint_rows", stop_after_first_restore)
        with pytest.raises(SystemExit, match="simulated process stop"):
            undo_latest_checkpoint(store, guard, session.session_id)

    assert {path.read_text(encoding="utf-8") for path in paths} == {
        "before-1",
        "after-0",
    }
    assert len(store.latest_file_checkpoints(session.session_id)) == 2

    restored = undo_latest_checkpoint(store, guard, session.session_id)

    assert set(restored) == set(paths)
    assert [path.read_text(encoding="utf-8") for path in paths] == [
        "before-0",
        "before-1",
    ]
    assert store.latest_file_checkpoints(session.session_id) == []


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
async def test_checkpoint_diff_reports_agent_file_mode_change(tmp_path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("unchanged\n", encoding="utf-8")
    path.chmod(0o640)
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    middleware = FileCheckpointMiddleware(
        store, guard, lambda: (session.session_id, "turn-1", "call-1")
    )
    patch = "diff --git a/file.txt b/file.txt\nold mode 100640\nnew mode 100755\n"

    tool = ApplyPatchTool(guard)
    await middleware.before_tool("apply_patch", {"patch": patch}, tool)
    result = await tool.run(patch=patch)
    assert result.success, result.error
    actual_mode = stat.S_IMODE(path.stat().st_mode)
    assert actual_mode & stat.S_IXUSR
    assert path.read_text(encoding="utf-8") == "unchanged\n"
    await middleware.after_tool("apply_patch", {"patch": patch}, result)

    rendered = diff_latest_checkpoint(store, guard, session.session_id)

    assert f"File mode changed: file.txt 0o640 -> {actual_mode:#o}" in rendered


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
async def test_combined_rewind_retries_after_process_stops_mid_restore(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ash.core.checkpoints as checkpoints

    path = tmp_path / "file.txt"
    path.write_text("version-0", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    await _record_edit_turn(
        store, guard, session.session_id, "turn-1", "file.txt", "version-1"
    )
    await _record_edit_turn(
        store, guard, session.session_id, "turn-2", "file.txt", "version-2"
    )

    original_apply = checkpoints._apply_checkpoint_rows

    def stop_after_first_restore(rows, file_guard, originals, rollback_expected):
        original_apply(rows[:1], file_guard, originals, rollback_expected)
        raise SystemExit("simulated process stop after one restored turn")

    with monkeypatch.context() as patch:
        patch.setattr(checkpoints, "_apply_checkpoint_rows", stop_after_first_restore)
        with pytest.raises(SystemExit, match="simulated process stop"):
            rewind_session_with_files(store, guard, session.session_id, 0)

    assert path.read_text(encoding="utf-8") == "version-1"
    assert len(store.load_session(session.session_id).messages) == 4

    rewound, restored = rewind_session_with_files(
        store, guard, session.session_id, 0
    )

    assert path.read_text(encoding="utf-8") == "version-0"
    assert restored == [path]
    assert rewound.messages == []


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
        dispatched=True,
        result="done" if executed else None,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_noop_session_recovery_does_not_contend_with_an_unrelated_file_mutation(
    tmp_path,
) -> None:
    # The caller holds a distinct session lease. No pending turn means no
    # compensating file effect and no reason to acquire the global write lock.
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    lease = store.acquire_session_runtime_lease(session.session_id)
    try:
        with workspace_mutation_lock():
            summary = await asyncio.to_thread(
                recover_interrupted_turns, store, SafetyGuard(tmp_path),
                session.session_id,
            )
    finally:
        lease.close()
    assert summary.interrupted_turns == 0
    assert summary.recovered_calls == ()


@pytest.mark.asyncio
async def test_pending_tool_recovery_still_requires_global_file_mutation_lock(
    tmp_path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-1", "pending action")
    store.save_tool_call(
        session.session_id,
        _tool_record("call-1", "run_command", executed=False),
        turn_id="turn-1",
    )
    with workspace_mutation_lock():
        with pytest.raises(ScopedIOError):
            await asyncio.to_thread(
                recover_interrupted_turns, store, SafetyGuard(tmp_path),
                session.session_id,
            )
    assert len(store.recoverable_turns(session.session_id)) == 1
    summary = recover_interrupted_turns(store, SafetyGuard(tmp_path), session.session_id)
    assert summary.unknown_calls == ("run_command (call-1)",)


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
    assert report["compensated_files"] == ["second.txt"]
    assert (
        recover_interrupted_turns(store, guard, session.session_id).interrupted_turns
        == 0
    )


def test_recovery_retry_reports_preimage_without_claiming_a_second_restore(
    tmp_path,
) -> None:
    """An uncommitted restore attempt cannot be attributed on the next pass."""
    import sqlite3

    from ash.core.session import get_db_connection

    file = tmp_path / "changed.txt"
    file.write_text("before", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "interrupted-edit"
    call_id = "call-edit"
    store.start_turn(session.session_id, turn_id, "edit")
    store.save_tool_call(
        session.session_id,
        _tool_record(call_id, "whole_edit", executed=False),
        turn_id=turn_id,
    )
    store.save_file_checkpoint(
        session.session_id,
        turn_id,
        "whole_edit",
        str(file),
        existed=True,
        before_content=b"before",
        before_mode=file.stat().st_mode,
        call_id=call_id,
    )
    file.write_text("after", encoding="utf-8")
    store.finish_file_checkpoint(
        session.session_id,
        turn_id,
        str(file),
        hashlib.sha256(b"after").hexdigest(),
        call_id=call_id,
    )
    with get_db_connection(store.db_path) as conn:
        conn.executescript(
            "CREATE TRIGGER fail_recovery_commit BEFORE UPDATE ON turn_journal "
            "WHEN NEW.recovery_json != '{}' BEGIN "
            "SELECT RAISE(ABORT, 'injected recovery commit failure'); END;"
        )

    with pytest.raises(
        sqlite3.IntegrityError, match="injected recovery commit failure"
    ):
        recover_interrupted_turns(store, SafetyGuard(tmp_path), session.session_id)

    assert file.read_text(encoding="utf-8") == "before"
    assert len(store.pending_tool_calls(session.session_id, turn_id)) == 1
    assert store.interrupted_recovery_reports(session.session_id) == []
    assert store.list_audit_logs(session.session_id) == []
    assert store.file_checkpoints_for_call(session.session_id, turn_id, call_id)

    with get_db_connection(store.db_path) as conn:
        conn.execute("DROP TRIGGER fail_recovery_commit")

    summary = recover_interrupted_turns(
        store, SafetyGuard(tmp_path), session.session_id
    )
    assert summary.interrupted_turns == 1
    assert summary.compensated_calls == 0
    assert summary.compensated_files == ()
    assert summary.unknown_calls == ("whole_edit (call-edit)",)
    assert file.read_text(encoding="utf-8") == "before"
    assert len(store.list_audit_logs(session.session_id)) == 1
    assert len(store.interrupted_recovery_reports(session.session_id)) == 1
    report = store.interrupted_recovery_reports(session.session_id)[0]
    assert report["status"] == "needs_attention"
    assert report["compensated_calls"] == []
    assert report["compensated_files"] == []
    assert report["unknown_calls"] == [
        {"call_id": "call-edit", "tool": "whole_edit"}
    ]
    recovered = store.load_session(session.session_id).tool_calls[0]
    assert "no file restoration" in (recovered.error or "").lower()
    assert "rolled back" not in (recovered.error or "").lower()
    assert store.verify_audit_log(session.session_id) == []
    assert (
        recover_interrupted_turns(
            store, SafetyGuard(tmp_path), session.session_id
        ).interrupted_turns
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="requires POSIX permission denial for a partial git apply",
)
async def test_partial_patch_recovery_restores_only_verified_patch_output(
    tmp_path,
) -> None:
    first = tmp_path / "first.txt"
    blocked = tmp_path / "locked"
    second = blocked / "second.txt"
    first.write_text("first-before\n", encoding="utf-8")
    blocked.mkdir()
    second.write_text("second-before\n", encoding="utf-8")
    blocked.chmod(0o555)
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-1", "patch both files")
    call_id = "partial-patch"
    store.save_tool_call(
        session.session_id,
        _tool_record(call_id, "apply_patch", executed=False),
        turn_id="turn-1",
    )
    guard = SafetyGuard(tmp_path)
    middleware = FileCheckpointMiddleware(
        store, guard, lambda: (session.session_id, "turn-1", call_id)
    )
    tool = ApplyPatchTool(guard)
    arguments = {
        "patch": (
            "diff --git a/first.txt b/first.txt\n"
            "--- a/first.txt\n"
            "+++ b/first.txt\n"
            "@@ -1 +1 @@\n"
            "-first-before\n"
            "+first-after\n"
            "diff --git a/locked/second.txt b/locked/second.txt\n"
            "--- a/locked/second.txt\n"
            "+++ b/locked/second.txt\n"
            "@@ -1 +1 @@\n"
            "-second-before\n"
            "+second-after\n"
        )
    }

    await middleware.before_tool("apply_patch", arguments, tool)
    result = await tool.run(**arguments)
    blocked.chmod(0o755)
    assert result.success is False
    assert result.outcome.value == "unknown"
    assert first.read_text(encoding="utf-8") == "first-after\n"
    assert second.read_text(encoding="utf-8") == "second-before\n"
    await middleware.after_tool("apply_patch", arguments, result)

    rows = store.file_checkpoints_for_call(
        session.session_id, "turn-1", call_id
    )
    assert len(rows) == 2
    assert all(row["after_sha256"] is not None for row in rows)
    summary = recover_interrupted_turns(store, guard, session.session_id)

    assert first.read_text(encoding="utf-8") == "first-before\n"
    assert second.read_text(encoding="utf-8") == "second-before\n"
    assert summary.compensated_calls == 1
    assert summary.compensated_files == (first,)
    assert summary.needs_attention is False


def test_recovery_resumes_after_process_exit_midway_through_multi_file_restore(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery resumes a partly restored patch without rerunning the edit."""
    import ash.core.checkpoints as checkpoints

    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id, call_id = "incomplete-patch", "call-patch"
    store.start_turn(session.session_id, turn_id, "patch files")
    store.save_tool_call(
        session.session_id,
        _tool_record(call_id, "apply_patch", executed=False),
        turn_id=turn_id,
    )
    paths = [tmp_path / "a.txt", tmp_path / "b.txt"]
    for path in paths:
        path.write_text("before", encoding="utf-8")
        store.save_file_checkpoint(
            session.session_id,
            turn_id,
            "apply_patch",
            str(path),
            existed=True,
            before_content=b"before",
            before_mode=path.stat().st_mode,
            call_id=call_id,
        )
        path.write_text("after", encoding="utf-8")
        store.finish_file_checkpoint(
            session.session_id,
            turn_id,
            str(path),
            hashlib.sha256(b"after").hexdigest(),
            call_id=call_id,
        )

    original_apply = checkpoints._apply_checkpoint_rows

    def halt_after_first_restore(rows, guard, originals, rollback_expected):
        original_apply(rows[:1], guard, originals, rollback_expected)
        raise SystemExit("simulated hard stop after first restored file")

    with monkeypatch.context() as patch:
        patch.setattr(checkpoints, "_apply_checkpoint_rows", halt_after_first_restore)
        with pytest.raises(SystemExit, match="simulated hard stop"):
            recover_interrupted_turns(store, SafetyGuard(tmp_path), session.session_id)

    assert {path.read_text(encoding="utf-8") for path in paths} == {"before", "after"}
    assert store.pending_tool_calls(session.session_id, turn_id)
    assert store.interrupted_recovery_reports(session.session_id) == []

    summary = recover_interrupted_turns(
        store, SafetyGuard(tmp_path), session.session_id
    )
    assert summary.interrupted_turns == 1
    assert summary.compensated_calls == 1
    assert summary.unknown_calls == ()
    assert all(path.read_text(encoding="utf-8") == "before" for path in paths)
    assert len(store.list_audit_logs(session.session_id)) == 1
    assert len(store.interrupted_recovery_reports(session.session_id)) == 1
    assert store.verify_audit_log(session.session_id) == []
    assert (
        recover_interrupted_turns(
            store, SafetyGuard(tmp_path), session.session_id
        ).interrupted_turns
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

    original_digest = checkpoints._digest_and_mode
    swapped = False
    displaced_parent = workspace / "victim-displaced"

    def swap_parent_after_hash(
        path: Path, guard: SafetyGuard
    ) -> tuple[str, int | None]:
        nonlocal swapped
        digest = original_digest(path, guard)
        if path == target and not swapped:
            swapped = True
            parent.rename(displaced_parent)
            parent.symlink_to(outside, target_is_directory=True)
        return digest

    monkeypatch.setattr(checkpoints, "_digest_and_mode", swap_parent_after_hash)
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


def test_startup_recovery_directs_ambiguous_mcp_calls_to_remote_service(
    tmp_path,
) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    store.start_turn(session.session_id, "turn-1", "refund an invoice")
    store.save_tool_call(
        session.session_id,
        _tool_record("call-refund", "mcp__billing__refund", executed=False),
        turn_id="turn-1",
    )

    summary = recover_interrupted_turns(
        store, SafetyGuard(tmp_path), session.session_id
    )

    assert summary.needs_attention is True
    recovered = store.load_session(session.session_id).tool_calls[0]
    assert "remote service" in (recovered.error or "")
    assert "Inspect the workspace" not in (recovered.error or "")
    assert "did not replay" in (recovered.error or "")
    audit = store.list_audit_logs(session.session_id)
    recovery_audit = next(item for item in audit if item.target_resource == recovered.tool_name)
    assert recovery_audit.details["ambiguous"] is True
    assert recovery_audit.details["replayed"] is False


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


def test_recovery_projects_oversized_terminal_result_without_losing_authoritative_data(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.core.session as session_module
    import ash.providers.messages as messages_module

    monkeypatch.setattr(messages_module, "MAX_CANONICAL_CONTENT_BYTES", 2048)
    monkeypatch.setattr(
        session_module,
        "MAX_RECOVERY_AUDIT_OUTPUT_PREVIEW_BYTES",
        64,
    )
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-terminal-large-result"
    call_id = "call-terminal-large"
    output = "x" * 5000
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
            result=output,
            error=None,
            timestamp=datetime.now(timezone.utc),
        ),
        turn_id=turn_id,
    )
    store.interrupt_turn(turn_id)

    summary = recover_interrupted_turns(
        store,
        SafetyGuard(tmp_path),
        session.session_id,
    )

    assert summary.recovered_calls[0].output == output
    loaded = store.load_session(session.session_id)
    assert loaded.tool_calls[0].result == output
    tool_message = next(message for message in loaded.messages if message.role == "tool")
    payload = json.loads(tool_message.content)
    assert payload["recovery_result_truncated"] is True
    assert payload["original_output_bytes"] == 5000
    assert len(payload["original_output_sha256"]) == 64
    assert len(payload["output"].encode("utf-8")) <= 512
    audit = store.list_audit_logs(session.session_id)[-1]
    assert audit.details["output_truncated"] is True
    assert len(audit.details["output_preview"].encode("utf-8")) <= 64
    assert audit.details["output_bytes"] == 5000
    assert len(audit.details["output_sha256"]) == 64


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


def test_recovery_repairs_oversized_legacy_assistant_arguments_atomically(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ash.providers.messages as messages_module

    monkeypatch.setattr(messages_module, "MAX_TOOL_CALL_ARGUMENT_BYTES", 512)
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    turn_id = "turn-legacy-oversized-arguments"
    call_id = "call-legacy-oversized"
    original_argument = "x" * 2000
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
                        "arguments": {"command_line": original_argument},
                    }
                ]
            },
        ),
        turn_id=turn_id,
    )
    store.interrupt_turn(turn_id)

    summary = recover_interrupted_turns(
        store,
        SafetyGuard(tmp_path),
        session.session_id,
    )

    assert len(summary.recovered_calls) == 1
    recovered = summary.recovered_calls[0]
    assert recovered.call_id == call_id
    assert recovered.tool_name == "run_command"
    assert recovered.dispatched is False
    assert recovered.assistant_message_id is not None
    assert recovered.assistant_arguments is not None
    assert recovered.assistant_arguments["_ash_recovery_arguments_truncated"] is True
    assert len(recovered.assistant_arguments["original_sha256"]) == 64
    assert (
        len(
            json.dumps(
                recovered.assistant_arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        <= 512
    )

    loaded = store.load_session(session.session_id)
    durable = loaded.tool_calls[0]
    assert durable.call_id == call_id
    assert durable.tool_name == "run_command"
    assert durable.arguments == recovered.assistant_arguments
    assistant = next(message for message in loaded.messages if message.role == "assistant")
    repaired_call = assistant.metadata["tool_calls"][0]
    assert repaired_call["call_id"] == call_id
    assert repaired_call["name"] == "run_command"
    assert repaired_call["arguments"] == recovered.assistant_arguments
    assert original_argument not in json.dumps(assistant.metadata)
    tool_message = next(message for message in loaded.messages if message.role == "tool")
    tool_payload = json.loads(tool_message.content)
    assert tool_payload["dispatched"] is False
    assert tool_payload["replayed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, "true", "false"])
async def test_patch_checkpoint_distinguishes_dry_run_from_real_edit(tmp_path, dry_run) -> None:
    path = tmp_path / "file.txt"
    path.write_text("before\n", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(tmp_path))
    guard = SafetyGuard(tmp_path)
    tool = ApplyPatchTool(guard)
    middleware = FileCheckpointMiddleware(
        store, guard, lambda: (session.session_id, "turn", "call")
    )
    arguments = {"dry_run": dry_run, "patch": (
        "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
        "@@ -1 +1 @@\n-before\n+after\n"
    )}
    await middleware.before_tool("apply_patch", arguments, tool)
    result = await tool.run(**arguments)
    assert result.success, result.error
    if dry_run != "false":
        assert path.read_text(encoding="utf-8") == "before\n"
        path.write_text("after\n", encoding="utf-8")
    await middleware.after_tool("apply_patch", arguments, result)
    restored = undo_latest_checkpoint(store, guard, session.session_id)
    if dry_run == "false":
        assert restored == [path]
        assert path.read_text(encoding="utf-8") == "before\n"
    else:
        assert restored == []
        assert not store.file_checkpoints_for_call(session.session_id, "turn", "call")
        assert path.read_text(encoding="utf-8") == "after\n"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX temporary directory alias")
async def test_patch_checkpoint_finalizes_under_symlinked_temp_directory(tmp_path, monkeypatch) -> None:
    import tempfile

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    alias = tmp_path / "scratch-alias"
    alias.symlink_to(scratch, target_is_directory=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "source.txt"
    path.write_text("before\n", encoding="utf-8")
    guard = SafetyGuard(workspace)
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session(str(workspace))
    tool = ApplyPatchTool(guard)
    middleware = FileCheckpointMiddleware(store, guard, lambda: (session.session_id, "turn", "call"))
    arguments = {"patch": "--- a/source.txt\n+++ b/source.txt\n@@ -1 +1 @@\n-before\n+after\n"}
    await middleware.before_tool("apply_patch", arguments, tool)
    result = await tool.run(**arguments)
    assert result.success, result.error
    with monkeypatch.context() as patch:
        patch.setattr(tempfile, "tempdir", str(alias))
        await middleware.after_tool("apply_patch", arguments, result)
    assert undo_latest_checkpoint(store, guard, session.session_id) == [path]
    assert path.read_text(encoding="utf-8") == "before\n"
    assert list(scratch.iterdir()) == []
